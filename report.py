#!/usr/bin/env python3
"""
Site analysis report

Combines the output of all phases into one self-contained HTML page with six tabs:
  1. Site           locales (URLs, hreflang, language switcher, translated sections) and the header and
                    footer navigation as link trees
  2. URL patterns   every pattern the crawl came across, crawled or not, with counts and page types
  3. Page types     every page type with kind, size, URL patterns, typical component sequence and findings
  4. Components     every component type once, with one representative screenshot, variants and fields
  5. Content model  the draft headless content model (content_model.py): types, fields, blocks, relations
  6. SEO            a quick check of the basic SEO setup on the crawled pages

Usage:
    python report.py output/extremis
    python report.py output/extremis --offline   # don't fetch robots.txt for crawls without site.json

Needs: inventory.csv and skipped.csv (crawl.py), clusters.csv (cluster.py), page-types.json
(name_types_rules.py) and components.json (components.py); content-model.json
(content_model.py) is optional.

Output (in the crawl folder):
    report.html   one file; the component screenshots are embedded, so it can be shared as is
"""

import argparse
import base64
import csv
import html
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import unquote, urlparse

from common import load_pages
from components import COMPONENTS, HTML_STYLE, crop, pick_examples, variant_label
from crawl import url_pattern
from url_groups import in_scope, landing_label, pattern_groups
from site_analysis import analyse_locales, analyse_navigation, seo_checks

STATUS_LABELS = {"crawled": "Crawled", "sampled": "Sampled out", "out_of_scope": "Out of scope", "other": "Other"}

e = html.escape


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def url_status(reason: str) -> str:
    if reason.startswith("pattern-sampled") or reason == "max-pages":
        return "sampled"
    if reason in ("not-included", "excluded-by-pattern", "excluded-path", "robots.txt", "file-extension"):
        return "out_of_scope"
    return "other"


def locale_of(url: str) -> str:
    first = urlparse(url).path.strip("/").split("/")[0]
    return first.lower() if url_pattern(url).startswith("/{locale}") else ""


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def collect_patterns(crawl_dir: Path, page_type_of: dict[str, str]) -> list[dict]:
    """One row per grouped URL pattern; nested patterns that were grouped are listed in "nested"."""
    groups = pattern_groups(crawl_dir)
    rows = defaultdict(lambda: {"counts": Counter(), "locales": Counter(), "page_types": Counter(), "examples": [], "nested": Counter()})
    for r in read_csv(crawl_dir / "inventory.csv"):
        raw = url_pattern(r["url"])
        row = rows[groups(raw)]
        if groups(raw) != raw:
            row["nested"][raw] += 1
        row["counts"]["crawled"] += 1
        row["locales"][locale_of(r["url"])] += 1
        row["page_types"][page_type_of.get(r["id"], "unclassified")] += 1
        row["examples"].append(r["url"])
    for r in read_csv(crawl_dir / "skipped.csv"):
        if not in_scope(crawl_dir, r["url"]):
            continue              # the URL structure is mapped for the crawled locale only
        raw = url_pattern(r["url"])
        row = rows[groups(raw)]
        if groups(raw) != raw:
            row["nested"][raw] += 1
        row["counts"][url_status(r["reason"])] += 1
        row["locales"][locale_of(r["url"])] += 1
        if len(row["examples"]) < 3:
            row["examples"].append(r["url"])
    out = []
    for pattern, row in rows.items():
        out.append({"pattern": pattern, "known": sum(row["counts"].values()), **{k: row["counts"][k] for k in STATUS_LABELS},
                    "locales": [loc for loc, _ in row["locales"].most_common() if loc],
                    "page_types": dict(row["page_types"].most_common()), "examples": row["examples"][:3],
                    "nested": dict(row["nested"].most_common())})
    return sorted(out, key=lambda r: (-(r["crawled"] + r["sampled"]), -r["known"], r["pattern"]))


def other_locale_urls(crawl_dir: Path) -> Counter:
    """Known URLs of locales that were not crawled, per locale: left out of the URL structure."""
    return Counter(locale_of(r["url"]) or "(no locale)" for r in read_csv(crawl_dir / "skipped.csv") if not in_scope(crawl_dir, r["url"]))


def tree_rows(crawl_dir: Path, patterns: list[dict]) -> list[dict]:
    """Rows for the URL patterns tab: fixed pages with a branch below them link to it (section pages), fixed pages
    without anything below them are grouped into one "landing pages" row per locale prefix."""
    tree = pattern_groups(crawl_dir).tree()
    out, landing = [], {}
    for p in patterns:
        node = tree.get(p["pattern"], {"role": "template", "parent": None, "children": []})
        row = {**p, "role": node["role"], "parent": node["parent"], "children": node["children"]}
        if node["role"] != "landing":
            out.append(row)
            continue
        label = landing_label(p["pattern"])
        group = landing.get(label)
        if group is None:
            group = landing[label] = {**row, "pattern": label, "role": "landing group", "nested": {}, "page_types": Counter(),
                                      "locales": list(row["locales"]), "examples": []}
            for k in STATUS_LABELS:
                group[k] = 0
            group["known"] = 0
            out.append(group)
        for k in [*STATUS_LABELS, "known"]:
            group[k] += p[k]
        group["nested"][p["pattern"]] = p["known"]
        group["page_types"].update(p["page_types"])
        group["locales"] = sorted(set(group["locales"]) | set(p["locales"]))
        group["examples"] = (group["examples"] + p["examples"][:1])[:3]
    for g in landing.values():
        g["page_types"] = dict(g["page_types"].most_common())
        g["nested"] = dict(sorted(g["nested"].items(), key=lambda kv: -kv[1]))
    return sorted(out, key=lambda r: (-(r["crawled"] + r["sampled"]), -r["known"], r["pattern"]))


def _short_path(url: str, limit: int = 90) -> str:
    """Readable path for a link text: decoded, and cut off when a URL is a whole sentence."""
    path = unquote(urlparse(url).path) or "/"
    return path if len(path) <= limit else path[:limit - 1] + "…"


def _anchor(pattern: str) -> str:
    return "pat-" + re.sub(r"[^a-z0-9]+", "-", pattern.lower()).strip("-")


def _tree_cell(p: dict) -> str:
    """Role in the URL tree: section page with its branch, or the section page a pattern sits under."""
    if p.get("role") == "landing group":
        return (f"<details class=nested><summary class='muted small'><span class=badge>landing pages</span> "
                f"{len(p['nested'])} pages with nothing below them</summary><ul class=members>"
                + "".join(f"<li><code>{e(x)}</code> <span class=muted>×{n}</span></li>" for x, n in p["nested"].items())
                + "</ul></details>")
    parts = []
    if p.get("role") == "section":
        links = ", ".join(f'<a href="#{_anchor(c)}"><code>{e(c)}</code></a>' for c in p["children"])
        parts.append(f"<div class='small'><span class=badge>section page</span> parent of {links}</div>")
    if p.get("parent"):
        parts.append(f'<div class="small muted">under <a href="#{_anchor(p["parent"])}"><code>{e(p["parent"])}</code></a></div>')
    return "".join(parts) + (_nested_cell(p["nested"]) if p.get("role") != "landing group" else "")


