#!/usr/bin/env python3
"""
Page-type naming without AI (Phase 2b, rule-based)

Names each cluster with heuristics on URL patterns, schema.org types and page
structure. Review page-types.md: the names are a first draft.

Usage:
    python name_types_rules.py output/extremis

Output (in the crawl folder):
    page-types.json   clusters + names, the input for Phase 3 and for your review
    page-types.md     the same, readable
"""

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup

from common import CHROME_TAGS, CONSENT, load_pages, write_reports
from page_recognition import TYPES as RECOGNISED_TYPES, recognise_cluster, recognise_pages, write_csv as write_recognition

PLACEHOLDER = re.compile(r"^\{\w+\}(\.\w+)?$")   # {slug}, and {slug}.html for flat file URLs
JUNK = re.compile(r"(^|[-_/])(test|testing|qsdf|asdf|qwerty|lorem|ipsum|dummy|placeholder|tmp|temp|copy|old)([-_/]|$|\d)", re.I)
NOT_FOUND = re.compile(r"not found|404|page doesn.t exist|error", re.I)

# schema.org @type -> content entity
SCHEMA_ENTITIES = {
    "Product": "Product", "ProductGroup": "Product", "Offer": "Product",
    "Article": "Article", "NewsArticle": "Article", "BlogPosting": "Article",
    "Event": "Event", "Person": "Person", "Recipe": "Recipe", "JobPosting": "Job",
    "LocalBusiness": "Location", "Store": "Location", "Place": "Location",
    "VideoObject": "Video", "Course": "Course",
}
NOISE_SCHEMA = {"BreadcrumbList", "ListItem", "WebPage", "WebSite", "Organization", "SearchAction",
                "ImageObject", "ItemList", "SiteNavigationElement", "EntryPoint", "FAQPage", "Question", "Answer", "HowTo"}


# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------

def pascal(text: str) -> str:
    return "".join(w.capitalize() for w in re.split(r"[^A-Za-z0-9]+", text) if w) or "Page"


def singular(word: str) -> str:
    word = word.lower()
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("sses", "shes", "ches", "xes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is", "ws")):   # news, nieuws stay as they are
        return word[:-1]
    return word


def literal_segments(pattern: str) -> list[str]:
    """Segments of a URL pattern that are not placeholders: /{locale}/products/{slug}/{id} -> [products]."""
    return [s for s in pattern.strip("/").split("/") if s and not PLACEHOLDER.match(s)]


def is_template_pattern(pattern: str) -> bool:
    """True for patterns that stand for many pages (they contain {slug}, {id}, ...), apart from {locale}."""
    return any(PLACEHOLDER.match(s) and s != "{locale}" for s in pattern.strip("/").split("/"))


def words(pascal_name: str) -> str:
    """ProductDownload -> product download."""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", pascal_name).lower()


def ids_by_section(crawl_dir: Path) -> dict[tuple[str, str], set[str]]:
    """IDs per (locale, collection section) from every URL the crawl saw, crawled or not.

    e.g. ("en", "products") -> {"203", "204"}; locale is "" for sites without a locale prefix.
    """
    urls = []
    for name in ("inventory.csv", "skipped.csv"):
        path = crawl_dir / name
        if path.exists():
            with open(path, encoding="utf-8") as f:
                urls += [row["url"] for row in csv.DictReader(f)]
    out: dict[tuple[str, str], set[str]] = {}
    for url in urls:
        segs = [s for s in url.split("://", 1)[-1].split("/")[1:] if s]
        locale = ""
        if segs and re.fullmatch(r"[a-z]{2}(-[a-z]{2})?", segs[0], re.I):
            locale, segs = segs[0].lower(), segs[1:]
        if len(segs) >= 2 and segs[-1].isdigit():
            out.setdefault((locale, segs[0]), set()).add(segs[-1])
    return out


