#!/usr/bin/env python3
"""
Run the whole site analysis from the terminal.

Asks for the website and a few options, checks the address (redirects to another
domain, TLS certificate), then runs every step with a live step list and progress bar:

    1. Crawl the site            crawl.py
    2. Group page templates      cluster.py
    3. Name page types           name_types_rules.py
    4. Detect components         components.py
    5. Draft the content model   content_model.py
    6. Build the report          report.py

Usage:
    python3 run.py                                   # asks for everything
    python3 run.py https://www.example.com --yes     # defaults, no questions

Output goes to output/<host>/, with the full log of every step in run.log.
"""

import argparse
import csv
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
VENV_PYTHON = HERE / ".venv" / "bin" / "python"

# Use the project's virtual environment, so `python3 run.py` works without activating it first
if VENV_PYTHON.exists() and Path(sys.prefix).resolve() != (HERE / ".venv").resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), __file__, *sys.argv[1:]])

# Import names of requirements.txt packages whose module is called differently
MODULE_NAMES = {"scikit-learn": "sklearn", "beautifulsoup4": "bs4", "pillow": "PIL"}


def missing_packages() -> list[str]:
    """Packages from requirements.txt that can't be imported by this Python."""
    from importlib.util import find_spec

    missing = []
    for line in (HERE / "requirements.txt").read_text().splitlines():
        package = re.split(r"[<>=!~\[ ;]", line.strip(), maxsplit=1)[0]
        if package and not line.lstrip().startswith("#") and find_spec(MODULE_NAMES.get(package.lower(), package)) is None:
            missing.append(package)
    return missing


def ensure_packages() -> None:
    """
    Offer to install missing Python packages, before anything else runs. This is plain text: the coloured
    output needs `rich`, which may be one of the missing packages. Packages go into the project's .venv
    (created when needed: Homebrew's Python doesn't allow installing into it), then the script restarts.
    """
    missing = missing_packages()
    if not missing:
        return
    in_venv = Path(sys.prefix).resolve() == (HERE / ".venv").resolve()
    target = ".venv" if in_venv else "a new .venv in this folder"
    print(f"Missing Python packages: {', '.join(missing)}")
    if "--yes" in sys.argv or "-y" in sys.argv:
        print(f"Installing them into {target} (--yes was given).")
    else:
        answer = input(f"Install them into {target}? [Y/n] ").strip().lower()
        if answer not in ("", "y", "yes"):
            sys.exit("Stopped. Install them yourself with:\n"
                     "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt")
    try:
        if not in_venv:
            subprocess.run([sys.executable, "-m", "venv", str(HERE / ".venv")], check=True)
        subprocess.run([str(VENV_PYTHON), "-m", "pip", "install", "-r", str(HERE / "requirements.txt")], check=True)
    except (subprocess.CalledProcessError, OSError) as e:
        sys.exit(f"Installing failed ({e}). See the pip output above.")
    print("Packages installed. Restarting…\n")
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), __file__, *sys.argv[1:]])


ensure_packages()

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
    from rich.prompt import Confirm, Prompt
    from rich.table import Table
    from rich.text import Text
    from rich.cells import cell_len
except ImportError:   # only when a package is installed but broken: ensure_packages() handled missing ones
    sys.exit("Broken dependencies. Run: .venv/bin/pip install --force-reinstall -r requirements.txt")

console = Console()
USER_AGENT = "ContentModelCrawler/0.1 (+content audit)"


