#!/usr/bin/env python3
"""
Content-model crawler (proof of concept)

Phase 1 of the "headless content model from an existing website" workflow.
Renders every page with a headless browser and stores what later phases need:
cleaned HTML, visible text, screenshot, metadata, JSON-LD, a DOM fingerprint
and the URL pattern.

Usage:
    python crawl.py https://www.example.com                      # every page
    python crawl.py https://www.example.com --max-per-pattern 5  # a sample per URL pattern

Output (in --out, default ./output):
    pages/<page-id>/page.json        metadata, JSON-LD, headings, fingerprint
    pages/<page-id>/clean.html       DOM without scripts, styles and noise attributes
    pages/<page-id>/text.txt         visible text
    pages/<page-id>/screenshot.jpg   full-page screenshot
    inventory.csv                    one row per crawled page
    patterns.csv                     URL patterns with page counts and examples
    skipped.csv                      URLs not crawled, with the reason
"""

import argparse
import asyncio
import csv
import gzip
import hashlib
import json
import re
import socket
import ssl
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib import robotparser
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from xml.etree import ElementTree

from playwright.async_api import async_playwright

USER_AGENT = "ContentModelCrawler/0.1 (+content audit; contact: you@example.com)"

TRACKING_PARAMS = re.compile(r"^(utm_.*|gclid|fbclid|msclkid|mc_cid|mc_eid|_ga|_gl|ref|source)$", re.I)
SKIP_EXTENSIONS = re.compile(
    r"\.(pdf|jpe?g|png|gif|webp|svg|ico|css|js|json|xml|zip|gz|mp4|mp3|webm|woff2?|ttf|docx?|xlsx?|pptx?)$", re.I
)
SKIP_PATHS = re.compile(r"/(wp-admin|wp-login|cart|checkout|account|login|logout|search)(/|$)", re.I)

# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def normalize_url(url: str, keep_query: bool) -> str:
    """Strip fragments and tracking parameters; sort the remaining query."""
    p = urlparse(url)
    query = ""
    if keep_query:
        pairs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)]
        query = urlencode(sorted(pairs))
    path = p.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", query, ""))


def url_pattern(url: str) -> str:
    """Turn /blog/2024/my-first-post into /blog/{year}/{slug} (heuristic).

    Also handles /products/{slug}/{id} and ignores a leading locale when deciding
    whether a page sits inside a section (/en/about-us stays a page of its own).
    """
    segments = [s for s in urlparse(url).path.split("/") if s]
    out = []
    for i, seg in enumerate(segments):
        if re.fullmatch(r"(19|20)\d{2}", seg):
            out.append("{year}")
        elif re.fullmatch(r"\d+", seg):
            out.append("{id}")
        elif re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", seg, re.I):
            out.append("{uuid}")
        elif re.fullmatch(r"[a-z]{2}(-[a-z]{2})?", seg, re.I) and i == 0:
            out.append("{locale}")
        else:
            out.append(seg.lower())

    section_start = 1 if out[:1] == ["{locale}"] else 0
    for i in range(section_start + 1, len(out)):
        is_last = i == len(out) - 1
        before_id = not is_last and out[i + 1] in ("{id}", "{uuid}")
        if not out[i].startswith("{") and (is_last or before_id):
            out[i] = "{slug}"
    return "/" + "/".join(out)


def page_id(url: str) -> str:
    path = urlparse(url).path.strip("/").replace("/", "__") or "home"
    path = re.sub(r"[^A-Za-z0-9_\-]", "-", path)[:80]
    return f"{path}-{hashlib.sha1(url.encode()).hexdigest()[:8]}"


# ---------------------------------------------------------------------------
# In-browser extraction (one round trip per page)
# ---------------------------------------------------------------------------

