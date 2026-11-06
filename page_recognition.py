#!/usr/bin/env python3
"""
Page type recognition (rule-based, no AI)

Recognises what kind of page each crawled page is (article, product, contact, legal,
job vacancy, case study, listing, …) from signals that work across sites:

  - URL keywords (English, Dutch, French, German)
  - title and H1 wording
  - schema.org and Open Graph types
  - what is on the page, from blocks.json: forms, phone/email/map links, prices,
    file downloads, long lists of cards, dates
  - technical signals: status code, noindex

Every page gets a recognised type with a confidence and the evidence behind it.
name_types_rules.py uses this to name page types; a page type is recognised by a vote
across its pages.

Usage:
    python page_recognition.py output/extremis

Output (in the crawl folder):
    page-recognition.csv   one row per page: recognised type, confidence, evidence, runner-up
"""

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

from common import load_pages

LOCALE = re.compile(r"^[a-z]{2}([-_][a-z]{2})?$", re.I)


def kw(*words: str) -> re.Pattern:
    """Whole-word (or hyphen/underscore-delimited) keyword matcher for URL segments and titles."""
    return re.compile(r"(?<![a-z])(" + "|".join(words) + r")(?![a-z])", re.I)


# Page types, with a short description and the keywords that point to them.
# url: matched against URL path segments; title: matched against <title> and the H1.
TYPES = {
    "HomePage": {"desc": "The site's entry page.", "url": None, "title": None},
    "ArticlePage": {"desc": "Editorial article: blog post, news item, insight, story.",
                    "url": kw("blog", "blogs", "news", "nieuws", "actualiteit", "actualites", "actualités", "aktuelles", "articles?",
                              "artikels?", "artikel", "insights?", "stories", "story", "verhalen", "geschichten", "magazine",
                              "journal", "press", "pers", "presse", "press-releases?", "persberichten", "updates?", "posts?"),
                    "title": None},
    "ProductPage": {"desc": "A single product with its details, variants or price.",
                    "url": kw("products?", "producten", "produits?", "produkte?", "shop", "winkel", "boutique", "p", "item", "sku"),
                    "title": None},
    "ListingPage": {"desc": "Overview of entries of another type, often with filters or pagination.", "url": None, "title": None},
    "CaseStudyPage": {"desc": "Client project or reference: challenge, solution, result.",
                      "url": kw("cases?", "case-stud(y|ies)", "projects?", "projecten", "projets?", "projekte?", "references?",
                                "referenties", "referenzen", "realisaties", "realisations", "réalisations", "work", "portfolio",
                                "klanten", "clients?", "kunden", "success-stor(y|ies)"),
                      "title": kw("case study", "case")},
    "JobPage": {"desc": "A single job vacancy.",
                "url": kw("vacanc(y|ies)", "vacatures?", "vacature", "jobs?", "careers?", "career", "werken-bij", "emplois?", "offres?-d-emploi",
                          "stellen", "stellenangebote?", "karriere", "jobboard"),
                "title": kw("vacature", "vacancy", "job", "m/v/x", "m/f/x", "m/w/d", "h/f")},
    "ContactPage": {"desc": "How to reach the organisation: form, address, phone, map.",
                    "url": kw("contact", "contact-us", "contacteer-ons", "contactez-nous", "kontakt", "get-in-touch", "neem-contact-op"),
                    "title": kw("contact", "kontakt", "get in touch")},
    "AboutPage": {"desc": "About the organisation: story, mission, history, values.",
                  "url": kw("about", "about-us", "over-ons", "over", "a-propos", "à-propos", "qui-sommes-nous", "uber-uns", "ueber-uns",
                            "über-uns", "mission", "missie", "history", "geschiedenis", "historique", "geschichte", "who-we-are", "our-story",
                            "trust", "values", "waarden", "company", "bedrijf"),
                  "title": kw("about", "over ons", "à propos", "über uns", "our story", "mission", "history")},
    "TeamPage": {"desc": "People: team members with photo and role.",
                 "url": kw("team", "teams", "people", "mensen", "medewerkers", "equipe", "équipe", "mitarbeiter", "leadership", "management"),
                 "title": kw("team", "people", "our people")},
    "LegalPage": {"desc": "Legal text: privacy, cookies, terms, disclaimer, imprint.",
                  "url": kw("privacy", "privacy-policy", "cookies?", "cookie-policy", "terms", "terms-and-conditions", "conditions",
                            "voorwaarden", "algemene-voorwaarden", "disclaimer", "imprint", "impressum", "legal", "mentions-legales",
                            "datenschutz", "agb", "gdpr", "rgpd", "avg", "list-of-cookies", "voucher-conditions", "general-terms.*",
                            "competition-terms.*", "commercial-warranty-statement", "warranty-statement"),
                  "title": kw("privacy", "cookie", "terms", "conditions", "disclaimer", "voorwaarden", "impressum", "datenschutz")},
    "ServicePage": {"desc": "A service or solution the organisation offers.",
                    "url": kw("services?", "diensten", "dienst", "solutions?", "oplossingen", "loesungen", "lösungen", "what-we-do",
                              "what-we-solve", "expertise", "capabilities", "offering", "aanbod", "leistungen", "prestations",
                              "consulting", "our-solutions"),
                    "title": None},
    "LocationPage": {"desc": "A single store, dealer, office or venue with address and opening details.",
                     "url": kw("dealers?", "stores?", "shops?", "winkels", "locations?", "locaties", "vestigingen", "offices?",
                               "kantoren", "magasins?", "revendeurs?", "handler", "haendler", "händler", "filialen", "showrooms?", "branches?"),
                     "title": None},
    "EventPage": {"desc": "An event, webinar or trade fair with a date and place.",
                  "url": kw("events?", "evenementen", "agenda", "webinars?", "evenements", "événements", "veranstaltungen", "seminars?"),
                  "title": kw("webinar", "event", "seminar")},
    "GalleryPage": {"desc": "Visual inspiration: photo galleries, lookbooks, project images.",
                    "url": kw("gallery", "galleries", "galerij", "galerie", "inspiration", "inspiratie", "inspirations", "lookbooks?",
                              "photos?", "fotos?", "images", "beelden", "showcase"),
                    "title": kw("gallery", "inspiration", "lookbook")},
    "SupportPage": {"desc": "Help and after-sales: care and repair instructions, manuals, warranty, service.",
                    "url": kw("support", "help", "hulp", "aide", "hilfe", "care", "product-care", "maintenance", "onderhoud", "entretien.*",
                              "pflege", "produktpflege", "after-sales.*", "service-na-verkoop", "service-apres-vente", "kundenservice",
                              "warranty", "garantie", "manuals?", "handleidingen", "how-to", "repairs?", "refurbishments?", "spare-parts"),
                    "title": kw("how to", "care", "repair", "warranty", "manual")},
    "FaqPage": {"desc": "Frequently asked questions.",
                "url": kw("faq", "faqs", "veelgestelde-vragen", "questions-frequentes", "haeufige-fragen", "häufige-fragen"),
                "title": kw("faq", "veelgestelde vragen", "frequently asked")},
    "DownloadsPage": {"desc": "Downloads: brochures, catalogues, files, media kit.",
                      "url": kw("downloads?", "product-downloads", "produkt-downloads", "produit-telechargements", "resources", "catalog(ue)?s?", "catalogi", "kataloge?", "brochures?", "media-?kit",
                                "press-kit", "logos?", "e-?books?", "whitepapers?", "documentation", "telechargements", "téléchargements"),
                      "title": kw("download", "catalog", "brochure", "e-book", "whitepaper")},
    "LandingPage": {"desc": "Campaign or topic page built to convert: hero, arguments, calls to action.",
                    "url": kw("lp", "landing", "campaign", "campagne", "promo", "actie", "offer", "aanbieding", "newsletter", "nieuwsbrief",
                              "sale", "signup", "sign-up", "register", "inschrijven", "aanmelden"),
                    "title": None},
    "ErrorPage": {"desc": "An error page (404, 500) or a broken URL.", "url": kw("404", "500", "undefined", "null", "error"),
                  "title": kw("not found", "page not found", "niet gevonden", "introuvable", "nicht gefunden", "404", "error")},
    "ContentPage": {"desc": "General informational page that fits no specific type.", "url": None, "title": None},
}

