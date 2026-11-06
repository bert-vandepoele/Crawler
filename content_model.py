#!/usr/bin/env python3
"""
Phase 4: draft a headless content model from the crawl, with rules only (no AI).

What it decides, and from which evidence:
  - content types     collections (one entry per page, e.g. Article), single types (HomePage) and a
                      generic Page with a block zone, from the page types of Phase 2
  - fixed fields      components that sit in the same slot on most pages of a type (PageHeader → title,
                      intro; a closing CallToAction → cta), from the component sequences of Phase 3
  - block zone        components whose order or presence varies, as a list of allowed block types
  - DOM fields        for types with several pages, the <main> DOM of all pages is aligned: text that is
                      the same everywhere is template text, text that varies is a field (date, author,
                      client, location, ...). Field names come from JSON-LD, itemprop, labels next to
                      the value ("Client:", "Locatie") or class names; types from the values.
  - references        links from one type's pages to another collection's URL pattern
  - taxonomies        varying links that share a URL pattern that is not a content type (/tag/{slug})
  - block library     every component type with merged fields and its style options
  - globals           header and footer navigation, site settings
  - SEO and locales   an SEO component on every routable type; which types are translated
  - coverage          share of main-content words that fall in a block or field of the model

Usage:
    python content_model.py output/extremis.com-en
    python content_model.py output/two-point-o.com --sample 80

Needs: page-types.json (name_types_rules.py) and components.json (components.py).

Output (in the crawl folder):
    content-model.json   the model, with evidence and a confidence per decision
    content-model.md     the same, readable
"""

import argparse
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from common import CONSENT, load_pages
from crawl import url_pattern
from url_groups import pattern_groups
from site_analysis import analyse_locales, analyse_navigation, url_locale

FIXED_SHARE = 0.8        # same component in the same slot on this share of pages → fixed field
REQUIRED_SHARE = 0.95    # present on this share of pages → required
FIELD_SHARE = 0.25       # a varying DOM value on fewer pages than this is noise
LABEL_SHARE = 0.8        # the same text on this share of the pages that have it → template text
MIN_DIFF_PAGES = 3       # fewer pages give no variance to tell fields from template text
MAX_DIFF_FIELDS = 20
GENERATED = {"Breadcrumb", "Pagination", "SiteHeader", "SiteFooter"}   # built from data, no editor fields
LAYOUT_ONLY = {"Divider"}
BLOCK_TAGS = {"div", "p", "section", "article", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "tr",
              "td", "th", "dl", "dt", "dd", "figure", "figcaption", "blockquote", "aside", "header", "footer", "main"}
STRIP_TAGS = ["script", "style", "noscript", "svg", "template", "header", "footer", "nav", "form", "dialog"]
STYLE_OPTIONS = {"layout", "background", "full_bleed", "marquee", "two_columns", "columns", "map", "media_position"}

MONTHS = {m: i for i, names in enumerate([
    "jan january januari janvier januar", "feb february februari février fevrier februar",
    "mar march maart mars märz marz", "apr april avril", "may mei mai", "jun june juni juin",
    "jul july juli juillet", "aug august augustus août aout", "sep sept september septembre",
    "oct october oktober octobre", "nov november novembre", "dec december décembre decembre dezember"], 1)
    for m in names.split()}
DATE_NUMERIC = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-]((?:19|20)\d{2})\b")
DATE_ISO = re.compile(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})")
DATE_WORDS = re.compile(r"\b(\d{1,2})?\.?\s*([a-zA-Zéûä]{3,10})\.?\s+(\d{1,2},?\s+)?((?:19|20)\d{2})\b")
ISBN = re.compile(r"^(97[89][- ]?)?\d{1,5}[- ]?\d{1,7}[- ]?\d{1,7}[- ]?[\dX]$")
READING_TIME = re.compile(r"^\d+\s*(min|mins|minutes|minuten|minuti|minutos)\b", re.I)
PRICE = re.compile(r"(€|\$|£|\bEUR\b|\bUSD\b|\bGBP\b)\s*\d|\d[\d.,]*\s*(€|\$|£|EUR|USD)")
NUMBER = re.compile(r"^[\d.,]+\s*[a-zA-Z%]{0,4}$")
EMAIL = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")
PHONE = re.compile(r"^\+?[\d ()./-]{7,20}$")
INLINE_LABEL = re.compile(r"^([^:\n]{2,40}):\s+(\S.{0,300})$")

# Label text and class tokens → field name (en / nl / fr / de)
NAMES = [
    ("date", r"date|datum|published|publication|gepubliceerd|publié|veröffentlicht|posted|geplaatst"),
    ("author", r"author|auteur|autor|written by|geschreven door|byline|door|by|par|von"),
    ("client", r"client|klant|customer|kunde"),
    ("industry", r"industry|sector|branche|secteur|industrie"),
    ("location", r"location|locatie|lieu|standort|place|plaats|city|stad|ville|stadt|region|regio"),
    ("address", r"address|adres|adresse|anschrift"),
    ("country", r"country|land|pays"),
    ("category", r"category|categorie|catégorie|kategorie|topic|onderwerp|thème|theme|thema|type"),
    ("tags", r"tags?|labels?|keywords|trefwoorden|schlagwörter"),
    ("reading_time", r"reading time|read time|leestijd|temps de lecture|lesezeit|min read"),
    ("price", r"price|prijs|prix|preis"),
    ("designer", r"designer|design by|ontwerper|design|ontwerp"),
    ("material", r"materials?|materia(a)?l(en)?|matériaux?|werkstoff"),
    ("dimensions", r"dimensions?|outer dimensions|afmetingen|abmessungen|maße|size|maat"),
    ("weight", r"weight|gewicht|poids"),
    ("year", r"year|jaar|année|jahr"),
    ("role", r"role|function|functie|fonction|position|job title|funktion"),
    ("employment_type", r"employment|contract|dienstverband|regime|type of contract|vertrag"),
    ("salary", r"salary|salaris|loon|salaire|gehalt"),
    ("duration", r"duration|duur|durée|dauer"),
    ("phone", r"phone|telefoon|tel|téléphone|telefon"),
    ("email", r"e-?mail"),
    ("website", r"website|site web|webseite"),
    ("services", r"services?|diensten|dienst|leistungen"),
    ("technology", r"technolog(y|ie|ies)|tech stack|platform|stack"),
    ("partner", r"partners?"),
    ("sku", r"sku|article number|artikelnummer|art\.? ?nr\.?|référence|reference number|product code"),
    ("subtitle", r"subtitle|subheading|ondertitel|kicker|eyebrow|overline"),
    ("summary", r"summary|excerpt|samenvatting|résumé|teaser|lead|intro"),
    ("photographer", r"photos?|pictures? by|photography|foto'?s?|fotografie"),
]
NAME_PATTERNS = [(name, re.compile(rf"^(?:{rx})$", re.I)) for name, rx in NAMES]
CLASS_PATTERNS = [(name, re.compile(rf"(?:^|[-_]){rx}(?:$|[-_])", re.I)) for name, rx in NAMES
                  if name not in ("author", "category", "summary")] + [
    ("author", re.compile(r"(?:^|[-_])(author|byline)(?:$|[-_])", re.I)),
    ("category", re.compile(r"(?:^|[-_])(category|topic|categorie)(?:$|[-_])", re.I)),
    ("summary", re.compile(r"(?:^|[-_])(summary|excerpt|teaser|lead|intro)(?:$|[-_])", re.I)),
]
JSONLD_SKIP = {"@context", "@id", "@type", "url", "mainEntityOfPage", "inLanguage", "publisher", "isPartOf", "potentialAction",
               "logo", "sameAs", "breadcrumb", "position", "item", "itemListElement", "headline", "name", "description", "mainEntity"}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def snake(text: str, limit: int = 4) -> str:
    words = re.findall(r"[a-z0-9]+", re.sub(r"\(.*?\)", "", text.lower()))
    return "_".join(words[:limit]) or "field"


def camel_to_snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def words(text: str) -> int:
    return len(text.split())


def parse_date(text: str) -> str | None:
    """ISO date for '12 March 2024', 'March 12, 2024', '12/03/2024', '2024-03-12'; month-only dates get day 1."""
    if m := DATE_ISO.search(text):
        return f"{m[1]}-{m[2]}-{m[3]}"
    if m := DATE_NUMERIC.search(text):
        day, month = int(m[1]), int(m[2])
        if month <= 12 and day <= 31:
            return f"{m[3]}-{month:02d}-{day:02d}"
    if m := DATE_WORDS.search(text):
        month = MONTHS.get(m[2].lower())
        if month:
            day = m[1] or (m[3] or "").strip(" ,") or "1"
            try:
                return date(int(m[4]), month, int(day)).isoformat()
            except ValueError:
                return None
    return None


def label_name(label: str) -> tuple[str, bool]:
    """Field name for a label text; True when it came from the dictionary."""
    clean = norm(re.sub(r"[:©]", " ", label))
    for name, rx in NAME_PATTERNS:
        if rx.match(clean):
            return name, True
    # 'Design by', 'Visit website': a known label inside a short text
    if words(clean) <= 4:
        # Short generic words ("type", "by") inside a longer label say little: "Cover type" stays cover_type
        hits = [(len(m[0]), name) for name, rx in NAMES if (m := re.search(rf"\b(?:{rx})\b", clean, re.I))
                and len(m[0]) >= 5 and m[0].lower() not in ("type", "theme", "thema")]
        if hits:
            return max(hits)[1], True
    return snake(clean), False


def plural(name: str) -> str:
    base = camel_to_snake(name)
    return base[:-1] + "ies" if base.endswith("y") else base + ("es" if base.endswith("s") else "s")


def confidence(share: float, pages: int, named: bool) -> str:
    if pages >= 5 and share >= 0.6 and named:
        return "high"
    if pages >= 3 and (share >= 0.4 or named):
        return "medium"
    return "low"


def field(name, ftype, share, pages, *, evidence, named=True, examples=(), list_=False, ref=None, values=None,
          source="components") -> dict:
    return {"name": name, "type": ftype, "required": share >= REQUIRED_SHARE, "list": list_, "ref": ref,
            "enum_values": values, "share": round(share, 2), "examples": [e for e in examples if e][:3],
            "evidence": evidence, "source": source, "confidence": confidence(share, pages, named)}


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def load_inputs(d: Path) -> dict:
    missing = [n for n in ("page-types.json", "components.json") if not (d / n).exists()]
    if missing:
        sys.exit(f"Missing in {d}: {', '.join(missing)}. Run name_types_rules.py and components.py first.")
    page_types = json.loads((d / "page-types.json").read_text(encoding="utf-8"))["page_types"]
    components = json.loads((d / "components.json").read_text(encoding="utf-8"))
    pages = {p["id"]: p for p in load_pages(d)}
    for cp in components["pages"]:
        if cp["id"] in pages:
            pages[cp["id"]]["blocks"] = cp["blocks"]
            pages[cp["id"]]["page_type"] = cp.get("page_type")
    return {"page_types": [t for t in page_types if t.get("name")], "components": components, "pages": pages}


