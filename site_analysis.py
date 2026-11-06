"""
Site-level analysis for report.py: locales, header/footer navigation trees and a basic SEO check.

All of it works on the crawl output (page.json, clean.html, inventory.csv, skipped.csv, site.json);
only the robots.txt check fetches one URL when an older crawl has no site.json.
"""

import csv
import json
import re
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from common import main_text

LOCALE = re.compile(r"^[a-z]{2}([-_][a-z]{2})?$", re.I)
SOCIAL_HOSTS = {"facebook": "Facebook", "instagram": "Instagram", "linkedin": "LinkedIn", "twitter": "Twitter", "x.com": "X",
                "youtube": "YouTube", "vimeo": "Vimeo", "pinterest": "Pinterest", "tiktok": "TikTok", "threads": "Threads"}
HIDDEN_HINT = re.compile(r"\b(dialog|drawer|offcanvas|off-canvas|mobile-menu|menu-panel|flyout|mega-?menu|dropdown|submenu|sheet)\b", re.I)
LEGAL = re.compile(r"privacy|cookie|disclaimer|terms|conditions|imprint|impressum|legal|mentions|voorwaarden|datenschutz", re.I)
LABEL_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "button", "summary", "strong", "b", "p", "span", "div", "label", "legend"}


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def url_locale(url: str) -> str:
    first = urlparse(url).path.strip("/").split("/")[0]
    return first.lower() if LOCALE.match(first) else ""


def path_without_locale(url: str) -> list[str]:
    segs = [s for s in urlparse(url).path.split("/") if s]
    return segs[1:] if segs and LOCALE.match(segs[0]) else segs


# ---------------------------------------------------------------------------
# Navigation trees
# ---------------------------------------------------------------------------

def _text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True))


def _link_count(el) -> int:
    return (1 if el.name == "a" and el.get("href") else 0) + len(el.find_all("a", href=True))


def _link_node(a, site_host: str) -> dict:
    href = a.get("href", "").strip()
    parsed = urlparse(href)
    host = parsed.netloc.lower()
    label = _text(a) or a.get("aria-label") or a.get("title") or next((i.get("alt") for i in a.find_all("img") if i.get("alt")), "")
    if not label:
        label = next((name for key, name in SOCIAL_HOSTS.items() if key in host), "")
    path = parsed.path or "/"
    if not label and (path in ("", "/") or (len(path.strip("/").split("/")) == 1 and LOCALE.match(path.strip("/")))) and not host:
        label = "Logo (home)"
    return {
        "kind": "link", "label": label or href, "href": href, "children": [],
        "external": bool(host) and host != site_host,
        "placeholder": href in ("#", "") or href.startswith("javascript:"),
    }


def _label_of(el) -> str | None:
    """Text of a heading, button or label element without links, used as a group title."""
    if el.name not in LABEL_TAGS or el.find("a"):
        return None
    text = _text(el)
    return text if 0 < len(text.split()) <= 6 else None


def _build(el, site_host: str) -> list[dict]:
    if el.name == "a" and el.get("href") is not None:
        return [_link_node(el, site_host)]
    kids = el.find_all(recursive=False)

    # <li><a>Products</a><ul>…</ul></li>: the link is the parent of the nested list
    if el.name == "li":
        direct = next((k for k in kids if k.name == "a"), None)
        nested = [k for k in kids if k is not direct and _link_count(k)]
        if direct is not None and nested:
            node = _link_node(direct, site_host)
            node["children"] = [n for k in nested for n in _build(k, site_host)]
            return [node]

    label = None
    columns = sum(1 for k in kids if _link_count(k) >= 2) >= 2
    nodes = []
    for k in kids:
        if not _link_count(k):
            if label is None and not nodes:
                label = _label_of(k)
            continue
        sub = _build(k, site_host)
        if columns and _link_count(k) >= 2 and not (len(sub) == 1 and sub[0]["kind"] == "group"):
            sub = [_group(None, sub, k)]
        nodes.extend(sub)
    if not nodes:
        return []
    container = el.name in ("nav", "ul", "ol", "dialog", "details", "menu") or bool(HIDDEN_HINT.search(" ".join(el.get("class", []))))
    if label or (container and len(nodes) >= 2):
        return [_group(label or el.get("aria-label"), nodes, el)]
    return nodes


