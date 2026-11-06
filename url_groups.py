"""
Group URL patterns whose nested segments are really slugs.

crawl.url_pattern() only turns the last segment into a placeholder, so nested routes give one pattern per
parent: /collections/amanu/{slug}, /collections/elio/{slug}, ... After the crawl, with every known URL in
hand, this module groups them:

  - 3 or more patterns that differ only in one middle segment become one pattern with a placeholder there:
    /collections/{group}/{slug}. Repeated until nothing changes, so trees collapse level by level:
    /onderwijsniveaus/{group}/{subgroup}/{slug}. A literal next to an already grouped branch joins it.
  - Placeholders are named by depth under the section ({group}, {subgroup}), so sibling branches end up
    with the same pattern. The section itself and the last segment are never grouped, so translated
    sections (/collections, /collecties) stay apart.
  - 10 or more root-level URLs with the same file extension become /{slug}.html (shop themes with flat
    product URLs).

Each grouped pattern also gets a role in the URL tree (PatternGroups.tree()):
  - root      the home page: / or /{locale}
  - section   a fixed page with a URL branch below it: /{locale}/collections is the parent of
              /{locale}/collections/{slug} and /{locale}/collections/{group}/{slug} (also /cases for /case/{slug})
  - landing   a fixed page right under the root without anything below it (/about, /contact): grouped as
              "landing pages" in the report
  - child     a fixed page deeper in the tree (/about/team), under its parent
  - template  a pattern with placeholders (/collections/{slug}), under its section page when there is one

Used by common.load_pages (every analysis step sees grouped patterns) and by report.py (URL patterns tab).
"""

import csv
import re
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from crawl import url_pattern

MIN_SIBLINGS = 3
MIN_FLAT = 10
NAMES = ["group", "subgroup", "subgroup2", "subgroup3"]
FILE_EXT = re.compile(r"\.\w{2,5}$")


def _segments(pattern: str) -> tuple[list[str], int]:
    segs = pattern.strip("/").split("/")
    return segs, (1 if segs and segs[0] == "{locale}" else 0)


def _placeholder(depth: int) -> str:
    return "{" + NAMES[min(depth, len(NAMES) - 1)] + "}"


def group_patterns(counts: Counter) -> dict[str, str]:
    """Raw pattern -> grouped pattern (the same pattern when nothing applies)."""
    mapping = {p: p for p in counts}
    changed = True
    while changed:
        changed = False
        buckets, existing = defaultdict(set), {}
        for p in set(mapping.values()):
            segs, start = _segments(p)
            for i in range(start + 1, len(segs) - 1):          # never the section, never the last segment
                key = (tuple(segs[:i]), i, tuple(segs[i + 1:]))
                if not segs[i].startswith("{"):
                    buckets[key].add(segs[i])
                elif segs[i] != "{slug}":
                    existing[key] = segs[i]
        for (head, i, tail), literals in buckets.items():
            if len(literals) < MIN_SIBLINGS and (head, i, tail) not in existing:
                continue
            start = 1 if head and head[0] == "{locale}" else 0
            ph = existing.get((head, i, tail)) or _placeholder(i - start - 1)
            for raw, current in mapping.items():
                segs, _ = _segments(current)
                if len(segs) == len(head) + 1 + len(tail) and tuple(segs[:i]) == head and tuple(segs[i + 1:]) == tail \
                        and segs[i] in literals:
                    mapping[raw] = "/" + "/".join(list(head) + [ph] + list(tail))
                    changed = True

    # Flat pages with a file extension at the root (/some-title.html)
    flat = Counter()
    for raw in counts:
        segs, start = _segments(mapping[raw])
        if len(segs) == start + 1 and FILE_EXT.search(segs[-1]):
            flat[(tuple(segs[:start]), segs[-1].rsplit(".", 1)[1].lower())] += counts[raw]
    for raw in counts:
        segs, start = _segments(mapping[raw])
        if len(segs) == start + 1 and FILE_EXT.search(segs[-1]):
            ext = segs[-1].rsplit(".", 1)[1].lower()
            if flat[(tuple(segs[:start]), ext)] >= MIN_FLAT:
                mapping[raw] = "/" + "/".join(segs[:start] + ["{slug}." + ext])
    return mapping