def page_root(page: dict) -> Tag | None:
    path = page["dir"] / "clean.html"
    if not path.exists():
        return None
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    for el in soup.find_all(attrs={"id": CONSENT}) + soup.find_all(attrs={"class": CONSENT}):
        el.decompose()
    root = soup.find("main") or soup.body or soup
    for el in root.find_all(STRIP_TAGS):
        el.decompose()
    for el in root.find_all(attrs={"aria-hidden": "true"}):
        el.decompose()
    return root


# ---------------------------------------------------------------------------
# 1. Content types from page types
# ---------------------------------------------------------------------------

def entity_of(t: dict) -> str:
    name = t.get("domain_entity") or re.sub(r"(Detail|Page)?\d*$", "", t["name"]) or t["name"]
    return re.sub(r"\W", "", name[:1].upper() + name[1:])


ENTITY_SCHEMA = {"Product", "ProductGroup", "Book", "Article", "NewsArticle", "BlogPosting", "Event", "JobPosting", "Recipe",
                 "Course", "Person", "Place", "LocalBusiness", "Store", "Movie", "MusicAlbum", "SoftwareApplication",
                 "Service", "Vehicle", "Hotel", "Restaurant", "Review", "Podcast", "PodcastEpisode", "VideoObject", "Car"}
SCHEMA_NAMES = {"BlogPosting": "BlogPost", "JobPosting": "Job", "ProductGroup": "Product"}
CATEGORY_SCHEMA = {"CollectionPage", "ItemList", "OfferCatalog"}


def page_signals(page_types: list[dict], pages: dict) -> dict[str, dict]:
    """Per page type: the dominant entity type from schema.org / og:type, and whether its pages are category pages.

    Schema types on (nearly) every page type are site-wide boilerplate. Book on 80% of all pages is not, when
    it only occurs in one page type: that page type is simply most of the site."""
    spread = Counter()
    per_type = {}
    for t in page_types:
        members = [pages[m["id"]] for m in t["members"] if m["id"] in pages]
        types = Counter(s for p in members for s in set(p.get("schema_types") or []))
        og = Counter((p.get("og") or {}).get("og:type") or "" for p in members)
        per_type[t["name"]] = (members, types, og)
        spread.update(types.keys())
    sitewide = {s for s, c in spread.items() if len(page_types) >= 3 and c / len(page_types) >= 0.6}
    out = {}
    for name, (members, types, og) in per_type.items():
        n = len(members) or 1
        entity = next(((s, c) for s, c in types.most_common() if s in ENTITY_SCHEMA and s not in sitewide and c / n >= 0.6), None)
        signal = {"entity": None, "evidence": None, "category": False}
        if entity:
            signal.update(entity=SCHEMA_NAMES.get(entity[0], entity[0]), evidence=f"schema.org {entity[0]} on {entity[1] / n:.0%} of its pages")
        elif og.get("product", 0) / n >= 0.6:
            signal.update(entity="Product", evidence=f"og:type product on {og['product'] / n:.0%} of its pages")
        if og.get("product.group", 0) / n >= 0.6 or sum(c for s, c in types.items() if s in CATEGORY_SCHEMA) / n >= 0.6:
            signal["category"] = True
        out[name] = signal
    return out


def group_types(page_types: list[dict], pages: dict) -> list[dict]:
    """Collections merge by entity, compositions and listings become one generic Page, singletons stay single types.

    Structured data overrules the URL: a "composition" whose pages are all schema.org Books is a Book collection
    (flat product URLs like /some-title.html look like one-off pages), and pages marked as product groups or
    collection pages are category landing pages."""
    groups: dict[str, dict] = {}
    signals = page_signals(page_types, pages)

    def add(key, kind, t, note=None):
        g = groups.setdefault(key, {"name": key, "kind": kind, "page_types": [], "members": [], "patterns": Counter(),
                                    "notes": [], "listings": []})
        g["page_types"].append(t["name"])
        g["members"] += [m["id"] for m in t["members"]]
        g["patterns"].update(t["patterns"])
        if note and note not in g["notes"]:
            g["notes"].append(note)
        return g

    composition_bases = {re.sub(r"\d+$", "", t["name"]) for t in page_types if t.get("kind") == "composition"}
    for t in page_types:
        kind, sig = t.get("kind"), signals.get(t["name"], {})
        if sig.get("category") and len(t["members"]) >= 1 and kind != "singleton":
            add("Category", "category", t, "Category landing pages (product groups): a hierarchical taxonomy whose terms "
                "have their own page, listing the entries of that category.")
        elif sig.get("entity") and kind in ("collection", "composition") and len(t["members"]) >= MIN_DIFF_PAGES:
            note = f"Named from structured data: {sig['evidence']}" + (f" (page type {t['name']})" if kind == "composition" else "")
            add(sig["entity"], "collection", t, note)
        elif kind == "collection":
            add(entity_of(t), "collection", t)
        elif kind == "listing":
            g = add("Page", "page", t, f"{t['name']}: listing pages, modelled as a Page with a CollectionList block")
            g["listings"].append(t["name"])
        elif kind == "singleton" and re.sub(r"\d+$", "", t["name"]) not in composition_bases | {"ContentPage"}:
            add(re.sub(r"\d+$", "", t["name"]), "single", t)
        else:
            add("Page", "page", t)

    # A composition page type can mix entity pages with others: move the non-entity pages to Page
    for g in list(groups.values()):
        if g["kind"] != "collection" or not any(n.startswith("Named from structured data") for n in g["notes"]):
            continue
        keep, move = [], []
        for pid in g["members"]:
            p = pages.get(pid)
            has = p and (g["name"] in [SCHEMA_NAMES.get(s, s) for s in p.get("schema_types") or []]
                         or ((p.get("og") or {}).get("og:type") == "product" and g["name"] == "Product"))
            (keep if has else move).append(pid)
        if move and keep:
            g["members"] = keep
            page = groups.setdefault("Page", {"name": "Page", "kind": "page", "page_types": [], "members": [], "patterns": Counter(),
                                              "notes": [], "listings": []})
            page["members"] += move
            g["notes"].append(f"{len(move)} pages of the same page types without that structured data were moved to Page.")
        g["patterns"] = Counter(pages[pid].get("pattern") or url_pattern(pages[pid]["final_url"]) for pid in g["members"] if pid in pages)
        if g["patterns"] and len(g["patterns"]) > 0.5 * len(g["members"]):
            g["patterns"] = Counter({"(one URL per entry, no shared pattern)": len(g["members"])})
    order = {"collection": 0, "category": 1, "single": 2, "page": 3}
    return sorted(groups.values(), key=lambda g: (order[g["kind"]], -len(g["members"])))


# ---------------------------------------------------------------------------
# 2. Fixed fields and block zone from component sequences
# ---------------------------------------------------------------------------

SITE_WIDE: set = set()   # signatures of blocks repeated on most pages of the site (promo banners), filled by build()


def block_signature(b: dict) -> tuple:
    return b["type"], norm(b.get("text") or "")[:80], round(b["rect"]["h"] / 10)


def find_site_wide(pages: list[dict], share: float = 0.6) -> dict[tuple, int]:
    """Blocks inside the content area that are the same on most pages of most page types: site chrome
    (a promo banner above the breadcrumb), not content. One page type that is most of the site does not make a
    block site-wide; a banner on every page type except the books does."""
    per_type = Counter(p.get("page_type") for p in pages)
    by_type = defaultdict(Counter)
    for p in pages:
        for sig in {block_signature(b) for b in p.get("blocks", []) if b["region"] == "main"}:
            by_type[sig][p.get("page_type")] += 1
    out = {}
    for sig, counts in by_type.items():
        if sig[0] in GENERATED | LAYOUT_ONLY or len(pages) < 10 or len(per_type) < 3:
            continue
        typical = [t for t, n in counts.items() if n / per_type[t] >= share]
        if len(typical) / len(per_type) >= 0.5:
            out[sig] = sum(counts.values())
    return out


def main_blocks(page: dict) -> list[dict]:
    return [b for b in page.get("blocks", []) if b["region"] == "main" and b["type"] not in GENERATED | LAYOUT_ONLY
            and block_signature(b) not in SITE_WIDE]


def split_slots(seqs: list[list[dict]]) -> tuple[list, list, list]:
    """Leading and trailing components that most pages share (fixed slots), and what is left in between."""
    n = len(seqs)
    rest = [list(s) for s in seqs]
    head, tail = [], []
    for side, slots in ((0, head), (-1, tail)):
        while True:
            ends = Counter(s[side]["type"] for s in rest if s)
            if not ends:
                break
            ctype, count = ends.most_common(1)[0]
            if count / n < FIXED_SHARE or ctype == "Unknown":
                break
            slots.append({"type": ctype, "share": count / n,
                          "blocks": [s[side] for s in rest if s and s[side]["type"] == ctype]})
            rest = [(s[1:] if side == 0 else s[:-1]) if s and s[side]["type"] == ctype else s for s in rest]
    return head, tail[::-1], rest


def slot_fields(slot: dict, is_first: bool, pages: int) -> list[dict]:
    """Fields for one fixed slot: the opening header is flattened (title, intro, media), others are embedded components."""
    ctype, share, blocks = slot["type"], slot["share"], slot["blocks"]
    counts = Counter(f for b in blocks for f in b["fields"])
    if is_first and ctype in ("PageHeader", "Hero"):
        out = []
        for spec, n in counts.most_common():
            name, _, ftype = spec.partition(": ")
            sub_share = share * n / len(blocks)
            if sub_share >= FIELD_SHARE:
                out.append(field(name, ftype, sub_share, pages, evidence=f"{ctype} opens {share:.0%} of the pages"))
        return out
    if ctype == "RichText":
        return [field("body", "rich text", share, pages, evidence=f"one RichText block in this slot on {share:.0%} of the pages")]
    headings = Counter(norm(b.get("heading") or "") for b in blocks)
    heading, hn = headings.most_common(1)[0]
    named = bool(heading) and hn / len(blocks) >= 0.6 and words(heading) <= 5
    name = snake(heading) if named else camel_to_snake(ctype if ctype != "CallToAction" else "cta")
    ev = f"{ctype} in the same slot on {share:.0%} of the pages" + (f", always titled '{heading}'" if named else "")
    return [field(name, f"component: {ctype}", share, pages, evidence=ev, named=True,
                  examples=[b.get("heading") or b.get("text", "")[:60] for b in blocks[:3]])]