def shared_id_notes(section: str, locale: str, id_sets: dict[tuple[str, str], set[str]]) -> list[str]:
    """Notes on other sections whose IDs mostly overlap with this one's."""
    mine = id_sets.get((locale, section), set())
    if len(mine) < 3:
        return []
    views, translations = [], []
    for (other_locale, other), theirs in sorted(id_sets.items()):
        if (other_locale, other) == (locale, section) or len(mine & theirs) / min(len(mine), len(theirs)) < 0.5:
            continue
        if other_locale == locale:
            views.append(f"/{other}/")
        else:
            translations.append(f"/{other_locale}/{other}/" if other_locale else f"/{other}/ (no locale prefix)")
    notes = []
    if views:
        notes.append(f"Its IDs also appear under {', '.join(views)}: probably one entity shown in several views, "
                     "so model one entry type with several page templates.")
    if translations:
        notes.append(f"The same IDs appear in other locales ({', '.join(translations)}): "
                     "check which fields and slugs are localised.")
    return notes


def schema_entity(schema_types: list[str]) -> str | None:
    for t in schema_types:
        if t in SCHEMA_ENTITIES:
            return SCHEMA_ENTITIES[t]
    return None


# ---------------------------------------------------------------------------
# Content regions: the top-level blocks of <main>, compared across samples
# ---------------------------------------------------------------------------

def page_regions(page: dict) -> list[dict]:
    soup = BeautifulSoup((page["dir"] / "clean.html").read_text(encoding="utf-8"), "html.parser")
    for el in soup.find_all(attrs={"id": CONSENT}) + soup.find_all(attrs={"class": CONSENT}):
        el.decompose()
    root = soup.find("main")
    if root is None:
        root = soup.body or soup
        for el in root.find_all(CHROME_TAGS):
            el.decompose()
    # Descend through wrapper elements that have a single child
    children = root.find_all(recursive=False)
    while len(children) == 1 and children[0].find_all(recursive=False):
        root = children[0]
        children = root.find_all(recursive=False)

    regions = []
    for el in children:
        text = el.get_text(" ", strip=True)
        images = len(el.find_all("img"))
        if not text and not images:
            continue
        kids = el.find_all(recursive=False)
        while len(kids) == 1 and kids[0].find_all(recursive=False):
            kids = kids[0].find_all(recursive=False)
        kid_sigs = Counter(_sig(k) for k in kids)
        top_sig, top_count = kid_sigs.most_common(1)[0] if kid_sigs else ("", 0)
        heading = el.find(["h1", "h2", "h3"])
        regions.append({
            "sig": _sig(el),
            "tag": el.name,
            "heading_level": heading.name if heading else None,
            "heading": heading.get_text(" ", strip=True)[:80] if heading else None,
            "repeating": top_count >= 3 or len(el.find_all("li", recursive=True)) >= 3,
            "items": top_count,
            "words": len(text.split()),
            "images": images,
            "links": len(el.find_all("a")),
            "has_video": bool(el.find(["video"]) or el.find(class_=re.compile("video|aspect-video", re.I))),
            "has_form": bool(el.find(["form", "input", "select", "textarea"])),
            "is_breadcrumb": el.name == "nav" or "breadcrumb" in " ".join(el.get("class", [])).lower(),
        })
    return regions


def _sig(el) -> str:
    return el.name + "".join(f".{c}" for c in el.get("class", [])[:2])


def describe_region(region: dict, heading_is_shared: bool) -> tuple[str, str]:
    """Return (name, description) for a region from what it contains."""
    if region["is_breadcrumb"]:
        return "Breadcrumb", "Navigation trail back to the parent pages."
    if region["heading_level"] == "h1":
        return "Title and intro", f"Page title (h1){' with intro text' if region['words'] > 15 else ''}" \
                                  f"{' and media' if region['images'] else ''}."
    if region["has_form"]:
        return "Form", "Interactive form or filter controls."
    # Headings that repeat across samples are template labels ("Accessories"); drop counters like "( 3 )"
    label = re.sub(r"\s*\(\s*\d+\s*\)\s*$", "", region["heading"]) if region["heading"] and heading_is_shared else None
    if region["repeating"]:
        what = "cards with images" if region["images"] >= 3 else "links" if region["links"] >= 3 else "items"
        name = label or ("Card grid" if region["images"] >= 3 else "List")
        return name, f"Repeating list of about {max(region['items'], 3)} {what}."
    if region["has_video"]:
        return label or "Video", "Embedded video or media player."
    if region["images"] and region["words"] < 25:
        return label or "Media", f"Image block ({region['images']} image{'s' if region['images'] > 1 else ''}) with little text."
    if region["words"] >= 25:
        return label or "Body text", f"Text section of about {region['words']} words"\
                                      f"{' with images' if region['images'] else ''}."
    return label or "Section", f"Short section ({region['words']} words, {region['images']} images, {region['links']} links)."