class Section:
    """
    Output of the questions before the crawl.

    With boxed=True everything happens inside one cyan panel: the top border and intro first, then every line
    and question with the panel's side border in front of it. A question needs a live terminal line, so the
    panel stays open at the bottom while you answer; once all questions are answered it is cleared and drawn
    again as one closed panel. Without a terminal (piped output) the open panel just gets its bottom border.
    Without boxed (the checks after a redirect, later in the run), lines are printed as they are.
    """

    def __init__(self, title: str = "", intro: str = "", boxed: bool = False):
        self.title, self.intro, self.boxed = title, intro, boxed
        self.items, self.rows = [], 0
        if boxed:
            self._write(self._panel([Text("")])[:-1])     # top border, intro and a blank line; no bottom border

    def _panel(self, items: list, intro: bool = True) -> list[str]:
        """The panel as printed lines (with colour codes on a terminal)."""
        body = ([Text.from_markup(self.intro)] if intro and self.intro else []) + items
        panel = Panel(Group(*body), title=self.title, border_style="cyan", width=console.width)
        with console.capture() as captured:
            console.print(panel)
        return captured.get().rstrip("\n").split("\n")

    def _write(self, lines: list[str]) -> None:
        console.file.write("\n".join(lines) + "\n")
        console.file.flush()
        self.rows += len(lines)

    def print(self, markup="") -> None:
        renderable = Text.from_markup(markup) if isinstance(markup, str) else markup
        if not self.boxed:
            console.print(renderable)
            return
        self.items.append(renderable)
        self._write(self._panel([renderable], intro=False)[1:-1])   # side borders only

    def ask(self, prompt: str, confirm: bool = False, **kwargs):
        border = "[cyan]│[/] " if self.boxed else ""
        answer = (Confirm if confirm else Prompt).ask(border + prompt, console=console, **kwargs)
        if not sys.stdin.isatty():
            console.file.write("\n")   # piped answers aren't echoed, so the line wasn't ended
        if not self.boxed:
            return answer
        shown_answer = ("yes" if answer else "no") if confirm else answer
        # The terminal wraps the typed line itself: count its rows to clear them later
        suffix = (" [y/n]" if confirm else "") + (f" ({kwargs['default']})" if "default" in kwargs else "") + ": "
        typed = Text.from_markup(border + prompt).cell_len + cell_len(suffix + str(shown_answer))
        self.rows += max(1, -(-typed // console.width))
        self.items.append(Text.from_markup(prompt) + Text("  ") + Text(str(shown_answer), style="bold cyan"))
        return answer

    def drop_last(self, count: int) -> None:
        """Leave the last lines out of the closed panel (a rejected answer and its error)."""
        if self.boxed:
            del self.items[-count:]

    def close(self) -> None:
        if not self.boxed:
            return
        if console.is_terminal and self.rows < console.height:
            console.file.write(f"\x1b[{self.rows}A\x1b[J")   # cursor up over the open panel, clear below
            console.file.flush()
            console.file.write("\n".join(self._panel([Text("")] + self.items)) + "\n")
        else:
            console.file.write(self._panel([], intro=False)[-1] + "\n")  # can't redraw: just close the open panel
        console.file.flush()


# ---------------------------------------------------------------------------
# Questions and pre-flight checks
# ---------------------------------------------------------------------------

def normalise_url(raw: str) -> str | None:
    raw = raw.strip()
    if not raw:
        return None
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if not parsed.netloc or "." not in parsed.netloc:
        return None
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path or '/'}"


def certificate_error(host: str) -> str | None:
    """None when the certificate is valid; otherwise the reason. Uses normal, verified TLS."""
    try:
        with socket.create_connection((host, 443), timeout=15) as sock:
            with ssl.create_default_context().wrap_socket(sock, server_hostname=host):
                return None
    except (ssl.SSLError, ssl.CertificateError) as e:
        return getattr(e, "verify_message", None) or str(e)
    except OSError as e:
        return f"could not connect: {e}"


def final_url(url: str) -> tuple[str | None, str | None]:
    """Follow redirects with a verified request. Returns (final URL, error)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.geturl(), None
    except urllib.error.HTTPError as e:
        return e.geturl(), None if e.code < 500 else f"HTTP {e.code}"
    except Exception as e:
        return None, str(getattr(e, "reason", e))


def browser_installed() -> bool:
    """True when Playwright's Chromium (used by crawl.py) is downloaded."""
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


def ensure_browser(out: Section, ask: bool) -> None:
    """Check the browser the crawler needs, and offer to download it when it's missing."""
    with console.status("Checking dependencies…"):
        ready = browser_installed()
    if ready:
        out.print("[green]✓[/] Python packages and the Chromium browser are installed")
        return
    out.print("[yellow]![/] Playwright's Chromium browser is missing; the crawler needs it to load pages.")
    if ask and not out.ask("  Download it now (about 150 MB)?", confirm=True, default=True):
        out.close()
        sys.exit("Stopped. Install it yourself with: .venv/bin/python -m playwright install chromium")
    with console.status("Downloading Chromium…"):
        result = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                                capture_output=True, text=True)
    if result.returncode != 0 or not browser_installed():
        out.close()
        console.print(f"[red]✕ Downloading Chromium failed.[/] Last output:")
        for line in (result.stdout + result.stderr).strip().splitlines()[-10:]:
            console.print(f"  [dim]{line}[/]")
        sys.exit(1)
    out.print("[green]✓[/] Chromium installed")