SCHEMA_TYPES = {
    "Article": "ArticlePage", "NewsArticle": "ArticlePage", "BlogPosting": "ArticlePage", "Report": "ArticlePage",
    "Product": "ProductPage", "ProductGroup": "ProductPage", "Offer": "ProductPage",
    "JobPosting": "JobPage", "Event": "EventPage", "LocalBusiness": "LocationPage", "Store": "LocationPage", "Place": "LocationPage",
    "AboutPage": "AboutPage", "ContactPage": "ContactPage", "CollectionPage": "ListingPage", "ItemList": "ListingPage",
    "FAQPage": "FaqPage", "ProfilePage": "TeamPage",
}


def path_segments(url: str) -> list[str]:
    segs = [s for s in urlparse(url).path.lower().split("/") if s]
    return segs[1:] if segs and LOCALE.match(segs[0]) else segs


def load_blocks(page: dict) -> list[dict]:
    path = page["dir"] / "blocks.json"
    return json.loads(path.read_text(encoding="utf-8"))["blocks"] if path.exists() else []


def page_signals(page: dict) -> dict:
    """Facts about a page that the scoring rules use."""
    blocks = [b for b in load_blocks(page) if b["region"] == "main"]
    items = [b["items"] for b in blocks if b.get("items")]
    main_height = max(1, sum(b["rect"]["h"] for b in blocks))
    h1 = next((h["text"] for h in page["headings"] if h["level"] == "h1"), "")
    return {
        "segments": path_segments(page["final_url"]),
        "title": f"{page['title'] or ''} | {h1}",
        "schema": set(page["schema_types"]),
        "og_type": (page["og"].get("og:type") or "").lower(),
        "status": page["status"],
        "noindex": bool(page["robots"] and "noindex" in page["robots"].lower()),
        "words": page["wordCount"],
        "main_words": sum(b["words"] for b in blocks),
        "forms": sum(b["inputs"] >= 2 or b["textareas"] > 0 for b in blocks),
        "email_inputs": sum(b["email_inputs"] for b in blocks),
        "tel": sum(b["tel_links"] for b in blocks),
        "mail": sum(b["mail_links"] for b in blocks),
        "map": sum(bool(b["canvas"] or b["map_links"] or any(re.search(r"google\.|maps\.", h) for h in b["iframes"])) for b in blocks),
        "address": sum(b["address"] for b in blocks),
        "files": sum(b["file_links"] for b in blocks),
        "prices": sum(1 for it in items if it["has_price"] >= 0.5) + sum(bool(re.search(r"[€$£]\s?\d", b["text_sample"])) for b in blocks[:4]),
        # A list that takes up most of the page (an overview), not a row of related cards at the bottom
        "dominant_list": max((b["items"]["count"] for b in blocks if b.get("items") and b["items"]["count"] >= 6
                              and (b["items"]["has_heading"] >= 0.5 or b["items"]["has_media"] >= 0.5)
                              and b["rect"]["h"] >= 0.4 * main_height), default=0),
        "linked_list": any(it["count"] >= 3 and it["has_link"] >= 0.6 for it in items),
        "filters": sum(b["selects"] + (b["checkboxes"] >= 2) + bool(re.search(r"^\s*(filter|sort)", b["text_sample"], re.I)) for b in blocks),
        "people_cards": sum(1 for it in items if it["has_media"] >= 0.8 and it["has_heading"] >= 0.8 and it["avg_words"] <= 15 and it["has_link"] < 0.5),
        "dates": sum(b["time_tags"] for b in blocks),
        "buttons": sum(b["button_links"] for b in blocks),
        "expandables": sum(b["expandables"] for b in blocks),
    }