def _group(label, children, el) -> dict:
    classes = " ".join(el.get("class", []))
    hidden = el.name == "dialog" or el.get("aria-modal") == "true" or el.has_attr("hidden") or bool(HIDDEN_HINT.search(classes))
    return {"kind": "group", "label": label, "children": children, "hidden": hidden, "tag": el.name}


def _links(node: dict) -> list[dict]:
    if node["kind"] == "link":
        return [node] + [l for c in node["children"] for l in _links(c)]
    return [l for c in node["children"] for l in _links(c)]


def _tidy(nodes: list[dict]) -> list[dict]:
    """Collapse wrapper groups and give unlabelled groups a name from what they contain."""
    out = []
    for n in nodes:
        n["children"] = _tidy(n["children"])
        if n["kind"] == "group":
            if not n["children"]:
                continue
            if len(n["children"]) == 1 and not n["label"] and not n.get("hidden"):
                out.extend(n["children"])
                continue
            if len(n["children"]) == 1 and n["children"][0]["kind"] == "group":
                inner = n["children"][0]
                inner["label"] = n["label"] or inner["label"]
                inner["hidden"] = n.get("hidden") or inner.get("hidden")
                out.append(inner)
                continue
            links = _links(n)
            # Language switcher: one short link per locale (to the locale home or to the translated page)
            if len(links) >= 2 and len({url_locale(l["href"]) for l in links} - {""}) == len(links) \
                    and all(not urlparse(l["href"]).netloc and len(l["label"]) <= 12 for l in links):
                current = n["label"].split()[0] if n["label"] and LOCALE.match(n["label"].split()[0]) else None
                n["label"], n["role"] = "Language switcher" + (f" (current: {current})" if current else ""), "languages"
            elif not n["label"]:
                if links and all(any(k in urlparse(l["href"]).netloc for k in SOCIAL_HOSTS) for l in links):
                    n["label"], n["role"] = "Social links", "social"
                if links and sum(bool(LEGAL.search(l["label"] + l["href"])) for l in links) >= max(2, len(links) * 0.6):
                    n["label"], n["role"] = "Legal", "legal"
                elif n.get("hidden"):
                    n["label"] = "Menu panel (opens on click)"
        out.append(n)
    return out


def _signature(nodes: list[dict]) -> str:
    """Comparable form of a tree: labels and locale-independent paths."""
    def norm(n):
        href = n.get("href") or ""
        parsed = urlparse(href)
        # Ignore locale prefixes and query strings (tracking parameters differ per page)
        href = parsed.netloc + "/" + "/".join(path_without_locale(href)) if href else ""
        if n.get("role") == "languages":
            return [n["kind"], "languages", "", [c["label"] for c in n["children"]]]  # targets differ per page by design
        # Group labels are partly derived by heuristics, so only links and nesting count
        return [n["kind"], n.get("label") if n["kind"] == "link" else "", href, [norm(c) for c in n["children"]]]
    return json.dumps([norm(n) for n in nodes])


def page_navigation(page: dict, site_host: str) -> dict:
    soup = BeautifulSoup((page["dir"] / "clean.html").read_text(encoding="utf-8"), "html.parser")
    main = soup.find("main") or soup.find(attrs={"role": "main"})
    outside = lambda el: not (main and (el is main or main in el.parents))
    headers = [h for h in soup.find_all(["header"]) + soup.find_all(attrs={"role": "banner"}) if outside(h)]
    footers = [f for f in soup.find_all(["footer"]) + soup.find_all(attrs={"role": "contentinfo"}) if outside(f)]
    headers = [h for h in headers if not any(o is not h and o in h.parents for o in headers)]
    footers = [f for f in footers if not any(o is not f and o in f.parents for o in footers)]
    return {"header": _unique_trees(headers, site_host), "footer": _unique_trees(footers[-1:], site_host)}