EXTRACT_JS = r"""
() => {
  const meta = (sel, attr = 'content') => document.querySelector(sel)?.getAttribute(attr) || null;

  // JSON-LD blocks
  const jsonld = [];
  document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
    try { jsonld.push(JSON.parse(s.textContent)); } catch (e) { jsonld.push({ _parseError: true }); }
  });

  // Open Graph
  const og = {};
  document.querySelectorAll('meta[property^="og:"]').forEach(m => { og[m.getAttribute('property')] = m.getAttribute('content'); });

  // hreflang alternates
  const hreflang = [...document.querySelectorAll('link[rel="alternate"][hreflang]')]
    .map(l => ({ lang: l.getAttribute('hreflang'), href: l.href }));

  // Headings outline
  const headings = [...document.querySelectorAll('h1,h2,h3')]
    .map(h => ({ level: h.tagName.toLowerCase(), text: h.innerText.trim().slice(0, 200) }))
    .filter(h => h.text);

  // Landmark regions present on the page
  const landmarks = ['header', 'nav', 'main', 'aside', 'footer', 'form', 'article', 'section']
    .reduce((acc, t) => { acc[t] = document.getElementsByTagName(t).length; return acc; }, {});

  // Links (for discovery)
  const links = [...document.querySelectorAll('a[href]')].map(a => a.href);

  // Images with alt text
  const images = [...document.querySelectorAll('main img, article img, img')].slice(0, 100)
    .map(i => ({ src: i.currentSrc || i.src, alt: i.getAttribute('alt'), w: i.naturalWidth, h: i.naturalHeight }));

  // Structural fingerprint: tag tree of <body> to depth 6, classes without utility noise.
  // <main> always gets at least 4 levels below it: app frameworks (Next.js) nest it so deep that the
  // depth limit would otherwise cut off the content the page types are compared on.
  const UTIL = /^(js-|is-|has-|u-|mt-|mb-|ml-|mr-|pt-|pb-|px-|py-|p-|m-|w-|h-|text-|bg-|flex|grid|col-|row|d-|hidden|visible|active|sm:|md:|lg:|xl:)/;
  const MAIN_LEVELS = 4;
  const fp = [];
  const walk = (el, depth, levelsLeft) => {
    if (levelsLeft < 0) return;
    for (const c of el.children) {
      const tag = c.tagName.toLowerCase();
      if (['script', 'style', 'noscript', 'svg', 'template', 'iframe'].includes(tag)) continue;
      const cls = [...c.classList].filter(x => !UTIL.test(x) && !/\d{3,}/.test(x)).slice(0, 2).join('.');
      fp.push(`${depth}:${tag}${cls ? '.' + cls : ''}`);
      walk(c, depth + 1, tag === 'main' ? Math.max(levelsLeft - 1, MAIN_LEVELS) : levelsLeft - 1);
    }
  };
  if (document.body) walk(document.body, 0, 6);

  // Cleaned HTML: drop noise elements and most attributes
  const clone = document.documentElement.cloneNode(true);
  clone.querySelectorAll('script, style, noscript, svg, template, iframe, link, meta').forEach(n => n.remove());
  const KEEP = new Set(['class', 'id', 'href', 'src', 'alt', 'title', 'role', 'aria-label', 'datetime', 'lang']);
  clone.querySelectorAll('*').forEach(n => {
    [...n.attributes].forEach(a => { if (!KEEP.has(a.name)) n.removeAttribute(a.name); });
  });

  return {
    title: document.title || null,
    lang: document.documentElement.getAttribute('lang'),
    description: meta('meta[name="description"]'),
    robots: meta('meta[name="robots"]'),
    canonical: meta('link[rel="canonical"]', 'href'),
    og, hreflang, jsonld, headings, landmarks, images, links,
    fingerprint: fp,
    text: document.body ? document.body.innerText : '',
    cleanHtml: '<!doctype html>\n' + clone.outerHTML,
    wordCount: document.body ? document.body.innerText.split(/\s+/).filter(Boolean).length : 0,
  };
}
"""

# Splits the page into visual blocks and records layout facts per block (see blocks.js)
BLOCKS_JS = (Path(__file__).parent / "blocks.js").read_text(encoding="utf-8")

# Cookie/consent widgets cover screenshots and pollute text and fingerprints on every page
REMOVE_CONSENT_JS = r"""
() => {
  const CONSENT = /cybot|cookie|consent|onetrust|usercentrics|didomi|truste|iubenda/i;
  document.querySelectorAll('body *').forEach(el => {
    const cls = typeof el.className === 'string' ? el.className : '';
    if (el.isConnected && (CONSENT.test(el.id) || CONSENT.test(cls)) && !['MAIN', 'BODY'].includes(el.tagName)) el.remove();
  });
  [document.documentElement, document.body].forEach(el => {
    el.style.overflow = 'visible';
    [...el.classList].filter(c => CONSENT.test(c)).forEach(c => el.classList.remove(c));
  });
}
"""

# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------


class Crawler:
    def __init__(self, args):
        self.args = args
        self.start = normalize_url(args.url, args.keep_query)
        self.host = urlparse(self.start).netloc
        self.scheme = urlparse(self.start).scheme
        self.out = Path(args.out)
        self.pages_dir = self.out / "pages"
        self.pages_dir.mkdir(parents=True, exist_ok=True)

        self.queue: asyncio.Queue = asyncio.Queue()
        self.seen: set[str] = set()
        self.pattern_counts: Counter = Counter()
        self.content_hashes: dict[str, str] = {}
        self.canonicals: set[str] = set()
        self.inventory: list[dict] = []
        self.skipped: list[dict] = []
        self.crawled = 0
        self.robots = robotparser.RobotFileParser()
        self.last_request = 0.0
        self.rate_lock = asyncio.Lock()
        self.site_info: dict = {}
        self.pattern_pending: Counter = Counter()  # queued URLs per pattern, for the progress estimate
        self.completed = 0

    # -- discovery ---------------------------------------------------------

    def allowed(self, url: str) -> tuple[bool, str]:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False, "scheme"
        if p.netloc != self.host:
            return False, "external"
        if SKIP_EXTENSIONS.search(p.path):
            return False, "file-extension"
        if SKIP_PATHS.search(p.path):
            return False, "excluded-path"
        if self.args.include and not re.search(self.args.include, p.path):
            return False, "not-included"
        if self.args.exclude and re.search(self.args.exclude, p.path):
            return False, "excluded-by-pattern"
        # normalize_url strips trailing slashes, so test both forms against robots.txt
        if not all(self.robots.can_fetch(USER_AGENT, u) for u in (url, url.rstrip("/") + "/")):
            return False, "robots.txt"
        return True, ""

    def enqueue(self, raw_url: str, source: str):
        url = normalize_url(raw_url, self.args.keep_query)
        if url in self.seen:
            return
        self.seen.add(url)
        ok, reason = self.allowed(url)
        if not ok:
            if reason not in ("external", "scheme"):
                self.skipped.append({"url": url, "reason": reason, "found_via": source})
            return
        self.pattern_pending[url_pattern(url)] += 1
        self.queue.put_nowait((url, source))

    async def load_robots(self, request):
        robots_url = f"{self.scheme}://{self.host}/robots.txt"
        try:
            r = await request.get(robots_url, timeout=15000)
            body = await r.text() if r.ok else ""
        except Exception:
            body = ""
        self.robots.parse(body.splitlines())
        sitemaps = self.robots.site_maps() or []
        self.site_info = {"start_url": self.start, "robots_txt": body or None, "robots_url": robots_url,
                          "sitemaps_listed": sitemaps}
        print(f"robots.txt: {'found' if body else 'not found'}; sitemaps listed: {len(sitemaps)}")
        return sitemaps

    async def load_sitemaps(self, request, sitemap_urls):
        todo = list(sitemap_urls) or [f"{self.scheme}://{self.host}/sitemap.xml"]
        done, found = set(), 0
        while todo and len(done) < 50:
            sm = todo.pop(0)
            if sm in done:
                continue
            done.add(sm)
            try:
                r = await request.get(sm, timeout=30000)
                if not r.ok:
                    continue
                data = await r.body()
                if sm.endswith(".gz") or data[:2] == b"\x1f\x8b":
                    data = gzip.decompress(data)
                root = ElementTree.fromstring(data)
            except Exception as e:
                print(f"  sitemap {sm}: {e.__class__.__name__}")
                continue
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            for loc in root.findall(".//sm:sitemap/sm:loc", ns):
                todo.append(loc.text.strip())
            for loc in root.findall(".//sm:url/sm:loc", ns):
                self.enqueue(loc.text.strip(), "sitemap")
                found += 1
        print(f"sitemaps: {len(done)} read, {found} URLs listed")
        self.site_info.update(sitemaps_read=sorted(done), sitemap_urls=found)

    # -- fetching ----------------------------------------------------------

    async def polite_wait(self):
        async with self.rate_lock:
            wait = self.args.delay - (time.monotonic() - self.last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self.last_request = time.monotonic()

    async def worker(self, context, wid: int):
        while True:
            url, source = await self.queue.get()
            self.pattern_pending[url_pattern(url)] -= 1
            try:
                if self.args.max_pages and self.crawled >= self.args.max_pages:
                    self.skipped.append({"url": url, "reason": "max-pages", "found_via": source})
                    continue
                pattern = url_pattern(url)
                if self.args.max_per_pattern and self.pattern_counts[pattern] >= self.args.max_per_pattern:
                    self.skipped.append({"url": url, "reason": f"pattern-sampled ({pattern})", "found_via": source})
                    continue
                self.pattern_counts[pattern] += 1
                self.crawled += 1
                await self.polite_wait()
                await self.crawl_page(context, url, source, pattern)
                self.completed += 1
                self.report_progress(url)
            finally:
                self.queue.task_done()

    def expected_pages(self) -> int:
        """How many pages this crawl will end up with, given what is queued and the sampling limits."""
        patterns = set(self.pattern_counts) | set(self.pattern_pending)
        limit = self.args.max_per_pattern or float("inf")
        expected = sum(min(limit, self.pattern_counts[p] + max(0, self.pattern_pending[p])) for p in patterns)
        if self.args.max_pages:
            expected = min(self.args.max_pages, expected)
        return max(self.completed, int(expected))

    def report_progress(self, url: str = ""):
        """Machine-readable progress line for run.py: @@progress <done> <expected total> <url>."""
        if self.args.progress:
            print(f"@@progress {self.completed} {self.expected_pages()} {url}", flush=True)

    async def crawl_page(self, context, url, source, pattern):
        page = await context.new_page()
        started = time.monotonic()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=self.args.timeout * 1000)
            # Rate limited: wait as long as the server asks (Retry-After, 5–60 s) and try again, twice at most
            for attempt in range(2):
                if not resp or resp.status not in (429, 503):
                    break
                retry_after = resp.headers.get("retry-after", "")
                delay = min(60, max(5, int(retry_after) if retry_after.isdigit() else 10 * (attempt + 1)))
                await asyncio.sleep(delay)
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=self.args.timeout * 1000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass  # some sites never go idle (analytics, chat widgets)
            if self.args.scroll:
                await page.evaluate(
                    "async () => { for (let y = 0; y < document.body.scrollHeight; y += 800) "
                    "{ window.scrollTo(0, y); await new Promise(r => setTimeout(r, 100)); } window.scrollTo(0, 0); }"
                )
            status = resp.status if resp else None
            final_url = normalize_url(page.url, self.args.keep_query)
            content_type = resp.headers.get("content-type", "") if resp else ""
            if urlparse(final_url).netloc != self.host:
                self.skipped.append({"url": url, "reason": f"external-redirect to {final_url}", "found_via": source})
                return
            if "html" not in content_type:
                self.skipped.append({"url": url, "reason": f"content-type {content_type}", "found_via": source})
                return

            await page.evaluate(REMOVE_CONSENT_JS)
            data = await page.evaluate(EXTRACT_JS)

            # Discover more links even if this page turns out to be a duplicate
            links = data.pop("links")
            if not self.args.urls:
                for link in links:
                    self.enqueue(link, url)

            # Deduplicate: redirects, canonicals pointing elsewhere, identical content
            text_hash = hashlib.sha1(re.sub(r"\s+", " ", data["text"]).strip().encode()).hexdigest()
            canonical = (
                normalize_url(urljoin(final_url, data["canonical"]), self.args.keep_query)
                if data["canonical"]
                else final_url
            )
            dup_reason = None
            if final_url != url and final_url in self.canonicals:
                dup_reason = f"redirect to {final_url}"
            elif canonical in self.canonicals:
                dup_reason = f"canonical {canonical}"
            elif text_hash in self.content_hashes:
                dup_reason = f"same content as {self.content_hashes[text_hash]}"
            if dup_reason:
                self.skipped.append({"url": url, "reason": f"duplicate: {dup_reason}", "found_via": source})
                return
            self.canonicals.update({canonical, final_url})
            self.content_hashes[text_hash] = url

            pid = page_id(final_url)
            pdir = self.pages_dir / pid
            pdir.mkdir(parents=True, exist_ok=True)

            blocks = await page.evaluate(BLOCKS_JS)
            (pdir / "blocks.json").write_text(json.dumps(blocks, ensure_ascii=False), encoding="utf-8")

            fingerprint = data.pop("fingerprint")
            text = data.pop("text")
            clean_html = data.pop("cleanHtml")
            (pdir / "clean.html").write_text(clean_html, encoding="utf-8")
            (pdir / "text.txt").write_text(text, encoding="utf-8")
            (pdir / "fingerprint.txt").write_text("\n".join(fingerprint), encoding="utf-8")
            if not self.args.no_screenshots:
                await page.screenshot(path=str(pdir / "screenshot.jpg"), full_page=True, type="jpeg", quality=75)

            schema_types = sorted({t for block in data["jsonld"] for t in _schema_types(block)})
            record = {
                "id": pid,
                "url": url,
                "final_url": final_url,
                "canonical": canonical,
                "status": status,
                "pattern": pattern,
                "found_via": source,
                "schema_types": schema_types,
                "dom_fingerprint": hashlib.sha1("\n".join(fingerprint).encode()).hexdigest()[:12],
                "dom_shape": _shape(fingerprint),
                "content_hash": text_hash[:12],
                "load_seconds": round(time.monotonic() - started, 2),
                **data,
            }
            (pdir / "page.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

            self.inventory.append({
                "id": pid, "url": final_url, "status": status, "pattern": pattern,
                "title": data["title"], "lang": data["lang"], "schema_types": "|".join(schema_types),
                "h1": next((h["text"] for h in data["headings"] if h["level"] == "h1"), ""),
                "word_count": data["wordCount"], "images": len(data["images"]),
                "dom_fingerprint": record["dom_fingerprint"], "found_via": source,
            })
            print(f"[{len(self.inventory):>4}] {status} {pattern:<40} {final_url}")
        except Exception as e:
            self.skipped.append({"url": url, "reason": f"error: {e.__class__.__name__}: {str(e)[:120]}", "found_via": source})
            print(f"[ err] {url}: {e.__class__.__name__}")
        finally:
            await page.close()

    # -- run ---------------------------------------------------------------

    def check_certificate(self) -> dict:
        """One normal, verified TLS handshake, so the report can flag an invalid certificate."""
        if self.scheme != "https":
            return {"certificate_ok": None, "certificate_error": "site is not served over https"}
        try:
            with socket.create_connection((self.host, 443), timeout=15) as sock:
                with ssl.create_default_context().wrap_socket(sock, server_hostname=self.host) as tls:
                    cert = tls.getpeercert()
            return {"certificate_ok": True, "certificate_expires": cert.get("notAfter")}
        except (ssl.SSLError, ssl.CertificateError) as e:
            return {"certificate_ok": False, "certificate_error": getattr(e, "verify_message", None) or str(e)}
        except OSError as e:
            return {"certificate_ok": None, "certificate_error": f"could not connect: {e}"}

    async def run(self):
        cert = self.check_certificate()
        if cert["certificate_ok"] is False:
            print(f"TLS certificate invalid: {cert['certificate_error']}")
            if not self.args.ignore_https_errors:
                sys.exit("Stopping. Fix the certificate, or re-run with --ignore-https-errors to crawl anyway.")
            print("Continuing because --ignore-https-errors is set.")
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1440, "height": 900},
                locale=self.args.locale,
                ignore_https_errors=self.args.ignore_https_errors,
            )
            sitemaps = await self.load_robots(context.request)
            self.site_info.update(cert, https_errors_ignored=self.args.ignore_https_errors)
            if self.args.urls:
                for line in Path(self.args.urls).read_text(encoding="utf-8").splitlines():
                    if line.strip() and not line.startswith("#"):
                        self.enqueue(line.strip(), "url-list")
            else:
                if not self.args.no_sitemap:
                    await self.load_sitemaps(context.request, sitemaps)
                self.enqueue(self.start, "start")
            self.report_progress()

            workers = [asyncio.create_task(self.worker(context, i)) for i in range(self.args.concurrency)]
            await self.queue.join()  # done when every queued URL is processed, including ones found late
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            await browser.close()
        self.write_reports()

    def write_reports(self):
        (self.out / "site.json").write_text(json.dumps(self.site_info, indent=2, ensure_ascii=False), encoding="utf-8")
        inv_cols = ["id", "url", "status", "pattern", "title", "lang", "schema_types", "h1",
                    "word_count", "images", "dom_fingerprint", "found_via"]
        _write_csv(self.out / "inventory.csv", inv_cols, sorted(self.inventory, key=lambda r: r["url"]))
        _write_csv(self.out / "skipped.csv", ["url", "reason", "found_via"], self.skipped)

        by_pattern = defaultdict(list)
        for r in self.inventory:
            by_pattern[r["pattern"]].append(r)
        sampled = Counter(
            s["reason"].split("(")[-1].rstrip(")") for s in self.skipped if s["reason"].startswith("pattern-sampled")
        )
        rows = []
        for pat, items in sorted(by_pattern.items(), key=lambda kv: -len(kv[1])):
            rows.append({
                "pattern": pat,
                "crawled": len(items),
                "not_crawled_sampled": sampled.get(pat, 0),
                "distinct_dom_fingerprints": len({i["dom_fingerprint"] for i in items}),
                "schema_types": "|".join(sorted({t for i in items for t in i["schema_types"].split("|") if t})),
                "examples": " ".join(i["url"] for i in items[:3]),
            })
        _write_csv(self.out / "patterns.csv", list(rows[0].keys()) if rows else ["pattern"], rows)

        reasons = Counter(s["reason"].split(":")[0].split(" (")[0] for s in self.skipped)
        print("\nDone.")
        print(f"  crawled:  {len(self.inventory)} pages in {len(by_pattern)} URL patterns")
        print(f"  skipped:  {len(self.skipped)} " + ", ".join(f"{k}={v}" for k, v in reasons.most_common()))
        print(f"  output:   {self.out.resolve()}")


