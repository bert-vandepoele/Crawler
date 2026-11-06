#!/usr/bin/env python3
"""
Page-type clustering (Phase 2a)

Groups crawled pages by DOM structure, independent of their URLs, so templates
that share a layout end up together even when their URLs differ (and URL
patterns that hide several templates get split).

Usage:
    python cluster.py output/extremis --threshold 0.3

Output (in the crawl folder):
    clusters.json   clusters with size, URL patterns, members and representative samples
    clusters.csv    one row per page: page id, URL, pattern, cluster
"""

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

import numpy as np
from sklearn.cluster import AgglomerativeClustering
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import silhouette_score
from sklearn.metrics.pairwise import cosine_distances

from common import load_pages, main_subtree


def fingerprint_doc(lines: list[str]) -> str:
    """Tokens are "depth:tag.class" nodes plus parent>child pairs, which capture nesting."""
    tokens, stack = [], []
    for line in lines:
        depth_str, _, node = line.partition(":")
        depth = int(depth_str)
        del stack[depth:]
        tokens.append(f"{depth}:{node}")
        if stack:
            tokens.append(f"{stack[-1]}>{node}")
        stack.append(node)
    return " ".join(tokens)


def pick_samples(idx: list[int], dist: np.ndarray, patterns: list[str], n: int) -> list[int]:
    """Medoid first, then the members farthest from those already picked (favouring unseen URL patterns)."""
    sub = dist[np.ix_(idx, idx)]
    chosen = [idx[int(sub.sum(axis=1).argmin())]]
    while len(chosen) < min(n, len(idx)):
        seen_patterns = {patterns[c] for c in chosen}
        best = max(
            (i for i in idx if i not in chosen),
            key=lambda i: (patterns[i] not in seen_patterns, min(dist[i, c] for c in chosen)),
        )
        chosen.append(best)
    return chosen


def is_template_pattern(pattern: str) -> bool:
    """True for URL patterns that stand for many pages, e.g. /insights/{slug} (a {locale} prefix alone doesn't count)."""
    return any(seg.startswith("{") and seg != "{locale}" for seg in pattern.strip("/").split("/"))


def merge_by_template(labels: np.ndarray, patterns: list[str]) -> np.ndarray:
    """Clusters made of one templated URL pattern belong together: /insights/{slug} is one route, so one
    page type, even when some articles have an extra block. Their differences are kept as layout variants."""
    by_pattern = {}
    merged = labels.copy()
    for label in sorted(set(labels)):
        members = [patterns[i] for i in np.where(labels == label)[0]]
        pattern, n = Counter(members).most_common(1)[0]
        if is_template_pattern(pattern) and n / len(members) >= 0.8:
            target = by_pattern.setdefault(pattern, label)
            merged[labels == label] = target
    return merged


def main():
    ap = argparse.ArgumentParser(description="Cluster crawled pages into candidate page types.")
    ap.add_argument("crawl_dir", type=Path, help="Crawl output folder, e.g. output/extremis")
    ap.add_argument("--threshold", type=float, default=0.3,
                    help="Cosine distance at which clusters stop merging; lower = more, tighter clusters (default: 0.3)")
    ap.add_argument("--whole-page", action="store_true",
                    help="Fingerprint the whole <body> instead of only <main> (use when headers/footers differ per type)")
    ap.add_argument("--samples", type=int, default=5, help="Representative pages per cluster (default: 5)")
    ap.add_argument("--split-layouts", action="store_true",
                    help="Keep layout variants of one URL template (e.g. /insights/{slug}) as separate clusters")
    args = ap.parse_args()

    pages = load_pages(args.crawl_dir)
    if len(pages) < 2:
        raise SystemExit(f"Need at least 2 crawled pages in {args.crawl_dir}/pages")

    select = (lambda lines: lines) if args.whole_page else main_subtree
    selected = [select(p["fingerprint_lines"]) for p in pages]
    if not args.whole_page and sum(1 for lines in selected if lines) < len(pages) / 2:
        # <main> is empty in most fingerprints (e.g. a crawl made before <main> got its own depth budget):
        # nothing to compare there, so compare whole pages instead
        print("  <main> is empty in most page fingerprints; comparing whole pages instead")
        selected = [p["fingerprint_lines"] for p in pages]
    docs = [fingerprint_doc(lines) for lines in selected]
    try:
        X = TfidfVectorizer(tokenizer=str.split, token_pattern=None, lowercase=False, sublinear_tf=True,
                            min_df=2).fit_transform(docs)
    except ValueError:
        # No structure shared by 2+ pages (tiny or very diverse site): use every node, even unique ones
        X = TfidfVectorizer(tokenizer=str.split, token_pattern=None, lowercase=False, sublinear_tf=True,
                            min_df=1).fit_transform(doc or "empty" for doc in docs)
    dist = cosine_distances(X)

    model = AgglomerativeClustering(
        n_clusters=None, distance_threshold=args.threshold, metric="precomputed", linkage="average"
    )
    labels = model.fit_predict(dist)
    patterns = [p["pattern"] for p in pages]
    layouts = labels.copy()
    if not args.split_layouts:
        labels = merge_by_template(labels, patterns)

    # Number clusters by size so cluster 0 is the biggest
    order = [label for label, _ in Counter(labels).most_common()]
    renumber = {old: new for new, old in enumerate(order)}
    labels = np.array([renumber[label] for label in labels])

    clusters = []
    for cid in range(len(order)):
        idx = [i for i, label in enumerate(labels) if label == cid]
        sub = dist[np.ix_(idx, idx)]
        clusters.append({
            "id": cid,
            "size": len(idx),
            "cohesion": round(1 - float(sub.mean()), 3),
            "layout_variants": len({layouts[i] for i in idx}),
            "patterns": dict(Counter(patterns[i] for i in idx).most_common()),
            "schema_types": sorted({t for i in idx for t in pages[i]["schema_types"]}),
            "samples": [pages[i]["id"] for i in pick_samples(idx, dist, patterns, args.samples)],
            "members": [{"id": pages[i]["id"], "url": pages[i]["final_url"], "title": pages[i]["title"]} for i in idx],
        })

    sil = silhouette_score(dist, labels, metric="precomputed") if 1 < len(order) < len(pages) else None
    result = {
        "crawl_dir": str(args.crawl_dir),
        "threshold": args.threshold,
        "pages": len(pages),
        "silhouette": round(float(sil), 3) if sil is not None else None,
        "clusters": clusters,
    }
    (args.crawl_dir / "clusters.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    with open(args.crawl_dir / "clusters.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "url", "pattern", "cluster"])
        for p, label in zip(pages, labels):
            w.writerow([p["id"], p["final_url"], p["pattern"], int(label)])

    singletons = sum(1 for c in clusters if c["size"] == 1)
    print(f"{len(pages)} pages -> {len(clusters)} clusters ({singletons} singletons), "
          f"silhouette {result['silhouette']} at threshold {args.threshold}\n")
    for c in clusters:
        top = ", ".join(f"{pat} ×{n}" for pat, n in list(c["patterns"].items())[:3])
        more = f" +{len(c['patterns']) - 3} more" if len(c["patterns"]) > 3 else ""
        variants = f"  ({c['layout_variants']} layout variants)" if c["layout_variants"] > 1 else ""
        print(f"  [{c['id']:>2}] {c['size']:>3} pages  cohesion {c['cohesion']:.2f}  {top}{more}{variants}")
    print(f"\nWrote {args.crawl_dir / 'clusters.json'} and clusters.csv")


if __name__ == "__main__":
    main()
