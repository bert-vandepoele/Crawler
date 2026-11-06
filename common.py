"""Shared helpers for the analysis phases: loading crawl output and stripping page chrome."""

import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

# Cookie/consent widgets sit on every page and drown out the real structure
CONSENT = re.compile(r"cybot|cookie|consent|onetrust|usercentrics|didomi|truste|iubenda", re.I)
CHROME_TAGS = ["header", "nav", "footer", "aside", "form"]


def load_pages(crawl_dir: Path) -> list[dict]:
    """Return page.json records, each with its fingerprint lines attached, sorted by URL.

    page["pattern"] is the grouped URL pattern (nested slugs grouped, see url_groups.py);
    page["pattern_raw"] keeps the pattern the crawler recorded."""
    from url_groups import pattern_groups   # imported here: url_groups imports crawl, which is heavy

    groups = pattern_groups(crawl_dir)
    pages = []
    for pdir in sorted((crawl_dir / "pages").iterdir()):
        meta_file = pdir / "page.json"
        if not meta_file.exists():
            continue
        page = json.loads(meta_file.read_text(encoding="utf-8"))
        page["dir"] = pdir
        page["pattern_raw"] = page.get("pattern") or ""
        page["pattern"] = groups(page["pattern_raw"]) if page["pattern_raw"] else page["pattern_raw"]
        page["fingerprint_lines"] = clean_fingerprint((pdir / "fingerprint.txt").read_text(encoding="utf-8").splitlines())
        pages.append(page)
    return sorted(pages, key=lambda p: p["final_url"])


def screenshot_path(page: dict) -> Path | None:
    """The page's full-page screenshot: screenshot.jpg, or screenshot.png from older crawls."""
    return next((p for p in (page["dir"] / "screenshot.jpg", page["dir"] / "screenshot.png") if p.exists()), None)


def clean_fingerprint(lines: list[str]) -> list[str]:
    """Drop consent-widget subtrees from a depth-prefixed fingerprint ("3:div.card")."""
    out, skip_below = [], None
    for line in lines:
        depth_str, _, node = line.partition(":")
        depth = int(depth_str)
        if skip_below is not None:
            if depth > skip_below:
                continue
            skip_below = None
        if CONSENT.search(node):
            skip_below = depth
            continue
        out.append(line)
    return out


def main_subtree(lines: list[str]) -> list[str]:
    """Fingerprint lines inside <main>, re-based to depth 0; the whole page when there is no <main>."""
    for i, line in enumerate(lines):
        depth_str, _, node = line.partition(":")
        if node == "main" or node.startswith("main."):
            base = int(depth_str) + 1
            out = []
            for child in lines[i + 1:]:
                d, _, n = child.partition(":")
                if int(d) < base:
                    break
                out.append(f"{int(d) - base}:{n}")
            return out
    return lines


def main_text(page: dict, limit: int = 2000) -> str:
    """Visible text of the page body without consent widgets and site chrome."""
    soup = BeautifulSoup((page["dir"] / "clean.html").read_text(encoding="utf-8"), "html.parser")
    for el in soup.find_all(attrs={"id": CONSENT}) + soup.find_all(attrs={"class": CONSENT}):
        el.decompose()
    root = soup.find("main") or soup.body or soup
    for el in root.find_all(CHROME_TAGS):
        el.decompose()
    text = re.sub(r"\n\s*\n+", "\n", root.get_text("\n", strip=True))
    return text[:limit] + (" […]" if len(text) > limit else "")


def write_reports(crawl_dir: Path, clusters: list[dict], results: dict, source: str):
    """Write page-types.json and page-types.md from clusters plus a naming result per cluster id."""
    page_types = []
    for c in clusters:
        named = results.get(c["id"])
        page_types.append({"cluster": c["id"], "size": c["size"], "patterns": c["patterns"],
                           **(named or {"name": None}), "samples": c["samples"], "members": c["members"]})
    (crawl_dir / "page-types.json").write_text(
        json.dumps({"named_by": source, "page_types": page_types}, indent=2, ensure_ascii=False), encoding="utf-8")

    md = ["# Page types", "", f"Named by {source} from `clusters.json`. Review and correct this file before Phase 3.", ""]
    md += ["| Cluster | Name | Kind | Pages | Entity | Recognised as | Confidence |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for t in page_types:
        md.append(f"| {t['cluster']} | {t['name'] or '(not named)'} | {t.get('kind', '')} | {t['size']} | "
                  f"{t.get('domain_entity') or ''} | {(t.get('recognised_as') or {}).get('type', '')} | {t.get('confidence', '')} |")
    for t in page_types:
        if not t["name"]:
            continue
        md += ["", f"## {t['cluster']}. {t['name']} ({t['kind']}, {t['size']} pages)", "", t["description"], "",
               f"**Purpose:** {t['purpose']}", "", "**URL patterns:** " + ", ".join(f"`{p}` ×{n}" for p, n in t["patterns"].items()), "",
               "**Content regions:**", ""]
        for r in t["content_regions"]:
            flags = ", ".join(f for f, on in (("repeating", r["repeating"]), ("varies", r["varies_between_samples"])) if on)
            md.append(f"- **{r['name']}**{f' ({flags})' if flags else ''}: {r['description']}")
        if t["merge_candidates"]:
            md += ["", "**Possibly the same type as clusters:** " + ", ".join(map(str, t["merge_candidates"]))]
        if t["split_suggestion"]:
            md += ["", f"**Split suggestion:** {t['split_suggestion']}"]
        if t["anomalies"]:
            md += ["", "**Anomalies:**", ""] + [f"- {a}" for a in t["anomalies"]]
        if t.get("recognised_as"):
            r = t["recognised_as"]
            md += ["", f"**Recognised as:** {r['type']} ({r['confidence']}, {r['share']:.0%} of its pages)"
                   + (f"; evidence: {'; '.join(r['evidence'])}" if r.get("evidence") else "")]
        if t["notes"]:
            md += ["", f"**Notes:** {t['notes']}"]
    (crawl_dir / "page-types.md").write_text("\n".join(md) + "\n", encoding="utf-8")