def structure(pages: list[dict], kind: str) -> dict:
    seqs = [main_blocks(p) for p in pages]
    n = len(seqs)
    if n >= MIN_DIFF_PAGES and kind != "page":
        head, tail, middle = split_slots(seqs)
    else:
        first = [s[0] for s in seqs if s and s[0]["type"] in ("PageHeader", "Hero")]
        head = [{"type": first[0]["type"], "share": len(first) / n, "blocks": first}] if first else []
        middle = [s[1:] if s and s[0]["type"] in ("PageHeader", "Hero") else s for s in seqs]
        tail = []
    fields = []
    for i, slot in enumerate(head + tail):
        fields += slot_fields(slot, i == 0 and slot in head, n)

    used = [s for s in middle if s]
    zone = None
    if used:
        types = Counter(t for s in used for t in {b["type"] for b in s})
        only_text = sum(1 for s in used if [b["type"] for b in s] == ["RichText"])
        if n >= MIN_DIFF_PAGES and only_text / n >= FIXED_SHARE and not any(f["name"] == "body" for f in fields):
            fields.append(field("body", "rich text", only_text / n, n, evidence="the middle of the page is one RichText block"))
        else:
            zone = {"name": "blocks", "share": round(len(used) / n, 2),
                    "allowed": {t: round(c / n, 2) for t, c in types.most_common()},
                    "lengths": {"min": min(map(len, used)), "median": statistics.median(map(len, used)), "max": max(map(len, used))}}
    return {"fields": fields, "zone": zone, "head": [s["type"] for s in head], "tail": [s["type"] for s in tail]}


# ---------------------------------------------------------------------------
# 3. DOM diff: template text versus fields
# ---------------------------------------------------------------------------

def _sig(el: Tag) -> str:
    # Tailwind arbitrary values and variants (text-[1.2rem], md:flex) are styling, and "[" marks labels in keys
    classes = [c for c in el.get("class", []) if not re.search(r"\d{3,}|active|open|selected|hidden|^is-|^js-|swiper|slick|[\[\]:]", c)][:2]
    return el.name + "".join("." + c for c in classes)


def _hint(el: Tag) -> str | None:
    """Semantic name from itemprop or class tokens on the element or two ancestors (.author, .c-card__date)."""
    node = el
    for _ in range(3):
        if not isinstance(node, Tag):
            break
        if node.get("itemprop"):
            return "itemprop:" + node["itemprop"]
        for cls in node.get("class", []):
            for name, rx in CLASS_PATTERNS:
                if rx.search(cls.split("__")[-1]):
                    return "class:" + name
        node = node.parent
    return None


def _href(el: Tag) -> str | None:
    if el.name == "a":
        return el.get("href")
    parent = el.find_parent("a")
    if parent:
        return parent.get("href")
    inner = el.find_all("a")
    return inner[0].get("href") if len(inner) == 1 else None


PROSE = re.compile(r"prose|rich-?text|wysiwyg|richtext|cms-content|article-body|body-text|text-content|editor", re.I)


def _in_prose(el: Tag) -> bool:
    """Inside running text: a container with a rich-text class or with three or more paragraphs."""
    for node in el.parents:
        if not isinstance(node, Tag) or node.name in ("main", "body"):
            return False
        if any(PROSE.search(c) for c in node.get("class", [])) or len(node.find_all("p", recursive=False)) >= 3:
            return True
    return False


def extract_leaves(root: Tag) -> list[dict]:
    """Text and image leaves of <main> in document order, keyed by their tag/class path."""
    out = []

    def leaf(el: Tag, key: str, text: str):
        if m := INLINE_LABEL.match(text):
            label, value = norm(m[1]), norm(m[2])
            if words(label) <= 5 and not label.lower().startswith("http"):
                out.append({"key": key + "@label", "text": label + ":", "tag": el.name, "href": None})
                key, text = f"{key}[{label.lower()}]", value
        time = el if el.name == "time" else el.find("time")
        out.append({"key": key, "text": text, "tag": el.name, "href": _href(el), "hint": _hint(el),
                    "datetime": time.get("datetime") if time else None, "prose": _in_prose(el)})

    def visit(el: Tag, path: tuple):
        # Ancestors by tag only (themes change wrapper classes per page), parent and element with classes
        key = "/".join(t for t, _ in path[:-1]) + "/" + (path[-1][1] if path else "") + "/" + _sig(el)
        child_path = path + ((el.name, _sig(el)),)
        if el.name == "img":
            src = el.get("src") or el.get("data-src") or ""
            if src and not src.startswith("data:"):
                out.append({"key": key, "text": "", "src": src.split("?")[0], "alt": norm(el.get("alt", "")),
                            "tag": "img", "href": _href(el), "hint": None})
            return
        if el.name == "dd":
            dt = el.find_previous_sibling("dt")
            if dt:
                key += f"[{norm(dt.get_text(' ')).lower()[:40]}]"
        direct = norm(" ".join(s for s in el.find_all(string=True, recursive=False)))
        inline_parts = [c for c in el.children if isinstance(c, Tag) and norm(c.get_text(" "))]
        if el.find(BLOCK_TAGS) is None and (direct or len(inline_parts) < 2 or el.name in ("a", "h1", "h2", "h3", "h4", "p")):
            text = norm(el.get_text(" "))
            if text:
                leaf(el, key, text)
            for img in el.find_all("img"):
                visit(img, child_path)
            return
        # Separate inline parts ("<span>Author</span><span>12 May 2025</span>") are separate values
        if direct:
            leaf(el, key + "#text", direct)
        for child in el.children:
            if isinstance(child, Tag):
                visit(child, child_path)

    for child in root.children:
        if isinstance(child, Tag):
            visit(child, ())
    return out


class LinkIndex:
    """Which content type a link points to: exact crawled URL first, then a templated URL pattern."""

    def __init__(self, groups: list[dict], pages: dict, host: str, pattern_of=url_pattern):
        self.host = host
        self.pattern_of = pattern_of   # URL -> grouped pattern (nested slugs grouped)
        self.by_path, self.by_pattern = {}, {}
        for g in groups:
            if g["kind"] not in ("collection", "category"):
                continue
            for pid in g["members"]:
                if pid in pages:
                    self.by_path[self._path(pages[pid]["final_url"])] = g["name"]
            for pattern in g["patterns"]:
                if "{" in pattern.replace("{locale}", ""):
                    self.by_pattern[pattern] = g["name"]

    @staticmethod
    def _path(url: str) -> str:
        return urlparse(url).path.rstrip("/") or "/"

    def resolve(self, href: str | None, base: str) -> tuple[str, str | None, str]:
        """(kind, content type, pattern) with kind one of none / internal / external / mail / tel."""
        if not href or href.startswith("#") or href.startswith("javascript"):
            return "none", None, ""
        if href.startswith("mailto:"):
            return "mail", None, ""
        if href.startswith("tel:"):
            return "tel", None, ""
        url = urljoin(base, href)
        parsed = urlparse(url)
        if re.sub(r"^www\.", "", parsed.netloc) != re.sub(r"^www\.", "", self.host):
            return "external", None, ""
        path = self._path(url)
        # A query without key=value pairs ("?", "?print") has no parameter name to group on
        params = sorted(parse_qs(parsed.query, keep_blank_values=True))
        grouped = self.pattern_of(url)
        pattern = grouped + (f"?{params[0]}=" if params else "")
        return "internal", self.by_path.get(path) or self.by_pattern.get(grouped), pattern


def jsonld_values(page: dict, sitewide: set[str]) -> dict[str, str]:
    """Flat {property: value} of the page's JSON-LD, without site-wide objects (Organization, WebSite)."""
    out = {}

    def walk(node, key=""):
        if isinstance(node, list):
            for x in node:
                walk(x, key)
        elif isinstance(node, dict):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if any(t in sitewide for t in types if t):
                return
            for k, v in node.items():
                if k in JSONLD_SKIP and not (k in ("name", "headline", "description") and key):
                    continue
                walk(v, k if k not in ("name", "text", "value", "@value") or not key else key)
        elif isinstance(node, (str, int, float)) and key and key not in JSONLD_SKIP:
            out.setdefault(key, norm(str(node)))

    walk(page.get("jsonld") or [])
    return out


def _match_jsonld(values: list[tuple[int, str]], ld: list[dict]) -> str | None:
    props = Counter()
    for pi, text in values:
        t, iso = text.lower(), parse_date(text)
        for prop, v in ld[pi].items():
            v = v.lower()
            if t == v or (len(t) >= 4 and len(v) >= 4 and (t in v or v in t)) or (iso and v[:10] == iso):
                props[prop] += 1
    if props:
        prop, n = props.most_common(1)[0]
        if n / len(values) >= 0.5:
            return prop
    return None