class PatternGroups:
    """Grouped patterns for one crawl; also groups patterns of URLs the crawl never saw (links)."""

    def __init__(self, counts: Counter):
        self.mapping = group_patterns(counts)
        self.grouped = sorted(set(self.mapping.values()), key=lambda p: -p.count("{"))
        self.members = defaultdict(list)
        for raw, g in self.mapping.items():
            if raw != g:
                self.members[g].append(raw)

    def __call__(self, pattern: str) -> str:
        if pattern in self.mapping:
            return self.mapping[pattern]
        segs, _ = _segments(pattern)
        for g in self.grouped:
            gsegs, _ = _segments(g)
            if len(gsegs) == len(segs) and g != pattern and all(
                    a == b or (a.startswith("{") and a not in ("{locale}",)) for a, b in zip(gsegs, segs)):
                return g
        return pattern

    def of_url(self, url: str) -> str:
        return self(url_pattern(url))

    def tree(self) -> dict[str, dict]:
        return build_tree(sorted(set(self.mapping.values())))


def _singular(word: str) -> str:
    for end, repl in (("ies", "y"), ("sses", "ss"), ("s", "")):
        if word.endswith(end) and len(word) > len(end) + 2 and not word.endswith(("ss", "us", "ws")):
            return word[: -len(end)] + repl
    return word


def _is_fixed(pattern: str) -> bool:
    return "{" not in pattern.replace("{locale}", "")


def build_tree(patterns: list[str]) -> dict[str, dict]:
    """pattern -> {"role", "parent", "children"} for grouped patterns (see the module docstring)."""
    tree = {p: {"role": "template", "parent": None, "children": []} for p in patterns}
    fixed = [p for p in patterns if _is_fixed(p)]
    for p in fixed:
        segs, start = _segments(p)
        if len(segs) <= start or p == "/":
            tree[p]["role"] = "root"
    # The closest fixed ancestor of every pattern is its parent: /a/b/{slug} sits under /a/b, else under /a
    for p in patterns:
        segs, start = _segments(p)
        for cut in range(len(segs) - 1, start, -1):
            head = segs[:cut]
            candidates = ["/" + "/".join(head)]
            if cut == start + 1:          # a section listed under its plural: /case/{slug} under /cases
                candidates += ["/" + "/".join(head[:-1] + [head[-1] + "s"]), "/" + "/".join(head[:-1] + [head[-1] + "es"])]
            parent = next((c for c in candidates if c in tree and c != p and tree[c]["role"] != "root"), None)
            if parent:
                tree[p]["parent"] = parent
                tree[parent]["children"].append(p)
                break
    for p in fixed:
        if tree[p]["role"] == "root":
            continue
        segs, start = _segments(p)
        if tree[p]["children"]:
            tree[p]["role"] = "section"
        elif tree[p]["parent"]:
            tree[p]["role"] = "child"
        else:
            tree[p]["role"] = "landing" if len(segs) == start + 1 else "child"
    return tree


def landing_label(pattern: str) -> str:
    """The grouped row for landing pages: /{locale}/{landing page} or /{landing page}."""
    segs, start = _segments(pattern)
    return "/" + "/".join(segs[:start] + ["{landing page}"])


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


LOCALE = re.compile(r"^[a-z]{2}([-_][a-z]{2})?$", re.I)


def url_locale(url: str) -> str:
    first = urlparse(url).path.strip("/").split("/")[0]
    return first.lower() if LOCALE.match(first) else ""


@lru_cache(maxsize=8)
def crawled_locales(crawl_dir: str) -> frozenset:
    """Locale prefixes of the crawled pages (empty for a site without locale prefixes)."""
    return frozenset(loc for r in _read(Path(crawl_dir) / "inventory.csv") if (loc := url_locale(r.get("url", ""))))


def in_scope(crawl_dir: Path, url: str) -> bool:
    """The URL structure is mapped for the crawled locale only: other locales' URLs are translations of it."""
    locales = crawled_locales(str(Path(crawl_dir).resolve()))
    return not locales or url_locale(url) in locales


@lru_cache(maxsize=8)
def _groups_for(crawl_dir: str) -> PatternGroups:
    d = Path(crawl_dir)
    counts = Counter(url_pattern(r["url"]) for name in ("inventory.csv", "skipped.csv") for r in _read(d / name)
                     if r.get("url") and in_scope(d, r["url"]))
    return PatternGroups(counts)


def pattern_groups(crawl_dir: Path) -> PatternGroups:
    """Grouping for a crawl folder, from every URL of the crawled locale in inventory.csv and skipped.csv."""
    return _groups_for(str(Path(crawl_dir).resolve()))
