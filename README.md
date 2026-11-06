# Site analysis for headless content modelling (proof of concept)

Crawls an existing website and maps everything a headless content model needs: URL patterns, page types, visual components, locales, header and footer navigation, and a basic SEO check. From that it drafts the content model itself. Everything runs without AI: no language model or AI service is called.

1. **Crawl** (`crawl.py`): render each page in headless Chromium and save HTML, text, screenshots and block layout.
2. **Page types**: group pages by DOM structure (`cluster.py`), then name each group with rules (`name_types_rules.py`).
3. **Components** (`components.py`): classify every visual block (Hero, RichText, CardGrid, Carousel…).
4. **Content model** (`content_model.py`): draft content types, fields, blocks, relations, taxonomies and globals.
5. **Report** (`report.py`): one HTML page with the site and locales, URL patterns, page types, components, the content model and SEO.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

`python3 run.py` does this for you: before the first question it checks the packages in `requirements.txt` and Playwright's Chromium browser, and offers to install what's missing (packages into `.venv`, which it creates when needed). With `--yes` it installs without asking.

## Run

The easiest way is the runner. It asks for the website only and shows each step with a progress bar:

```bash
python3 run.py
```

It works without activating the virtual environment first.

**Settings:** it crawls every page it can find, with no limits. Large sites take a while: about one page per second, so 1,000 pages take roughly 20 minutes. For a quicker sample, or a single section:

```bash
python3 run.py https://www.example.com --max-per-pattern 5      # at most 5 pages per URL pattern
python3 run.py https://www.example.com --include '^/en/'        # only the English section
python3 run.py https://www.example.com --max-pages 500          # stop after 500 pages
```

**Locales:** after you enter the address, it finds the site's languages and regions. It reads the hreflang tags and language switcher on the home page and counts the URLs per locale prefix in the sitemaps. Then it shows them in a numbered list (locale, language, URL count, start path) and asks which one to crawl:
- **A number or code** (for example `4` or `nl`) crawls only that locale, into `output/<host>-<locale>/`.
- **`a`** crawls all locales, into `output/<host>/`.

Single-language sites skip this question. `--locale nl` (or `--locale all`) answers it on the command line. With `--yes` and no `--locale`, it crawls all locales. `--include` replaces the locale choice with your own path filter.

**Before crawling**, it checks the address:
- If the site redirects to another domain, it analyses that domain instead.
- If the TLS certificate is invalid, it asks before crawling anyway. This is the only other question; `--yes` answers it with yes.

**Output:** `output/<host>/`, replacing the results of an earlier run for the same site. The full log of every step is in `run.log`. The report opens in your browser at the end; `--no-open` skips that.

You can also run the steps one by one:

```bash
python crawl.py https://www.example.com --max-pages 150 --max-per-pattern 5 --out output/example
python cluster.py output/example
python name_types_rules.py output/example   # rule-based page-type names, no AI
python components.py output/example          # Phase 3: visual components, no AI
python content_model.py output/example       # Phase 4: draft content model, no AI
python report.py output/example              # one HTML report of everything above
```

### url_groups.py

Groups URL patterns whose nested segments are really slugs. The crawler only turns the last segment into `{slug}`, so `/collections/amanu/sofa` and `/collections/elio/chair` give one pattern per collection. After the crawl, with every known URL in hand, these rules apply:
- **Nested slugs:** 3 or more patterns that differ only in one middle segment become one, with a placeholder named by depth: `/collections/{group}/{slug}`, `/onderwijsniveaus/{group}/{subgroup}/{slug}`. A literal next to an already grouped branch joins it. The section itself and the last segment are never grouped, so translated sections (`/collections`, `/collecties`) stay apart.
- **Flat file URLs:** 10 or more root-level URLs with the same file extension become `/{slug}.html`, which covers shop themes with flat product URLs.

**One locale:** the URL structure is mapped for the crawled locale only. URLs of other locales are translations of the same structure, so the grouping, the URL tree and the URL patterns tab leave them out; the tab says how many were left out per locale. Sites without locale prefixes keep all their URLs.