def preflight(url: str, ask: bool, out: Section | None = None) -> tuple[str, bool]:
    """Check the address: redirects to another host and the TLS certificate. Returns (URL to crawl, ignore TLS errors)."""
    with console.status("Checking the website…"):
        host = urlparse(url).netloc
        cert_problem = certificate_error(host) if url.startswith("https://") else None
        landed, error = (None, None) if cert_problem else final_url(url)
    out = out or Section()

    if cert_problem and not cert_problem.startswith("could not connect"):
        out.print(f"[yellow]![/] The TLS certificate of [b]{host}[/] is invalid: {cert_problem}")
        out.print("  Visitors see a security warning. The report will flag this as a failed SEO check.")
        if ask and not out.ask("  Crawl anyway, ignoring the certificate?", confirm=True, default=False):
            sys.exit("Stopped. Fix the certificate or choose another address.")
        if not ask:
            out.print("  Continuing because --yes was given.")
        return url, True
    if cert_problem:
        sys.exit(f"Could not reach {host}: {cert_problem}")
    if error:
        sys.exit(f"Could not load {url}: {error}")

    landed_host = urlparse(landed).netloc
    if landed_host and landed_host != host:
        target = f"{urlparse(landed).scheme}://{landed_host}{urlparse(landed).path or '/'}"
        out.print(f"[yellow]![/] {url} redirects to [b]{target}[/]")
        out.print(f"  Analysing {landed_host} instead.")
        return target, False
    out.print(f"[green]✓[/] {urlparse(landed or url).netloc} is reachable and its certificate is valid")
    return landed or url, False


def choose_locale(url: str, ignore_tls: bool, args, out: Section | None = None) -> tuple[str, bool, dict | None]:
    """Find the site's locales and ask which one to crawl. Returns (start URL, ignore TLS errors, chosen locale or None for all)."""
    from locales import detect_locales

    out = out or Section()

    if args.include:
        return url, ignore_tls, None  # the path filter already decides what to crawl
    try:
        with console.status("Looking for languages and regions…"):
            info = detect_locales(url, ignore_tls)
        if urlparse(info["final_url"]).netloc != urlparse(url).netloc:
            out.print(f"[yellow]![/] {urlparse(url).netloc} redirects to [b]{info['final_url']}[/]. Analysing that instead.")
            url, ignore_tls = preflight(info["final_url"], ask=not args.yes, out=out)
            with console.status("Looking for languages and regions…"):
                info = detect_locales(url, ignore_tls)
    except Exception as e:
        out.print(f"[yellow]![/] Could not check the site's languages ({e.__class__.__name__}); crawling everything.")
        return url, ignore_tls, None

    found = info["locales"]
    if not found:
        lang = f" ({info['html_lang']})" if info["html_lang"] else ""
        out.print(f"[green]✓[/] Single-language site{lang}, {info['sitemap_urls']} URLs in the sitemap")
        return info["final_url"], ignore_tls, None

    table = Table(title=f"{len(found)} languages/regions found (URLs = in the sitemap)", title_justify="left", box=None, padding=(0, 2))
    for col, kw in (("#", {"justify": "right"}), ("Locale", {"no_wrap": True}), ("Language", {"no_wrap": True}),
                    ("URLs", {"justify": "right"}), ("Starts at", {"no_wrap": True}), ("Found in", {"style": "dim"})):
        table.add_column(col, **kw)
    site_host = urlparse(info["final_url"]).netloc
    for i, loc in enumerate(found, 1):
        start = urlparse(loc["start_url"])
        shown = (start.path or "/") if start.netloc == site_host else loc["start_url"]
        table.add_row(str(i), f"[b]{loc['code']}[/]" + (" [dim]current[/]" if loc["current"] else ""), loc["name"],
                      str(loc["urls"] or "–"), shown, ", ".join(loc["sources"]))
    total = sum(loc["urls"] for loc in found)
    table.add_row("a", "[b]all[/]", "every locale", str(total or "–"), "", "")
    out.print()
    out.print(table)

    def pick(answer: str):
        answer = answer.strip().lower()
        if answer in ("a", "all"):
            return "all"
        if answer.isdigit() and 1 <= int(answer) <= len(found):
            return found[int(answer) - 1]
        return next((loc for loc in found if answer in (loc["code"].lower(), loc["prefix"])), None)

    choice = pick(args.locale) if args.locale else ("all" if args.yes else None)
    if args.locale and choice is None:
        sys.exit(f"Locale '{args.locale}' not found. Choose from: {', '.join(loc['code'] for loc in found)} or all.")
    while choice is None:
        default = str(next((i for i, loc in enumerate(found, 1) if loc["current"]), 1))
        choice = pick(out.ask("Which locale should be crawled? [dim](number, code, or a for all)[/]", default=default))
        if choice is None:
            out.print("[red]Not one of the options above.[/]")
            out.drop_last(2)
    if choice == "all":
        out.print(f"Crawling all {len(found)} locales.")
        return info["final_url"], ignore_tls, None
    out.print(f"Crawling [b]{choice['code']}[/] {('— ' + choice['name']) if choice['name'] else ''}")
    return choice["start_url"], ignore_tls, choice