def _schema_types(block):
    """Collect @type values from a JSON-LD block, including @graph and nested items."""
    if isinstance(block, list):
        for b in block:
            yield from _schema_types(b)
    elif isinstance(block, dict):
        t = block.get("@type")
        if isinstance(t, str):
            yield t
        elif isinstance(t, list):
            yield from (x for x in t if isinstance(x, str))
        for k in ("@graph", "mainEntity", "itemListElement"):
            if k in block:
                yield from _schema_types(block[k])


def _shape(fingerprint, depth=2):
    """Coarse page skeleton: the top levels of the tag tree, readable at a glance."""
    return " > ".join(f.split(":", 1)[1] for f in fingerprint if int(f.split(":", 1)[0]) < depth)[:300]


def _write_csv(path, cols, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description="Crawl a website for content-model analysis.")
    ap.add_argument("url", help="Start URL, e.g. https://www.example.com")
    ap.add_argument("--out", default="output", help="Output folder (default: output)")
    ap.add_argument("--max-pages", type=int, default=0, help="Stop after this many pages (default: 0 = no limit)")
    ap.add_argument("--max-per-pattern", type=int, default=0,
                    help="Crawl at most N pages per URL pattern, e.g. /products/{slug} (default: 0 = no limit)")
    ap.add_argument("--concurrency", type=int, default=2, help="Parallel browser tabs (default: 2)")
    ap.add_argument("--delay", type=float, default=1.0, help="Seconds between requests (default: 1.0)")
    ap.add_argument("--timeout", type=int, default=30, help="Page load timeout in seconds (default: 30)")
    ap.add_argument("--include", help="Only crawl paths matching this regex, e.g. '^/nl/'")
    ap.add_argument("--exclude", help="Skip paths matching this regex, e.g. '/tag/|/page/'")
    ap.add_argument("--keep-query", action="store_true", help="Treat query strings as distinct pages")
    ap.add_argument("--ignore-https-errors", action="store_true",
                    help="Crawl even when the site's TLS certificate is invalid (the problem is still recorded in site.json)")
    ap.add_argument("--progress", action="store_true", help="Print machine-readable progress lines (used by run.py)")
    ap.add_argument("--urls", help="Crawl only the URLs in this file (one per line), without following links")
    ap.add_argument("--no-sitemap", action="store_true", help="Discover through links only")
    ap.add_argument("--no-screenshots", action="store_true", help="Skip full-page screenshots (faster)")
    ap.add_argument("--scroll", action="store_true", help="Scroll each page to trigger lazy-loaded content")
    ap.add_argument("--locale", default="en-US", help="Browser locale (default: en-US)")
    args = ap.parse_args()

    if not urlparse(args.url).scheme:
        sys.exit("Start URL needs a scheme, e.g. https://www.example.com")
    asyncio.run(Crawler(args).run())


if __name__ == "__main__":
    main()