def cluster_regions(samples: list[dict]) -> list[dict]:
    """Regions of the first sample (the medoid), flagged when they are missing or different in other samples."""
    per_page = [page_regions(p) for p in samples]
    if not per_page or not per_page[0]:
        return []
    headings_seen = Counter(r["heading"] for regions in per_page for r in regions if r["heading"])
    out = []
    for region in per_page[0]:
        present = sum(1 for regions in per_page if any(r["sig"] == region["sig"] for r in regions))
        name, description = describe_region(region, headings_seen[region["heading"]] > 1)
        out.append({
            "name": name,
            "description": description if present == len(per_page) else
            f"{description} Present in {present} of {len(per_page)} samples.",
            "repeating": region["repeating"] and not region["is_breadcrumb"],
            "varies_between_samples": present < len(per_page),
        })
    return out


# ---------------------------------------------------------------------------
# Per-cluster classification
# ---------------------------------------------------------------------------

def classify(cluster: dict, pages: dict, all_template_sections: set[str], id_sets: dict[tuple[str, str], set[str]],
             site_wide_schema: set[str]) -> dict:
    members = [pages[m["id"]] for m in cluster["members"]]
    samples = [pages[s] for s in cluster["samples"]]
    patterns = cluster["patterns"]
    main_pattern, main_count = next(iter(patterns.items()))
    template_pages = sum(n for p, n in patterns.items() if is_template_pattern(p))
    # Schema types that appear on most pages of the site (site-wide FAQ or organisation markup) say nothing about this type
    entity_from_schema = schema_entity([t for t in cluster["schema_types"] if t not in NOISE_SCHEMA and t not in site_wide_schema])
    notes = []

    # collection: most pages come from one or more templated URL patterns
    if template_pages / cluster["size"] >= 0.6 and template_pages >= 2:
        section = literal_segments(main_pattern)[0] if literal_segments(main_pattern) else "item"
        entity = entity_from_schema or pascal(singular(section))
        sub = literal_segments(main_pattern)[1:]  # e.g. stories/detail/{slug}
        kind = "collection"
        name = f"{entity}Detail" if not sub else f"{entity}{pascal(sub[-1])}"
        if pascal(singular(section)) != entity:
            notes.append(f"Entity taken from schema.org type rather than the URL section '{section}'.")
        description = f"Detail page for a single {words(entity)}, one page per entry under /{section}/."
        purpose = f"Show everything a visitor needs about one {words(entity)}."
        locale = members[0]["final_url"].split("://", 1)[-1].split("/")[1].lower() \
            if main_pattern.startswith("/{locale}") else ""
        notes += shared_id_notes(section, locale, id_sets)
        domain_entity = entity
        if len(patterns) > 1 and all(is_template_pattern(p) for p in patterns) and \
                len({literal_segments(p)[0] for p in patterns if literal_segments(p)}) > 1:
            notes.append("Several URL sections share this template: "
                         + ", ".join(f"/{literal_segments(p)[0]}/" for p in patterns if literal_segments(p))
                         + ". They may be one entity shown in different views, or separate entities with the same layout.")

    # listing: the index page of a collection section (/products next to /products/{slug}/{id})
    elif all(not is_template_pattern(p) and len(literal_segments(p)) == 1 and literal_segments(p)[0] in all_template_sections
             for p in patterns):
        sections = [literal_segments(p)[0] for p in patterns]
        entities = [pascal(singular(s)) for s in sections]
        kind = "listing"
        name = f"{entities[0]}Listing" if len(entities) == 1 else "CollectionListing"
        description = "Overview page listing the entries of: " + ", ".join(f"/{s}/" for s in sections) + "."
        purpose = "Let visitors browse, filter and pick an entry."
        domain_entity = None
        if len(entities) > 1:
            notes.append("One listing template serves several collections (" + ", ".join(entities) + ").")

    # singleton: a page whose layout appears nowhere else
    elif cluster["size"] == 1:
        page = members[0]
        segs = literal_segments(page["pattern"])
        last = page["final_url"].rstrip("/").rsplit("/", 1)[-1]
        kind = "singleton"
        name = "HomePage" if not segs else f"{pascal(last)}Page"
        description = "The home page." if not segs else f"One-off page with its own layout ({page['final_url']})."
        purpose = "Entry point to the site and its main sections." if not segs else "Serve a single, specific need."
        domain_entity = entity_from_schema
        notes.append("A single page; check whether it could be a ContentPage with special blocks instead of its own type.")

    # composition: many unrelated URLs share one flexible layout
    else:
        kind = "composition"
        sections = Counter(literal_segments(p)[0] for p, n in patterns.items() for _ in range(n) if literal_segments(p))
        section, count = sections.most_common(1)[0] if sections else ("", 0)
        # Only name it after a section when most of its pages live there (product-care/...)
        name = f"{pascal(singular(section))}Page" if count / cluster["size"] > 0.5 and cluster["size"] < 10 else "ContentPage"
        description = f"Flexible page assembled from blocks, used by {len(patterns)} different URLs."
        purpose = "Editorial and informational pages that editors compose themselves."
        domain_entity = None

    if cluster.get("layout_variants", 1) > 1:
        notes.append(f"{cluster['layout_variants']} layout variants of one template: some pages have extra or fewer blocks, "
                     "so model those blocks as optional.")

    # Mixed clusters: a dominant template pattern plus a few unrelated pages
    split = None
    if kind == "collection":
        outliers = [p for p in patterns if not is_template_pattern(p)]
        if outliers:
            split = "Move these pages out of the cluster, they don't belong to the template: " + ", ".join(outliers)

    anomalies = []
    for p in members:
        slug = p["final_url"].rstrip("/").rsplit("/", 1)[-1]
        if p["status"] and p["status"] >= 400:
            anomalies.append(f"HTTP {p['status']}: {p['final_url']}")
        elif NOT_FOUND.search(p["title"] or ""):
            anomalies.append(f"Looks like an error page (title '{p['title']}'): {p['final_url']}")
        elif JUNK.search(slug):
            anomalies.append(f"Looks like a test or leftover page: {p['final_url']}")
        elif p["wordCount"] < 60 and kind != "listing":
            anomalies.append(f"Almost no content ({p['wordCount']} words incl. header and footer): {p['final_url']}")

    if kind == "collection" and cluster["cohesion"] >= 0.9 and not split:
        confidence = "high"
    elif kind in ("collection", "listing") or cluster["cohesion"] >= 0.85:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "name": name, "kind": kind, "description": description, "purpose": purpose,
        "domain_entity": domain_entity, "content_regions": cluster_regions(samples),
        "merge_candidates": [], "split_suggestion": split, "anomalies": anomalies,
        "confidence": confidence, "notes": " ".join(notes),
    }