def estimate_site_pages(page_types: list[dict], patterns: list[dict]) -> dict[int, int]:
    """Pages per page type on the whole site: each pattern's in-scope URLs split by how its crawled pages were typed."""
    in_scope = {p["pattern"]: p["crawled"] + p["sampled"] for p in patterns}
    crawled = {p["pattern"]: p["crawled"] for p in patterns}
    estimate = Counter()
    for t in page_types:
        for pattern, n in t["patterns"].items():
            if crawled.get(pattern):
                estimate[t["cluster"]] += round(in_scope[pattern] * n / crawled[pattern])
    return estimate


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def chips(items, cls="") -> str:
    return f'<div class="chips {cls}">' + "".join(f"<span>{x}</span>" for x in items) + "</div>"


def _nested_cell(nested: dict) -> str:
    """The original nested patterns behind a grouped pattern (/collections/amanu/{slug}, ...)."""
    if not nested:
        return ""
    items = "".join(f"<li><code>{e(p)}</code> <span class=muted>×{n}</span></li>" for p, n in list(nested.items())[:40])
    more = f"<li class=muted>+{len(nested) - 40} more</li>" if len(nested) > 40 else ""
    return (f"<details class=nested><summary class='muted small'>groups {len(nested)} nested patterns</summary>"
            f"<ul class=members>{items}{more}</ul></details>")


def tab_patterns(patterns: list[dict], other_locales: Counter) -> str:
    totals = Counter()
    for p in patterns:
        totals.update({k: p[k] for k in STATUS_LABELS})
    rows = []
    for p in patterns:
        status = "crawled" if p["crawled"] else "sampled" if p["sampled"] else "out_of_scope" if p["out_of_scope"] else "other"
        types = ", ".join(f"{e(t)} ({n})" for t, n in p["page_types"].items()) or '<span class="muted">not crawled</span>'
        examples = " <span class=sep>·</span> ".join(
            f'<a href="{e(u)}" target="_blank" rel="noopener" title="{e(unquote(u))}">{e(_short_path(u))}</a>' for u in p["examples"])
        # One <tbody> per pattern: the figures on the first row, its example URLs full width below them
        rows.append(
            f'<tbody class=pattern id="{_anchor(p["pattern"])}" data-status="{status}" data-search="{e(p["pattern"].lower())} '
            f'{e(" ".join(p["page_types"]).lower())} {e(" ".join(p["nested"]).lower())} {e(p.get("role", ""))}">'
            f'<tr><td><code>{e(p["pattern"])}</code>{_tree_cell(p)}</td><td class=num>{p["known"]}</td><td class=num>{p["crawled"] or ""}</td>'
            f'<td class=num>{p["sampled"] or ""}</td><td class=num>{p["out_of_scope"] or ""}</td><td class=num>{p["other"] or ""}</td>'
            f'<td>{e(", ".join(p["locales"]))}</td><td>{types}</td></tr>'
            + (f'<tr class=example-row><td colspan=8><span class=muted>Examples:</span> {examples}</td></tr>' if examples else "")
            + '</tbody>')
    filters = "".join(
        f'<label><input type=checkbox data-filter="{k}" checked> {label} <span class=muted>({sum(1 for p in patterns if _main_status(p) == k)})</span></label>'
        for k, label in STATUS_LABELS.items())
    return f"""
<section>
  <h2>URL patterns</h2>
  <p class=muted>{len(patterns)} patterns from {sum(totals.values())} known URLs of the crawled locale{
  f" ({sum(other_locales.values())} URLs of other locales left out: " + ", ".join(f"{e(k)} {v}" for k, v in other_locales.most_common()) + "; they are translations of the same structure)" if other_locales else ""}. Nested slugs are grouped: patterns that differ
  only in a middle segment (<code>/collections/amanu/{{slug}}</code>, <code>/collections/elio/{{slug}}</code>) form one pattern with
  <code>{{group}}</code> there, and many flat <code>/title.html</code> URLs form <code>/{{slug}}.html</code>. A fixed page with a URL
  branch below it is that branch's <b>section page</b> (<code>/collections</code> above <code>/collections/{{slug}}</code>); fixed pages
  with nothing below them are grouped as <b>landing pages</b>. Counts: {totals['crawled']} crawled,
  {totals['sampled']} sampled out (same pattern, not needed for the analysis), {totals['out_of_scope']} out of scope
  (other locales or excluded paths), {totals['other']} other (duplicates, redirects).</p>
  <div class=toolbar><input type=search id=pattern-search placeholder="Filter patterns or page types" aria-label="Filter patterns or page types">{filters}</div>
  <div class=scroll><table id=pattern-table>
    <thead><tr><th>Pattern</th><th class=num>Known URLs</th><th class=num>Crawled</th><th class=num>Sampled out</th>
    <th class=num>Out of scope</th><th class=num>Other</th><th>Locales</th><th>Page types (crawled pages)</th></tr></thead>
    {''.join(rows)}
  </table></div>
  <p class=muted id=pattern-count></p>
</section>"""


def _main_status(p: dict) -> str:
    return "crawled" if p["crawled"] else "sampled" if p["sampled"] else "out_of_scope" if p["out_of_scope"] else "other"