def infer_type(occ: list[dict], links: LinkIndex, urls: list[str], n_pages: int) -> dict:
    texts = [o["text"] for o in occ if o["text"]]
    total = len(occ)
    if all(o["tag"] == "img" for o in occ):
        return {"type": "media"}
    resolved = [links.resolve(o["href"], urls[o["page"]]) for o in occ]
    kinds = Counter(r[0] for r in resolved)
    refs = Counter(r[1] for r in resolved if r[1])
    if refs and refs.most_common(1)[0][1] / total >= 0.6:
        return {"type": "reference", "ref": refs.most_common(1)[0][0]}
    if texts and kinds["tel"] / total < 0.6 and (sum(1 for o in occ if o.get("datetime")) / total >= 0.5
                                                  or sum(bool(parse_date(t)) for t in texts) / len(texts) >= 0.8):
        return {"type": "date"}
    if texts and sum(bool(ISBN.match(t)) and len(re.sub(r"\D", "", t)) in (10, 13) for t in texts) / len(texts) >= 0.8:
        return {"type": "text (ISBN)", "name_hint": "isbn"}
    if kinds["mail"] / total >= 0.6 or (texts and sum(bool(EMAIL.match(t)) for t in texts) / len(texts) >= 0.8):
        return {"type": "email"}
    if kinds["tel"] / total >= 0.6 or (texts and sum(bool(PHONE.match(t)) for t in texts) / len(texts) >= 0.8):
        return {"type": "phone"}
    patterns = Counter(r[2] for r in resolved if r[0] == "internal")
    if patterns and kinds["internal"] / total >= 0.8:
        pattern, n = patterns.most_common(1)[0]
        if n / total >= 0.8 and ("{" in pattern.replace("{locale}", "") or "?" in pattern):
            return {"type": "taxonomy", "taxonomy_pattern": pattern}
    if texts:
        if sum(1 for o in occ if o.get("datetime")) / total >= 0.5 or sum(bool(parse_date(t)) for t in texts) / len(texts) >= 0.8:
            return {"type": "date"}
        if sum(bool(READING_TIME.match(t)) for t in texts) / len(texts) >= 0.8:
            return {"type": "number", "unit": "minutes", "name_hint": "reading_time"}
        if sum(bool(PRICE.search(t)) for t in texts) / len(texts) >= 0.8:
            return {"type": "number (price)", "name_hint": "price"}
        if sum(bool(NUMBER.match(t)) for t in texts) / len(texts) >= 0.8:
            return {"type": "number"}
    if kinds["external"] / total >= 0.8:
        return {"type": "link (url)"}
    distinct = Counter(texts)
    if n_pages >= 5 and len(texts) >= 5 and 2 <= len(distinct) <= 12 and len(distinct) <= 0.5 * len(texts) \
            and max(map(words, distinct)) <= 4:
        return {"type": "enum", "values": [v for v, _ in distinct.most_common(12)]}
    if kinds["internal"] / total >= 0.8:
        return {"type": "link"}
    median = statistics.median(map(words, texts)) if texts else 0
    return {"type": "text" if median <= 15 else "long text"}


def block_texts(page: dict) -> list[str]:
    """Texts of the blocks that are not the page's own header or body: their content belongs to those blocks."""
    out, skipped = [], set()
    for b in main_blocks(page):
        if b["type"] in ("Table", "Specs", "ContactDetails"):
            continue                      # data blocks: their values are the entry's own fields
        if b["type"] in ("PageHeader", "Hero", "RichText") and b["type"] not in skipped and \
                (b["type"] == "RichText" or not out):
            skipped.add(b["type"])
            if b["type"] != "RichText":
                skipped.update(("PageHeader", "Hero"))
            continue
        out.append(norm(f"{b.get('heading') or ''} {b.get('label') or ''} {b.get('text') or ''}").lower())
    return out


def relabel_pairs(leaves: list[list[dict]]):
    """Label/value rows that share one DOM path (spec tables, <li><span>ISBN</span><span>978…</span>) get a key per label.

    A label is a short text that recurs on at least half of the pages; the leaf right after it, with a different tag
    path, is its value. Values that continue a list after it keep the same key."""
    label_pages = Counter()
    for page_leaves in leaves:
        label_pages.update({lf["text"] for lf in page_leaves if lf["text"] and words(lf["text"]) <= 5 and not lf["href"]})
    is_label = lambda t: is_label_text(t) and label_pages[t] / len(leaves) >= 0.5
    for page_leaves in leaves:
        prev = None                          # (label text or None, original key, new key)
        for lf in page_leaves:
            if lf["tag"] == "img" or lf["href"]:
                continue                     # links are cards or buttons, not label/value rows
            orig = lf["key"]
            if prev and orig == prev[1] and prev[2] != prev[1]:
                lf["key"] = prev[2]
                value = True
            elif prev and prev[0] and orig.rsplit("/", 1)[-1] != prev[1].rsplit("/", 1)[-1] and "[" not in orig:
                lf["key"] = f"{orig}[{prev[0].lower()[:40]}]"
                value = True
            else:
                value = False
            prev = (lf["text"] if not value and is_label(lf["text"]) else None, orig, lf["key"])


