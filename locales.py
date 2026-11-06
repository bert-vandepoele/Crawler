"""
Locale detection for run.py: which languages/regions does a site have, and where does each one live?

Sources, from one request for the home page plus robots.txt and the sitemaps:
  - hreflang alternates on the home page
  - language switcher links (/nl, /fr-be, …)
  - URL prefixes in the sitemaps, with a URL count per locale
  - <html lang> of the home page
"""

import gzip
import re
from collections import Counter
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

from bs4 import BeautifulSoup

LOCALE = re.compile(r"^[a-z]{2}([-_][a-z]{2})?$", re.I)
USER_AGENT = "ContentModelCrawler/0.1 (+content audit)"
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
MAX_SITEMAPS = 30

LANGUAGES = {
    "en": "English", "nl": "Dutch", "fr": "French", "de": "German", "es": "Spanish", "it": "Italian", "pt": "Portuguese",
    "pl": "Polish", "sv": "Swedish", "da": "Danish", "no": "Norwegian", "nb": "Norwegian", "fi": "Finnish", "cs": "Czech",
    "sk": "Slovak", "hu": "Hungarian", "ro": "Romanian", "el": "Greek", "tr": "Turkish", "ru": "Russian", "uk": "Ukrainian",
    "ja": "Japanese", "zh": "Chinese", "ko": "Korean", "ar": "Arabic", "he": "Hebrew", "lb": "Luxembourgish", "et": "Estonian",
    "lv": "Latvian", "lt": "Lithuanian", "sl": "Slovenian", "hr": "Croatian", "bg": "Bulgarian", "sr": "Serbian",
}


def language_name(code: str) -> str:
    lang, _, region = code.replace("_", "-").partition("-")
    name = LANGUAGES.get(lang.lower(), "")
    return f"{name} ({region.upper()})" if name and region else name


def _prefix(url: str) -> str:
    first = urlparse(url).path.strip("/").split("/")[0]
    return first.lower() if LOCALE.match(first) else ""


def _host(url: str) -> str:
    return re.sub(r"^www\.", "", urlparse(url).netloc.lower())


def _sitemap_urls(request, sitemap_urls: list[str]) -> list[str]:
    todo, done, found = list(sitemap_urls), set(), []
    while todo and len(done) < MAX_SITEMAPS:
        sm = todo.pop(0)
        if sm in done:
            continue
        done.add(sm)
        try:
            r = request.get(sm)
            if not r.ok:
                continue
            data = r.body()
            if sm.endswith(".gz") or data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            root = ElementTree.fromstring(data)
        except Exception:
            continue
        todo += [loc.text.strip() for loc in root.findall(".//sm:sitemap/sm:loc", SITEMAP_NS) if loc.text]
        found += [loc.text.strip() for loc in root.findall(".//sm:url/sm:loc", SITEMAP_NS) if loc.text]
    return found


def detect_locales(url: str, ignore_tls: bool = False) -> dict:
    """Returns {final_url, html_lang, sitemap_urls, locales: [{code, name, prefix, host, start_url, urls, sources, current}]}."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        request = p.request.new_context(ignore_https_errors=ignore_tls, user_agent=USER_AGENT, timeout=20000)
        try:
            home = request.get(url)
            final = home.url
            html = home.text() if home.ok else ""
            parsed = urlparse(final)
            base = f"{parsed.scheme}://{parsed.netloc}"
            robots = request.get(f"{base}/robots.txt")
            listed = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots.text()) if robots.ok else []
            sitemap = _sitemap_urls(request, listed or [f"{base}/sitemap.xml"])
        finally:
            request.dispose()

    host = _host(final)
    soup = BeautifulSoup(html, "html.parser")
    html_lang = (soup.html.get("lang") if soup.html else "") or ""
    locales: dict[tuple[str, str], dict] = {}

    def entry(h: str, prefix: str) -> dict:
        return locales.setdefault((h, prefix), {"host": h, "prefix": prefix, "code": prefix, "start_url": None,
                                                "urls": 0, "sources": set()})

    for link in soup.find_all("link", attrs={"rel": "alternate", "hreflang": True}):
        code, href = link["hreflang"].strip(), urljoin(final, link.get("href", ""))
        if code.lower() == "x-default" or not href:
            continue
        e = entry(_host(href), _prefix(href))
        e["code"], e["start_url"] = code, e["start_url"] or href
        e["sources"].add("hreflang")

    # Language switcher: short links to a locale root on the same site (/nl, /fr-be)
    for a in soup.find_all("a", href=True):
        href = urljoin(final, a["href"])
        if _host(href) == host and _prefix(href) and len(urlparse(href).path.strip("/").split("/")) == 1 \
                and len(a.get_text(strip=True)) <= 15:
            e = entry(host, _prefix(href))
            e["start_url"] = e["start_url"] or href
            e["sources"].add("switcher")

    counts = Counter(_prefix(u) for u in sitemap if _host(u) == host)
    total = sum(counts.values())
    for prefix, n in counts.items():
        if prefix and ((host, prefix) in locales or n >= max(5, 0.05 * total)):
            e = entry(host, prefix)
            e["urls"] = n
            e["sources"].add("sitemap")

    current = (host, _prefix(final))
    if current not in locales and locales:
        entry(*current)["sources"].add("start URL")
    if current in locales and not current[1] and html_lang:
        locales[current]["code"] = html_lang
    for e in locales.values():
        e["start_url"] = e["start_url"] or (f"{urlparse(final).scheme}://{e['host'] if e['host'] != host else urlparse(final).netloc}/"
                                            + (f"{e['prefix']}" if e["prefix"] else ""))
        e["name"] = language_name(e["code"])
        e["current"] = (e["host"], e["prefix"]) == current
        e["sources"] = sorted(e["sources"])

    found = sorted(locales.values(), key=lambda e: (not e["current"], e["host"] != host, e["code"]))
    return {"final_url": final, "html_lang": html_lang, "sitemap_urls": total,
            "unprefixed_urls": counts.get("", 0), "locales": found if len(found) > 1 else []}