def tab_page_types(page_types: list[dict], structures: dict, estimate: dict[int, int], named_by: str) -> str:
    kinds = Counter(t.get("kind") for t in page_types)
    summary = "".join(
        f'<tr><td><a href="#type-{t["cluster"]}" data-open="type-{t["cluster"]}">{e(t["name"] or "(not named)")}</a></td>'
        f'<td>{e(t.get("kind") or "")}</td><td class=num>{t["size"]}</td><td class=num>{estimate.get(t["cluster"], t["size"])}</td>'
        f'<td>{e(t.get("domain_entity") or "")}</td><td>{_recognised_cell(t.get("recognised_as"))}</td>'
        f'<td>{e(t.get("confidence") or "")}</td></tr>' for t in page_types)
    cards = []
    for t in page_types:
        s = structures.get(t["name"], {})
        sequence = " <span class=arrow>→</span> ".join(f"<span class=step>{e(x)}</span>" for x in s.get("typical_sequence", "").split(" → ") if x)
        presence = [f"{e(k)} <b>{v:.0%}</b>" for k, v in s.get("components", {}).items()]
        members = "".join(f'<li><a href="{e(m["url"])}" target="_blank" rel="noopener">{e(m["title"] or m["url"])}</a></li>' for m in t["members"][:6])
        more = f'<li class=muted>+{len(t["members"]) - 6} more crawled</li>' if len(t["members"]) > 6 else ""
        extra = ""
        if t.get("anomalies"):
            extra += "<h4>Anomalies</h4><ul>" + "".join(f"<li>{e(a)}</li>" for a in t["anomalies"]) + "</ul>"
        if t.get("split_suggestion"):
            extra += f"<h4>Split suggestion</h4><p>{e(t['split_suggestion'])}</p>"
        if t.get("merge_candidates"):
            names = {x["cluster"]: x["name"] for x in page_types}
            extra += "<h4>Possibly the same type as</h4>" + chips(e(names.get(c, str(c))) for c in t["merge_candidates"])
        rec = t.get("recognised_as")
        if rec:
            votes = ", ".join(f"{k} ({v})" for k, v in rec["votes"].items())
            extra += (f"<h4>Recognised as</h4><p><b>{e(rec['type'])}</b> · {e(rec['confidence'])} · {rec['share']:.0%} of its pages"
                      + (f" · lists {e(rec['listing_of'])}" if rec.get("listing_of") else "") + "</p>"
                      + (chips(e(x) for x in rec["evidence"]) if rec.get("evidence") else "")
                      + (f"<p class='muted small'>Votes: {e(votes)}</p>" if len(rec["votes"]) > 1 else ""))
        if t.get("notes"):
            extra += f"<h4>Notes</h4><p>{e(t['notes'])}</p>"
        cards.append(f"""
<details class=card id="type-{t['cluster']}">
  <summary><span class=title>{e(t['name'] or '(not named)')}</span>
    <span class=badge>{e(t.get('kind') or '')}</span>
    <span class=muted>{t['size']} crawled · ~{estimate.get(t['cluster'], t['size'])} on site · {e(t.get('domain_entity') or 'no entity')} · confidence {e(t.get('confidence') or '')}</span></summary>
  <div class=card-body>
    <p>{e(t.get('description') or '')}</p>
    {f"<p class=muted><b>Purpose:</b> {e(t['purpose'])}</p>" if t.get('purpose') else ''}
    <h4>URL patterns</h4>{chips(f"<code>{e(p)}</code> ×{n}" for p, n in t['patterns'].items())}
    <h4>Typical component sequence</h4><div class=sequence>{sequence or '<span class=muted>not analysed</span>'}</div>
    <h4>Components on its pages</h4>{chips(presence) if presence else '<p class=muted>not analysed</p>'}
    {extra}
    <h4>Crawled pages</h4><ul class=members>{members}{more}</ul>
  </div>
</details>""")
    return f"""
<section>
  <h2>Page types</h2>
  <p class=muted>{len(page_types)} page types ({', '.join(f'{n} {k}' for k, n in kinds.most_common() if k)}), named by {e(named_by)}.
  "On site" estimates the number of pages per type from all in-scope URLs of its patterns.</p>
  <div class=scroll><table><thead><tr><th>Page type</th><th>Kind</th><th class=num>Crawled</th><th class=num>On site (est.)</th>
  <th>Entity</th><th>Recognised as</th><th>Confidence</th></tr></thead><tbody>{summary}</tbody></table></div>
  <div class=cards>{''.join(cards)}</div>
</section>"""


def _recognised_cell(rec: dict | None) -> str:
    if not rec:
        return ""
    weak = rec["confidence"] == "low"
    return f'{e(rec["type"])} <span class="muted small">{rec["share"]:.0%}</span>' + (' <span class="badge warn">unsure</span>' if weak else "")


def tab_components(catalogue: dict, pages: list[dict], crawl_dir: Path) -> str:
    crops_dir = crawl_dir / "components" / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)
    cards = []
    for ctype, c in catalogue.items():
        example = pick_examples(pages, ctype, 1)
        img, caption = '<div class="noimg muted">no screenshot</div>', ""
        if example:
            p, b = example[0]
            src = crop(p, b, crops_dir)
            if src:
                data = base64.b64encode((crawl_dir / src).read_bytes()).decode()
                img = f'<img loading="lazy" src="data:image/jpeg;base64,{data}" alt="Example of {e(ctype)}">'
            caption = (f'<p class="muted small">Example: <a href="{e(p["final_url"])}" target="_blank" rel="noopener">'
                       f'{e(p["title"] or p["final_url"])}</a> ({e(p["page_type"])})</p>')
        variants = list(c["variants"].items())[:6]
        cards.append(f"""
<article class=component id="component-{ctype}">
  <div class=shot>{img}</div>
  <div class=component-body>
    <h3>{e(ctype)}</h3>
    <p>{e(COMPONENTS.get(ctype, ''))}</p>
    <div class=meta><span><b>{c['count']}</b> instances</span><span><b>{c['pages']}</b> pages</span><span>confidence <b>{c['avg_confidence']}</b></span></div>
    {caption}
    <h4>Used on</h4>{chips(f"{e(t)} ({n})" for t, n in c['page_types'].items())}
    <h4>Variants</h4>{chips(f"{e(k)} ×{n}" for k, n in variants)}
    <h4>Candidate fields</h4>{chips((f"<code>{e(k)}</code> {v:.0%}" for k, v in c['fields'].items()), 'fields') if c['fields'] else '<p class=muted>none (derived or global)</p>'}
  </div>
</article>""")
    total = sum(c["count"] for c in catalogue.values())
    return f"""
<section>
  <h2>Components</h2>
  <p class=muted>{len(catalogue)} component types across {total} blocks on {len(pages)} pages. Each is shown once with
  its most representative example. Detected by rules: check unusual cases in component-catalogue.html.</p>
  <div class=toolbar><input type=search id=component-search placeholder="Filter components" aria-label="Filter components"></div>
  <div class=components>{''.join(cards)}</div>
</section>"""


def render_tree(nodes: list[dict]) -> str:
    items = []
    # Unnamed sibling groups are usually columns of one menu or footer
    unnamed = [n for n in nodes if n["kind"] == "group" and not n["label"]]
    columns = {id(n): f"Column {i}" for i, n in enumerate(unnamed, 1)} if len(unnamed) >= 2 else {}
    for n in nodes:
        if n["kind"] == "group":
            badges = ""
            if n.get("hidden"):
                badges += '<span class="badge">hidden until opened</span>'
            if n.get("role"):
                badges += f'<span class="badge">{e(n["role"])}</span>'
            count = sum(1 for _ in _iter_links(n))
            items.append(f'<li class=group><details open><summary><b>{e(n["label"] or columns.get(id(n), "Link group"))}</b> {badges}'
                         f'<span class=muted>{count} link{"s" if count != 1 else ""}</span></summary>{render_tree(n["children"])}</details></li>')
        else:
            href = n["href"]
            shown = urlparse(href).path or href if not n["external"] else href.split("?")[0]
            badges = ('<span class="badge ext">external</span>' if n["external"] else "") + \
                     ('<span class="badge warn">placeholder link</span>' if n["placeholder"] else "")
            sub = render_tree(n["children"]) if n["children"] else ""
            items.append(f'<li class=link><span>{e(n["label"])}</span> <code class=muted>{e(shown)}</code> {badges}{sub}</li>')
    return f"<ul class=tree>{''.join(items)}</ul>"