def _unique_trees(elements, site_host: str) -> list[dict]:
    """Trees of several header/footer elements, skipping copies (sticky or mobile duplicates of the same header)."""
    trees, seen = [], set()
    for el in elements:
        tree = _tidy(_build(el, site_host))
        sig = _signature(tree)
        if tree and sig not in seen:
            seen.add(sig)
            trees.append((el, tree))
    if len(trees) == 1:
        return trees[0][1]
    # Several different header elements (e.g. a full bar and a compact sticky bar): keep them apart
    return [{"kind": "group", "label": f"<{el.name}> element {i}", "children": tree, "hidden": False, "tag": el.name}
            for i, (el, tree) in enumerate(trees, 1)]


def analyse_navigation(pages: list[dict], site_host: str) -> dict:
    """The most common header and footer, how many pages share them, and link issues."""
    result = {}
    navs = {p["id"]: page_navigation(p, site_host) for p in pages}
    for region in ("header", "footer"):
        variants = defaultdict(list)
        for p in pages:
            variants[_signature(navs[p["id"]][region])].append(p)
        ranked = sorted(variants.values(), key=len, reverse=True)
        main_tree = navs[ranked[0][0]["id"]][region] if ranked else []
        links = [l for n in main_tree for l in _links(n)]
        result[region] = {
            "tree": main_tree,
            "pages_with_main_variant": len(ranked[0]) if ranked else 0,
            "variants": [{"pages": len(v), "example": v[0]["final_url"]} for v in ranked],
            "links": len(links),
            "internal": sum(not l["external"] and not l["placeholder"] for l in links),
            "external": [l for l in links if l["external"]],
            "placeholders": [l for l in links if l["placeholder"]],
            "depth": _depth(main_tree),
        }
    result["switcher_by_page"] = {
        p["final_url"]: {url_locale(l["href"]): l["href"] for region in ("header", "footer")
                         for g in _walk_groups(navs[p["id"]][region]) if g.get("role") == "languages" for l in _links(g)}
        for p in pages}
    return result


def _depth(nodes: list[dict]) -> int:
    return max((1 + _depth(n["children"]) for n in nodes), default=0)


# ---------------------------------------------------------------------------
# Locales
# ---------------------------------------------------------------------------