def options(args, url: str, locale: dict | None = None) -> dict:
    """Crawl options from the command line flags and the chosen locale."""
    host = re.sub(r"^www\.", "", urlparse(url).netloc)
    include = args.include
    if locale and locale["prefix"] and not include:
        include = f"^/{re.escape(locale['prefix'])}(/|$)"
    folder = f"{host}-{locale['code'].lower()}" if locale else host
    return {
        "max_pages": args.max_pages, "per_pattern": args.max_per_pattern, "include": include,
        "out": Path(args.out) if args.out else HERE / "output" / folder,
    }


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

class Step:
    def __init__(self, title: str, args: list[str], determinate: bool = False):
        self.title, self.args, self.determinate = title, args, determinate
        self.status = "pending"   # pending | running | done | failed
        self.detail = ""
        self.started = self.finished = None

    @property
    def seconds(self) -> float:
        if not self.started:
            return 0
        return (self.finished or time.monotonic()) - self.started


def steps_table(steps: list[Step]) -> Table:
    table = Table.grid(padding=(0, 2))
    table.add_column(width=2)
    table.add_column()
    table.add_column(justify="right", style="dim")
    icons = {"pending": "[dim]·[/]", "running": "[cyan]▸[/]", "done": "[green]✓[/]", "failed": "[red]✕[/]"}
    for i, s in enumerate(steps, 1):
        title = f"[b]{s.title}[/]" if s.status == "running" else s.title
        if s.status == "pending":
            title = f"[dim]{s.title}[/]"
        detail = f"  [dim]{s.detail}[/]" if s.detail else ""
        table.add_row(icons[s.status], f"{i}. {title}{detail}", f"{s.seconds:.0f}s" if s.started else "")
    return table


