# Site analysis for headless content modelling

Crawls an existing website and maps what a headless content model needs: URL patterns, page types, visual components, locales, navigation and a basic SEO check. From that it drafts the content model itself.

Everything runs locally with rules and statistics. No language model or AI service is called.

| Step | Script | What it does |
| --- | --- | --- |
| 1. Crawl the site | `crawl.py` (+ `blocks.js`) | Renders each page in headless Chromium; saves HTML, text, a screenshot and the page's visual blocks |
| 2. Group page templates | `cluster.py` | Groups pages with the same DOM structure |
| 3. Name page types | `name_types_rules.py` | Names each group (ArticleDetail, ContactPage…) and decides its kind |
| 4. Detect components | `components.py` | Classifies every visual block (Hero, RichText, CardGrid…) |
| 5. Draft the content model | `content_model.py` | Content types, fields, blocks, relations, taxonomies, globals |
| 6. Build the report | `report.py` | One shareable HTML page with everything above |

## Setup

`python3 run.py` sets itself up. Before the first question it checks the packages in `requirements.txt` and Playwright's Chromium browser, and offers to install whatever is missing. Packages go into a `.venv` in this folder, which it creates when needed. After that, `python3 run.py` always uses `.venv`, without activating it first.

To set up by hand:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

Before crawling a client site, put a real contact address in `USER_AGENT` in `crawl.py`.

## Run

```bash
python3 run.py
```

The runner asks its questions in a box at the top, then runs the six steps with a live step list and progress bar:

1. **Website:** the address to analyse. It then checks the address:
   - A redirect to another domain: it analyses that domain instead.
   - An invalid TLS certificate: it asks whether to crawl anyway. The SEO tab flags it as a failure.
2. **Locale:** it finds the site's languages and regions from hreflang, the language switcher and the locale prefixes in the sitemaps, and shows them in a numbered list:
   - A number or code (`4`, `nl`) crawls that locale only, into `output/<host>-<locale>/`.
   - `a` crawls all locales, into `output/<host>/`.
   - Single-language sites skip this question.

It crawls every page it can find, at about one page per second, so 1,000 pages take roughly 20 minutes. Each run replaces the earlier results for the same site. The report opens in your browser at the end.

| Option | Effect |
| --- | --- |
| `https://www.example.com` | the website, so it isn't asked |
| `--locale nl` / `--locale all` | the locale, so it isn't asked |
| `--max-per-pattern 5` | at most 5 pages per URL pattern: a quick sample of a large site |
| `--max-pages 500` | stop after 500 pages |
| `--include '^/en/'` | only paths matching this regex (replaces the locale choice) |
| `--out folder` | another output folder |
| `--yes` | ask nothing: install what's missing, crawl despite an invalid certificate, all locales unless `--locale` |
| `--no-open` | don't open the report at the end |

### Running the steps one by one

```bash
python crawl.py https://www.example.com --max-per-pattern 5 --out output/example
python cluster.py output/example
python name_types_rules.py output/example
python components.py output/example
python content_model.py output/example
python report.py output/example
```

Steps 2–6 only read the crawl folder and take seconds. After changing their rules, re-run them instead of crawling again. Only changes to `crawl.py` or `blocks.js` need a new crawl.

Start small (`--max-per-pattern 5`), check each step's output, then crawl the whole site. Review `page-types.md`; you can correct `page-types.json` by hand before running steps 4–6.

## How it works

### 1. Crawl: `crawl.py` and `blocks.js`

- Starts from the sitemaps in robots.txt (or `/sitemap.xml`), including sitemap indexes and `.gz` files, then follows internal links.
- Respects robots.txt and waits `--delay` seconds (default 1) between requests. Answers 429 or 503 are retried twice, after the server's `Retry-After` or 10–20 seconds.
- Checks the TLS certificate first, and stops when it is invalid unless `--ignore-https-errors` is given. The problem is recorded in `site.json` for the SEO tab.
- Skips duplicates (via redirects, canonicals or identical text) and redirects that leave the site.
- Removes cookie and consent widgets (Cookiebot, OneTrust, Usercentrics…) before extracting text and taking screenshots.
- Saves full-page screenshots as JPEG.
- Records a structural fingerprint of every page: its tag/class tree, 6 levels deep from `<body>`, and always at least 4 levels below `<main>` (app frameworks such as Next.js nest `<main>` deeply).