def diff_fields(pages: list[dict], links: LinkIndex, sitewide: set[str]) -> tuple[list[dict], dict]:
    """Fields found by aligning the DOM of several pages of one type."""
    leaves, urls, ld, titles, zones = [], [], [], [], []
    for p in pages:
        root = page_root(p)
        if root is None:
            continue
        leaves.append(extract_leaves(root))
        zones.append(block_texts(p))
        urls.append(p["final_url"])
        ld.append(jsonld_values(p, sitewide))
        h1 = root.find("h1")
        titles.append(norm(h1.get_text(" ")) if h1 else norm(p.get("title", "")))
    n = len(leaves)
    stats = {"pages": n, "keys": 0, "template_texts": 0, "fields": 0}
    if n < MIN_DIFF_PAGES:
        return [], stats

    relabel_pairs(leaves)
    by_key: dict[str, dict[int, list]] = defaultdict(lambda: defaultdict(list))
    for pi, page_leaves in enumerate(leaves):
        for pos, lf in enumerate(page_leaves):
            by_key[lf["key"]][pi].append({**lf, "page": pi, "pos": pos})
    # The same number of values on every page (address line 1 and 2): positional fields, not a list
    for key in list(by_key):
        occ = by_key[key]
        counts = {len(lst) for lst in occ.values()}
        if len(occ) >= MIN_DIFF_PAGES and len(counts) == 1 and 2 <= min(counts) <= 4 \
                and not any(o["href"] or o["tag"] == "img" for lst in occ.values() for o in lst):
            for pi, lst in occ.items():
                for i, o in enumerate(lst, 1):
                    o["key"] = leaves[pi][o["pos"]]["key"] = f"{key}#{i}"
                    by_key[o["key"]][pi].append(o)
            del by_key[key]
    stats["keys"] = len(by_key)

    constant = set()
    for key, occ in by_key.items():
        signature = Counter(tuple((o["text"] or o.get("src", "")).lower() for o in lst) for lst in occ.values())
        if len(occ) >= 2 and signature.most_common(1)[0][1] / len(occ) >= LABEL_SHARE:
            constant.add(key)
    stats["template_texts"] = len(constant)
    # Running text (prose containers or long paragraphs that vary): the type has a rich-text body
    with_body = sum(1 for page_leaves in leaves
                    if sum(words(lf["text"]) for lf in page_leaves if lf["key"] not in constant
                           and (lf.get("prose") or words(lf["text"]) > 30)) >= 40)
    stats["body_share"] = round(with_body / n, 2)

    # The same linked entries on most pages: a list filled by a query (latest, featured), not by editors
    automatic = {}
    for key in constant:
        flat = [o for lst in by_key[key].values() for o in lst if o["href"]]
        refs = Counter(links.resolve(o["href"], urls[o["page"]])[1] for o in flat)
        refs.pop(None, None)
        if refs and len(by_key[key]) / n >= FIELD_SHARE and refs.most_common(1)[0][1] / len(flat) >= 0.6:
            ref = refs.most_common(1)[0][0]
            first = next(iter(by_key[key].values()))[0]
            pos, label = first["pos"] - 1, None
            page_leaves = leaves[first["page"]]
            while pos >= 0 and page_leaves[pos]["key"] in constant and page_leaves[pos]["href"]:
                pos -= 1
            if pos >= 0 and page_leaves[pos]["key"] in constant and words(page_leaves[pos]["text"]) <= 6 \
                    and is_label_text(page_leaves[pos]["text"]):
                label = page_leaves[pos]["text"]
            entry = automatic.setdefault(ref, {"to": ref, "label": label, "share": 0, "entries": 0})
            entry["share"] = max(entry["share"], round(len(by_key[key]) / n, 2))
            entry["entries"] = max(entry["entries"], len(set(o["href"] for o in by_key[key][first["page"]])))
            entry["label"] = entry["label"] or label
    stats["automatic_lists"] = list(automatic.values())

    candidates = []
    for key, occ in by_key.items():
        share = len(occ) / n
        if share < FIELD_SHARE:
            continue
        flat = [o for lst in occ.values() for o in lst]
        if key in constant:
            # Same button text, different target on every page ("Visit website"): a link field
            # Directions and share buttons are generated from the address or the page URL
            flat = [o for o in flat if not (o["href"] and (MAPS.search(o["href"]) or SHARE.search(o["href"])))]
            hrefs = [o["href"] for o in flat if o["href"]]
            if flat and len(hrefs) >= 0.8 * len(flat) and len(set(hrefs)) >= 0.5 * len(occ) and flat[0]["text"] \
                    and words(flat[0]["text"]) <= 5 and is_label_text(flat[0]["text"]):
                typed = infer_type(flat, links, urls, n)
                if typed["type"] in ("link", "link (url)", "email", "phone"):
                    candidates.append({"key": key, "occ": flat, "share": share, "per_page": 1, "typed": typed,
                                       "label": flat[0]["text"], "jsonld": None, "hint": None, "tag": flat[0]["tag"], "button": True})
            continue
        per_page = statistics.median(len(lst) for lst in occ.values())
        texts = [o["text"] for o in flat if o["text"]]
        tag = Counter(o["tag"] for o in flat).most_common(1)[0][0]
        median_words = statistics.median(map(words, texts)) if texts else 0
        if tag == "h1" or (texts and sum(t == titles[o["page"]] for o, t in zip(flat, texts)) / len(texts) >= 0.6):
            continue                      # the page title: a field of the header slot already
        if median_words > 30 or (tag in ("p", "li", "blockquote", "td") and median_words > 12):
            continue                      # running text: covered by rich text / blocks
        if tag == "img" and per_page > 1:
            continue                      # galleries and card images: covered by blocks
        if sum(1 for o in flat if o["href"] and SHARE.search(o["href"])) / len(flat) >= 0.5:
            continue                      # share buttons: generated from the page URL
        typed = infer_type(flat, links, urls, n)
        if typed["type"] not in ("reference", "taxonomy", "date") and sum(o.get("prose", False) for o in flat) / len(flat) >= 0.5:
            continue                      # part of the rich-text body
        if typed["type"] not in ("reference", "taxonomy") and texts:
            inside = sum(1 for o in flat if o["text"] and any(o["text"].lower() in z for z in zones[o["page"]]))
            if inside / len(texts) >= 0.5:
                continue                  # text of a CTA, card grid or other block: a block field, not a type field
            if sum(t.endswith(":") for t in texts) / len(texts) >= 0.5:
                continue                  # inline labels in running text
        if texts and any(ch.isdigit() for ch in texts[0]) and typed["type"] in ("text", "enum") and \
                len({re.sub(r"\d+", "#", t) for t in texts}) <= 0.3 * len(texts):
            continue                      # generated counters ("Product images (3)")
        sep = next((x for x in (" | ", " · ", " • ", " / ") if texts and sum(x in t for t in texts) / len(texts) >= 0.7), None)
        if sep and typed["type"] in ("text", "long text", "enum"):
            parts = [x.strip() for t in texts for x in t.split(sep) if x.strip()]
            typed = {"type": "taxonomy" if len(set(parts)) <= 0.6 * len(parts) else "text",
                     "values": [v for v, _ in Counter(parts).most_common(40)], "separator": sep}
            per_page = max(per_page, 2)
        if per_page > 1 and typed["type"] in ("enum", "taxonomy") and not typed.get("separator"):
            on_pages = Counter(t.lower() for lst in occ.values() for t in {o["text"] for o in lst if o["text"]})
            if on_pages and statistics.mean(c / len(occ) for c in on_pages.values()) >= 0.5:
                continue                  # optional section titles ("Materials", "Colors"), not terms
        if per_page > 1 and typed["type"] == "enum" and sum(1 for o in flat if o["href"]) / len(flat) >= 0.5:
            continue                      # a row of buttons (download, find a dealer), not terms
        if per_page > 1 and typed["type"] == "enum":
            typed = {"type": "taxonomy", "values": [v for v, _ in Counter(texts).most_common(40)]}
        if per_page > 1 and not typed.get("separator"):
            short = texts and max(map(words, texts)) <= 6 and per_page <= 15
            if typed["type"] != "reference" and not (short and typed["type"] in ("taxonomy", "text", "enum", "link")):
                continue                  # card lists and repeated paragraphs: blocks
        # Label: the constant text right before the first occurrence on each page
        labels = Counter()
        for pi, lst in occ.items():
            pos = lst[0]["pos"] - 1
            while pos >= 0 and (leaves[pi][pos]["tag"] == "img" or leaves[pi][pos]["key"] == key):
                pos -= 1
            if pos >= 0 and leaves[pi][pos]["key"] in constant and 0 < words(leaves[pi][pos]["text"]) <= 6 \
                    and not leaves[pi][pos]["href"] and is_label_text(leaves[pi][pos]["text"]):   # a button is not a label
                labels[leaves[pi][pos]["text"].rstrip(":")] += 1
        if "[" in key and key.endswith("]"):
            suffix = key.rsplit("[", 1)[1][:-1]
            if is_label_text(suffix):
                labels[suffix] += len(occ)
        label = labels.most_common(1)[0][0] if labels and labels.most_common(1)[0][1] / len(occ) >= 0.5 else None
        prefix = _common_prefix(texts) if typed["type"] in ("text", "enum") else None
        prefix = prefix if prefix and not UI_WORDS.search(prefix) else None
        if prefix:
            label = label or prefix
            for o in flat:
                o["text"] = o["text"][len(prefix):].strip(" ,:") if o["text"].startswith(prefix) else o["text"]
            typed = infer_type(flat, links, urls, n)
        prop = _match_jsonld([(o["page"], o["text"]) for o in flat if o["text"]], ld) if texts else None
        hint = Counter(o.get("hint") for o in flat if o.get("hint")).most_common(1)
        candidates.append({"key": key, "occ": flat, "share": share, "per_page": per_page, "typed": typed,
                           "label": label, "jsonld": prop, "hint": hint[0][0] if hint else None, "tag": tag, "prefix": prefix})

    fields, taken, by_ref = [], Counter(), {}
    for c in sorted(candidates, key=lambda c: -c["share"]):
        name, how, named = _name(c)
        typed = c["typed"]
        if typed["type"] == "reference":
            name = name if how in ("label", "json-ld") else f"related_{plural(typed['ref'])}"
            if typed["ref"] in by_ref:    # title, image and button of one card list point to the same entries
                if how == "label" and by_ref[typed["ref"]]["name"].startswith("related_"):
                    by_ref[typed["ref"]]["name"] = name
                continue
        elif c["per_page"] > 1 and not named and typed["type"] in ("text", "link"):
            continue                      # an unnamed list of short texts: card or list content
        taken[name] += 1
        if taken[name] > 1:
            if typed["type"] == "reference" or how in ("type", "json-ld", "itemprop"):
                continue                  # same field reached through another DOM path (card image + title)
            name = f"{name}_{taken[name]}"
        is_list = c["per_page"] > 1
        ftype = {"reference": f"reference({typed.get('ref')})", "taxonomy": "taxonomy"}.get(typed["type"], typed["type"])
        examples = list(dict.fromkeys((o["href"] if c.get("button") else o["text"]) or o.get("alt") or o.get("src", "")
                                      for o in c["occ"]))[:3]
        ev = [f"varies on {c['share']:.0%} of {n} pages"]
        if how != "type":
            ev.append(f"named from {how}" + (f" '{c['label']}'" if how == "label" else f" '{c['jsonld']}'" if how == "json-ld" else ""))
        if typed.get("taxonomy_pattern"):
            ev.append(f"links to {typed['taxonomy_pattern']}")
        if c.get("prefix"):
            ev.append(f"shown after the fixed text '{c['prefix']}'")
        if c.get("button"):
            ev.append(f"same link text '{c['label']}', different target per page")
        if typed.get("separator"):
            ev.append(f"values separated by '{typed['separator'].strip()}'")
        value_set = {o["text"].lower() for o in c["occ"] if o["text"]}
        twin = next((f for f in fields if f.get("_values") and value_set and not f.get("ref") and typed["type"] != "reference" and
                     len(value_set & f["_values"]) / len(value_set | f["_values"]) >= 0.8), None)
        if twin:                          # the same values under two DOM paths: keep the better name
            if named and twin["confidence"] != "high":
                fields.remove(twin)
            else:
                continue
        f = field(name, ftype, c["share"], n, evidence="; ".join(ev), named=named, examples=examples, list_=is_list,
                  ref=typed.get("ref"), values=typed.get("values") if typed["type"] == "enum" else None, source="dom diff")
        if how == "type" and typed["type"] in ("enum", "text", "long text"):
            f["confidence"] = "low"       # no label or other hint: the name is a guess
        f["_values"] = value_set
        if typed["type"] == "taxonomy":
            f["values"] = typed.get("values") or list(dict.fromkeys(o["text"] for o in c["occ"] if o["text"]))[:40]
        if typed.get("taxonomy_pattern"):
            f["taxonomy_pattern"] = typed["taxonomy_pattern"]
        if typed["type"] == "reference":
            f["review"] = "Picked by an editor or filled automatically (same category, latest)? Not visible in the HTML."
            by_ref[typed["ref"]] = f
        fields.append(f)
        if len(fields) >= MAX_DIFF_FIELDS:
            break

    # Structured data that is never visible
    visible = {c["jsonld"] for c in candidates if c["jsonld"]}
    props = Counter(k for page_ld in ld for k in page_ld)
    for prop, count in props.most_common():
        if count / n < 0.5 or prop in visible or prop in ("image", "thumbnailUrl", "contentUrl", "width", "height"):
            continue
        values = [page_ld[prop] for page_ld in ld if prop in page_ld]
        if len(set(values)) < 2:
            continue
        ftype = "date" if all(parse_date(v) for v in values[:5]) else \
            "number (price)" if prop in ("price", "lowPrice", "highPrice") or all(PRICE.search(v) for v in values[:5]) else "text"
        enum = None
        if all(v.startswith(("http://schema.org/", "https://schema.org/")) for v in values):
            enum = sorted({v.rsplit("/", 1)[-1] for v in values})
            ftype = "enum"
        squash = lambda t: re.sub(r"\s+", "", t).lower()
        seen = sum(1 for pi, page_ld in enumerate(ld) if prop in page_ld and len(page_ld[prop]) >= 3
                   and squash(page_ld[prop]) in squash(" ".join(lf["text"] for lf in leaves[pi])))
        where = "visible inside another text on the page" if seen / count >= 0.5 else "only in JSON-LD, not visible on the page"
        fields.append(field(camel_to_snake(prop), ftype, count / n, n, evidence=f"JSON-LD '{prop}', {where}",
                            named=True, examples=values, source="json-ld", values=enum))
    for f in fields:
        f.pop("_values", None)
    stats["fields"] = len(fields)
    return fields, stats


SHARE = re.compile(r"sharer|/share\b|share\?|shareArticle|intent/tweet|twitter\.com/home\?status|addtoany|pinterest\.com/pin/create|whatsapp://|"
                   r"api\.whatsapp|mailto:\?|mailto:[^@]*\?subject", re.I)
LETTERS = re.compile(r"[^\W\d_]{2}")
LOCALE_CODE = re.compile(r"^[a-z]{2}([-_][a-z]{2})?$", re.I)
UI_WORDS = re.compile(r"\b(scroll|screenshot|menu|close|sluit|fermer|open|toggle|next|previous|prev|vorige|volgende|back|terug|"
                      r"play|pause|cookie|load more|show more|meer laden|zoom|print|share|delen|footer|header|skip)\b", re.I)


def is_label_text(text: str) -> bool:
    """Could this text be a field label? Needs real words, and not a language code or a UI control."""
    return bool(text) and bool(LETTERS.search(text)) and not LOCALE_CODE.match(text.strip()) and not UI_WORDS.search(text) \
        and text.strip()[:1].isalpha() and not re.search(r"\d(px|rem|em|vh|vw)\b", text)   # not CSS leftovers like "1.2rem"
MAPS = re.compile(r"google\.[a-z.]+/maps|maps\.google|maps\.apple|goo\.gl/maps|openstreetmap", re.I)
LOCATION = re.compile(r"^[^,\d]{2,40}, [^,\d]{2,30}$")
STREET = re.compile(r"^[^\d,]{3,40} \d+[a-zA-Z]?(\s?[-/]\s?\d+)?$|^\d+[a-zA-Z]? [^\d,]{3,40}$")


def _common_prefix(texts: list[str]) -> str | None:
    """A fixed lead-in shared by most values ("© Design by Dirk Wynants" → "© Design by")."""
    if len(texts) < MIN_DIFF_PAGES:
        return None
    first = Counter(" ".join(t.split()[:2]) for t in texts if words(t) > 2).most_common(1)
    if not first or first[0][1] / len(texts) < 0.7:
        return None
    prefix = first[0][0]
    for k in range(3, 6):
        if prefix.split()[-1].lower().rstrip(":") in ("by", "door", "par", "von", "van", "de", "from") or prefix.endswith(":"):
            break                         # the lead-in is complete: what follows is the value
        longer = Counter(" ".join(t.split()[:k]) for t in texts if words(t) > k).most_common(1)
        if longer and longer[0][1] / len(texts) >= 0.95 and longer[0][0].startswith(prefix):
            prefix = longer[0][0]
        else:
            break
    rest = {t[len(prefix):].strip() for t in texts if t.startswith(prefix)}
    return prefix if len(rest) >= 2 else None