def analyse_locales(crawl_dir: Path, pages: list[dict], navigation: dict) -> dict:
    known = read_csv(crawl_dir / "inventory.csv") + read_csv(crawl_dir / "skipped.csv")
    crawled_urls = {p["final_url"] for p in pages}
    known_urls = {r["url"] for r in known}
    url_counts = Counter(url_locale(r["url"]) for r in known)
    crawled_counts = Counter(url_locale(u) for u in crawled_urls)

    html_langs = Counter((p["lang"] or "(missing)") for p in pages)
    hreflang_langs, x_default, self_ref, missing = Counter(), 0, 0, []
    targets_known, targets_total = 0, 0
    sections = defaultdict(Counter)       # (source locale, section) -> Counter of (target locale, section)
    examples = {}
    for p in pages:
        alts = p["hreflang"]
        if not alts:
            missing.append(p["final_url"])
            continue
        hreflang_langs.update(a["lang"] for a in alts)
        x_default += any(a["lang"].lower() == "x-default" for a in alts)
        self_ref += any(a["href"].rstrip("/") == p["final_url"].rstrip("/") for a in alts)
        src_loc, src_path = url_locale(p["final_url"]), path_without_locale(p["final_url"])
        for a in alts:
            targets_total += 1
            targets_known += a["href"].rstrip("/") in known_urls
            dst_loc, dst_path = url_locale(a["href"]), path_without_locale(a["href"])
            if dst_loc and dst_loc != src_loc and src_path:
                key = (src_loc, src_path[0])
                sections[key][(dst_loc, dst_path[0] if dst_path else "")] += 1
                examples.setdefault((key, dst_loc), a["href"])

    locales = sorted({loc for loc in url_counts if loc} | {l.split("-")[0].lower() for l in hreflang_langs if l != "x-default"})
    section_rows = []
    for (src_loc, section), targets in sorted(sections.items(), key=lambda kv: -sum(kv[1].values())):
        row = {"locale": src_loc, "section": section, "pages": sum(1 for p in pages if path_without_locale(p["final_url"])[:1] == [section]),
               "translations": {}}
        for loc in locales:
            options = [(name, n) for (l, name), n in targets.items() if l == loc]
            if options:
                name, n = max(options, key=lambda x: x[1])
                row["translations"][loc] = {"section": name, "consistent": len(options) == 1, "example": examples.get(((src_loc, section), loc))}
        section_rows.append(row)

    # Does the switcher keep visitors on the translated page (its hreflang alternate) or send them to the locale home?
    switch = {"links": 0, "to_alternate": 0, "to_home": 0, "other": [], "locales": set()}
    for p in pages:
        alternates = {url_locale(a["href"]): a["href"].rstrip("/") for a in p["hreflang"]}
        base = urlparse(p["final_url"])
        for loc, href in navigation["switcher_by_page"].get(p["final_url"], {}).items():
            absolute = href if urlparse(href).netloc else f"{base.scheme}://{base.netloc}{href}"
            switch["links"] += 1
            switch["locales"].add(loc)
            if absolute.rstrip("/") == alternates.get(loc):
                switch["to_alternate"] += 1
            elif not path_without_locale(absolute):
                switch["to_home"] += 1
            else:
                switch["other"].append((p["final_url"], loc, href, alternates.get(loc)))
    return {
        "locales": [{"code": loc or "(none)", "known_urls": url_counts.get(loc, 0), "crawled": crawled_counts.get(loc, 0),
                     "in_hreflang": sum(v for k, v in hreflang_langs.items() if k.split("-")[0].lower() == loc),
                     "in_switcher": "current" if loc in crawled_counts else ("yes" if loc in switch["locales"] else "")}
                    for loc in locales + ([""] if url_counts.get("") else [])],
        "html_lang": dict(html_langs.most_common()),
        "hreflang": {"pages_with": len(pages) - len(missing), "pages": len(pages), "x_default": x_default, "self_reference": self_ref,
                     "missing": missing, "targets_known": targets_known, "targets_total": targets_total,
                     "values": dict(hreflang_langs.most_common())},
        "sections": section_rows,
        "switcher": {"locations": [r for r in ("header", "footer") if any(g.get("role") == "languages" for g in _walk_groups(navigation[r]["tree"]))],
                     "links": switch["links"], "to_alternate": switch["to_alternate"], "to_home": switch["to_home"],
                     "mismatches": switch["other"], "mismatch_count": len(switch["other"]),
                     "locales": sorted(switch["locales"])},
        "crawled_locales": sorted(loc for loc in crawled_counts if loc),
        "single_language": not any(url_counts.get(loc) for loc in url_counts if loc) and len(missing) == len(pages),
    }


def _walk_groups(nodes: list[dict]):
    for n in nodes:
        if n["kind"] == "group":
            yield n
        yield from _walk_groups(n["children"])


# ---------------------------------------------------------------------------
# SEO
# ---------------------------------------------------------------------------

def _check(key, name, why, issues, total, level_if_issue="warn", info=None):
    return {"key": key, "name": name, "why": why, "issues": issues, "total": total,
            "status": "pass" if not issues else level_if_issue, "info": info}


def robots_info(crawl_dir: Path, offline: bool) -> dict:
    site_file = crawl_dir / "site.json"
    if site_file.exists():
        return json.loads(site_file.read_text(encoding="utf-8"))
    inventory = read_csv(crawl_dir / "inventory.csv")
    if offline or not inventory:
        return {}
    parsed = urlparse(inventory[0]["url"])
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        req = urllib.request.Request(robots_url, headers={"User-Agent": "ContentModelCrawler/0.1 (+content audit)"})
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode("utf-8", "replace") if r.status == 200 else None
    except Exception:
        body = None
    sitemaps = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", body or "")
    return {"robots_url": robots_url, "robots_txt": body, "sitemaps_listed": sitemaps, "fetched_by_report": True}


def _per_page(mismatches) -> list[tuple[str, str]]:
    """One issue line per page from (page, locale, switcher href, hreflang href) tuples."""
    by_page = defaultdict(list)
    for page, loc, href, alt in mismatches:
        by_page[page].append(f"{loc}: switcher → {href}, hreflang → {urlparse(alt).path if alt else 'none'}")
    return [(page, "; ".join(lines)) for page, lines in by_page.items()]