`blocks.js` runs in each page and splits it into visual blocks: the site header, the sections of the main content, and the footer. For each block it records:
- position and size, flex/grid layout
- repeating items: how many, how many per row, whether they scroll horizontally, what each contains
- side-by-side columns and the share of media in each
- text, headings, links (files, phone, email, social, maps), buttons, inputs, iframes
- collapsible elements, tabs, large numbers and years

Pages without a `<main>` element (older CMS and shop themes, or `role="main"` on `<body>`) use a fallback:
- **Content:** it climbs from the `<h1>` to the highest container that holds neither the header nor the footer. With no `<h1>` it starts from the page wrapper; with several it keeps the one that leads to the most content. An `<h1>` used as a logo is ignored.
- **Header and footer:** without `<header>`/`<footer>` elements, the outermost bar at the top (or a fixed or sticky one) is the header, and the footer-like container that ends lowest is the footer.
- **Sidebar:** a content column next to a narrow sidebar is split, so the sidebar stays one block.
- **Lists:** three or more look-alike siblings in a row (news items, results) form one list block.

Everywhere, a `<form>` is one block, a heading stays with the block below it, and slider arrows or a "load more" button join the block above.

Other crawl flags: `--exclude '/tag/|/page/'`, `--scroll` (lazy-loaded content), `--no-screenshots` (faster), `--concurrency`, `--urls file.txt` (only these URLs), `--keep-query`, `--no-sitemap`. Run `python crawl.py -h` for all of them.

### URL patterns: `url_groups.py`

The crawler turns only the last URL segment into `{slug}`, so `/collections/amanu/sofa` and `/collections/elio/chair` give one pattern per collection. After the crawl, these rules group them:
- **Nested slugs:** 3 or more patterns that differ in one middle segment become one, with a placeholder named by depth: `/collections/{group}/{slug}`, `/onderwijsniveaus/{group}/{subgroup}/{slug}`. The section itself and the last segment are never grouped, so translated sections (`/collections`, `/collecties`) stay apart.
- **Flat file URLs:** 10 or more root-level URLs with the same extension become `/{slug}.html` (shop themes with flat product URLs).
- **One locale:** only the crawled locale's URLs are mapped. Other locales are translations of the same structure; the report says how many URLs it left out per locale.

Each pattern gets a role in the URL tree:
- **root:** the home page (`/`, `/{locale}`)
- **section page:** a fixed page with a branch below it, which it is the parent of. `/cases` is also the parent of `/case/{slug}`.
- **landing page:** a fixed page right under the root with nothing below it (`/about`, `/contact`)
- **child page:** a fixed page deeper in the tree