def score_page(page: dict, s: dict) -> dict[str, tuple[float, list[str]]]:
    """Score each page type; returns {type: (score, evidence)}."""
    scores: dict[str, list] = defaultdict(lambda: [0.0, []])

    def add(t, points, why):
        scores[t][0] += points
        scores[t][1].append(why)

    segs = s["segments"]
    if not segs:
        add("HomePage", 3, "site root URL")

    # URL keywords: a whole segment says most (first segment more than deeper ones);
    # a keyword inside a hyphenated segment ("product" in "product-care") counts much less
    for t, spec in TYPES.items():
        if spec["url"]:
            for depth, seg in enumerate(segs[:3]):
                if spec["url"].fullmatch(seg):
                    add(t, 1.6 if depth == 0 else 1.0, f"URL segment '{seg}'")
                    break
                if spec["url"].fullmatch(seg.split("-")[0]):
                    add(t, 1.2 if depth == 0 else 0.8, f"URL segment '{seg}' starts with a keyword")
                    break
                if spec["url"].search(seg):
                    add(t, 0.5, f"keyword in URL segment '{seg}'")
                    break
        if spec["title"] and spec["title"].search(s["title"]):
            add(t, 0.8, "title/H1 wording")

    for schema_type in s["schema"]:
        if schema_type in SCHEMA_TYPES:
            # Site-wide markup (e.g. FAQPage on every page) is discounted by the caller via site_schema
            add(SCHEMA_TYPES[schema_type], 1.5, f"schema.org {schema_type}")
    if s["og_type"] in ("article", "blog", "news"):
        add("ArticlePage", 0.8, f"og:type {s['og_type']}")
    if s["og_type"] == "product":
        add("ProductPage", 1.2, "og:type product")

    if s["status"] and s["status"] >= 400:
        add("ErrorPage", 4, f"HTTP {s['status']}")
    if s["noindex"]:
        add("LegalPage", 0.4, "noindex (common on legal pages)")

    if s["forms"] and s["email_inputs"] and s["main_words"] < 250:
        add("LandingPage", 0.6, "sign-up form")
    if s["forms"] and (s["tel"] or s["mail"] or s["map"] or s["address"]):
        add("ContactPage", 1.2, "form with phone/email/map")
    elif s["forms"]:
        add("ContactPage", 0.6, "form")
    if (s["tel"] or s["mail"]) and (s["map"] or s["address"]) and s["main_words"] < 400:
        add("LocationPage", 1.0, "address with phone/email and map")
    if s["prices"]:
        add("ProductPage", 0.9, "prices on the page")
    if s["dominant_list"]:
        add("ListingPage", 1.4 + (0.6 if s["filters"] else 0), f"list of {s['dominant_list']} entries fills the page" + (" with filters" if s["filters"] else ""))
    if s["people_cards"]:
        add("TeamPage", 0.8, "cards with photo and name")
    if s["files"] >= 3:
        add("DownloadsPage", 1.2, f"{s['files']} file downloads")
    if s["dates"] and s["main_words"] > 300:
        add("ArticlePage", 0.5, "dated long text")
    if s["expandables"] >= 4:
        add("FaqPage", 0.5, f"{s['expandables']} collapsible items")
    add("ContentPage", 0.6, "fallback")  # a single weak signal (one keyword fragment, a few accordions) doesn't beat this
    return {t: (round(v[0], 2), v[1]) for t, v in scores.items()}