def _iter_links(node: dict):
    if node["kind"] == "link":
        yield node
    for c in node["children"]:
        yield from _iter_links(c)


def tab_site(locales: dict, navigation: dict) -> str:
    if locales["single_language"]:
        return _single_language(locales) + _nav_sections(navigation)
    rows = "".join(
        f"<tr><td><b>{e(l['code'])}</b></td><td class=num>{l['known_urls']}</td><td class=num>{l['crawled']}</td>"
        f"<td class=num>{l['in_hreflang']}</td><td>{e(l['in_switcher'])}</td></tr>" for l in locales["locales"])
    hl, sw = locales["hreflang"], locales["switcher"]
    crawled = ", ".join(locales["crawled_locales"]) or "none"
    others = [l["code"] for l in locales["locales"] if l["code"] != "(none)"]
    sec_rows = "".join(
        f"<tr><td><code>/{e(r['locale'])}/{e(r['section'])}/</code></td>"
        + "".join(f"<td>{_translation_cell(r['translations'].get(loc))}</td>" for loc in others if loc != r["locale"])
        + "</tr>" for r in locales["sections"])
    sec_head = "".join(f"<th>{e(loc)}</th>" for loc in others if loc not in locales["crawled_locales"][:1])
    switch_text = (f"Found in the {' and '.join(sw['locations'])}. {sw['to_alternate']} of {sw['links']} switcher links go to the "
                   f"page's hreflang alternate, {sw['to_home']} to a locale home page"
                   + (f", <b>{sw['mismatch_count']} to a different URL than hreflang</b> (see the SEO tab)" if sw["mismatch_count"] else "")
                   + ".") if sw["links"] else "No language switcher found."

    return f"""
<section>
  <h2>Locales</h2>
  <p class=muted>From all URLs the crawl came across (sitemaps and links), the hreflang tags and the language switcher.
  Crawled locale: {e(crawled)}.</p>
  <div class=scroll><table><thead><tr><th>Locale</th><th class=num>Known URLs</th><th class=num>Crawled</th>
  <th class=num>In hreflang of crawled pages</th><th>In language switcher</th></tr></thead><tbody>{rows}</tbody></table></div>
  <h3>Language setup</h3>
  <ul>
    <li><b>&lt;html lang&gt;:</b> {e(', '.join(f'{k} ({v})' for k, v in locales['html_lang'].items()))}</li>
    <li><b>hreflang:</b> on {hl['pages_with']} of {hl['pages']} crawled pages, values {e(', '.join(f'{k} ({v})' for k, v in hl['values'].items()))};
      self-reference on {hl['self_reference']}, x-default on {hl['x_default']}.
      {hl['targets_known']} of {hl['targets_total']} alternate URLs are known from the sitemap or crawl.</li>
    <li><b>Language switcher:</b> {switch_text}</li>
  </ul>
  <h3>Translated sections</h3>
  <p class=muted>First URL segment per locale, taken from the hreflang alternates of the crawled pages. Content modelling
  needs to decide whether these slugs are localised fields.</p>
  <div class=scroll><table><thead><tr><th>Section</th>{sec_head}</tr></thead><tbody>{sec_rows}</tbody></table></div>
</section>
{_nav_sections(navigation)}"""


def _nav_sections(navigation: dict) -> str:
    def nav_section(region: str, title: str) -> str:
        r = navigation[region]
        variants = (f"Identical on all {r['pages_with_main_variant']} crawled pages." if len(r["variants"]) == 1 else
                    f"{len(r['variants'])} variants; the one shown is used on {r['pages_with_main_variant']} pages. Other examples: "
                    + ", ".join(f'<a href="{e(v["example"])}" target="_blank" rel="noopener">{v["pages"]} pages</a>' for v in r["variants"][1:6]))
        issues = ""
        if r["placeholders"]:
            issues = '<p class=warn-text>Placeholder links: ' + ", ".join(e(l["label"]) for l in r["placeholders"]) + "</p>"
        return f"""
<section>
  <h2>{title}</h2>
  <div class=meta><span><b>{r['links']}</b> links</span><span><b>{r['internal']}</b> internal</span>
  <span><b>{len(r['external'])}</b> external</span><span>nesting depth <b>{r['depth']}</b></span></div>
  <p class=muted>{variants} Groups come from the page structure (lists, columns, menu panels); names in plain text are
  taken from the page, the others are derived from their links.</p>{issues}
  <div class=tree-wrap>{render_tree(r['tree']) if r['tree'] else '<p class=muted>Not found.</p>'}</div>
</section>"""

    return nav_section("header", "Header navigation") + nav_section("footer", "Footer navigation")


def _single_language(locales: dict) -> str:
    langs = ", ".join(f"{k} ({v} pages)" for k, v in locales["html_lang"].items())
    known = sum(l["known_urls"] for l in locales["locales"])
    return f"""
<section>
  <h2>Locales</h2>
  <p><b>Single-language site.</b> None of the {known} known URLs has a locale prefix, no crawled page has hreflang,
  and no language switcher was found. <code>&lt;html lang&gt;</code>: {e(langs)}.</p>
  <p class=muted>For the content model this means no localisation is needed today. Decide early whether fields should
  be localisable anyway, because adding locales later is a bigger change.</p>
</section>"""


def _translation_cell(t: dict | None) -> str:
    if not t:
        return '<span class=muted>–</span>'
    link = f'<a href="{e(t["example"])}" target="_blank" rel="noopener"><code>{e(t["section"] or "/")}</code></a>' if t.get("example") else f"<code>{e(t['section'])}</code>"
    return link + ("" if t["consistent"] else ' <span class="badge warn">varies</span>')


STATUS_ICON = {"pass": "✓", "warn": "!", "fail": "✕"}