Every later step works with the grouped patterns (`page["pattern"]`; the crawler's own is in `page["pattern_raw"]`). The crawler's `--max-per-pattern` sampling uses the ungrouped ones.

### 2. Group page templates: `cluster.py`

Clusters pages on the tag/class tree inside `<main>`: TF-IDF over nodes and parent>child pairs, cosine distance, average-linkage agglomerative clustering. It ignores URLs, so it finds one template used under several URL patterns, and URL patterns that hide several templates.

- If `<main>` is empty in most fingerprints, it compares whole pages instead.
- Clusters made of one templated URL pattern are merged: `/insights/{slug}` is one route, so one page type. The number of layout variants is kept, so extra blocks can be modelled as optional. `--split-layouts` keeps them apart.
- `--threshold` (default 0.3): lower gives more, tighter clusters. The printed silhouette score helps compare values.
- `--whole-page`: compare all of `<body>`, for sites whose page types differ in their header or footer.
- `--samples` (default 5): representative pages per cluster, the most typical one first.

### 3. Name page types: `name_types_rules.py` and `page_recognition.py`

`page_recognition.py` recognises every page as one of: HomePage, ArticlePage, ProductPage, ListingPage, CaseStudyPage, JobPage, ContactPage, AboutPage, TeamPage, LegalPage, ServicePage, LocationPage, EventPage, GalleryPage, SupportPage, FaqPage, DownloadsPage, LandingPage, ErrorPage, or ContentPage as the fallback. Its signals:
- URL keywords in English, Dutch, French and German
- the title and H1
- schema.org and `og:type` (values present on most of the site are ignored)
- what's on the page: forms, phone/email/map links, addresses, prices, downloads, filters, people cards, dates, accordions
- status codes and noindex

A section page becomes a listing of what its child pages are (`/insights` above articles is an ArticleListing). Each page gets a type, a confidence, the evidence and a runner-up in `page-recognition.csv`.

`name_types_rules.py` runs it and names each cluster:
- **Kind:**
  - *collection*: most pages come from one templated URL (`/products/{slug}`)
  - *listing*: the index page of a collection (`/products`)
  - *singleton*: the only page with that layout
  - *composition*: many unrelated URLs sharing one flexible layout
- **Name:**
  - Collections are named after their URL section, singularised (`/dealers/` → `DealerDetail`), or after their schema.org type when one is present.
  - Other kinds take the recognised type by a vote across their pages. A large generic template used for several purposes stays ContentPage with a "Used for: …" note.
- **Content regions:** the top-level blocks of the most typical page, described by what they contain. A heading repeated across pages ("Accessories") becomes the region name.
- **Notes:**
  - shared `{id}` values across sections (one entity shown in several views)
  - anomalies: HTTP errors, "not found" titles, test slugs, near-empty pages
  - split suggestions and merge candidates

### 4. Detect components: `components.py`

Scores every block against one rule per component type and keeps the best score. Blocks below 0.5 become `Unknown`.

**Component types:** SiteHeader, Breadcrumb, Hero, PageHeader, Heading, RichText, Configurator, ImageText, Image, Gallery, Video, CardGrid, Carousel, Columns, Accordion, Tabs, Quote, CallToAction, Stats, LogoStrip, Timeline, Form, EmbeddedForm, Newsletter, FilterableList, FilterBar, Table, Specs, Downloads, ContactDetails, Map, SocialLinks, Pagination, Embed, Divider, SiteFooter.

Each block gets:
- a **variant**, such as media left/right, column count, Hero split/background/slider
- **candidate fields**, such as `items: list of {image: media, title: text, link: link}`
- a **confidence** that reflects the choice, not only the score: a plain text, image-text, card or divider block with no other rule close behind gets at least 0.7

Afterwards, per page:
- an h1 with text as the last block is SEO text (RichText), not a PageHeader
- consecutive input blocks become one Form (or a Newsletter for a single email field)
- one Hero per page
- accordion items merge into one Accordion
- a short text under an image becomes its caption
- a filter toggle followed by card lists becomes one FilterableList

### 5. Draft the content model: `content_model.py`

Every decision records its evidence and a confidence (high, medium, low), so you review a draft instead of starting from scratch.

**Content types**
- Pages that are schema.org entities (Book, Product, Event, JobPosting…) or `og:type product` become a collection named after that type, even with one-off URLs. Other pages of the same page type go to `Page`.
- Product-group or CollectionPage pages become a `Category` tree, and the collection they list gets a `categories` field.
- Collections become content types, merged by entity, with a slug (and `external_id` for numeric URL IDs).
- Singletons become single types (HomePage, ContactPage). Compositions and listings become one generic `Page`; a listing page gets a `CollectionList` block pointing to the collection it links to.
- An entry under a nested route whose page one level up is an entry too gets a parent reference (`parent: reference(Collection)`).
- Collections note their section page; `Page` counts its landing and section pages.

**Fixed fields and the block zone**
- A component in the same leading or trailing slot on at least 80% of a type's pages becomes a fixed field. The opening PageHeader or Hero becomes `title`, `intro` and `media`, and a RichText becomes `body`. Other components become embedded components, such as `cta: component: CallToAction`.
- Components whose order or presence varies form the `blocks` zone, with the allowed block types and how often each is used.
- Fields on at least 95% of the pages are required.

**Fields from the DOM:** for types with 3 or more pages (up to 60 sampled, `--sample`), it lines up the main-content DOM of the pages:
- text that is the same on every page is template text
- text that varies is a field
- repeated values are lists
- values in a fixed table ("ISBN", "Cover type") become separate fields

**Field names**, in order of trust:
1. JSON-LD properties
2. `itemprop`
3. the label next to the value, using a dictionary in en/nl/fr/de
4. class names
5. the value itself (`City, Country` → `location`)
6. the value type

**Field types:** date, number, price, email, phone, link, media, enum (a few repeating values), taxonomy (repeated short terms, or links to a shared URL pattern), reference (links to another collection).
- A list that shows the same entries on most pages is filled by a query and gets no field. A list that varies is a reference field, flagged for review.
- JSON-LD properties that are never visible are added and marked as such.
- Share buttons, language links, UI controls and similar are filtered out.

**Shared parts**
- a block library: every component type, with merged fields and style options
- an `Seo` component
- globals: navigation, footer, site settings, and site-wide banners (`SiteBanner`)
- localisation: which types are translated, and whether slugs are

**Coverage:** the share of main-content words that fall in a field or block, per type and overall, with the fragments that aren't covered.

**Quality:** strongest on collections with many pages and consistent markup. Field names are only as good as the site's labels, JSON-LD and class names; utility-class sites (Tailwind) give fewer hints. Types with 1 or 2 pages can't be compared, so their content stays in blocks. Validations, editorial choices and anything not rendered can't be seen in HTML. The review points say where to look.

### 6. Build the report: `report.py`

`report.html` is one self-contained page (screenshots embedded), so you can send it on its own. It has six tabs:

1. **Site:**
   - locales: URLs per locale, `<html lang>`, hreflang, the language switcher, translated section names
   - header and footer navigation as link trees, with menus that only open on click, social, legal, external and placeholder (`#`) links flagged
2. **URL patterns:** every grouped pattern with its role in the URL tree, page counts, locales, page types and examples. You can filter it by text and status.
3. **Page types:** a summary table and one card per type: kind, page counts, URL patterns, typical component sequence, anomalies, notes.
4. **Components:** each type once, with a screenshot, description, counts, page types, variants and candidate fields.
5. **Content model:** an overview, one card per content type (structure, fields with confidence and evidence, allowed blocks, review points), then relations, taxonomies, the block library, globals and localisation.
6. **SEO:** a quick technical check of the crawled pages, with pass/warning/fail per check:
   - robots.txt, the sitemap and its coverage
   - HTTPS, status codes, URL hygiene
   - title, description, H1, canonical, noindex
   - hreflang and `<html lang>`
   - Open Graph, JSON-LD, alt text, thin content
   - duplicates and errors

   It only covers the crawled pages, so it isn't a full audit.

For crawls without `site.json`, the report fetches robots.txt once; `--offline` skips that.

## Output

Everything goes into `output/<host>/` (or `output/<host>-<locale>/`).

| Path | Contents |
| --- | --- |
| `run.log` | full output of every step |
| `inventory.csv` | one row per crawled page |
| `patterns.csv` | URL patterns with counts and examples |
| `skipped.csv` | URLs not crawled, with the reason |
| `site.json` | robots.txt, the sitemaps read, the TLS check |
| `pages/<id>/page.json` | metadata, Open Graph, hreflang, JSON-LD, headings, landmarks, images |
| `pages/<id>/clean.html` | the rendered DOM without scripts, styles and noise attributes |
| `pages/<id>/text.txt` | visible text |
| `pages/<id>/fingerprint.txt` | tag/class tree used for clustering |
| `pages/<id>/blocks.json` | visual blocks with layout facts, from `blocks.js` |
| `pages/<id>/screenshot.jpg` | full-page screenshot |
| `clusters.json` / `clusters.csv` | clusters with patterns, cohesion, samples and members |
| `page-recognition.csv` | recognised type per page, with evidence |
| `page-types.json` / `.md` | named page types: the file to review |
| `components.json` / `.csv` | every block with type, confidence, variant and fields |
| `component-catalogue.html` / `.md` | component catalogue with screenshot crops and a review list (open the HTML from the crawl folder) |
| `components/crops/*.jpg` | crops used by the catalogue |
| `content-model.json` / `.md` | the draft content model |
| `report.html` | the report |