def _value_name(texts: list[str]) -> str | None:
    if not texts:
        return None
    if sum(bool(LOCATION.match(t)) for t in texts) / len(texts) >= 0.6 and \
            len({t.rsplit(", ", 1)[-1] for t in texts if ", " in t}) <= 0.6 * len(texts):
        return "location"
    if sum(bool(STREET.match(t)) for t in texts) / len(texts) >= 0.6:
        return "street"
    return None


def _name(c: dict) -> tuple[str, str, bool]:
    """(name, where it came from, whether it is a meaningful name)."""
    if c["jsonld"]:
        return camel_to_snake(c["jsonld"]), "json-ld", True
    if c["hint"] and c["hint"].startswith("itemprop:"):
        return camel_to_snake(c["hint"].split(":", 1)[1]), "itemprop", True
    by_value = _value_name([o["text"] for o in c["occ"] if o["text"]]) if c["typed"]["type"] in ("text", "enum") else None
    if c["label"]:
        name, known = label_name(c["label"])
        if known or not by_value:
            return name, "label", known or len(name) > 2
    if c["hint"]:
        return c["hint"].split(":", 1)[1], "class", True
    t = c["typed"]
    if by_value:
        return by_value, "value", True
    if t.get("name_hint"):
        return t["name_hint"], "value", True
    by_type = {"date": "date", "media": "image", "email": "email", "phone": "phone", "taxonomy": "category",
               "enum": "type", "link (url)": "website", "link": "link"}
    if t["type"] in by_type:
        return by_type[t["type"]], "type", t["type"] in ("date", "email", "phone")
    return "text", "type", False


# ---------------------------------------------------------------------------
# 4. Shared parts: block library, SEO, globals, locales
# ---------------------------------------------------------------------------

def _merge_specs(specs: dict[str, float]) -> list[dict]:
    """'items: list of {image: media, title: text}' variants → one items field with sub-fields and their shares."""
    merged: dict[str, dict] = {}
    for spec, share in specs.items():
        name, _, ftype = spec.partition(": ")
        m = re.match(r"list of \{(.*)\}$", ftype)
        entry = merged.setdefault(name, {"name": name, "type": ftype, "share": 0.0, "sub": Counter()})
        entry["share"] = round(min(1.0, entry["share"] + share), 2)
        if m and ":" in m[1]:
            entry["type"] = "list"
            for sub in m[1].split(", "):
                entry["sub"][sub] += share
    out = []
    for e in merged.values():
        if e["sub"]:
            subs = ", ".join(s for s, _ in sorted(e["sub"].items(), key=lambda kv: -kv[1]))
            e["type"] = f"list of {{{subs}}}"
        out.append({"name": e["name"], "type": e["type"], "share": e["share"]})
    return out


def add_taxonomy(taxonomies: list[dict], f: dict, type_name: str):
    """Taxonomy fields of different types with overlapping values (or one URL pattern) are one shared taxonomy."""
    values = {v.lower() for v in f.get("values") or f["examples"]}
    for tx in taxonomies:
        known = {v.lower() for v in tx["values"]}
        if (f.get("taxonomy_pattern") and f.get("taxonomy_pattern") == tx["pattern"]) or \
                (values and len(values & known) / len(values | known) >= 0.3):
            break
    else:
        tx = {"name": f["name"], "used_by": set(), "values": Counter(), "pattern": f.get("taxonomy_pattern")}
        taxonomies.append(tx)
    if f["name"] in ("tags", "category", "categories") and tx["name"] not in ("tags", "category"):
        tx["name"] = f["name"]
    tx["used_by"].add(f"{type_name}.{f['name']}")
    tx["values"].update(f.get("values") or f["examples"])
    f["taxonomy"] = tx["name"]


def listing_filters(page: dict) -> list[dict]:
    """Filter controls on a listing page (selects, checkbox and radio groups): candidate taxonomies."""
    path = page["dir"] / "clean.html"
    if not path.exists():
        return []
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    root = soup.find("main") or soup.body or soup
    groups = []
    for sel in root.find_all("select"):
        label = sel.get("aria-label") or (root.find("label", attrs={"for": sel.get("id")}) if sel.get("id") else None)
        label = label.get_text(" ", strip=True) if isinstance(label, Tag) else label or sel.get("name") or "filter"
        values = [norm(o.get_text(" ")) for o in sel.find_all("option") if o.get("value") not in (None, "", "*", "all")]
        groups.append({"name": label, "values": [v for v in values if v]})
    by_name = defaultdict(list)
    for inp in root.find_all("input", attrs={"type": re.compile("checkbox|radio")}):
        lab = root.find("label", attrs={"for": inp.get("id")}) if inp.get("id") else inp.find_parent("label")
        text = norm(lab.get_text(" ")) if lab else inp.get("value", "")
        fieldset = inp.find_parent("fieldset")
        legend = fieldset.find("legend") if fieldset else None
        by_name[norm(legend.get_text(" ")) if legend else inp.get("name") or "filter"].append(text)
    groups += [{"name": k, "values": [v for v in vals if v]} for k, vals in by_name.items()]
    return [g for g in groups if len(g["values"]) >= 2 and words(g["name"]) <= 5]


def block_library(catalogue: dict, type_of_page_type: dict[str, str], listings: list[dict]) -> list[dict]:
    blocks = []
    for ctype, c in catalogue.items():
        if ctype in GENERATED or ctype == "Unknown":
            continue
        options = defaultdict(set)
        for label in c["variants"]:
            for kv in label.split(", "):
                k, _, v = kv.partition("=")
                if k in STYLE_OPTIONS and v:
                    options[k].add(v)
        used_by = Counter()
        for pt, n in c["page_types"].items():
            used_by[type_of_page_type.get(pt, pt)] += n
        blocks.append({"name": ctype, "description": c["description"], "instances": c["count"], "pages": c["pages"],
                       "fields": [f for f in _merge_specs(c["fields"]) if f["share"] >= 0.1 or len(c["fields"]) == 1],
                       "options": {k: sorted(v) for k, v in options.items()}, "used_by": dict(used_by.most_common()),
                       "confidence": "high" if c["avg_confidence"] >= 0.8 else "medium" if c["avg_confidence"] >= 0.65 else "low"})
    if listings:
        targets = sorted({l["lists"] for l in listings if l["lists"]})
        blocks.append({"name": "CollectionList", "description": "Query-driven list of entries of one content type, "
                       "on listing pages (rendered as FilterableList or CardGrid).", "instances": len(listings),
                       "pages": len(listings), "fields": [
                           {"name": "content_type", "type": f"enum({', '.join(targets) or 'collection types'})", "share": 1.0},
                           {"name": "filters", "type": "list of taxonomy", "share": round(sum(l["filters"] for l in listings) / len(listings), 2)},
                           {"name": "limit", "type": "number", "share": 1.0}],
                       "options": {}, "used_by": {"Page": len(listings)}, "confidence": "medium"})
    return sorted(blocks, key=lambda b: -b["instances"])


def seo_component(pages: list[dict]) -> dict:
    n = len(pages) or 1
    titles = [p.get("title") or "" for p in pages]
    suffixes = Counter(re.split(r"\s[|–—-]\s", t)[-1] for t in titles if re.search(r"\s[|–—-]\s", t))
    suffix, sn = suffixes.most_common(1)[0] if suffixes else ("", 0)
    share = lambda f: sum(1 for p in pages if f(p)) / n
    fields = [
        {"name": "meta_title", "type": "text", "share": round(share(lambda p: p.get("title")), 2)},
        {"name": "meta_description", "type": "text", "share": round(share(lambda p: p.get("description")), 2)},
        {"name": "og_image", "type": "media", "share": round(share(lambda p: (p.get("og") or {}).get("og:image")), 2)},
        {"name": "canonical_url", "type": "url", "share": round(share(lambda p: p.get("canonical")), 2)},
        {"name": "no_index", "type": "boolean", "share": round(share(lambda p: "noindex" in (p.get("robots") or "").lower()), 2)},
    ]
    notes = []
    if sn / n >= 0.6:
        notes.append(f"Titles end with '{suffix}' on {sn / n:.0%} of the pages: added by the template, store the title without it.")
    if len(set(p.get("description") for p in pages if p.get("description"))) < share(lambda p: p.get("description")) * n * 0.8:
        notes.append("Many pages share the same meta description: probably a fallback, not edited per page.")
    return {"name": "Seo", "fields": fields, "notes": notes}


SOCIAL = re.compile(r"facebook|instagram|linkedin|twitter|x\.com|youtube|tiktok|pinterest|vimeo", re.I)


def globals_from(navigation: dict) -> list[dict]:
    def count_groups(nodes):
        return sum(1 for n in nodes if n.get("kind") == "group")

    out = []
    for region, name in (("header", "MainNavigation"), ("footer", "FooterNavigation")):
        nav = navigation[region]
        if not nav["links"]:
            continue
        out.append({"name": name, "kind": "global", "evidence": f"{nav['links']} links, {nav['depth']} levels, same on "
                    f"{nav['pages_with_main_variant']} pages", "fields": [
                        {"name": "items", "type": "list of {label: text, link: link or reference(Page), children: list}"
                         if nav["depth"] > 1 else "list of {label: text, link: link or reference(Page)}", "share": 1.0}],
                    "groups": count_groups(nav["tree"])})
    social = [l for l in navigation["header"]["external"] + navigation["footer"]["external"] if SOCIAL.search(l["href"])]
    fields = [{"name": "logo", "type": "media", "share": 1.0}, {"name": "site_name", "type": "text", "share": 1.0}]
    if social:
        fields.append({"name": "social_links", "type": "list of {network: enum, url: link}", "share": 1.0})
    out.append({"name": "SiteSettings", "kind": "global", "evidence": "logo and site-wide links found in header and footer"
                + (f"; {len(social)} social links" if social else ""), "fields": fields})
    return out


def localisation(group_pages: list[dict]) -> dict:
    with_alts = [p for p in group_pages if any(a["lang"].lower() != "x-default" and a["href"].rstrip("/") != p["final_url"].rstrip("/")
                                               for a in p.get("hreflang") or [])]
    slug_changes = 0
    for p in with_alts:
        own = urlparse(p["final_url"]).path.rstrip("/").split("/")[-1]
        others = [urlparse(a["href"]).path.rstrip("/").split("/")[-1] for a in p["hreflang"]
                  if a["href"].rstrip("/") != p["final_url"].rstrip("/")]
        slug_changes += any(o != own for o in others)
    share = len(with_alts) / (len(group_pages) or 1)
    return {"translated": share >= 0.5, "share": round(share, 2),
            "slug_translated": bool(with_alts) and slug_changes / len(with_alts) >= 0.5}