KIND_OF = {"ListingPage": "listing", "HomePage": "singleton"}


def apply_recognition(result: dict, rec: dict):
    """Use the recognised page type (LegalPage, SupportPage, …) to name pages that the URL alone can't name.
    Collections keep their entity name (ArticleDetail, DealerDetail); the recognition is recorded for all."""
    result["recognised_as"] = rec
    rtype = rec["type"]
    if result["kind"] == "composition" and len(result.get("_members", [])) >= 8 and rec["share"] < 0.75:
        # A generic block template used for several purposes: keep it generic and say what it's used for
        result["notes"] = (result["notes"] + " Used for: " + ", ".join(f"{t} ({n})" for t, n in rec["votes"].items()) + ".").strip()
        return
    if rtype == "ContentPage" or rec["confidence"] == "low" and rec["share"] < 0.6:
        return
    if result["kind"] == "listing" and not result["name"].startswith(("Collection", "Listing")):
        return  # already named after its collection (InspirationListing)
    if result["kind"] == "collection":
        if rtype not in ("ListingPage", "HomePage"):
            result["notes"] = (result["notes"] + f" Recognised as {rtype} ({rec['confidence']}).").strip()
        return
    if rtype == "ListingPage":
        target = rec.get("listing_of")
        name = f"{target.removesuffix('Page')}Listing" if target else "CollectionListing"
    else:
        name = rtype
    result["name"] = name
    result["kind"] = KIND_OF.get(rtype, result["kind"])
    result["description"] = RECOGNISED_TYPES[rtype]["desc"] + " " + result["description"]
    result["confidence"] = "high" if rec["confidence"] == "high" else result["confidence"]