**URL tree:** every grouped pattern also gets a role:
- **root:** the home page (`/`, `/{locale}`).
- **section page:** a fixed page with a URL branch below it is that branch's parent. `/{locale}/collections` is the parent of `/{locale}/collections/{slug}` and `/{locale}/collections/{group}/{slug}`, and `/cases` is the parent of `/case/{slug}` (singular and plural are matched).
- **landing page:** a fixed page right under the root with nothing below it (`/about`, `/contact`).
- **child page:** a fixed page deeper in the tree, under its parent.

In the URL patterns tab, section pages link to their branch, patterns link back to the section page they sit under, and landing pages are grouped into one `/{landing page}` row per locale prefix. In the content model, collections note their section page (the overview that lists them), and `Page` counts its landing and section pages. Clustering doesn't use the landing grouping: landing pages are usually different templates.

`common.load_pages` gives every analysis step the grouped pattern in `page["pattern"]` (the crawler's own is in `page["pattern_raw"]`). So clustering, page-type naming and the content model all work with grouped routes. The crawler's sampling (`--max-per-pattern`) still uses the ungrouped patterns.

Measured on test crawls: Tribu 242 → 90 patterns, Pelckmans 736 → 6, Extremis 226 → 176.

### report.py

Builds `report.html`, a single self-contained page with six tabs. The analysis behind the Site and SEO tabs is in `site_analysis.py`.

1. **Site:**
   - **Locales:** known/crawled URLs per locale, `<html lang>`, hreflang (self-reference, x-default, known targets) and the language switcher. The switcher check tells you whether it goes to the page's hreflang alternate or to the locale home. A table shows the translated section names per locale (`/en/stories/` → `/nl/verhalen/`, `/fr/articles/`, `/de/geschichten/`), taken from hreflang.
   - **Header and footer navigation:** shown as link trees with their nesting. Menu panels that only open on click are flagged, and so are the language switcher, social links, the legal group, external links and placeholder links (`#`). Copies of the same header (sticky or mobile versions) are merged. The tab also says whether every page has the same header and footer, ignoring locale prefixes, tracking parameters and page-specific switcher targets.
2. **URL patterns:** every (grouped) pattern the crawl came across, with the nested patterns it groups behind a toggle, including sampled-out, other-locale and duplicate URLs. Each pattern shows known/crawled/sampled/out-of-scope counts, locales, the page types of its crawled pages, and example URLs. You can filter by text and by status.
3. **Page types:** a summary table plus one expandable card per type. Each card has the kind, crawled pages, an estimated page count for the whole site, the entity, URL patterns, the typical component sequence, how often each component appears, anomalies, notes, and links to the crawled pages.
4. **Components:** each component type once, with one representative screenshot, its description, counts, the page types that use it, variants and candidate fields.
5. **Content model:** the draft from `content_model.py` (see below). It has an overview table, then one card per content type. Each card shows its structure (fixed slots → block zone → fixed slots), a fields table (type, required, confidence, evidence, examples), allowed blocks, notes and review points. After the cards come the relations, taxonomies and enums, the block library, and the globals, SEO component and localisation.
6. **SEO:** a quick check of basic technical SEO, with pass/warning/fail status and the affected pages per check:
   - **Site files:** robots.txt, sitemap and sitemap coverage.
   - **Basics:** HTTPS, status codes, URL hygiene.
   - **Metadata:** title (present, length, unique), meta description (present, length, unique), H1, canonical, noindex.
   - **Languages:** hreflang (present, self, x-default, matches the switcher, known targets), `<html lang>`.
   - **Content and markup:** Open Graph, JSON-LD, image alt attributes, thin content.
   - **Problems:** duplicates, errors and off-site redirects, placeholder navigation links.

   It only covers the crawled sample, so it isn't a full audit.

The report reads robots.txt from `site.json`, which the crawler writes. For older crawls without that file, the report fetches robots.txt once; use `--offline` to skip that.

The screenshots are embedded, so you can share or send the file on its own.

Start small, check the output of each step, then scale up. Then review `page-types.md` and correct `page-types.json` before Phase 3.

### crawl.py

Rate-limited answers (429 or 503) are retried twice, after the server's `Retry-After` or 10–20 seconds.

Useful flags: `--include '^/nl/'`, `--exclude '/tag/|/page/'`, `--scroll` (lazy-loaded content), `--no-screenshots` (faster), `--concurrency`, `--delay`. `--urls file.txt` crawls only the listed URLs. `--ignore-https-errors` crawls a site with an invalid TLS certificate: the crawler always checks the certificate first, stops when it is invalid unless this flag is set, and records the problem in `site.json` for the SEO tab. Run `python crawl.py -h` for all flags.

- Seeds from robots.txt sitemaps (or `/sitemap.xml`), including sitemap indexes and `.gz` files, then follows internal links.
- Respects robots.txt and waits `--delay` seconds between requests.
- Crawls every page by default. `--max-per-pattern N` samples at most N pages per URL pattern (`/products/{slug}/{id}`) for a quick look at a large site, and `--max-pages N` stops after N pages.
- Saves full-page screenshots as JPEG (about 10× smaller than PNG), so complete crawls stay manageable.
- Skips duplicates found through redirects, canonicals or identical text, and redirects that leave the site.
- Removes cookie/consent widgets (Cookiebot, OneTrust, Usercentrics, …) before extracting and taking screenshots.
- Set a real contact address in `USER_AGENT` in `crawl.py` before crawling a client site.

### cluster.py

Clusters pages on the tag/class tree inside `<main>` (TF-IDF over nodes and parent>child pairs, cosine distance, average-linkage agglomerative clustering). It ignores URLs, so it finds templates that share a layout across URL patterns and catches URL patterns that hide several templates.

- `--threshold` (default 0.3): lower gives more, tighter clusters. Compare a few values; the printed silhouette score helps.
- `--whole-page`: fingerprint all of `<body>`. Use this when page types differ in their header or footer rather than their main content.
- Clusters that consist of one templated URL pattern are merged: `/insights/{slug}` is one route, so one page type, even when some articles have an extra block. The number of layout variants is kept and shown in the page-type notes, so those blocks can be modelled as optional. `--split-layouts` keeps them apart.
- `--samples` (default 5): representative pages per cluster. The medoid comes first, then the most different members, favouring URL patterns not yet picked.

A large cluster of mixed URLs is usually the site's generic block-built page, which is the composition type in the model. Phase 3 breaks that type into components.

### page_recognition.py

Recognises what kind of page every crawled page is, with rules and without AI. `name_types_rules.py` runs it automatically; run it on its own for the per-page list:

```bash
python page_recognition.py output/example      # writes page-recognition.csv
```

**Page types it knows:** HomePage, ArticlePage, ProductPage, ListingPage, CaseStudyPage, JobPage, ContactPage, AboutPage, TeamPage, LegalPage, ServicePage, LocationPage, EventPage, GalleryPage, SupportPage, FaqPage, DownloadsPage, LandingPage, ErrorPage, and ContentPage as the fallback.

**Signals:**
- **URL keywords** in English, Dutch, French and German. A whole segment counts most, a segment that starts with the keyword counts less, and a keyword inside a segment ("product" in "product-care") counts little.
- **Wording:** the title and H1.
- **Markup:** schema.org and `og:type`. Values that appear on most pages of the site are ignored.
- **What's on the page** (from `blocks.json`): forms, phone/email/map links, addresses, prices, file downloads, a list that fills the page, filters, people cards, dates, accordions.
- **Technical:** status codes (ErrorPage) and noindex.

**Section pages** become a ListingPage of whatever their child pages were recognised as. For example, `/insights` above `/insights/…` is an ArticleListing. A one-segment URL like `/jobs` or `/cases` with a list of links counts as a section page too.

**Output:** each page gets a type, a confidence (high/medium/low), the evidence and a runner-up, in `page-recognition.csv`.

### name_types_rules.py

Names each cluster with rules, offline. It writes `page-types.json` / `page-types.md`, the input for the later phases. The names are a first draft, so review the result.

- **Kind:**
  - *collection*: most pages come from a templated URL such as `/products/{slug}/{id}`.
  - *listing*: the index page of a collection section, such as `/products`.
  - *singleton*: the only page with that layout.
  - *composition*: many unrelated URLs that share one flexible layout.
- **Recognised type:** each cluster is recognised by a vote across its pages (`page_recognition.py`).
  - Composition pages, singletons and listings take the recognised name (LegalPage, SupportPage, ArticleListing…).
  - A large generic template whose pages are used for several purposes stays ContentPage, with a "Used for: SupportPage (28), LegalPage (8)…" note.
  - Collections keep their entity name and record what they were recognised as.
  - The report and `page-types.md` show the recognised type, its vote share and the evidence.
- **Name and entity:** come from the URL section, singularised (`/dealers/` → `Dealer` → `DealerDetail`). A schema.org type such as `Product` or `Article` takes precedence when one is present.
- **Content regions:** the top-level blocks inside `<main>` of the medoid page, described by what they contain (title, text, media, repeating list, form). A heading that repeats across samples, like "Accessories", becomes the region name. Regions missing from some samples are marked *varies*.
- **Shared IDs:** if the same `{id}` values appear under several sections, the notes suggest one entity shown in several views (product, inspiration, downloads), and list the other locales that use the same IDs. This check uses every URL the crawl saw, including sampled-out ones.
- **Anomalies:** HTTP errors, "not found" titles, test-like slugs (`test`, `qsdf`, `lorem`, …) and near-empty pages.
- **Split suggestions** flag pages that don't fit a collection's URL template. **Merge candidates** flag clusters that share a name or entity.

### components.py

Recognises visual components on every page without AI. While crawling, `blocks.js` splits each rendered page into blocks: the site header, the sections of `<main>`, and the footer. Pages without a `<main>` element (older CMS and shop themes such as Magento, or `role="main"` on `<body>`) use a fallback:
- **Content:** it climbs from the `<h1>` to the highest container that holds neither the site header nor the footer. It keeps that container's content siblings, skipping top bars and menus before it and stopping at the first footer-like sibling.
- **Sidebar:** a content column next to a narrow sidebar is split, so the column is segmented and the sidebar stays one block.
- **Footer:** without a `<footer>` element, the footer-like container that ends lowest on the page is the footer.
- **Header:** without a `<header>` element, the outermost header-like bar at the top (or a fixed or sticky one) is the header.
- **No `<h1>` or several:** without one, it starts from the page wrapper's children; with several (a pop-up, list items), it keeps the h1 that leads to the most content. An `<h1>` used as a logo is ignored. A content wrapper that also holds the footer is opened up.
- **Lists and forms:** three or more look-alike siblings in a row (news items, results) form one list block, with those siblings as its items. A `<form>` is always one block. A heading is kept together with the block below it, and slider arrows or a "load more" button are attached to the block above. For each block it records layout facts:
- position and size
- flex/grid styles
- repeating items: how many, how many per row, whether they overflow horizontally, what each item contains
- side-by-side columns and the share of media in each
- text, headings, links (files, phone, email, social, maps), buttons, inputs, iframes
- collapsible elements, tabs, large numbers and years

`components.py` scores every block against one rule per component type and keeps the best score. Blocks below 0.5 become `Unknown`.

**Component types:** SiteHeader, Breadcrumb, Hero, PageHeader, Heading, RichText, Configurator (product configurators and 3D viewers), ImageText, Image, Gallery, Video, CardGrid, Carousel, Columns, Accordion, Tabs, Quote, CallToAction, Stats, LogoStrip, Timeline, Form, EmbeddedForm, Newsletter, FilterableList, FilterBar, Table, Specs, Downloads, ContactDetails, Map, SocialLinks, Pagination, Embed, Divider, SiteFooter.

Each block gets:
- a **variant**, for example ImageText media left/right, CardGrid column count, Hero split, background or slider, a FilterableList with a map
- **candidate fields**, for example `items: list of {image: media, title: text, link: link}`

**Confidence** is about the choice, not only the score: a plain text, image-text, card or divider block with no other rule close behind gets at least 0.7. RichText is the fallback, so it scores lower than specific components, but text set in `<div>`s instead of `<p>`s still counts as text, and so does a long article section with images in it.

Also recognised: page viewers ("look inside" previews with previous/next page controls) as Carousel, a button on its own as CallToAction (buttons only), and text over a background image as ImageText.

**Post-processing:**
- an h1 with text as the last content block is SEO text (RichText), not a PageHeader
- consecutive blocks with input fields are one Form (or Newsletter for a single email field)
- one Hero per page
- consecutive accordion items become one Accordion
- a short text under an image becomes its caption
- a filter toggle followed by card lists becomes one FilterableList

**Output:**
- `component-catalogue.html`: per component, its counts, page types, variants, candidate fields, and screenshot crops of examples. Also the typical block sequence per page type, and a review grid of unknown and low-confidence blocks. Open it from the crawl folder, since the crops are linked relatively from `components/crops/`.
- `component-catalogue.md`: the same without images.
- `components.json` / `components.csv`: every block with type, confidence, reason, alternatives, variant and fields.

Tune the rules in `components.py` and re-run it, which takes seconds. Only changes to `blocks.js` need a re-crawl. Crawls made before `blocks.js` existed have no `blocks.json`, so re-crawl them once.

### content_model.py

Drafts a headless content model from the output of the earlier steps, with rules only. Every decision records its evidence and a confidence (high, medium or low), so you review the draft instead of starting from scratch.

- **Content types:**
  - structured data comes first: page types whose pages are schema.org entities (Book, Product, Event, JobPosting…) or `og:type product` become a collection named after that type, even when their URLs look like one-off pages (`/some-title.html` on Magento shops); pages of the same page type without it go to `Page`
  - product-group or CollectionPage pages become a `Category` tree (with `parent` when URLs nest), and the collection they list gets a `categories` field
  - collections become content types, merged by entity (Article, Product, Dealer), with a slug (and `external_id` when the URL has a numeric ID)
  - singletons become single types (HomePage, ContactPage)
  - compositions and listings become one generic `Page`
  - a listing page gets a `CollectionList` block that points to the collection its links lead to
- **Parents from nested URLs:** for an entry under a grouped route (`/collections/{group}/{slug}`) whose page one level up is an entry of a content type, that type gets a parent reference: `parent: reference(Collection)`, or for example `collection: reference(Collection)` when the parent is another type. Listing pages one level up don't count.
- **Spec tables:** values in a fixed Table or Specs block that the DOM comparison names as separate fields ("Cover type", "ISBN") replace the generic table component.
- **Fixed fields or block zone:** a component in the same leading or trailing slot on at least 80% of a type's pages becomes a fixed field. The opening PageHeader/Hero is flattened to `title`, `intro` and `media`; a RichText becomes `body`; others become an embedded component (`cta: component: CallToAction`). Components whose order or presence varies form the `blocks` zone, with the allowed block types and how often each is used. Fields on at least 95% of the pages are marked required.
- **Fields from the DOM:** for types with 3 or more pages (up to 60 sampled, `--sample`), it lines up the `<main>` DOM of the pages:
  - text that is the same on every page is template text
  - text that varies is a field
  - repeated values are lists
  - the same number of values on every page (address line 1 and 2) become separate fields
  - text inside rich-text containers, blocks or CTAs is left to those blocks
- **Names**, in order of trust:
  1. JSON-LD properties (`datePublished`, `author`)
  2. `itemprop`
  3. the label next to the value ("Client", "Services", "Visit website", "© Design by"), with a dictionary in en/nl/fr/de
  4. class names (`.author`, `__date`)
  5. the values themselves (`City, Country` → `location`, street and number → `street`)
  6. the value type
- **Types** come from the values:
  - date, number, price, email, phone, link, media
  - enum: a few values that repeat
  - taxonomy: repeated short terms, values split by `|`, or links to a shared URL pattern
  - reference: links to another collection's URL pattern
  
  Taxonomies with overlapping values are merged across types. An enum that a listing also offers as filter chips becomes a taxonomy.
- **Manual or automatic lists:** a related list that shows the same entries on most pages is filled by a query ("You may also like"), so it gets no field. A list that varies per page is a reference field, flagged for review.
- **JSON-LD only:** structured-data properties that are never visible (`dateModified`, `price`, `availability`) are added, marked as such.
- **Noise filters:** share buttons, map directions, language switcher links, UI controls ("scroll to top"), rows of download buttons, generated counters and a menu inside `<main>` that repeats on three or more types are not fields. Names guessed from the value type alone get low confidence.
- **Pages without `<main>`:** when block detection sees the whole body as one block, the type gets a review note, coverage is shown as unknown, and the body comes from the DOM comparison (40+ words of running text that varies per page).
- **Shared parts:**
  - a block library: every component type with merged fields and style options (layout, background)
  - an `Seo` component, with notes such as a template title suffix
  - globals: main and footer navigation, site settings with logo and social links, and site-wide blocks inside the content area (a promo banner above the breadcrumb that looks the same on most pages of at least half the page types) as `SiteBanner`, so they aren't fields of every type
  - which types are translated, and whether slugs are translated
- **Coverage:** the share of main-content words that fall in a recognised field or block, per type and overall, with the fragments that are not covered.

**Quality:** strongest on collections with many pages and consistent markup. Field types, references and the split between fixed fields and blocks are reliable there. Field names are only as good as the site's labels, JSON-LD and class names; utility-class sites (Tailwind) give fewer hints. Types with 1 or 2 pages can't be compared, so their content stays in blocks. Whether a list is picked by editors or by a query, validations and anything not rendered can't be seen in HTML; the review points say where to look.

**Output:**
- `content-model.json`: content types with fields (name, type, required, list, ref, enum values, share, examples, evidence, source, confidence), block zone and coverage; plus blocks, taxonomies, enums, globals, relations, listings and locales.
- `content-model.md`: the same, readable.

## Output

| Path | Contents |
| --- | --- |
| `pages/<id>/page.json` | metadata, OG, hreflang, JSON-LD, headings, landmarks, images, DOM fingerprint hash and shape |
| `pages/<id>/clean.html` | DOM without scripts, styles and noise attributes |
| `pages/<id>/text.txt` | visible text |
| `pages/<id>/fingerprint.txt` | tag/class tree used for clustering |
| `pages/<id>/screenshot.jpg` | full-page screenshot |
| `inventory.csv` | one row per crawled page |
| `patterns.csv` | URL patterns with counts, sampled-out counts and examples |
| `skipped.csv` | URLs not crawled, with the reason |
| `clusters.json` / `clusters.csv` | clusters with URL patterns, cohesion, samples and members; page → cluster |
| `page-types.json` / `page-types.md` | named page types: the input for Phase 3, and the file you review |
| `pages/<id>/blocks.json` | visual blocks with layout features, captured by `blocks.js` |
| `components.json` / `components.csv` | every block classified: type, confidence, variant, candidate fields |
| `component-catalogue.html` / `.md` | component catalogue with screenshot crops, page structures and a review list |
| `components/crops/*.jpg` | the crops used by the HTML catalogue |
| `site.json` | robots.txt contents and the sitemaps that were read |
| `content-model.json` / `.md` | draft content model: types, fields with evidence and confidence, blocks, relations, taxonomies, globals |
| `report.html` | the report: site (locales, header, footer), URL patterns, page types, components, content model, SEO |
