// Block capture, run in the rendered page by crawl.py.
//
// Splits the page into visual blocks (sections of <main>, plus the site header and
// footer) and records layout facts per block, so components.py can classify them
// offline: position and size, layout styles, repeating items (cards, slides, logos),
// side-by-side columns, media, text, links, forms and interactive hints.
() => {
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'LINK', 'META']);
  const TEXT_LEVEL = new Set(['P', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'UL', 'OL', 'BLOCKQUOTE', 'TABLE', 'HR',
    'PRE', 'DL', 'SPAN', 'STRONG', 'EM', 'B', 'I', 'BR', 'SMALL', 'LABEL', 'A']);
  const MEDIA = 'img, picture, video, iframe, canvas';
  const vw = document.documentElement.clientWidth;
  const sx = window.scrollX, sy = window.scrollY;

  const box = el => { const r = el.getBoundingClientRect(); return { x: r.left + sx, y: r.top + sy, w: r.width, h: r.height }; };
  const shown = el => {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  const kids = el => [...el.children].filter(c => !SKIP.has(c.tagName) && c.tagName.toLowerCase() !== 'svg' && shown(c));
  const unwrap = el => { let k = kids(el); while (k.length === 1 && kids(k[0]).length) { el = k[0]; k = kids(el); } return el; };
  const sig = el => el.tagName.toLowerCase() + [...el.classList].filter(c => !/\d{3,}|active|current|selected/.test(c)).slice(0, 3).map(c => '.' + c).join('');
  const words = t => (t.match(/\S+/g) || []).length;
  const textOf = el => (el.innerText || '').trim();
  const hasMedia = el => el.matches(MEDIA) || !!el.querySelector(MEDIA) || hasBgImage(el);
  const hasBgImage = el => /url\(/.test(getComputedStyle(el).backgroundImage);

  // -- segmentation ---------------------------------------------------------

  const isHeadingOnly = el => {
    const r = el.getBoundingClientRect();
    return r.height < 220 && words(textOf(el)) <= 25 && !hasMedia(el)
      && !el.querySelector('ul, ol, table, form, input, button')
      && (/^H[1-6]$/.test(el.tagName) || !!el.querySelector('h1, h2, h3, h4, h5, h6'));
  };
  const isIntroText = el => !hasMedia(el) && !el.querySelector('ul, ol, table, form, input')
    && words(textOf(el)) <= 60 && el.getBoundingClientRect().height < 260;
  const stacked = list => list.every((c, i) => i === 0 || c.getBoundingClientRect().top >= list[i - 1].getBoundingClientRect().bottom - 8);
  // Siblings that look alike form a list (cards, rows) that stays one block. Page-builder <section>s often share
  // classes too, so sections, and siblings of very different heights, are never treated as a list.
  const repeating = list => {
    if (list.every(c => c.tagName === 'SECTION')) return false;
    const heights = list.map(c => c.getBoundingClientRect().height);
    if (Math.max(...heights) > 3 * Math.max(1, Math.min(...heights))) return false;
    const counts = {};
    list.forEach(c => { counts[sig(c)] = (counts[sig(c)] || 0) + 1; });
    return list.length >= 3 && Math.max(...Object.values(counts)) / list.length >= 0.6;
  };
  const allTextLevel = list => list.every(c => TEXT_LEVEL.has(c.tagName) && !c.querySelector(MEDIA + ', form'));
  // Two stacked parts where one is media and the other short text: one component (image + caption/text), not two
  const mediaPair = list => list.length === 2 && list.some(c => hasMedia(c) && words(textOf(c)) < 10)
    && list.some(c => !hasMedia(c) && words(textOf(c)) <= 120);

  // Pages with <main> keep the original segmentation; the extra rules below are for themes without it
  let hasMain = false;   // set once the page's <main> is known
  // Side-by-side children (ignoring full-width rows such as a breadcrumb above them): the content column when it is
  // clearly wider and wordier than the sidebar next to it, else null
  const contentColumn = (parent, k) => {
    const width = parent.getBoundingClientRect().width || 1;
    const overlap = (a, b) => Math.min(a.getBoundingClientRect().bottom, b.getBoundingClientRect().bottom)
      - Math.max(a.getBoundingClientRect().top, b.getBoundingClientRect().top) > 40;
    const side = k.filter(c => k.some(o => o !== c && overlap(c, o)));
    if (side.length < 2) return null;
    const widest = side.reduce((a, b) => (b.getBoundingClientRect().width > a.getBoundingClientRect().width ? b : a));
    const ww = widest.getBoundingClientRect().width;
    const rest = side.filter(c => c !== widest);
    return ww >= 0.55 * width && ww <= 0.9 * width && words(textOf(widest)) > 1.5 * words(rest.map(textOf).join(' ')) ? widest : null;
  };
  const segment = (el, depth) => {
    const k = kids(unwrap(el));
    // A form is one block, however many field rows it has
    if (unwrap(el).tagName === 'FORM' || el.tagName === 'FORM') return [[el]];
    // A content column with a narrow sidebar next to it (and maybe a breadcrumb above): segment the column,
    // keep the sidebar as one block
    if (!hasMain && depth < 5 && k.length >= 2 && !stacked(k)) {
      const column = contentColumn(unwrap(el), k);
      if (column) return k.flatMap(c => (c === column ? segment(c, depth + 1) : [[c]]));
    }
    if (depth >= 5 || k.length < 2 || !stacked(k) || allTextLevel(k) || repeating(k) || (depth > 0 && mediaPair(k))) return [[el]];
    const out = [];
    let pending = null; // a heading (plus intro text) is attached to the block that follows it
    for (let i = 0; i < k.length; i++) {
      const c = k[i];
      if (!hasMain && !isHeadingOnly(c)) {
        // Without <main>: three or more consecutive siblings with the same signature are one list (news items, results)
        let j = i;
        while (j + 1 < k.length && sig(k[j + 1]) === sig(c)) j++;
        if (j - i >= 2) {
          out.push((pending || []).concat(k.slice(i, j + 1)));
          pending = null;
          i = j;
          continue;
        }
      }
      if (isHeadingOnly(c) || (pending && isIntroText(c))) { pending = (pending || []).concat([c]); continue; }
      const parts = segment(c, depth + 1);
      if (pending) { parts[0] = pending.concat(parts[0]); pending = null; }
      out.push(...parts);
    }
    if (pending) out.push(pending);
    return out;
  };

  // -- features ---------------------------------------------------------------

  const within = (list, sel) => {
    const set = new Set();
    list.forEach(e => { if (e.matches(sel)) set.add(e); e.querySelectorAll(sel).forEach(x => set.add(x)); });
    return [...set];
  };
  const area = el => { const r = el.getBoundingClientRect(); return r.width * r.height; };
  const looksLikeButton = a => {
    const s = getComputedStyle(a);
    const filled = s.backgroundColor !== 'rgba(0, 0, 0, 0)' && s.backgroundColor !== 'transparent';
    const bordered = parseFloat(s.borderTopWidth) >= 1 && s.borderTopStyle !== 'none' && parseFloat(s.borderBottomWidth) >= 1;
    return a.getAttribute('role') === 'button' || /\b(btn|button|cta)\b/i.test(a.className)
      || ((filled || bordered) && parseFloat(s.paddingLeft) >= 8 && words(textOf(a)) <= 6);
  };

  const findItems = list => {
    let best = null;
    const visit = (el, depth) => {
      if (depth > 7) return;
      const k = kids(el);
      if (k.length >= 2) {
        const groups = {};
        k.forEach(c => { (groups[sig(c)] = groups[sig(c)] || []).push(c); });
        for (const g of Object.values(groups)) {
          if (g.length < 2 || /^(P|H[1-6]|SPAN|BR|STRONG|EM|B|I|SMALL|OPTION)$/.test(g[0].tagName)) continue;
          const avg = g.reduce((s, c) => s + area(c), 0) / g.length;
          if (avg < 1200) continue;
          const score = g.length * Math.sqrt(avg);
          if (!best || score > best.score) best = { score, container: el, items: g };
        }
      }
      k.forEach(c => visit(c, depth + 1));
    };
    list.forEach(e => visit(e, 0));
    // A block that is itself a run of look-alike siblings (news items grouped by segment()): those are the items
    const same = list.filter(e => sig(e) === sig(list[list.length - 1]));
    if (same.length >= 3) {
      const avg = same.reduce((s, c) => s + area(c), 0) / same.length;
      const score = same.length * Math.sqrt(avg);
      if (avg >= 1200 && (!best || score >= best.score)) best = { score, container: same[0].parentElement, items: same };
    }
    if (!best) return null;

    const rects = best.items.map(box);
    const tops = [];
    rects.forEach(r => { if (!tops.some(t => Math.abs(t - r.y) < 12)) tops.push(r.y); });
    const perRow = Math.max(...tops.map(t => rects.filter(r => Math.abs(r.y - t) < 12).length));
    const c = best.container, cr = box(c);
    let overflows = c.scrollWidth > c.clientWidth + 20;
    for (let a = c; a && !overflows && a !== document.body; a = a.parentElement) {
      if (a.scrollWidth > a.clientWidth + 20 && /hidden|auto|scroll|clip/.test(getComputedStyle(a).overflowX)) overflows = true;
    }
    if (rects.some(r => r.x + r.w > vw + 10 || r.x < -10)) overflows = true;
    const widths = rects.map(r => r.w), mean = widths.reduce((a, b) => a + b, 0) / widths.length;
    const sd = Math.sqrt(widths.reduce((a, b) => a + (b - mean) ** 2, 0) / widths.length);
    const frac = test => best.items.filter(test).length / best.items.length;
    return {
      count: best.items.length,
      sig: sig(best.items[0]),
      container_sig: sig(c),
      container_rect: cr,
      rows: tops.length,
      per_row: perRow,
      in_view: rects.filter(r => r.x >= -5 && r.x + r.w <= vw + 5).length,
      overflows,
      equal_width: mean > 0 && sd / mean < 0.15,
      avg_w: Math.round(mean),
      avg_h: Math.round(rects.reduce((a, r) => a + r.h, 0) / rects.length),
      avg_words: Math.round(best.items.reduce((a, it) => a + words(textOf(it)), 0) / best.items.length),
      has_media: frac(hasMedia),
      has_heading: frac(it => !!it.querySelector('h1, h2, h3, h4, h5, h6, strong, b, [class*="title"], [class*="heading"]')),
      has_text: frac(it => !!it.querySelector('p') || words(textOf(it)) > 12),
      has_link: frac(it => it.matches('a[href]') || !!it.querySelector('a[href]')),
      has_button: frac(it => !!it.querySelector('button') || [...it.querySelectorAll('a[href]')].some(looksLikeButton)),
      has_date: frac(it => !!it.querySelector('time') || /\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b|\b(19|20)\d{2}\b/.test(textOf(it).slice(0, 80))),
      has_price: frac(it => /[€$£]\s?\d|\d\s?[€$£]/.test(textOf(it))),
      has_year_heading: frac(it => /^(19|20)\d{2}\b/.test(textOf(it))),
      is_list_item: frac(it => it.tagName === 'LI'),
      sample: textOf(best.items[0]).slice(0, 120),
    };
  };

  const findColumns = list => {
    const queue = list.map(e => [e, 0]);
    while (queue.length) {
      const [el, depth] = queue.shift();
      const k = kids(unwrap(el));
      if (k.length >= 2 && k.length <= 4) {
        const rs = k.map(c => c.getBoundingClientRect());
        const sideBySide = rs.every((r, i) => i === 0 || (r.left >= rs[i - 1].right - 8 && r.top < rs[i - 1].bottom && r.bottom > rs[i - 1].top));
        const span = rs[rs.length - 1].right - rs[0].left, parent = el.getBoundingClientRect().width;
        if (sideBySide && parent > 0 && span / parent > 0.6) {
          return k.map(c => {
            const b = box(c);
            const mediaArea = [c, ...c.querySelectorAll(MEDIA)].filter(m => m.matches(MEDIA) || hasBgImage(m))
              .reduce((s, m) => s + area(m), 0);
            return {
              x: Math.round(b.x), w: Math.round(b.w), h: Math.round(b.h),
              media_frac: Math.min(1, mediaArea / Math.max(1, b.w * b.h)),
              words: words(textOf(c)),
              headings: c.querySelectorAll('h1, h2, h3, h4, h5, h6').length,
              buttons: [...c.querySelectorAll('a[href], button')].filter(a => a.tagName === 'BUTTON' || looksLikeButton(a)).length,
              video: !!c.querySelector('video, iframe'),
            };
          });
        }
      }
      if (depth < 4) k.forEach(c => queue.push([c, depth + 1]));
    }
    return null;
  };

  const features = (list, region) => {
    const rs = list.map(box);
    const x = Math.min(...rs.map(r => r.x)), y = Math.min(...rs.map(r => r.y));
    const rect = { x: Math.round(x), y: Math.round(y),
      w: Math.round(Math.max(...rs.map(r => r.x + r.w)) - x), h: Math.round(Math.max(...rs.map(r => r.y + r.h)) - y) };
    const text = list.map(textOf).join('\n');
    const elements = within(list, '*').slice(0, 1500);

    const imgs = within(list, 'img, picture > img').filter(shown);
    const bgs = elements.filter(e => area(e) > 4000 && hasBgImage(e));
    const mediaEls = [...imgs, ...bgs, ...within(list, 'video, iframe, canvas').filter(shown)];
    const mediaArea = mediaEls.reduce((s, m) => s + area(m), 0);
    const iframes = within(list, 'iframe').map(f => { try { return new URL(f.src, location.href).hostname; } catch (e) { return ''; } });

    const links = within(list, 'a[href]').filter(shown);
    const hrefs = links.map(a => a.href);
    const external = hrefs.filter(h => { try { return new URL(h).host !== location.host && /^https?:/.test(h); } catch (e) { return false; } });
    const fileLinks = hrefs.filter(h => /\.(pdf|zip|docx?|xlsx?|pptx?|dwg|dxf|stp|step|skp|3ds|obj|fbx|rfa|rvt|ai|eps|indd)(\?|#|$)/i.test(h));
    const social = hrefs.filter(h => /facebook\.com|instagram\.com|linkedin\.com|twitter\.com|\/\/x\.com|youtube\.com|pinterest\.|tiktok\.com|vimeo\.com/i.test(h));
    const buttonLinks = links.filter(looksLikeButton);
    const buttons = within(list, 'button').filter(shown);

    const headings = within(list, 'h1, h2, h3, h4, h5, h6').filter(shown).slice(0, 12)
      .map(h => ({ level: h.tagName.toLowerCase(), text: textOf(h).slice(0, 100), size: parseFloat(getComputedStyle(h).fontSize) }));
    const leaves = elements.filter(e => e.childElementCount === 0 && textOf(e) && shown(e));
    const fontMax = Math.max(0, ...leaves.slice(0, 300).map(e => parseFloat(getComputedStyle(e).fontSize)));
    const bigNumbers = leaves.filter(e => /^[+\-~]?\s?[\d.,]+\s?(%|\+|k|K|m|M|x|€|\$|£|km|m²|years?|jaar|ans|jahre)?$/.test(textOf(e))
      && parseFloat(getComputedStyle(e).fontSize) >= 26).length;
    const years = leaves.filter(e => /^(19|20)\d{2}$/.test(textOf(e))).length;

    const inputs = within(list, 'input, select, textarea').filter(i => shown(i) && !/hidden|submit|button|image|reset/.test(i.type || ''));
    const classes = [...new Set(elements.slice(0, 400).flatMap(e => [...e.classList, e.id]).filter(Boolean))].join(' ').slice(0, 1500);
    const aria = elements.slice(0, 400).map(e => [e.getAttribute('aria-label'), e.getAttribute('aria-roledescription'), e.getAttribute('role')].filter(Boolean).join(' ')).filter(Boolean).join(' | ').slice(0, 800);
    const first = unwrap(list[0]), fs = getComputedStyle(first), bs = getComputedStyle(document.body);
    const ownBg = [list[0], first].map(e => getComputedStyle(e).backgroundColor).find(c => c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent' && c !== bs.backgroundColor) || null;

    return {
      region, rect,
      tags: list.map(e => e.tagName.toLowerCase()),
      sig: list.map(sig).join(' + '),
      full_bleed: rect.w >= vw - 16,
      background: ownBg,
      bg_image: bgs.some(b => area(b) > 0.4 * rect.w * rect.h),
      sticky: /sticky|fixed/.test(fs.position) || /sticky|fixed/.test(getComputedStyle(list[0]).position),
      layout: { display: fs.display, flex_direction: fs.flexDirection, grid_columns: fs.gridTemplateColumns === 'none' ? 0 : fs.gridTemplateColumns.split(' ').length },
      words: words(text),
      text_sample: text.slice(0, 300),
      headings,
      font_max: fontMax,
      paragraphs: within(list, 'p').filter(shown).length,
      list_items: within(list, 'li').filter(shown).length,
      images: imgs.length,
      bg_images: bgs.length,
      media_frac: Math.min(1, mediaArea / Math.max(1, rect.w * rect.h)),
      largest_image_frac: Math.min(1, Math.max(0, ...mediaEls.map(area)) / Math.max(1, rect.w * rect.h)),
      avg_image_area: imgs.length ? Math.round(imgs.reduce((s, i) => s + area(i), 0) / imgs.length) : 0,
      videos: within(list, 'video').length,
      iframes,
      canvas: within(list, 'canvas').length,
      links: links.length,
      external_links: external.length,
      file_links: fileLinks.length,
      tel_links: hrefs.filter(h => h.startsWith('tel:')).length,
      mail_links: hrefs.filter(h => h.startsWith('mailto:')).length,
      map_links: hrefs.filter(h => /google\.[a-z.]+\/maps|maps\.app\.goo|goo\.gl\/maps|maps\.apple/i.test(h)).length,
      social_links: social.length,
      button_links: buttonLinks.length,
      buttons: buttons.length,
      nav_buttons: buttons.filter(b => /prev|next|vorige|volgende|précédent|suivant|zurück|weiter|arrow|slide/i.test((b.getAttribute('aria-label') || '') + ' ' + b.className)).length,
      dot_buttons: buttons.filter(b => /dot|bullet|indicator|pagination|go to slide|slide \d/i.test((b.getAttribute('aria-label') || '') + ' ' + b.className)).length,
      expandables: within(list, '[aria-expanded], details, summary').filter(shown).length,
      details: within(list, 'details').length,
      tabs: within(list, '[role="tab"]').length,
      inputs: inputs.length,
      email_inputs: inputs.filter(i => i.type === 'email' || /mail/i.test(i.name || '')).length,
      textareas: inputs.filter(i => i.tagName === 'TEXTAREA').length,
      selects: inputs.filter(i => i.tagName === 'SELECT' || i.getAttribute('role') === 'combobox').length,
      checkboxes: within(list, 'input[type="checkbox"], input[type="radio"], [role="checkbox"]').length,
      forms: within(list, 'form').length,
      tables: within(list, 'table').length,
      table_rows: within(list, 'tr').length,
      dl_terms: within(list, 'dt').length,
      blockquotes: within(list, 'blockquote, q').length,
      address: within(list, 'address').length,
      hr: within(list, 'hr').length,
      time_tags: within(list, 'time').length,
      big_numbers: bigNumbers,
      years,
      classes,
      aria,
      items: findItems(list),
      columns: findColumns(list),
    };
  };

  // -- page ------------------------------------------------------------------

  // No <main> (older CMS and shop themes): find the element that holds the page's own content.
  // Climb from the h1 to the highest ancestor that holds neither the site header nor the footer;
  // without an h1, take the largest common content container. Falls back to the body.
  const contentRoot = (header, footer) => {
    const chrome = el => (header && (el === header || el.contains(header))) || (footer && (el === footer || el.contains(footer)));
    // The content sits in a run of siblings: skip top bars and menus before the anchor, stop at the first footer-like one
    const contentSiblings = (all, at) => {
      const end = all.findIndex((c, i) => i > at && (chrome(c) || FOOTERISH.test(idClass(c))));
      return all.slice(0, end < 0 ? all.length : end).filter((c, i) => !chrome(c)
        && !['HEADER', 'FOOTER', 'NAV'].includes(c.tagName) && (words(textOf(c)) || hasMedia(c))
        && (i >= at || !HEADERISH.test(idClass(c))));
    };
    // Logos are often an h1 inside a link in the header bar: not the page title
    const isLogo = h => {
      const a = h.closest('a');
      return /logo|brand/i.test(idClass(h) + ' ' + idClass(h.parentElement || h) + ' ' + (a ? idClass(a) : ''))
        || (a && new URL(a.href, location.href).pathname.replace(/\/(index\.\w+)?$/, '') === '');
    };
    // From each h1, climb to the highest ancestor that holds neither the site header nor the footer, and take the run
    // of content siblings there. Several h1s (a pop-up, a teaser): keep the one that leads to the most content.
    const fromH1 = h1 => {
      let el = h1;
      while (el.parentElement && el.parentElement !== document.body && !chrome(el.parentElement)) el = el.parentElement;
      const all = el.parentElement ? kids(el.parentElement) : [el];
      return contentSiblings(all, all.indexOf(el));
    };
    const best = [...document.querySelectorAll('h1')]
      .filter(h => shown(h) && !(header && header.contains(h)) && !isLogo(h))
      .map(fromH1)
      .map(list => ({ list, n: words(list.map(textOf).join(' ')) }))
      .sort((a, b) => b.n - a.n)[0];
    if (best && best.n >= 5) return best.list;
    // No h1: the page wrapper's children, from the first one that is not header-like or a menu
    let wrapper = document.body;
    while (true) {
      const k = kids(wrapper).filter(c => !chrome(c) || c.contains(header) && c.contains(footer));
      const holder = k.length === 1 ? k[0] : k.find(c => header && footer && c.contains(header) && c.contains(footer));
      if (!holder || holder === wrapper) break;
      wrapper = holder;
    }
    const isContent = c => !(header && (c === header || c.contains(header))) && c !== footer && !HEADERISH.test(idClass(c))
      && !['HEADER', 'NAV'].includes(c.tagName) && (words(textOf(c)) >= 5 || hasMedia(c)) && c.getBoundingClientRect().height >= 40;
    let all = kids(wrapper);
    let at = all.findIndex(isContent);
    // A content wrapper that also holds the footer: its own children are the blocks
    while (at >= 0 && footer && all[at].contains(footer)) {
      all = kids(all[at]);
      at = all.findIndex(isContent);
    }
    const siblings = at < 0 ? [] : contentSiblings(all, at);
    return siblings.length ? siblings : [document.body];
  };

  const idClass = el => `${el.id || ''} ${typeof el.className === 'string' ? el.className : ''}`;
  const FOOTERISH = /footer|copyright|bottom-container|site-info|colophon/i;
  const HEADERISH = /header|top-?links|link-list|top-?bar|navigation|\bnav\b|menu|skip/i;
  // No <footer> element: the outermost footer-like container in the lower part of the page
  const footerLike = () => {
    const list = [...document.querySelectorAll('[id*="footer" i], [class*="footer" i]')]
      .filter(el => shown(el) && el.getBoundingClientRect().top + window.scrollY > document.documentElement.scrollHeight / 2
        && !el.querySelector('h1'));
    if (!list.length) return null;
    // The one that ends lowest on the page (a "footer-before" block inside the content does not), then its outermost
    // footer-like ancestor
    let best = list.reduce((a, b) => (b.getBoundingClientRect().bottom > a.getBoundingClientRect().bottom + 1 ? b : a));
    for (const o of list) if (o !== best && o.contains(best)) best = o;
    return best;
  };

  // No <header> element: the outermost header-like container at the top of the page
  const headerLike = () => {
    const list = [...document.querySelectorAll('[id*="header" i], [class*="header" i]')]
      .filter(el => shown(el) && el.getBoundingClientRect().height < 400 && el.getBoundingClientRect().width >= 0.6 * vw
        // a fixed or sticky bar can sit anywhere after scrolling; otherwise it must be at the top of the page
        && (['fixed', 'sticky'].includes(getComputedStyle(el).position) || el.getBoundingClientRect().top + window.scrollY < 200));
    return list.find(el => !list.some(o => o !== el && o.contains(el))) || null;
  };

  // A content column next to a narrow sidebar: segment the column, keep the sidebar as one block
  const splitSidebar = el => {
    const inner = unwrap(el);
    const k = kids(inner);
    const column = k.length >= 2 && !stacked(k) ? contentColumn(inner, k) : null;
    if (!column) return [{ el, segment: true }];
    return k.map(c => (c === column ? splitSidebar(c) : [{ el: c, segment: c.getBoundingClientRect().width > 0.9 * inner.getBoundingClientRect().width }])).flat();
  };


  // role="main" on <body> (some themes) says nothing about where the content is
  const main = [...document.querySelectorAll('main, [role="main"]')].find(m => m !== document.body && shown(m)) || null;
  hasMain = !!main;
  const blocks = [];
  const header = [...document.querySelectorAll('header, [role="banner"]')].find(h => shown(h) && !(main && main.contains(h)))
    || (main ? null : headerLike());
  const footer = [...document.querySelectorAll('footer, [role="contentinfo"]')].reverse().find(f => shown(f) && !(main && main.contains(f)))
    || (main ? null : footerLike());
  if (header) blocks.push(features([header], 'header'));
  const roots = (main ? [main] : contentRoot(header, footer)).flatMap(splitSidebar);
  const parts = [];
  for (const part of roots.flatMap(r => (r.segment ? segment(r.el, 0) : [[r.el]]))) {
    if (part.some(e => e === header || e === footer || (header && header.contains(e)) || (footer && footer.contains(e)))) continue;
    if (part.every(e => !words(textOf(e)) && !hasMedia(e) && e.getBoundingClientRect().height < 4)) continue;
    // Slider arrows, dots or a "load more" button rendered as a separate strip belong to the block above
    const controlsOnly = part.every(e => e.getBoundingClientRect().height < 90 && !hasMedia(e) && words(textOf(e)) <= 4
      && !!(e.matches('button') || e.querySelector('button'))
      && !e.matches('[aria-expanded], details') && !e.querySelector('h1, h2, h3, h4, h5, h6, [aria-expanded], details'));
    if (controlsOnly && parts.length) { parts[parts.length - 1] = parts[parts.length - 1].concat(part); continue; }
    parts.push(part);
  }
  parts.forEach(part => blocks.push(features(part, 'main')));
  if (footer) blocks.push(features([footer], 'footer'));
  return { viewport: vw, page_height: document.documentElement.scrollHeight, blocks };
}