def run_step(step: Step, live: Live, steps: list[Step], log, progress: Progress, task) -> list[str]:
    step.status, step.started = "running", time.monotonic()
    progress.reset(task, total=None if not step.determinate else 1, completed=0, description=step.title)
    lines = []
    log.write(f"\n===== {step.title}: {' '.join(step.args)}\n")
    proc = subprocess.Popen([str(sys.executable), "-u", *step.args], cwd=HERE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in proc.stdout:
        line = line.rstrip("\n")
        if line.startswith("@@progress"):
            _, done, total, *rest = line.split(" ", 3) + [""]
            progress.update(task, completed=int(done), total=max(int(total), 1))
            url = rest[0] if rest else ""
            step.detail = f"{done}/{total} pages" + (f" · {urlparse(url).path or '/'}" if url else "")
        else:
            lines.append(line)
            log.write(line + "\n")
            if not step.determinate and line.strip():
                step.detail = line.strip()[:70]
        live.update(render(steps, progress))
    proc.wait()
    step.finished = time.monotonic()
    step.status = "done" if proc.returncode == 0 else "failed"
    if step.status == "done":
        step.detail = summarise(step, lines)
        if step.determinate:
            progress.update(task, completed=progress.tasks[task].total or 1)
    live.update(render(steps, progress))
    return lines


def summarise(step: Step, lines: list[str]) -> str:
    """One-line result per step, taken from the step's own output."""
    text = "\n".join(lines)
    patterns = {
        "Crawl": r"crawled:\s+(\d+ pages in \d+ URL patterns)",
        "Group": r"(\d+ pages -> \d+ clusters)",
        "Detect": r"(\d+ pages, \d+ blocks)",
        "Draft": r"(\d+ content types, \d+ fields)",
        "Build": r"\((\d+ KB)\)",
    }
    for key, pat in patterns.items():
        if step.title.startswith(key) and (m := re.search(pat, text)):
            return m.group(1).replace("->", "→")
    if step.title.startswith("Name"):
        return f"{sum(1 for l in lines if re.match(r'\s+\[\s*\d+\]', l))} page types"
    return ""


def render(steps: list[Step], progress: Progress):
    running = any(s.status == "running" for s in steps)
    return Panel(Group(steps_table(steps), Text(""), progress) if running else steps_table(steps),
                 title="Site analysis", border_style="cyan", padding=(1, 2), width=console.width)


def crawl_outcome(out: Path) -> tuple[int, str | None]:
    """Pages crawled, and the off-site redirect target when the start page left the site."""
    def rows(name):
        path = out / name
        return list(csv.DictReader(open(path, encoding="utf-8"))) if path.exists() else []
    pages = len(rows("inventory.csv"))
    redirect = next((r["reason"].split(" to ", 1)[1] for r in rows("skipped.csv")
                     if r["reason"].startswith("external-redirect") and r["found_via"] == "start"), None)
    return pages, redirect


def final_summary(out: Path) -> Table:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim")
    table.add_column()
    try:
        inventory = (out / "inventory.csv").read_text(encoding="utf-8").count("\n") - 1
        types = json.loads((out / "page-types.json").read_text(encoding="utf-8"))["page_types"]
        components = json.loads((out / "components.json").read_text(encoding="utf-8"))["catalogue"]
        table.add_row("Pages crawled", str(inventory))
        table.add_row("Page types", ", ".join(t["name"] for t in types[:6] if t["name"]) + (f" +{len(types) - 6} more" if len(types) > 6 else ""))
        table.add_row("Components", ", ".join(c for c in components if c not in ("SiteHeader", "SiteFooter", "Unknown"))[:110])
        model = json.loads((out / "content-model.json").read_text(encoding="utf-8"))
        table.add_row("Content model", ", ".join(f"{t['name']} ({len(t['fields'])})" for t in model["content_types"][:6])
                      + (f" +{len(model['content_types']) - 6} more" if len(model["content_types"]) > 6 else ""))
    except (OSError, KeyError, ValueError):
        pass
    table.add_row("Report", str(out / "report.html"))
    table.add_row("Log", str(out / "run.log"))
    return table


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Analyse a website: crawl, page types, components, report.")
    ap.add_argument("url", nargs="?", help="Website to analyse (asked when left out)")
    ap.add_argument("--max-pages", type=int, default=0, help="Maximum pages to crawl (default: 0 = all pages)")
    ap.add_argument("--max-per-pattern", type=int, default=0,
                    help="Maximum pages per URL pattern, for a quick sample of large sites (default: 0 = all)")
    ap.add_argument("--include", help="Only crawl paths matching this regex, e.g. '^/en/'")
    ap.add_argument("--locale", help="Locale to crawl without asking, e.g. nl, fr-be or all (default: ask; all with --yes)")
    ap.add_argument("--out", help="Output folder (default: output/<host> or output/<host>-<locale>)")
    ap.add_argument("--yes", "-y", action="store_true", help="Don't ask anything (also not about an invalid TLS certificate)")
    ap.add_argument("--no-open", action="store_true", help="Don't open the report in the browser at the end")
    args = ap.parse_args()

    setup = Section("Crawl", "Crawl a website and map its URL patterns, page types, components, navigation and SEO.",
                    boxed=True)
    ensure_browser(setup, ask=not args.yes)
    url = normalise_url(args.url) if args.url else None
    if url:
        setup.print(f"Website to analyse  [bold cyan]{url}[/]")
    while not url:
        if args.yes:
            sys.exit("Give the website as an argument when using --yes.")
        url = normalise_url(setup.ask("Website to analyse [dim](e.g. www.example.com)[/]"))
        if not url:
            setup.print("[red]That doesn't look like a website address.[/] Example: https://www.example.com")
            setup.drop_last(2)

    url, ignore_tls = preflight(url, ask=not args.yes, out=setup)
    url, ignore_tls, locale = choose_locale(url, ignore_tls, args, out=setup)
    opts = options(args, url, locale)
    limits = [f"up to {opts['max_pages']} pages" if opts["max_pages"] else "",
              f"{opts['per_pattern']} per URL pattern" if opts["per_pattern"] else "",
              f"only paths matching {opts['include']}" if opts["include"] else ""]
    limits = ", ".join(l for l in limits if l)
    setup.print(f"[dim]Crawling {limits or 'every page'}. "
                "For a quick sample of a large site use --max-per-pattern 5.[/]")
    setup.close()
    while True:
        result = run_pipeline(url, ignore_tls, opts, args)
        if result is None:
            return
        # The start page redirected to another site: offer to analyse that one instead
        url = normalise_url(result)
        console.print(f"\n[yellow]![/] Nothing was crawled: the site redirects to [b]{url}[/]. Analysing that instead.")
        url, ignore_tls = preflight(url, ask=not args.yes)
        if not args.out:
            opts["out"] = HERE / "output" / re.sub(r"^www\.", "", urlparse(url).netloc)


def run_pipeline(url: str, ignore_tls: bool, opts: dict, args) -> str | None:
    """Run all steps. Returns a redirect target when the start page left the site, else None."""
    out: Path = opts["out"]
    if out.exists() and any(out.iterdir()):
        shutil.rmtree(out)  # results of an earlier run for the same site
    out.mkdir(parents=True, exist_ok=True)

    crawl = ["crawl.py", url, "--out", str(out), "--max-pages", str(opts["max_pages"]),
             "--max-per-pattern", str(opts["per_pattern"]), "--scroll", "--progress"]
    if opts["include"]:
        crawl += ["--include", opts["include"]]
    if ignore_tls:
        crawl.append("--ignore-https-errors")
    steps = [
        Step("Crawl the site", crawl, determinate=True),
        Step("Group page templates", ["cluster.py", str(out)]),
        Step("Name page types", ["name_types_rules.py", str(out)]),
        Step("Detect components", ["components.py", str(out)]),
        Step("Draft the content model", ["content_model.py", str(out)]),
        Step("Build the report", ["report.py", str(out)]),
    ]

    console.print()
    progress = Progress(SpinnerColumn(), TextColumn("{task.description}"), BarColumn(bar_width=None),
                        MofNCompleteColumn(), TimeElapsedColumn(), console=console, expand=True)
    task = progress.add_task("", total=None)
    started = time.monotonic()
    with open(out / "run.log", "w", encoding="utf-8") as log, Live(render(steps, progress), console=console, refresh_per_second=8) as live:
        for step in steps:
            lines = run_step(step, live, steps, log, progress, task)
            if step is steps[0] and step.status == "done":
                pages, redirect = crawl_outcome(out)
                if pages == 0:
                    live.stop()
                    if redirect and urlparse(redirect).netloc != urlparse(url).netloc:
                        return redirect
                    console.print(f"\n[red]✕ No pages could be crawled.[/] See {out / 'skipped.csv'} and {out / 'run.log'}")
                    sys.exit(1)
            if step.status == "failed":
                live.stop()
                console.print(f"\n[red]✕ {step.title} failed.[/] Last output:")
                for line in [l for l in lines if l.strip()][-15:]:
                    console.print(f"  [dim]{line}[/]")
                console.print(f"\nFull log: {out / 'run.log'}")
                sys.exit(1)

    console.print(f"\n[green]✓ Done in {time.monotonic() - started:.0f}s[/]\n")
    console.print(final_summary(out))
    report = out / "report.html"
    if report.exists() and not args.no_open:
        webbrowser.open(report.as_uri())
    return None


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/]")
        sys.exit(130)