# ---------------------------------------------------------------------------
# 5. Coverage
# ---------------------------------------------------------------------------

def coverage(page: dict) -> tuple[int, int, list[str]]:
    """(covered words, total words, uncovered fragments) of the page's main content."""
    raw_file = page["dir"] / "blocks.json"
    if not raw_file.exists() or not page.get("blocks"):
        return 0, 0, []
    raw = json.loads(raw_file.read_text(encoding="utf-8"))["blocks"]
    starts = sorted((b["index"], b["type"]) for b in page["blocks"])
    covered = total = 0
    for j, rb in enumerate(raw):
        if rb["region"] != "main":
            continue
        owner = next((t for i, t in reversed(starts) if i <= j), "Unknown")
        total += rb["words"]
        covered += rb["words"] if owner != "Unknown" and owner not in GENERATED else 0
    fragments = [b["text"][:100] for b in page["blocks"] if b["type"] == "Unknown" and b["region"] == "main" and b["text"].strip()]
    return covered, total, fragments


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build(d: Path, sample: int) -> dict:
    data = load_inputs(d)
    pages, page_types = data["pages"], data["page_types"]
    host = urlparse(next(iter(pages.values()))["final_url"]).netloc if pages else d.name
    all_pages = list(pages.values())
    navigation = analyse_navigation(all_pages, host)
    locales = analyse_locales(d, all_pages, navigation)
    groups = group_types(page_types, pages)
    links = LinkIndex(groups, pages, host, pattern_groups(d).of_url)
    # Site-wide schema types (Organization, WebSite, a FAQPage on every page) occur in most page types
    spread = Counter(s for t in page_types for s in {x for m in t["members"] if m["id"] in pages
                                                      for x in pages[m["id"]].get("schema_types") or []})
    sitewide = {s for s, c in spread.items() if len(page_types) >= 3 and c / len(page_types) >= 0.6}
    type_of_page_type = {pt: g["name"] for g in groups for pt in g["page_types"]}
    url_tree = pattern_groups(d).tree()
    site_wide = find_site_wide(all_pages)
    SITE_WIDE.clear()
    SITE_WIDE.update(site_wide)
    # Parents are entries of a content type; a generic Page one level up (a listing) is navigation, not a parent
    type_of_path = {urlparse(pages[pid]["final_url"]).path.rstrip("/"): g["name"] for g in groups if g["kind"] != "page"
                    for pid in g["members"] if pid in pages}

    content_types, relations, taxonomies, listings, filter_chips, category_of = [], [], [], [], [], {}
    for g in groups:
        members = [pages[i] for i in g["members"] if i in pages]
        if not members:
            continue
        s = structure(members, g["kind"])
        step = max(1, len(members) // sample) if sample else 1
        diffed = members[::step][:sample] if sample else members
        # Single types are separate pages (ContactPage1, 2, 3), not one template: comparing them finds noise
        dom_fields, diff_stats = diff_fields(diffed, links, sitewide) if g["kind"] in ("collection", "category") else ([], {"pages": 0})

        fields = []
        if g["kind"] == "collection":
            pattern = g["patterns"].most_common(1)[0][0]
            fields.append(field("slug", "slug", 1.0, len(members), evidence=f"URL pattern {pattern}", source="url"))
            if "{id}" in pattern:
                fields.append(field("external_id", "number", 1.0, len(members), evidence="numeric ID in the URL", source="url"))
        elif g["kind"] == "page":
            fields.append(field("slug", "slug (path)", 1.0, len(members), evidence="pages at their own path", source="url"))
        elif g["kind"] == "category":
            depths = Counter(len(urlparse(p["final_url"]).path.strip("/").split("/")) for p in members)
            fields.append(field("slug", "slug", 1.0, len(members), evidence="each category has its own path", source="url"))
            if len(depths) > 1:
                fields.append(field("parent", "reference(Category)", sum(c for d_, c in depths.items() if d_ > min(depths)) / len(members),
                                    len(members), evidence="URLs nest " + ", ".join(f"{c} pages at depth {d_}" for d_, c in sorted(depths.items()))
                                    + ": a category tree", source="url"))
        names = {f["name"] for f in fields}
        # Nested URLs (/collections/{group}/{slug}): the page one level up is the entry's parent
        if any(re.search(r"\{(group|subgroup\d*)\}", p) for p in g["patterns"]):
            parents = Counter(type_of_path.get(urlparse(p["final_url"]).path.rstrip("/").rsplit("/", 1)[0]) for p in members)
            nested_pages = sum(1 for p in members if re.search(r"\{(group|subgroup\d*)\}", p.get("pattern") or ""))
            parents.pop(None, None)
            if parents and nested_pages:
                parent, n = parents.most_common(1)[0]
                if n / nested_pages >= 0.5 and "parent" not in names:
                    name = "parent" if parent == g["name"] else camel_to_snake(parent)
                    pattern = next(p for p in g["patterns"] if "{group}" in p or "{subgroup" in p)
                    fields.append(field(name, f"reference({parent})", n / len(members), len(members), named=True, ref=parent,
                                        source="url", evidence=f"nested URL {pattern}: the page one level up is a {parent} "
                                        f"({n} of {nested_pages} nested pages)"))
                    names.add(name)
        if g["kind"] != "page" and not any(f["name"] == "title" for f in s["fields"]):
            fields.append(field("title", "text", 1.0, len(members), evidence="h1 / page title (the header is not in a fixed slot)",
                                named=len(members) >= MIN_DIFF_PAGES, source="page"))
            names.add("title")
        for f in s["fields"] + dom_fields:
            if f["name"] in names:
                continue
            names.add(f["name"])
            fields.append(f)
        # A fixed Table/Specs slot whose rows became named fields: the fields replace the component
        label_fields = [f for f in dom_fields if "named from label" in f["evidence"] or "json-ld" in f["evidence"]]
        if len(label_fields) >= 2:
            fields = [f for f in fields if f["type"] not in ("component: Table", "component: Specs")]
        if s["tail"] and s["tail"][-1] == "CardGrid" and any(f["type"].startswith("reference(") and f["list"] for f in dom_fields):
            fields = [f for f in fields if f["type"] != "component: CardGrid"]   # the related list is the reference field

        if g["kind"] == "category":
            targets = Counter()
            for p in diffed[:20]:
                root = page_root(p)
                hrefs = {a.get("href") for a in root.find_all("a")} if root else set()
                found = Counter(links.resolve(h, p["final_url"])[1] for h in hrefs)
                found.pop(None, None)
                found.pop(g["name"], None)
                targets.update({k for k, v in found.items() if v >= 3})
            if targets:
                lists, n_pages = targets.most_common(1)[0]
                relations.append({"from": g["name"], "field": "(query) entries in this category", "to": lists, "cardinality": "query",
                                  "share": round(n_pages / min(20, len(diffed)), 2)})
                category_of[lists] = g["name"]

        # Listings: which collection do their pages link to?
        if g["listings"]:
            for t in page_types:
                if t["name"] not in g["listings"]:
                    continue
                for m in t["members"]:
                    p = pages.get(m["id"])
                    if not p:
                        continue
                    root = page_root(p)
                    hrefs = [a.get("href") for a in root.find_all("a")] if root else []
                    targets = Counter(links.resolve(h, p["final_url"])[1] for h in hrefs)
                    targets.pop(None, None)
                    has_filters = any(b["type"] in ("FilterBar", "FilterableList") for b in p.get("blocks", []))
                    target = targets.most_common(1)[0][0] if targets else None
                    chips = [norm(line) for b in p.get("blocks", []) if b["type"] in ("FilterBar", "FilterableList")
                             for line in b["text"].split("\n")[1:] if 0 < words(line) <= 3 and ":" not in line
                             and norm(line).lower() not in ("filter", "filters")]
                    if len(chips) >= 2:
                        filter_chips.append({"page": urlparse(p["final_url"]).path, "lists": target, "values": chips})
                    for flt in listing_filters(p):
                        name, _ = label_name(flt["name"])
                        add_taxonomy(taxonomies, {"name": name, "values": flt["values"], "examples": flt["values"][:3]},
                                     f"{target or 'Page'} (filter on {urlparse(p['final_url']).path})")
                    listings.append({"page": p["final_url"], "page_type": t["name"], "filters": has_filters,
                                     "lists": targets.most_common(1)[0][0] if targets else None,
                                     "entries_linked": targets.most_common(1)[0][1] if targets else 0})

        for f in fields:
            if f.get("ref"):
                relations.append({"from": g["name"], "field": f["name"], "to": f["ref"],
                                  "cardinality": "many" if f["list"] else "one", "share": f["share"]})
            if f["type"] == "taxonomy":
                add_taxonomy(taxonomies, f, g["name"])

        for auto in diff_stats.get("automatic_lists", []):
            label = f"'{auto['label']}'" if auto["label"] else "A list"
            relations.append({"from": g["name"], "field": f"(query) {auto['label'] or 'list'}", "to": auto["to"],
                              "cardinality": "query", "share": auto["share"]})
            g["notes"].append(f"{label} shows the same {auto['entries']} {auto['to']} entries on {auto['share']:.0%} of the pages: "
                              "filled by a query (latest or featured), so no editor field.")
        for f in fields:
            if f["name"] == "author" and f["type"] in ("enum", "text") and f["source"] == "dom diff":
                f["type"], f["ref"] = "reference(Author)", "Author"
                f["evidence"] += "; few distinct people: model authors as their own entries"

        # The share image (og:image) differs per entry: the entry has an image even when no <img> was found
        og_images = [(p.get("og") or {}).get("og:image") for p in members]
        if g["kind"] != "page" and len(members) >= MIN_DIFF_PAGES and not any(f["type"] == "media" or f["name"] == "image" for f in fields) and \
                sum(1 for x in og_images if x) / len(members) >= 0.6 and len(set(filter(None, og_images))) >= 0.6 * len(members):
            fields.append(field("image", "media", sum(1 for x in og_images if x) / len(members), len(members), named=True,
                                evidence="og:image differs per page: the entry's main image", source="page",
                                examples=[x for x in og_images if x][:2]))

        has_body = any(f["name"] == "body" or f["type"] == "rich text" for f in fields) or (s["zone"] and "RichText" in s["zone"]["allowed"])
        if not has_body and diff_stats.get("body_share", 0) >= 0.6:
            fields.append(field("body", "rich text", diff_stats["body_share"], len(members), named=True, source="dom diff",
                                evidence=f"40+ words of running text that differs per page on {diff_stats['body_share']:.0%} of the pages"))

        # Place in the URL tree: the section page above a collection, landing versus section pages in Page
        if g["kind"] == "collection":
            sections = Counter(url_tree.get(p, {}).get("parent") for p in g["patterns"])
            sections.pop(None, None)
            if sections:
                g["notes"].append("Section page(s) above these entries in the URL tree: " + ", ".join(sections)
                                  + " (the overview that lists them; a Page with a CollectionList block).")
        if g["kind"] == "page":
            roles = Counter(url_tree.get(p.get("pattern"), {}).get("role", "other") for p in members)
            if roles.get("landing") or roles.get("section"):
                g["notes"].append(f"{roles.get('landing', 0)} landing pages (nothing below them in the URL tree) and "
                                  f"{roles.get('section', 0)} section pages (parent of a URL branch, usually an overview).")

        cov = [coverage(p) for p in members]
        covered, total = sum(c[0] for c in cov), sum(c[1] for c in cov)
        fragments = Counter(fr for c in cov for fr in c[2])
        review = []
        content_blocks = statistics.median(len([b for b in main_blocks(p) if b["type"] != "Unknown"]) for p in members)
        segmentation_failed = content_blocks < 1
        if segmentation_failed:
            review.append("Block detection could not split these pages (typically no <main> element, so the whole body is one "
                          "block): coverage is unknown, the fields come from the DOM comparison only.")
        if len(members) < MIN_DIFF_PAGES:
            review.append(f"Only {len(members)} page{'s' if len(members) != 1 else ''}: fields can't be told apart from template "
                          "text, so the content sits in blocks.")
        generic = [f["name"] for f in fields if f["confidence"] == "low" and f["source"] == "dom diff"]
        if generic:
            review.append("Low-confidence fields to check: " + ", ".join(generic[:8]))
        if any(f.get("review") for f in fields):
            review.append("References: decide whether editors pick them or a query fills them.")
        if g["kind"] == "page" and len(g["page_types"]) > 1:
            review.append("Covers several page types (" + ", ".join(g["page_types"]) + "): split if they need their own fields.")
        content_types.append({
            "name": g["name"], "kind": g["kind"], "page_types": g["page_types"], "pages": len(members),
            "url_patterns": dict(g["patterns"].most_common(6)), "fields": fields, "zone": s["zone"],
            "slots": {"head": s["head"], "tail": s["tail"]}, "seo": True, "localisation": localisation(members),
            "diff": diff_stats, "coverage": {"covered_words": covered, "total_words": total,
                                             "share": round(covered / total, 2) if total and not segmentation_failed else None,
                                             "uncovered": [fr for fr, _ in fragments.most_common(5)]},
            "review": review, "notes": g["notes"],
            "examples": [{"url": p["final_url"], "title": p.get("title")} for p in members[:5]]})

    # The same automatic list on three or more types is a menu inside <main> (mega menu), not related content
    autos = Counter((r["field"], r["to"]) for r in relations if r["field"].startswith("(query) ") and r["from"] != "Page")
    menus = {k for k, c in autos.items() if c >= 3}
    if menus:
        relations = [r for r in relations if (r["field"], r["to"]) not in menus]
        labels = {f[len("(query) "):] for f, _ in menus}
        for t in content_types:
            t["notes"] = [n for n in t["notes"] if not any(n.startswith(f"'{l}'") for l in labels)]

    # Entries listed on category pages get a category field (the category page lists them by query)
    for t in content_types:
        if t["name"] in category_of and not any(f.get("ref") == category_of[t["name"]] for f in t["fields"]):
            cat = category_of[t["name"]]
            t["fields"].append(field("categories", f"reference({cat})", 1.0, t["pages"], list_=True, named=True, ref=cat, source="links",
                                     evidence=f"{cat} pages list {t['name']} entries; one entry can sit in several categories"))
            relations.append({"from": t["name"], "field": "categories", "to": cat, "cardinality": "many", "share": 1.0})

    # Enum values that a listing also offers as filter chips: a shared taxonomy
    for chip in filter_chips:
        for t in content_types:
            for f in t["fields"]:
                if f["type"] == "enum" and len({v.lower() for v in f["enum_values"]} & {v.lower() for v in chip["values"]}) >= 2:
                    f["type"], f["values"] = "taxonomy", list(dict.fromkeys(f["enum_values"] + chip["values"]))
                    f["evidence"] += f"; also a filter on {chip['page']}"
                    f["enum_values"] = None
                    add_taxonomy(taxonomies, f, t["name"])
    for tx in taxonomies:
        for chip in filter_chips:
            if len({v.lower() for v in tx["values"]} & {v.lower() for v in chip["values"]}) >= 2:
                tx["used_by"].add(f"filter on {chip['page']}")

    blocks = block_library(data["components"]["catalogue"], type_of_page_type, listings)
    for lst in listings:
        if lst["lists"]:
            relations.append({"from": "Page", "field": "blocks.CollectionList", "to": lst["lists"], "cardinality": "query", "share": 1.0})
    relations = list({(r["from"], r["field"], r["to"]): r for r in relations}.values())

    measured = [t for t in content_types if t["coverage"]["share"] is not None]
    covered = sum(t["coverage"]["covered_words"] for t in measured)
    total = sum(t["coverage"]["total_words"] for t in measured)
    return {
        "site": host, "generated_by": "content_model.py (rules, no AI)", "pages": len(pages),
        "content_types": content_types, "blocks": blocks,
        "components": [seo_component(all_pages)],
        "taxonomies": [{"name": t["name"], "used_by": sorted(t["used_by"]), "values": [x for x, _ in t["values"].most_common(40)],
                        "pattern": t["pattern"]} for t in taxonomies],
        "enums": [{"type": t["name"], "field": f["name"], "values": f["enum_values"]}
                  for t in content_types for f in t["fields"] if f.get("enum_values")],
        "globals": globals_from(navigation) + [
            {"name": "SiteBanner" if sig[0] in ("Image", "Hero", "Carousel") else f"Global{sig[0]}", "kind": "global",
             "evidence": f"the same {sig[0]} inside the content area on {n} of {len(all_pages)} pages: site chrome, edited once",
             "fields": [{"name": "content", "type": f"component: {sig[0]}", "share": round(n / len(all_pages), 2)}]}
            for sig, n in sorted(site_wide.items(), key=lambda kv: -kv[1])],
        "relations": relations, "listings": listings,
        "locales": {"single_language": locales["single_language"], "crawled": locales["crawled_locales"],
                    "all": [l["code"] for l in locales["locales"] if l["code"] != "(none)"],
                    "translated_types": [t["name"] for t in content_types if t["localisation"]["translated"]]},
        "coverage": {"covered_words": covered, "total_words": total, "share": round(covered / total, 2) if total else None},
    }


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def write_markdown(model: dict, path: Path):
    md = [f"# Content model draft: {model['site']}", "",
          f"Drafted by {model['generated_by']} from {model['pages']} crawled pages. "
          f"Coverage: {model['coverage']['share']:.0%} of main-content words fall in a field or block." if model["coverage"]["share"] is not None
          else f"Drafted by {model['generated_by']} from {model['pages']} crawled pages.", ""]
    for t in model["content_types"]:
        md += [f"## {t['name']} ({t['kind']}, {t['pages']} pages)", "",
               "Page types: " + ", ".join(t["page_types"]) + " · URL: " + ", ".join(f"`{p}`" for p in t["url_patterns"]), "",
               "| Field | Type | Required | Confidence | Evidence | Example |", "| --- | --- | --- | --- | --- | --- |"]
        for f in t["fields"]:
            ftype = ("list of " if f["list"] else "") + f["type"] + (f" ({', '.join(f['enum_values'])})" if f.get("enum_values") else "")
            md.append(f"| `{f['name']}` | {ftype} | {'yes' if f['required'] else ''} | {f['confidence']} | {f['evidence']} | "
                      f"{(f['examples'] or [''])[0][:60].replace('|', '/')} |")
        if t["zone"]:
            md += ["", "**Block zone `blocks`:** " + ", ".join(f"{k} ({v:.0%})" for k, v in t["zone"]["allowed"].items())]
        if t["review"]:
            md += ["", "**Review:**", ""] + [f"- {r}" for r in t["review"]]
        md.append("")
    md += ["## Blocks", ""]
    for b in model["blocks"]:
        md.append(f"- **{b['name']}** ({b['instances']}×): " + ", ".join(f"`{f['name']}`: {f['type']}" for f in b["fields"])
                  + (" · options: " + ", ".join(f"{k} ({'/'.join(v)})" for k, v in b["options"].items()) if b["options"] else ""))
    md += ["", "## Relations", ""] + [f"- {r['from']}.{r['field']} → {r['to']} ({r['cardinality']})" for r in model["relations"]]
    md += ["", "## Taxonomies and enums", ""]
    md += [f"- **{x['name']}** ({', '.join(x['used_by'])}): {', '.join(x['values'])}" for x in model["taxonomies"]]
    md += [f"- {x['type']}.{x['field']}: {', '.join(x['values'])}" for x in model["enums"]]
    md += ["", "## Globals", ""] + [f"- **{g['name']}**: {g['evidence']}" for g in model["globals"]]
    path.write_text("\n".join(md) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Draft a headless content model from the crawl (rules, no AI).")
    ap.add_argument("crawl_dir", type=Path, help="Crawl output folder, e.g. output/extremis.com-en")
    ap.add_argument("--sample", type=int, default=60, help="Pages per type to align for DOM fields (default 60, 0 = all)")
    args = ap.parse_args()
    model = build(args.crawl_dir, args.sample)
    (args.crawl_dir / "content-model.json").write_text(json.dumps(model, indent=2, ensure_ascii=False), encoding="utf-8")
    write_markdown(model, args.crawl_dir / "content-model.md")
    cov = model["coverage"]["share"]
    print(f"Wrote {args.crawl_dir / 'content-model.json'}: {len(model['content_types'])} content types, "
          f"{sum(len(t['fields']) for t in model['content_types'])} fields, {len(model['blocks'])} blocks, "
          f"{len(model['relations'])} relations" + (f", coverage {cov:.0%}" if cov is not None else ""))


if __name__ == "__main__":
    main()