def recognise_pages(pages: list[dict]) -> dict[str, dict]:
    """Recognised type per page id: {type, confidence, score, evidence, runner_up}."""
    # Markup on most pages (site-wide FAQ schema, og:type "article" everywhere) says nothing about one page
    site_schema = Counter(t for p in pages for t in set(p["schema_types"]))
    common_schema = {t for t, n in site_schema.items() if len(pages) >= 5 and n > 0.5 * len(pages)}
    og_types = Counter((p["og"].get("og:type") or "").lower() for p in pages)
    common_og = {t for t, n in og_types.items() if t and len(pages) >= 5 and n > 0.5 * len(pages)}
    out, signals = {}, {}
    for p in pages:
        s = page_signals(p)
        s["schema"] -= common_schema
        if s["og_type"] in common_og:
            s["og_type"] = ""
        signals[p["id"]] = s
        # Section listings: the URL is the bare section (/products, /insights) of a collection that has detail pages
        scores = score_page(p, s)
        ranked = sorted(scores.items(), key=lambda kv: -kv[1][0])
        (best, (score, evidence)), runner = ranked[0], (ranked[1] if len(ranked) > 1 else (None, (0, [])))
        margin = score - runner[1][0]
        confidence = "high" if score >= 2.5 and margin >= 1 else "medium" if score >= 1.5 and margin >= 0.5 else "low"
        out[p["id"]] = {"type": best, "confidence": confidence, "score": score, "evidence": evidence,
                        "runner_up": runner[0] if runner[1][0] >= 1 else None}
    _mark_section_listings(pages, out, signals)
    return out


COLLECTION_TYPES = ("ArticlePage", "ProductPage", "CaseStudyPage", "JobPage", "LocationPage", "EventPage")