def tab_seo(seo: dict) -> str:
    summary = seo["summary"]
    rows = []
    for c in sorted(seo["checks"], key=lambda c: {"fail": 0, "warn": 1, "pass": 2}[c["status"]]):
        result = ("OK" if c["status"] == "pass" else
                  f"{len(c['issues'])} of {c['total']} pages" if c["total"] > 1 else f"{len(c['issues'])} issue{'s' if len(c['issues']) != 1 else ''}")
        details = ""
        if c["issues"]:
            items = "".join(f'<li><a href="{e(u)}" target="_blank" rel="noopener">{e(u)}</a> <span class=muted>{e(str(d))}</span></li>'
                            if u.startswith("http") else f"<li>{e(u)} <span class=muted>{e(str(d))}</span></li>" for u, d in c["issues"][:200])
            more = f'<li class=muted>+{len(c["issues"]) - 200} more</li>' if len(c["issues"]) > 200 else ""
            details = f"<details><summary>Show {len(c['issues'])}</summary><ul class=issues>{items}{more}</ul></details>"
        info = f'<p class="muted small">{e(c["info"])}</p>' if c.get("info") and c["key"] != "robots" else ""
        if c["key"] == "robots" and c.get("info"):
            info = f'<details><summary>robots.txt</summary><pre class=robots>{e(c["info"])}</pre></details>'
        rows.append(f'<tr class="seo-{c["status"]}"><td class=status><span class="dot {c["status"]}" aria-label="{c["status"]}">{STATUS_ICON[c["status"]]}</span></td>'
                    f'<td><b>{e(c["name"])}</b><br><span class="muted small">{e(c["why"])}</span>{info}</td>'
                    f'<td class=result>{result}{details}</td></tr>')
    source = "fetched by the report" if seo["robots"].get("fetched_by_report") else "saved by the crawler"
    return f"""
<section>
  <h2>SEO quick check</h2>
  <p class=muted>Basic technical SEO on the {seo['pages']} crawled pages, plus robots.txt ({source}) and the sitemaps.
  A sample, not a full audit: other locales and sampled-out pages were not checked.</p>
  <div class=scorecards>
    <div class="score fail"><b>{summary.get('fail', 0)}</b> failed</div>
    <div class="score warn"><b>{summary.get('warn', 0)}</b> warnings</div>
    <div class="score pass"><b>{summary.get('pass', 0)}</b> passed</div>
  </div>
  <div class=scroll><table class=seo><thead><tr><th></th><th>Check</th><th>Result</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
</section>"""


CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}


def _conf(level: str) -> str:
    return f'<span class="badge conf-{e(level)}">{e(level)}</span>'


def _field_type(f: dict) -> str:
    ftype = ("list of " if f.get("list") else "") + f["type"]
    values = f.get("enum_values") or (f.get("values") if f["type"] == "taxonomy" else None)
    extra = f'<br><span class="muted small">{e(", ".join(values[:8]))}{" …" if len(values) > 8 else ""}</span>' if values else ""
    return f"<code>{e(ftype)}</code>{extra}"


def _fields_table(fields: list[dict]) -> str:
    if not fields:
        return "<p class=muted>No fixed fields: all content sits in the block zone.</p>"
    rows = "".join(
        f"<tr><td><code><b>{e(f['name'])}</b></code></td><td>{_field_type(f)}</td><td>{'yes' if f.get('required') else ''}</td>"
        f"<td>{_conf(f['confidence'])}</td><td class=small>{e(f['evidence'])}"
        + (f"<br><span class=warn-text>{e(f['review'])}</span>" if f.get("review") else "")
        + f"</td><td class='small examples'>{'<br>'.join(e(x[:70]) for x in f.get('examples', [])[:2])}</td></tr>" for f in fields)
    return (f"<div class=scroll><table class=model><thead><tr><th>Field</th><th>Type</th><th>Required</th><th>Confidence</th>"
            f"<th>Evidence</th><th>Examples</th></tr></thead><tbody>{rows}</tbody></table></div>")


def _coverage_bar(share) -> str:
    if share is None:
        return '<span class=muted>unknown</span>'
    return (f'<span class=bar role=img aria-label="{share:.0%} covered"><span style="width:{share * 100:.0f}%"></span></span> '
            f'{share:.0%}')


def _type_structure(t: dict) -> str:
    """Page tree: fixed head slots → block zone → fixed tail slots."""
    steps = [f"<span class=step>{e(x)}</span>" for x in t["slots"]["head"]]
    if t["zone"]:
        allowed = ", ".join(list(t["zone"]["allowed"])[:6]) + (" …" if len(t["zone"]["allowed"]) > 6 else "")
        steps.append(f'<span class="step zone">blocks: {e(allowed)}</span>')
    steps += [f"<span class=step>{e(x)}</span>" for x in t["slots"]["tail"]]
    return " <span class=arrow>→</span> ".join(steps) or '<span class=muted>no components found</span>'