def seo_checks(crawl_dir: Path, pages: list[dict], navigation: dict, locales: dict, offline: bool = False) -> dict:
    n = len(pages)
    checks = []
    site = robots_info(crawl_dir, offline)
    known = read_csv(crawl_dir / "inventory.csv") + read_csv(crawl_dir / "skipped.csv")
    in_sitemap = {r["url"] for r in known if r.get("found_via") == "sitemap"}

    # -- site level --------------------------------------------------------
    robots = site.get("robots_txt")
    blocks_all = bool(robots and re.search(r"(?ims)user-agent:\s*\*(?:(?!user-agent).)*?^\s*disallow:\s*/\s*$", robots))
    checks.append(_check("robots", "robots.txt", "Tells crawlers what they may index and where the sitemap is.",
                         [] if robots and not blocks_all else [(site.get("robots_url", "robots.txt"),
                                                               "blocks the whole site for all crawlers" if blocks_all else "missing or not reachable")],
                         1, "fail", info=robots.strip()[:400] if robots else None))
    checks.append(_check("sitemap", "XML sitemap", "Lists the pages to index; should be referenced in robots.txt.",
                         [] if site.get("sitemaps_listed") and in_sitemap else
                         [("robots.txt", "no Sitemap: line" if not site.get("sitemaps_listed") else "sitemap found but empty")],
                         1, "warn", info=f"{len(in_sitemap)} URLs in sitemap(s): " + ", ".join(site.get("sitemaps_listed", []))))
    site_host = urlparse(site.get("start_url") or (pages[0]["final_url"] if pages else "")).netloc
    other_hosts = sorted({urlparse(u).netloc for u in site.get("sitemaps_listed", [])} - {site_host})
    checks.append(_check("sitemap-host", "Sitemap on the site's own host",
                         "A sitemap on another host (www vs. no www) only counts when that host is verified for the site.",
                         [(u, f"listed in robots.txt, but the site runs on {site_host}") for u in site.get("sitemaps_listed", [])
                          if urlparse(u).netloc != site_host] if other_hosts else [], 1))
    if "certificate_ok" in site:
        ok = site["certificate_ok"]
        checks.append(_check("tls", "Valid TLS certificate", "An invalid certificate shows visitors a security warning and blocks crawlers.",
                             [] if ok else [(site.get("start_url", "site"), site.get("certificate_error") or "certificate could not be checked")],
                             1, "fail" if ok is False else "warn",
                             info=f"Valid until {site['certificate_expires']}" if ok and site.get("certificate_expires") else None))
    not_in_sitemap = [(p["final_url"], "found through links only") for p in pages if p["final_url"] not in in_sitemap and p["url"] not in in_sitemap]
    checks.append(_check("sitemap-coverage", "Crawled pages listed in the sitemap",
                         "Pages missing from the sitemap are found later, or not at all.", not_in_sitemap, n))
    checks.append(_check("https", "HTTPS", "All pages should be served over HTTPS.",
                         [(p["final_url"], "served over http") for p in pages if not p["final_url"].startswith("https://")], n, "fail"))

    # -- per page ----------------------------------------------------------
    checks.append(_check("status", "HTTP status 200", "Crawled pages should answer 200 OK.",
                         [(p["final_url"], f"status {p['status']}, linked from {p['found_via']}") for p in pages if p["status"] != 200], n, "fail"))

    titles = Counter((p["title"] or "").strip() for p in pages)
    checks.append(_check("title", "Title present", "The title is the main headline in search results.",
                         [(p["final_url"], "no <title>") for p in pages if not (p["title"] or "").strip()], n, "fail"))
    checks.append(_check("title-length", "Title length 30–65 characters", "Shorter titles waste space; longer ones get cut off.",
                         [(p["final_url"], f"{len(p['title'])} chars: {p['title']}") for p in pages
                          if p["title"] and not 30 <= len(p["title"].strip()) <= 65], n))
    checks.append(_check("title-unique", "Unique titles", "Duplicate titles make pages compete with each other.",
                         [(p["final_url"], f"same title as {titles[p['title'].strip()] - 1} other page(s): {p['title']}")
                          for p in pages if p["title"] and titles[p["title"].strip()] > 1], n))

    descs = Counter((p["description"] or "").strip() for p in pages)
    checks.append(_check("description", "Meta description present", "Used as the snippet in search results.",
                         [(p["final_url"], "no meta description") for p in pages if not (p["description"] or "").strip()], n))
    checks.append(_check("description-length", "Meta description 70–160 characters", "Too short says little; too long gets cut off.",
                         [(p["final_url"], f"{len(p['description'])} chars") for p in pages
                          if p["description"] and not 70 <= len(p["description"].strip()) <= 160], n))
    checks.append(_check("description-unique", "Unique meta descriptions", "Duplicates are often replaced by Google's own snippet.",
                         [(p["final_url"], f"shared with {descs[p['description'].strip()] - 1} other page(s)")
                          for p in pages if p["description"] and descs[p["description"].strip()] > 1], n))

    h1 = {p["final_url"]: sum(1 for h in p["headings"] if h["level"] == "h1") for p in pages}
    checks.append(_check("h1", "Exactly one H1", "One clear main heading per page.",
                         [(u, "no h1" if c == 0 else f"{c} h1 elements") for u, c in h1.items() if c != 1], n))

    def canon_issue(p):
        if not p["canonical"]:
            return "no canonical"
        if p["canonical"].rstrip("/") != p["final_url"].rstrip("/") and urlparse(p["canonical"]).path.rstrip("/") != urlparse(p["final_url"]).path.rstrip("/"):
            return f"points to {p['canonical']}"
        return None
    checks.append(_check("canonical", "Self-referencing canonical", "Prevents duplicate-content issues from parameters and copies.",
                         [(p["final_url"], canon_issue(p)) for p in pages if canon_issue(p)], n))
    checks.append(_check("indexable", "Indexable (no noindex)", "noindex pages drop out of search; check that this is intended.",
                         [(p["final_url"], f"meta robots: {p['robots']}") for p in pages if p["robots"] and "noindex" in p["robots"].lower()], n))

    hl = locales["hreflang"]
    if locales["single_language"]:
        checks.append(_check("hreflang", "Language setup", "hreflang is only needed when a site has several languages or regions.",
                             [], n, info="Single-language site: no locale prefixes and no hreflang, so hreflang checks don't apply."))
        return _finish_seo(checks, site, n, crawl_dir, pages, navigation, locales, skip_hreflang=True)
    return _finish_seo(checks, site, n, crawl_dir, pages, navigation, locales, skip_hreflang=False)