def _mark_section_listings(pages: list[dict], out: dict, signals: dict):
    """Section pages list the entries of a collection: /insights next to /insights/{slug}, or /jobs, /cases
    and /press-releases (a one-segment URL with a collection keyword and a list of links)."""
    deeper = Counter(path_segments(p["final_url"])[0] for p in pages if len(path_segments(p["final_url"])) >= 2)
    # What the pages under each section were recognised as: /products/… are ProductPages
    below = defaultdict(Counter)
    for p in pages:
        segs = path_segments(p["final_url"])
        if len(segs) >= 2:
            below[segs[0]][out[p["id"]]["type"]] += 1
    for p in pages:
        segs = path_segments(p["final_url"])
        r = out[p["id"]]
        if len(segs) == 1 and deeper.get(segs[0], 0) < 2 and r["type"] in COLLECTION_TYPES and signals[p["id"]]["linked_list"]:
            r["evidence"] = [f"section URL /{segs[0]} with a list of links"] + r["evidence"]
            r["listing_of"], r["type"], r["confidence"] = r["type"], "ListingPage", "medium"
            continue
        if len(segs) == 1 and deeper.get(segs[0], 0) >= 2:
            detail_type = below[segs[0]].most_common(1)[0][0]
            r["evidence"] = [f"section page above {deeper[segs[0]]} pages under /{segs[0]}/"] + r["evidence"]
            r["listing_of"] = detail_type if detail_type not in ("ContentPage", "ListingPage") else None
            r["type"], r["confidence"] = "ListingPage", "high" if r["confidence"] != "low" else "medium"


def recognise_cluster(member_ids: list[str], per_page: dict[str, dict]) -> dict:
    """Vote across the pages of a page type."""
    votes = Counter(per_page[i]["type"] for i in member_ids if i in per_page)
    if not votes:
        return {"type": "ContentPage", "confidence": "low", "share": 0, "votes": {}, "evidence": []}
    best, n = votes.most_common(1)[0]
    share = n / sum(votes.values())
    conf = Counter(per_page[i]["confidence"] for i in member_ids if per_page.get(i, {}).get("type") == best)
    confidence = ("high" if share >= 0.75 and conf["high"] >= conf["low"] else "medium" if share >= 0.5 else "low")
    evidence = Counter(e for i in member_ids if per_page.get(i, {}).get("type") == best for e in per_page[i]["evidence"])
    listing_of = Counter(per_page[i].get("listing_of") for i in member_ids if per_page.get(i, {}).get("listing_of"))
    if len(listing_of) > 1:
        listing_of = Counter()  # lists several collections (products and references): no single target
    return {"type": best, "confidence": confidence, "share": round(share, 2), "votes": dict(votes.most_common()),
            "evidence": [e for e, _ in evidence.most_common(4)], "listing_of": listing_of.most_common(1)[0][0] if listing_of else None}


def write_csv(crawl_dir: Path, pages: list[dict], per_page: dict[str, dict]):
    with open(crawl_dir / "page-recognition.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "url", "recognised_as", "listing_of", "confidence", "score", "runner_up", "evidence"])
        for p in pages:
            r = per_page[p["id"]]
            w.writerow([p["id"], p["final_url"], r["type"], r.get("listing_of") or "", r["confidence"], r["score"],
                        r["runner_up"] or "", "; ".join(r["evidence"])])


def main():
    ap = argparse.ArgumentParser(description="Recognise page types (article, product, contact, legal, …) with rules.")
    ap.add_argument("crawl_dir", type=Path, help="Crawl output folder, e.g. output/extremis")
    args = ap.parse_args()

    pages = load_pages(args.crawl_dir)
    if not pages:
        sys.exit(f"No crawled pages in {args.crawl_dir}/pages")
    per_page = recognise_pages(pages)
    write_csv(args.crawl_dir, pages, per_page)

    counts = Counter(r["type"] for r in per_page.values())
    print(f"{len(pages)} pages recognised:\n")
    for t, n in counts.most_common():
        conf = Counter(r["confidence"] for r in per_page.values() if r["type"] == t)
        print(f"  {t:<15} {n:>4}   high {conf['high']}, medium {conf['medium']}, low {conf['low']}   {TYPES[t]['desc']}")
    print(f"\nWrote {args.crawl_dir / 'page-recognition.csv'}")


if __name__ == "__main__":
    main()