def tab_content_model(model: dict | None) -> str:
    if not model:
        return ("<section><h2>Content model</h2><p class=muted>No content-model.json yet. Run "
                "<code>python content_model.py &lt;crawl folder&gt;</code> and build the report again.</p></section>")
    types, blocks = model["content_types"], model["blocks"]
    n_fields = sum(len(t["fields"]) for t in types)
    levels = Counter(f["confidence"] for t in types for f in t["fields"] if f["source"] == "dom diff")
    kinds = Counter(t["kind"] for t in types)
    cov = model["coverage"]["share"]
    scores = [("Content types", len(types)), ("Fields", n_fields), ("Blocks", len(blocks)), ("Relations", len(model["relations"])),
              ("Taxonomies", len(model["taxonomies"])), ("Globals", len(model["globals"]))]
    scorecards = "".join(f"<div class=score><b>{v}</b>{e(k)}</div>" for k, v in scores) + \
        (f"<div class=score><b>{cov:.0%}</b>Coverage</div>" if cov is not None else "")

    overview = "".join(
        f'<tr><td><a href="#model-{e(t["name"])}" data-open="model-{e(t["name"])}">{e(t["name"])}</a></td><td>{e(t["kind"])}</td>'
        f'<td class=num>{t["pages"]}</td><td class=num>{len(t["fields"])}</td>'
        f'<td>{e(", ".join(list((t["zone"] or {}).get("allowed", {}))[:4])) or "<span class=muted>none</span>"}</td>'
        f'<td>{"yes" + (" (slug too)" if t["localisation"]["slug_translated"] else "") if t["localisation"]["translated"] else ""}</td>'
        f'<td>{_coverage_bar(t["coverage"]["share"])}</td>'
        f'<td>{"<span class=warn-text>" + str(len(t["review"])) + "</span>" if t["review"] else ""}</td></tr>' for t in types)

    cards = []
    for t in types:
        notes = "".join(f"<li>{e(x)}</li>" for x in t["notes"])
        review = "".join(f"<li>{e(x)}</li>" for x in t["review"])
        zone = ""
        if t["zone"]:
            z = t["zone"]
            zone = (f"<h4>Block zone <code>blocks</code></h4><p class='muted small'>Used on {z['share']:.0%} of the pages, "
                    f"{z['lengths']['min']}–{z['lengths']['max']} blocks (median {z['lengths']['median']:g}). Allowed blocks and "
                    f"the share of pages that use them:</p>" + chips(f"{e(k)} <b>{v:.0%}</b>" for k, v in z["allowed"].items()))
        diff = t.get("diff") or {}
        diff_text = (f"DOM of {diff['pages']} pages aligned: {diff.get('template_texts', 0)} template texts, "
                     f"{diff.get('fields', 0)} fields." if diff.get("pages") else "Not enough pages to compare the DOM.")
        uncovered = t["coverage"]["uncovered"]
        examples = "".join(f'<li><a href="{e(x["url"])}" target="_blank" rel="noopener">{e(x["title"] or x["url"])}</a></li>'
                           for x in t["examples"])
        cards.append(f"""
<details class=card id="model-{e(t['name'])}">
  <summary><span class=title>{e(t['name'])}</span> <span class=badge>{e(t['kind'])}</span>
    <span class=muted>{t['pages']} pages · {len(t['fields'])} fields · coverage {'unknown' if t['coverage']['share'] is None else format(t['coverage']['share'], '.0%')}</span>
    {'<span class="badge warn">review</span>' if t['review'] else ''}</summary>
  <div class=card-body>
    <p class='muted small'>From page types {e(', '.join(t['page_types']))} · URL {', '.join(f'<code>{e(p)}</code>' for p in list(t['url_patterns'])[:4])}
    · {e(diff_text)}{' · translated' + (', slug translated' if t['localisation']['slug_translated'] else '') if t['localisation']['translated'] else ''}</p>
    <h4>Content structure</h4><div class=sequence>{_type_structure(t)}</div>
    <h4>Fields</h4>{_fields_table(t['fields'])}
    {zone}
    <p class='muted small'>Plus the shared <code>seo</code> component.</p>
    {f'<h4>Notes</h4><ul>{notes}</ul>' if notes else ''}
    {f'<h4>Review</h4><ul class=warn-list>{review}</ul>' if review else ''}
    {'<h4>Not covered by the model</h4>' + chips(e(x) for x in uncovered) if uncovered else ''}
    <h4>Example pages</h4><ul class=members>{examples}</ul>
  </div>
</details>""")

    block_cards = "".join(
        f"""<article class=block-def><h3>{e(b['name'])} {_conf(b['confidence'])}</h3><p class='muted small'>{e(b['description'])}</p>
<p class=small><b>{b['instances']}</b> instances on {b['pages']} pages · used by {e(', '.join(list(b['used_by'])[:4]))}</p>
<ul class=fieldlist>{''.join(f"<li><code>{e(f['name'])}</code> <span class=muted>{e(f['type'])}</span></li>" for f in b['fields']) or '<li class=muted>no fields (layout only)</li>'}</ul>
{('<p class=small><b>Style options:</b> ' + e('; '.join(f"{k}: {', '.join(v)}" for k, v in b['options'].items())) + '</p>') if b['options'] else ''}
</article>""" for b in blocks)

    rel_rows = "".join(f"<tr><td>{e(r['from'])}</td><td><code>{e(r['field'])}</code></td><td>→ <b>{e(r['to'])}</b></td>"
                       f"<td>{e(r['cardinality'])}</td><td class=num>{r['share']:.0%}</td></tr>" for r in model["relations"])
    tax_rows = "".join(f"<tr><td><b>{e(x['name'])}</b></td><td>{e(', '.join(x['used_by']))}</td>"
                       f"<td class=small>{e(', '.join(x['values'][:20]))}</td></tr>" for x in model["taxonomies"])
    enum_rows = "".join(f"<tr><td>{e(x['type'])}.<code>{e(x['field'])}</code></td><td class=small>{e(', '.join(x['values']))}</td></tr>"
                        for x in model["enums"])
    globals_html = "".join(
        f"<article class=block-def><h3>{e(g['name'])}</h3><p class='muted small'>{e(g['evidence'])}</p><ul class=fieldlist>"
        + "".join(f"<li><code>{e(f['name'])}</code> <span class=muted>{e(f['type'])}</span></li>" for f in g["fields"]) + "</ul></article>"
        for g in model["globals"])
    seo = model["components"][0]
    seo_html = ("<ul class=fieldlist>" + "".join(f"<li><code>{e(f['name'])}</code> <span class=muted>{e(f['type'])} · filled on "
                                                 f"{f['share']:.0%} of the pages</span></li>" for f in seo["fields"]) + "</ul>"
                + "".join(f"<p class='small warn-text'>{e(x)}</p>" for x in seo["notes"]))
    loc = model["locales"]
    loc_text = ("Single-language site: no localisation needed." if loc["single_language"] else
                f"Locales {e(', '.join(loc['all']))} (crawled: {e(', '.join(loc['crawled']))}). Translated content types: "
                f"{e(', '.join(loc['translated_types']) or 'none')}. Make text, rich text, media alt and slug fields localised.")

    return f"""
<section>
  <h2>Content model</h2>
  <p class=muted>A draft headless content model, derived by rules from the page types, components and DOM of the crawled pages
  (no AI). Every field shows its evidence and a confidence. Fields from the DOM comparison: {levels.get('high', 0)} high,
  {levels.get('medium', 0)} medium, {levels.get('low', 0)} low confidence. Coverage is the share of main-content words that fall in a
  recognised field or block. The same model is in <code>content-model.json</code> and <code>content-model.md</code>.</p>
  <div class=scorecards>{scorecards}</div>
  <div class=scroll><table><thead><tr><th>Content type</th><th>Kind</th><th class=num>Pages</th><th class=num>Fields</th>
  <th>Block zone</th><th>Translated</th><th>Coverage</th><th>Review</th></tr></thead><tbody>{overview}</tbody></table></div>
  <p class='muted small'>{', '.join(f'{n} {k}' for k, n in kinds.items())}. <b>collection</b>: many entries with their own URL;
  <b>single</b>: one page with its own structure; <b>page</b>: generic pages built from blocks.</p>
  <div class=cards>{''.join(cards)}</div>
</section>
<section>
  <h2>Relations</h2>
  <p class=muted>Links from one type's pages to another collection. "query" means the list is filled automatically (the same
  entries on most pages, or a listing), so it needs no editor field.</p>
  {f'<div class=scroll><table><thead><tr><th>From</th><th>Field</th><th>To</th><th>Cardinality</th><th class=num>Pages</th></tr></thead><tbody>{rel_rows}</tbody></table></div>' if rel_rows else '<p class=muted>No links between content types found.</p>'}
</section>
<section>
  <h2>Taxonomies and enums</h2>
  {f'<div class=scroll><table><thead><tr><th>Taxonomy</th><th>Used by</th><th>Values seen</th></tr></thead><tbody>{tax_rows}</tbody></table></div>' if tax_rows else '<p class=muted>No taxonomies found.</p>'}
  {f'<h3>Enums</h3><div class=scroll><table><thead><tr><th>Field</th><th>Values</th></tr></thead><tbody>{enum_rows}</tbody></table></div>' if enum_rows else ''}
</section>
<section>
  <h2>Block library</h2>
  <p class=muted>Every component type as a reusable block, with fields merged over all its instances. Style options are
  presentation choices (layout, background), not content.</p>
  <div class=block-grid>{block_cards}</div>
</section>
<section>
  <h2>Globals, SEO and locales</h2>
  <div class=block-grid>{globals_html}<article class=block-def><h3>Seo (component)</h3><p class='muted small'>On every routable type.</p>{seo_html}</article></div>
  <h3>Localisation</h3><p>{loc_text}</p>
</section>"""