def _finish_seo(checks, site, n, crawl_dir, pages, navigation, locales, skip_hreflang):
    hl = locales["hreflang"]
    if not skip_hreflang:
        _hreflang_checks(checks, pages, locales, n)
    _content_checks(checks, site, n, crawl_dir, pages, navigation)
    counts = Counter(c["status"] for c in checks)
    return {"checks": checks, "summary": dict(counts), "pages": n, "robots": site}


def _hreflang_checks(checks, pages, locales, n):
    hl = locales["hreflang"]
    checks.append(_check("hreflang", "hreflang alternates", "Tells search engines which URL to show per language.",
                         [(u, "no hreflang") for u in hl["missing"]], n))
    checks.append(_check("hreflang-self", "hreflang includes the page itself", "Each page should list itself among its alternates.",
                         [(p["final_url"], "missing self-reference") for p in pages
                          if p["hreflang"] and not any(a["href"].rstrip("/") == p["final_url"].rstrip("/") for a in p["hreflang"])], n))
    sw = locales["switcher"]
    checks.append(_check("hreflang-switcher", "hreflang matches the language switcher",
                         "When hreflang and the switcher disagree, one of them points to the wrong or an outdated URL.",
                         _per_page(sw["mismatches"]),
                         n, info=f"{sw['to_alternate']} of {sw['links']} switcher links match the hreflang alternate"
                                 + (f"; {sw['mismatch_count']} differ" if sw["mismatch_count"] else "")))
    unknown_targets = hl["targets_total"] - hl["targets_known"]
    checks.append(_check("hreflang-known", "hreflang targets are known URLs",
                         "Alternates that are not in the sitemap may not exist, redirect, or be missing from the sitemap.",
                         [] if not unknown_targets else [("site", f"{unknown_targets} of {hl['targets_total']} hreflang targets are not in the sitemap or crawl")],
                         1, info="Only checked against the URLs this crawl knows; other locales were not crawled."))
    checks.append(_check("hreflang-x-default", "hreflang x-default", "Fallback URL for visitors whose language is not offered.",
                         [(p["final_url"], "no x-default") for p in pages if p["hreflang"] and not any(a["lang"].lower() == "x-default" for a in p["hreflang"])], n))