def add_merge_candidates(clusters: list[dict], results: dict):
    """Clusters with the same name, or collections of the same entity, are candidates to merge."""
    for c in clusters:
        mine = results[c["id"]]
        for other in clusters:
            theirs = results[other["id"]]
            if other["id"] == c["id"]:
                continue
            same_name = theirs["name"] == mine["name"]
            same_entity = mine["kind"] == theirs["kind"] == "collection" and mine["domain_entity"] == theirs["domain_entity"]
            if same_name or same_entity:
                mine["merge_candidates"].append(other["id"])


def dedupe_names(results: dict):
    """Give clusters that ended up with the same name a numeric suffix, so names stay unique keys."""
    counts = Counter(r["name"] for r in results.values())
    seen = Counter()
    for cid in sorted(results):
        name = results[cid]["name"]
        if counts[name] > 1:
            seen[name] += 1
            results[cid]["name"] = f"{name}{seen[name]}"


def main():
    ap = argparse.ArgumentParser(description="Name page-type clusters with heuristics (no AI).")
    ap.add_argument("crawl_dir", type=Path, help="Crawl output folder containing clusters.json")
    args = ap.parse_args()

    clusters_file = args.crawl_dir / "clusters.json"
    if not clusters_file.exists():
        sys.exit(f"{clusters_file} not found; run cluster.py first")
    clusters = json.loads(clusters_file.read_text(encoding="utf-8"))["clusters"]
    pages = {p["id"]: p for p in load_pages(args.crawl_dir)}

    # Sections with at least two templated pages are collections (/products/{slug}/{id}); one sub-page isn't
    section_counts = Counter(literal_segments(p["pattern"])[0] for p in pages.values()
                             if is_template_pattern(p["pattern"]) and literal_segments(p["pattern"]))
    template_sections = {s for s, n in section_counts.items() if n >= 2}
    id_sets = ids_by_section(args.crawl_dir)
    schema_counts = Counter(t for p in pages.values() for t in set(p["schema_types"]))
    site_wide_schema = {t for t, n in schema_counts.items() if n > 0.5 * len(pages)}
    results = {c["id"]: classify(c, pages, template_sections, id_sets, site_wide_schema) for c in clusters}
    per_page = recognise_pages(list(pages.values()))
    write_recognition(args.crawl_dir, list(pages.values()), per_page)
    for c in clusters:
        results[c["id"]]["_members"] = c["members"]
        apply_recognition(results[c["id"]], recognise_cluster([m["id"] for m in c["members"]], per_page))
        del results[c["id"]]["_members"]
    add_merge_candidates(clusters, results)
    dedupe_names(results)

    write_reports(args.crawl_dir, clusters, results, "rules (`name_types_rules.py`, no AI)")
    for c in clusters:
        r = results[c["id"]]
        flags = (" merge?" if r["merge_candidates"] else "") + (" split?" if r["split_suggestion"] else "") \
            + (f" {len(r['anomalies'])} anomalies" if r["anomalies"] else "")
        print(f"  [{c['id']:>2}] {r['name']:<28} {r['kind']:<12} {c['size']:>3} pages  {r['confidence']}{flags}")
    print(f"\nWrote {args.crawl_dir / 'page-types.json'} and page-types.md")


if __name__ == "__main__":
    main()