REPORT_STYLE = """
.tabs { display:flex; gap:4px; border-bottom:1px solid var(--line); padding:0 24px; max-width:1280px; margin:0 auto; overflow-x:auto; }
.tabs button { font:inherit; background:none; border:0; border-bottom:3px solid transparent; padding:12px 14px; color:var(--muted); cursor:pointer; white-space:nowrap; }
.tabs button[aria-selected="true"] { color:var(--text); border-bottom-color:var(--accent); font-weight:600; }
.tabs button:focus-visible { outline:2px solid var(--accent); outline-offset:-2px; }
.tabs .count { font-size:12px; color:var(--muted); margin-left:4px; }
[role="tabpanel"][hidden] { display:none; }
.toolbar { display:flex; flex-wrap:wrap; gap:8px 16px; align-items:center; margin:12px 0; font-size:14px; }
.toolbar input[type=search] { font:inherit; padding:7px 10px; border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--text); min-width:260px; flex:1 1 260px; max-width:420px; }
.scroll { overflow-x:auto; }
td.num, th.num { text-align:right; white-space:nowrap; }
td.examples { font-size:12px; max-width:320px; word-break:break-all; }
#pattern-table tbody.pattern td { border-bottom:0; }
#pattern-table tbody.pattern { border-bottom:1px solid var(--line); }
#pattern-table tr.example-row td { padding-top:0; font-size:12px; overflow-wrap:anywhere; }
#pattern-table tr.example-row a { color:var(--accent); }
#pattern-table .sep { color:var(--muted); margin:0 4px; }
td.examples a, .members a, .small a { color:var(--accent); }
.cards { margin-top:16px; }
details.nested summary { cursor:pointer; margin-top:2px; }
details.nested ul { font-size:12px; max-height:240px; overflow:auto; }
details.card { border:1px solid var(--line); border-radius:8px; margin:8px 0; background:var(--panel); }
details.card summary { cursor:pointer; padding:12px 14px; display:flex; flex-wrap:wrap; gap:4px 12px; align-items:baseline; }
details.card summary .title { font-weight:600; }
.badge { font-size:12px; border-radius:4px; padding:1px 7px; background:var(--bg); border:1px solid var(--line); }
.card-body { padding:0 14px 14px; }
h4 { font-size:13px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); margin:14px 0 6px; }
.sequence { display:flex; flex-wrap:wrap; gap:4px; align-items:center; font-size:13px; }
.sequence .step { border:1px solid var(--line); border-radius:6px; padding:2px 8px; background:var(--bg); }
.sequence .arrow { color:var(--muted); }
.members { margin:0; padding-left:18px; font-size:14px; }
.components { display:grid; grid-template-columns:repeat(auto-fill, minmax(340px, 1fr)); gap:16px; margin-top:12px; }
article.component { border:1px solid var(--line); border-radius:10px; overflow:hidden; background:var(--panel); display:flex; flex-direction:column; }
.shot { background:var(--bg); border-bottom:1px solid var(--line); height:220px; overflow:hidden; display:flex; align-items:flex-start; }
.shot img { width:100%; height:auto; display:block; }  /* full block width; long blocks show their top part */
.noimg { padding:20px; }
.component-body { padding:14px 16px 16px; }
.component-body h3 { margin:0 0 4px; font-size:18px; }
.component-body p { margin:4px 0; }
.small { font-size:13px; }
.chips.fields span { font-size:12px; }
.tree-wrap { margin-top:10px; font-size:14px; }
ul.tree { list-style:none; margin:4px 0 4px 8px; padding-left:16px; border-left:1px solid var(--line); }
.tree-wrap > ul.tree { border-left:0; padding-left:0; margin-left:0; }
ul.tree li { margin:3px 0; }
ul.tree li.link > span { font-weight:500; }
ul.tree summary { cursor:pointer; }
ul.tree code { font-size:12px; }
.badge.ext { color:var(--accent); }
.badge.warn, .warn-text { color:var(--warn); }
.scorecards { display:flex; gap:12px; flex-wrap:wrap; margin:12px 0 16px; }
.score { border:1px solid var(--line); border-radius:8px; padding:10px 16px; min-width:120px; background:var(--bg); }
.score b { font-size:24px; display:block; }
.score.fail b { color:#c0392b; } .score.warn b { color:var(--warn); } .score.pass b { color:#2e8b57; }
table.seo td.status { width:32px; }
.dot { display:inline-flex; width:24px; height:24px; border-radius:50%; align-items:center; justify-content:center; font-weight:700; font-size:13px; color:#fff; }
.dot.pass { background:#2e8b57; } .dot.warn { background:#d08a2e; } .dot.fail { background:#c0392b; }
table.seo td.result { width:38%; }
ul.issues { margin:6px 0; padding-left:18px; font-size:13px; max-height:320px; overflow:auto; word-break:break-all; }
ul.issues a { color:var(--accent); }
pre.robots { font-size:12px; background:var(--bg); padding:8px; border-radius:6px; overflow:auto; }
.badge.conf-high { color:#2e8b57; } .badge.conf-medium { color:var(--warn); } .badge.conf-low { color:#c0392b; }
table.model td { font-size:13px; } table.model td.examples { max-width:260px; word-break:break-word; }
.bar { display:inline-block; width:70px; height:8px; border-radius:4px; background:var(--line); vertical-align:middle; overflow:hidden; }
.bar span { display:block; height:100%; background:var(--accent); }
.sequence .step.zone { border-style:dashed; }
.block-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(280px, 1fr)); gap:12px; margin-top:12px; }
article.block-def { border:1px solid var(--line); border-radius:8px; padding:12px 14px; background:var(--bg); }
article.block-def h3 { margin:0 0 4px; font-size:16px; }
ul.fieldlist { margin:6px 0; padding-left:18px; font-size:13px; }
ul.warn-list li { color:var(--warn); }
@media (max-width:600px) { .tabs { padding:0 16px; } .components { grid-template-columns:1fr; } .toolbar input[type=search] { min-width:0; } }
"""