def _content_checks(checks, site, n, crawl_dir, pages, navigation):

    def lang_issue(p):
        if not p["lang"]:
            return "no lang attribute"
        loc = url_locale(p["final_url"])
        if loc and not p["lang"].lower().startswith(loc[:2]):
            return f"lang=\"{p['lang']}\" on a /{loc}/ URL"
        return None
    checks.append(_check("lang", "<html lang> matches the URL locale", "Helps browsers, screen readers and search engines.",
                         [(p["final_url"], lang_issue(p)) for p in pages if lang_issue(p)], n))

    checks.append(_check("og", "Open Graph title, description and image", "Controls how links look when shared on social media.",
                         [(p["final_url"], "missing " + ", ".join(k for k in ("og:title", "og:description", "og:image") if not p["og"].get(k)))
                          for p in pages if not all(p["og"].get(k) for k in ("og:title", "og:description", "og:image"))], n))
    types = Counter(t for p in pages for t in p["schema_types"])
    checks.append(_check("structured-data", "Structured data (JSON-LD)", "Enables rich results (breadcrumbs, products, articles).",
                         [(p["final_url"], "no JSON-LD") for p in pages if not p["schema_types"]], n,
                         info="Types used: " + (", ".join(f"{t} ({c})" for t, c in types.most_common()) or "none")))
    content_types = {"Product", "Article", "NewsArticle", "BlogPosting", "LocalBusiness", "Organization", "Event"}
    checks.append(_check("structured-data-content", "Structured data beyond breadcrumbs", "Content types (Product, Article, LocalBusiness…) give richer results.",
                         [] if content_types & set(types) else [("site", "only " + (", ".join(types) or "no") + " markup found")], 1,
                         info="Consider Product on product pages, Article on stories and LocalBusiness on dealer pages."))

    def missing_alt(p):
        return sum(1 for i in p["images"] if i.get("alt") is None and (i.get("w") or 0) > 2)
    checks.append(_check("alt", "Images have alt attributes", "Needed for accessibility and image search (alt=\"\" is fine for decoration).",
                         [(p["final_url"], f"{missing_alt(p)} image(s) without alt") for p in pages if missing_alt(p)], n))

    thin = []
    for p in pages:
        words = len(main_text(p, limit=10**9).split())
        if words < 150:
            thin.append((p["final_url"], f"{words} words of main content"))
    checks.append(_check("thin", "Enough main content (150+ words)", "Very short pages rarely rank; check listings and empty pages.", thin, n))

    def url_issue(u):
        path = urlparse(u).path
        problems = [label for bad, label in ((re.search(r"[A-Z]", path), "uppercase"), ("_" in path, "underscores"),
                                             (len(u) > 115, f"{len(u)} characters long"), (urlparse(u).query, "query string")) if bad]
        return ", ".join(problems) or None
    checks.append(_check("urls", "Clean URLs", "Lowercase, hyphenated, short URLs without parameters.",
                         [(p["final_url"], url_issue(p["final_url"])) for p in pages if url_issue(p["final_url"])], n))

    skipped = read_csv(crawl_dir / "skipped.csv")
    dupes = [(r["url"], r["reason"].replace("duplicate: ", "")) for r in skipped if r["reason"].startswith("duplicate")]
    checks.append(_check("duplicates", "No duplicate pages", "Same content on several URLs splits ranking signals.", dupes, n))
    broken = [(r["url"], r["reason"]) for r in skipped if r["reason"].startswith(("error", "external-redirect", "content-type"))]
    checks.append(_check("redirects", "No errors or off-site redirects", "Internal links and sitemap URLs should resolve on the site.", broken, n))
    placeholders = [(f"{region}: {l['label']}", f'href="{l["href"]}"') for region in ("header", "footer") for l in navigation[region]["placeholders"]]
    checks.append(_check("nav-links", "Navigation links are real links", "Links to # or javascript: are dead ends for crawlers.", placeholders, 1))