REPORT_SCRIPT = """
const tabs = [...document.querySelectorAll('[role="tab"]')];
const selectTab = (tab, focus) => {
  tabs.forEach(t => {
    const on = t === tab;
    t.setAttribute('aria-selected', on);
    t.tabIndex = on ? 0 : -1;
    document.getElementById(t.getAttribute('aria-controls')).hidden = !on;
  });
  if (focus) tab.focus();
  history.replaceState(null, '', '#' + tab.dataset.name);
};
tabs.forEach((tab, i) => {
  tab.addEventListener('click', () => selectTab(tab, false));
  tab.addEventListener('keydown', ev => {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[ev.key];
    if (step) { ev.preventDefault(); selectTab(tabs[(i + step + tabs.length) % tabs.length], true); }
    if (ev.key === 'Home') { ev.preventDefault(); selectTab(tabs[0], true); }
    if (ev.key === 'End') { ev.preventDefault(); selectTab(tabs[tabs.length - 1], true); }
  });
});
selectTab(tabs.find(t => '#' + t.dataset.name === location.hash) || tabs[0], false);

const patternRows = [...document.querySelectorAll('#pattern-table tbody.pattern')];
const filterPatterns = () => {
  const q = document.getElementById('pattern-search').value.trim().toLowerCase();
  const on = new Set([...document.querySelectorAll('[data-filter]')].filter(c => c.checked).map(c => c.dataset.filter));
  let shown = 0;
  patternRows.forEach(r => {
    const visible = on.has(r.dataset.status) && (!q || r.dataset.search.includes(q));
    r.hidden = !visible;
    shown += visible;
  });
  document.getElementById('pattern-count').textContent = `${shown} of ${patternRows.length} patterns shown`;
};
document.getElementById('pattern-search').addEventListener('input', filterPatterns);
document.querySelectorAll('[data-filter]').forEach(c => c.addEventListener('change', filterPatterns));
filterPatterns();

document.querySelectorAll('[data-open]').forEach(a => a.addEventListener('click', () => {
  document.getElementById(a.dataset.open).open = true;
}));

const componentCards = [...document.querySelectorAll('article.component')];
document.getElementById('component-search').addEventListener('input', ev => {
  const q = ev.target.value.trim().toLowerCase();
  componentCards.forEach(c => { c.hidden = q && !c.textContent.toLowerCase().includes(q); });
});
"""


def main():
    ap = argparse.ArgumentParser(description="Build an HTML report from all analysis phases.")
    ap.add_argument("crawl_dir", type=Path, help="Crawl output folder, e.g. output/extremis")
    ap.add_argument("--offline", action="store_true", help="Don't fetch robots.txt when the crawl has no site.json")
    args = ap.parse_args()
    d = args.crawl_dir

    missing = [n for n in ("inventory.csv", "clusters.csv", "page-types.json", "components.json") if not (d / n).exists()]
    if missing:
        sys.exit(f"Missing in {d}: {', '.join(missing)}. Run crawl.py, cluster.py, name_types(_rules).py and components.py first.")

    types_data = json.loads((d / "page-types.json").read_text(encoding="utf-8"))
    page_types = types_data["page_types"]
    names = {t["cluster"]: t["name"] or f"cluster {t['cluster']}" for t in page_types}
    page_type_of = {r["id"]: names.get(int(r["cluster"]), "unclassified") for r in read_csv(d / "clusters.csv")}
    components = json.loads((d / "components.json").read_text(encoding="utf-8"))
    model_file = d / "content-model.json"
    model = json.loads(model_file.read_text(encoding="utf-8")) if model_file.exists() else None

    # Pages with screenshots (for crops) joined with their classified blocks
    blocks_by_id = {p["id"]: p for p in components["pages"]}
    pages = []
    for p in load_pages(d):
        if p["id"] in blocks_by_id:
            p["page_type"], p["blocks"] = blocks_by_id[p["id"]]["page_type"], blocks_by_id[p["id"]]["blocks"]
            pages.append(p)

    patterns = collect_patterns(d, page_type_of)
    estimate = estimate_site_pages(page_types, patterns)
    site = urlparse(next(iter(blocks_by_id.values()))["url"]).netloc if blocks_by_id else d.name
    all_pages = load_pages(d)
    navigation = analyse_navigation(all_pages, site)
    locales = analyse_locales(d, all_pages, navigation)
    seo = seo_checks(d, all_pages, navigation, locales, offline=args.offline)

    n_locales = len([l for l in locales["locales"] if l["code"] != "(none)"]) or len(locales["html_lang"])
    panels = [
        ("site", "Site", n_locales, tab_site(locales, navigation)),
        ("patterns", "URL patterns", len(rows := tree_rows(d, patterns)), tab_patterns(rows, other_locale_urls(d))),
        ("page-types", "Page types", len(page_types), tab_page_types(page_types, components["page_structures"], estimate, types_data.get("named_by", "unknown"))),
        ("components", "Components", len(components["catalogue"]), tab_components(components["catalogue"], pages, d)),
        ("content-model", "Content model", len(model["content_types"]) if model else 0, tab_content_model(model)),
        ("seo", "SEO", seo["summary"].get("fail", 0) + seo["summary"].get("warn", 0), tab_seo(seo)),
    ]
    tablist = "".join(f'<button role=tab id="tab-{n}" data-name="{n}" aria-controls="panel-{n}" aria-selected="false" tabindex="-1">'
                      f'{label}<span class=count>{count}</span></button>' for n, label, count, _ in panels)
    body = "".join(f'<div role=tabpanel id="panel-{n}" aria-labelledby="tab-{n}" tabindex="0" hidden><main>{content}</main></div>'
                   for n, _, _, content in panels)
    page = (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content=\"width=device-width, initial-scale=1\">"
            f"<title>Site analysis: {e(site)}</title><style>{HTML_STYLE}{REPORT_STYLE}</style></head><body>"
            f"<header class=top><h1>Site analysis: {e(site)}</h1><p class=muted>{len(pages)} pages crawled · "
            f"{n_locales} locale{'s' if n_locales != 1 else ''} · "
            f"{len(rows)} URL patterns · {len(page_types)} page types · {len(components['catalogue'])} component types</p></header>"
            f'<nav class=tabs role=tablist aria-label="Report sections">{tablist}</nav>{body}<script>{REPORT_SCRIPT}</script></body></html>')
    (d / "report.html").write_text(page, encoding="utf-8")
    print(f"Wrote {d / 'report.html'} ({(d / 'report.html').stat().st_size // 1024} KB): "
          f"{len(rows)} URL patterns, {len(page_types)} page types, {len(components['catalogue'])} components")


if __name__ == "__main__":
    main()
