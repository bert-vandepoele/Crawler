#!/usr/bin/env python3
"""
Component detection (Phase 3, rule-based)

Classifies the visual blocks that crawl.py captured on every page (blocks.json)
into component types such as Hero, RichText, ImageText, CardGrid, Carousel,
Accordion and Form, with variants and candidate content fields, then builds a
component catalogue for the content model.

Each component type has a scoring rule over the block's layout features; the
best score wins, and blocks that score below 0.5 everywhere become Unknown.

Usage:
    python components.py output/extremis

Output (in the crawl folder):
    components.json                every page with its classified blocks, plus the catalogue
    components.csv                 one row per block
    component-catalogue.md         component types with counts, variants, fields, page types
    component-catalogue.html       the same with screenshot crops of examples, page structures
                                   and a review list of unknown / low-confidence blocks
    components/crops/*.jpg         the crops used by the HTML catalogue
"""

import argparse
import csv
import html
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from PIL import Image

from common import load_pages, screenshot_path

Image.MAX_IMAGE_PIXELS = None  # full-page screenshots of long pages are large

CONFIGURATOR_HINT = re.compile(r"configurator|3d-?viewer|product-?viewer|emersya|threekit|zakeke|roomle|sketchfab|"
                               r"expivi|cylindo|vntana|modelviewer|model-viewer", re.I)
HEADLINE_CLASS = re.compile(r"headline|heading|section-?title|block-?title|title-?block", re.I)
CAROUSEL_HINT = re.compile(r"swiper|slick|splide|embla|carousel|slider|glide|flickity|owl-|keen-slider|snap-x|snap-mandatory", re.I)
MAP_HINT = re.compile(r"gm-style|leaflet|mapbox|maplibre|google-map|\bmap(-container|-wrapper|box)?\b|store-locator|locator", re.I)
VIDEO_HOSTS = re.compile(r"youtube|youtu\.be|vimeo|wistia|vidyard|dailymotion|brightcove|jwplayer|player\.", re.I)
MAP_HOSTS = re.compile(r"google\.[a-z.]+$|maps\.|openstreetmap|mapbox", re.I)
LOAD_MORE = re.compile(r"\b(load|show|view|see) (more|all)\b|meer (laden|tonen)|toon meer|alles bekijken|mehr (laden|anzeigen)|voir plus|afficher plus", re.I)
FILTER_LABEL = re.compile(r"^\s*(filters?|sort( by)?|refine|filteren|filter op|filtern|filtrer|trier)\b", re.I)
NEWSLETTER = re.compile(r"newsletter|subscribe|sign up|nieuwsbrief|inschrijven|abonn|anmelden|s.inscrire", re.I)

# Shown in the catalogue, in this order
COMPONENTS = {
    "SiteHeader": "Global header: logo, main navigation, utility links. Usually a global settings entry, not page content.",
    "Breadcrumb": "Trail of links to parent pages. Generated from the page tree, so no editor fields.",
    "Hero": "Large opening block with title and dominant media (image, video or slider).",
    "PageHeader": "Opening block with title and intro text but little or no media.",
    "Heading": "A section title on its own, optionally with a one-line subtitle or eyebrow.",
    "RichText": "Flowing text: paragraphs, subheadings, lists, links.",
    "ImageText": "Media and text side by side (or stacked), often alternating left and right.",
    "Image": "A single prominent image, optionally with a caption.",
    "Gallery": "Several images shown together in a grid or row, with little or no text.",
    "Video": "Embedded or hosted video.",
    "CardGrid": "Repeating cards (image, title, text, link) laid out in columns.",
    "Carousel": "Horizontally scrolling set of slides or cards with arrows or dots.",
    "Columns": "Two to four columns of text side by side.",
    "Accordion": "Collapsible items: question/answer, spec sections.",
    "Tabs": "Content split over tabs.",
    "Quote": "Quotation or testimonial with optional author.",
    "CallToAction": "Short message with one or more buttons, often on a coloured background.",
    "Stats": "Key figures: large numbers with labels.",
    "LogoStrip": "Row or grid of partner or client logos.",
    "Timeline": "Chronological items with years or dates.",
    "Form": "Contact, request or other input form.",
    "EmbeddedForm": "Form loaded from a third-party tool in an iframe (marketing automation, form builder).",
    "Newsletter": "Newsletter sign-up with an email field.",
    "FilterableList": "Long list of entries with search or filters (listing, locator).",
    "FilterBar": "Search and filter controls on their own.",
    "Table": "Tabular data.",
    "Specs": "Key/value specifications (definition list).",
    "Downloads": "List of downloadable files.",
    "ContactDetails": "Address, phone, email and website of a person or location.",
    "Map": "Interactive or embedded map.",
    "Configurator": "Product configurator or 3D viewer (often a third-party iframe): options for frame, fabric, size.",
    "SocialLinks": "Links to social media profiles.",
    "Pagination": "Page numbers or a 'load more' button.",
    "Embed": "Other embedded third-party content (iframe).",
    "Divider": "Visual separator or spacer without content.",
    "SiteFooter": "Global footer: link columns, contact, legal, social. Usually a global settings entry.",
    "Unknown": "No rule matched with enough confidence: review these by eye.",
}


# ---------------------------------------------------------------------------
# Rules: each returns (score 0..1, reason, variant dict)
# ---------------------------------------------------------------------------

def _items(f):
    """Repeating items, ignoring decorative ones (no text, media or links, e.g. an animated grid)."""
    it = f.get("items") or {}
    if it and it["avg_words"] == 0 and it["has_media"] == 0 and it["has_link"] == 0:
        return {}
    return it


def _cols(f):
    return f.get("columns") or []


def _has_h1(f):
    return any(h["level"] == "h1" for h in f["headings"])


def _classes(f):
    return f"{f['classes']} {f['aria']}"


def _media_cols(f):
    return [i for i, c in enumerate(_cols(f)) if c["media_frac"] >= 0.45 or c["video"]]


def _text_cols(f):
    return [i for i, c in enumerate(_cols(f)) if c["media_frac"] < 0.3 and c["words"] >= 10]


def r_site_header(f, ctx):
    return (0.99, "page header outside <main>", {}) if f["region"] == "header" else (0, "", {})


def r_site_footer(f, ctx):
    return (0.99, "page footer outside <main>", {}) if f["region"] == "footer" else (0, "", {})


def r_breadcrumb(f, ctx):
    if "breadcrumb" in _classes(f).lower():
        return 0.95, "breadcrumb class or label", {}
    if f["rect"]["h"] <= 80 and f["words"] <= 15 and f["links"] >= 1 and f["media_frac"] == 0 and ctx["index"] == 0:
        return 0.75, "short row of links at the top of <main>", {}
    return 0, "", {}


def r_hero(f, ctx):
    idx = ctx["content_index"]
    if idx > 1 or f["words"] > 250:
        return 0, "", {}
    score, why = (0.35 if idx == 0 else 0.15), ["first block" if idx == 0 else "second block"]
    mf = f["media_frac"]
    if mf >= 0.5 or f["bg_image"]:
        score += 0.35; why.append("dominant media")
    elif mf >= 0.3:
        score += 0.2; why.append("large media")
    else:
        return 0, "", {}
    if _has_h1(f) or f["font_max"] >= 40:
        score += 0.2; why.append("page title")
    if f["rect"]["h"] >= 350:
        score += 0.1
    if f["full_bleed"]:
        score += 0.05
    if idx == 0 and mf >= 0.7 and f["rect"]["h"] >= 400:
        score += 0.1  # a big media block opening the page is a hero even when it is a video or slider
    it = _items(f)
    slider = f["nav_buttons"] >= 2 or f["dot_buttons"] >= 3 or bool(CAROUSEL_HINT.search(_classes(f)))
    if it.get("count", 0) >= 3 and it.get("has_media", 0) >= 0.6 and it.get("overflows") and mf < 0.6:
        score -= 0.3  # a card carousel near the top, not a hero
    if it.get("count", 0) >= 6 and it.get("has_heading", 0) >= 0.5:
        score -= 0.4  # a listing that starts with the page title
    if _media_cols(f) and _text_cols(f):
        layout = "split, media " + ("left" if _media_cols(f)[0] < _text_cols(f)[0] else "right")
    elif f["bg_image"] or (mf >= 0.8 and f["words"]):
        layout = "text over background media"
    elif not f["words"]:
        layout = "media only"
    else:
        layout = "stacked"
    variant = {"layout": layout, "slider": slider, "video": bool(f["videos"] or any(VIDEO_HOSTS.search(h) for h in f["iframes"]))}
    return min(score, 0.97), ", ".join(why), variant


def r_page_header(f, ctx):
    if not _has_h1(f) or f["media_frac"] >= 0.25 or f["words"] > 150 or _items(f).get("count", 0) >= 4:
        return 0, "", {}
    return (0.85 if ctx["content_index"] <= 1 else 0.7), "h1 with intro text and little media", {
        "intro": f["words"] > 15, "two_columns": len(_cols(f)) >= 2}


def r_carousel(f, ctx):
    it = _items(f)
    score, why = 0.0, []
    if CAROUSEL_HINT.search(_classes(f)) or "carousel" in f["aria"].lower():
        score += 0.45; why.append("slider class or label")
    if it.get("overflows") and it.get("count", 0) >= 2:
        score += 0.35; why.append(f"{it['count']} items overflowing horizontally")
    if f["nav_buttons"] >= 2:
        score += 0.25; why.append("prev/next buttons")
    if f["dot_buttons"] >= 3:
        score += 0.15; why.append("dots")
    if f["nav_buttons"] < 2 and f["images"] >= 1 and re.search(
            r"\b(vorige|volgende|previous|next|précédent|suivant|zurück|weiter)\b", f["text_sample"], re.I) and f["words"] <= 30:
        score += 0.35; why.append("previous/next controls (page viewer)")
    if re.search(r"(^|[\s_-])(n|p|next|prev)-?button|arrow", _classes(f), re.I) and score:
        score += 0.15; why.append("arrow controls")
    if it.get("count", 0) < 2 and f["media_frac"] < 0.5:
        if score >= 0.6 and f["images"] >= 1 and f["links"] >= 1:   # slider class plus arrow controls
            return min(score + 0.1, 0.9), ", ".join(why + ["linked images"]), {"slides": "cards", "items": f["images"], "visible": 1}
        return 0, "", {}
    if it.get("rows", 1) > 1 and not it.get("overflows"):
        score -= 0.2
    kind = "cards" if it.get("has_heading", 0) >= 0.5 or it.get("has_link", 0) >= 0.6 else \
        "images" if it.get("has_media", 0) >= 0.6 else "text slides"
    return min(score, 0.97), ", ".join(why), {"slides": kind, "items": it.get("count"), "visible": it.get("in_view")}


def r_card_grid(f, ctx):
    it = _items(f)
    if it.get("count", 0) >= 2 and it.get("per_row", 1) >= 2 and not it.get("overflows"):
        score = 0.55
        score += 0.15 if it["has_media"] >= 0.6 else 0
        score += 0.1 if it["has_heading"] >= 0.6 else 0
        score += 0.1 if it["has_link"] >= 0.6 else 0
        score += 0.05 if it["equal_width"] else 0
        score += 0.05 if it["count"] >= 3 else 0
        if it["has_media"] >= 0.9 and it["avg_words"] <= 3 and it["has_heading"] < 0.5:
            score -= 0.3  # images without titles: a gallery
        if it["has_media"] < 0.2 and it["has_link"] < 0.5:
            score -= 0.25  # text-only columns
        return min(score, 0.95), f"{it['count']} similar items, {it['per_row']} per row", {
            "columns": it["per_row"], "items": it["count"], "long_list": it["count"] >= 12}
    # Rows of profile-style cards without links (team members): image + name + short text
    cols = _cols(f)
    if it.get("count", 0) >= 2 and it["has_media"] >= 0.8 and it["has_heading"] >= 0.8 and it["avg_words"] <= 30 \
            and (it.get("per_row", 1) >= 2 or (len(cols) >= 2 and all(c["media_frac"] >= 0.5 for c in cols))):
        per_row = max(it.get("per_row", 1), len(cols))
        return 0.72, f"rows of {per_row} image cards with titles", {"columns": per_row, "items": it["count"] * (len(cols) if it.get("per_row") == 1 else 1),
                                                                      "long_list": False, "linked": it["has_link"] >= 0.5}
    # Cards stacked one per row (article lists, download rows)
    if it.get("count", 0) >= 2 and it.get("per_row") == 1 and it["has_media"] >= 0.5 and it["has_heading"] >= 0.5 and it["has_link"] >= 0.5:
        return 0.68, f"{it['count']} cards stacked in one column", {"columns": 1, "items": it["count"], "long_list": it["count"] >= 12}
    # A single card ("Accessories", "Related product"): heading + small linked image
    titled = f["headings"] or HEADLINE_CLASS.search(_classes(f))
    if not it.get("count") and 1 <= f["images"] <= 2 and titled and f["links"] >= 1 and f["words"] <= 40 and f["media_frac"] < 0.5:
        return (0.75 if len(f["headings"]) >= 2 else 0.6), "heading with one linked image card", {"columns": 1, "items": 1, "long_list": False}
    # Several linked images with a short name each ("Complete your setting"), without a repeating card structure
    if not it.get("count") and f["images"] >= 2 and f["links"] >= f["images"] and f["words"] <= 8 * f["images"] + 6 \
            and f["media_frac"] < 0.7:
        return 0.68, f"{f['images']} linked images with short names", {"columns": f["images"], "items": f["images"], "long_list": False}
    return 0, "", {}


def r_gallery(f, ctx):
    it = _items(f)
    cols = _cols(f)
    if it.get("count", 0) >= 3 and it["has_media"] >= 0.8 and it["avg_words"] <= 4 and not it.get("overflows") \
            and (it["has_heading"] < 0.5 or it["has_link"] < 0.5):
        return 0.82, f"{it['count']} image items with little text", {"images": it["count"]}
    if len(cols) >= 2 and all(c["media_frac"] >= 0.5 for c in cols) and f["words"] <= 25:
        return 0.8, f"{len(cols)} images side by side", {"images": len(cols)}
    if f["images"] + f["bg_images"] >= 2 and f["media_frac"] >= 0.5 and f["words"] <= 25 and f["avg_image_area"] > 30000:
        return 0.72, f"{f['images']} images, little text", {"images": f["images"]}
    return 0, "", {}


def r_logo_strip(f, ctx):
    # Logo walls and marquees: many images, (almost) no text; animated tracks repeat the logos
    if f["images"] >= 6 and f["words"] <= 5 and 0 < f["avg_image_area"] < 40000:
        return 0.8, f"{f['images']} logos without text", {"logos": f["images"], "marquee": f["images"] >= 20}
    if f["images"] >= 4 and 0 < f["avg_image_area"] < 25000 and f["words"] <= 20 + f["images"] and f["media_frac"] < 0.5:
        bonus = 0.2 if re.search(r"logo|partner|client|brand", _classes(f), re.I) else 0
        return 0.72 + bonus, f"{f['images']} small images", {"logos": f["images"]}
    return 0, "", {}


def r_image_text(f, ctx):
    media, text = _media_cols(f), _text_cols(f)
    if media and text:
        c = _cols(f)[media[0]]
        return 0.85, "media column next to a text column", {
            "media_position": "left" if media[0] < text[0] else "right",
            "media": "video" if c["video"] else "image", "button": any(_cols(f)[i]["buttons"] for i in text)}
    if not _cols(f) and f["images"] + f["bg_images"] >= 1 and f["media_frac"] > 0.8 and f["words"] >= 20 and not f["videos"] \
            and f["images"] + f["bg_images"] <= 2 and not (f["tel_links"] or f["mail_links"]):
        return 0.66, "text over a background image", {"media_position": "background", "media": "image", "button": f["button_links"] > 0}
    if not _cols(f) and f["images"] + f["bg_images"] >= 1 and 0.25 <= f["media_frac"] <= 0.8 and f["words"] >= 20 \
            and not (f["tel_links"] or f["mail_links"]):
        return (0.62 if f["headings"] else 0.56), "image with text, stacked or overlaid", {
            "media_position": "stacked", "media": "image", "button": f["button_links"] > 0}
    return 0, "", {}


def r_columns(f, ctx):
    cols = _cols(f)
    if len(cols) >= 2 and all(c["media_frac"] < 0.2 for c in cols) and sum(c["words"] >= 8 for c in cols) >= 2:
        return 0.7, f"{len(cols)} text columns side by side", {"columns": len(cols)}
    it = _items(f)
    if it.get("count", 0) >= 2 and it.get("per_row", 1) >= 2 and it["has_media"] < 0.2 and it["has_text"] >= 0.6 and it["has_link"] < 0.5:
        return 0.7, f"{it['count']} text items in columns", {"columns": it["per_row"]}
    return 0, "", {}


def r_heading(f, ctx):
    """A section title on its own ("Contact us", "OUR COLLECTIONS"): short, no media, no list, no controls."""
    w = f["words"]
    if not 1 <= w <= 14 or f["media_frac"] > 0.05 or f["images"] or f["inputs"] or f["links"] >= 2 or _items(f).get("count", 0) >= 3 \
            or f["expandables"] or f["details"] or f["blockquotes"] or f["button_links"] or f["buttons"] \
            or re.match(r'^\s*["“„«]', f["text_sample"]):
        return 0, "", {}
    if f["headings"] and w <= 2 * len(f["headings"][0]["text"].split()) + 8:
        return 0.72, "heading on its own", {"subtitle": w > len(f["headings"][0]["text"].split())}
    if f["font_max"] >= 28 and w <= 10:
        return 0.66, "large text on its own", {"subtitle": False}
    if HEADLINE_CLASS.search(_classes(f)) and w <= 10 and not (CAROUSEL_HINT.search(_classes(f)) and f["rect"]["h"] > 300):
        return 0.68, "headline class on its own", {"subtitle": False}
    return 0, "", {}


def r_rich_text(f, ctx):
    w = f["words"]
    if w < 25:
        if (w >= 3 or f["headings"]) and f["media_frac"] < 0.05 and not f["inputs"]:
            return (0.55 if f["headings"] else 0.5), "short text", {"length": "short", "subheadings": len(f["headings"]), "lists": False}
        return 0, "", {}
    score = 0.45 + min(0.3, f["paragraphs"] * 0.05) + (0.1 if f["headings"] else 0) + (0.05 if f["list_items"] >= 2 else 0)
    if not f["paragraphs"] and f["media_frac"] < 0.05 and not f["inputs"] and not re.match(r'^\s*["“„«]', f["text_sample"]):
        score += 0.15   # text set in <div>s instead of <p>s
    long_article = w >= 150 and (f["paragraphs"] >= 3 or len(f["headings"]) >= 2)
    if f["media_frac"] > 0.3:
        score -= 0.1 if long_article else 0.3   # a long article section with images in it is still text
    if f["inputs"]:
        score -= 0.4
    it = _items(f)
    # Images stacked inside an article are fine; cards side by side (team, products) are a grid
    if it.get("count", 0) >= 3 and it.get("has_media", 0) >= 0.5 and not (
            long_article and it.get("per_row", 1) == 1 and it.get("has_link", 0) < 0.5 and len(_cols(f)) < 2):
        score -= 0.3
    return min(score, 0.9), f"{w} words in {f['paragraphs']} paragraphs", {
        "length": "long" if w > 300 else "medium" if w > 80 else "short",
        "subheadings": len(f["headings"]), "lists": f["list_items"] > 0,
        **({"images": f["images"]} if long_article and f["media_frac"] > 0.3 else {})}


def r_image(f, ctx):
    media = f["images"] + f["bg_images"]
    if media >= 1 and (f["media_frac"] >= 0.6 or f["largest_image_frac"] >= 0.55) and f["words"] <= 20 and f["images"] <= 1 \
            and not f["videos"]:
        return 0.85, "one image filling the block", {"full_bleed": f["full_bleed"], "caption": f["words"] > 0}
    if media >= 1 and f["media_frac"] >= 0.45 and f["words"] <= 20 and f["images"] <= 1 and not f["videos"]:
        return 0.72, "one large image with a short title or caption", {"full_bleed": f["full_bleed"], "caption": f["words"] > 0}
    return 0, "", {}


def r_video(f, ctx):
    hosts = [h for h in f["iframes"] if VIDEO_HOSTS.search(h)]
    if (f["videos"] or hosts) and f["words"] <= 80:
        return 0.88, "video element or video embed", {"source": hosts[0] if hosts else "hosted file"}
    if re.search(r"video|player", _classes(f), re.I) and f["media_frac"] >= 0.5 and f["words"] <= 40:
        return 0.6, "video class with large media", {"source": "unknown"}
    return 0, "", {}


def r_accordion(f, ctx):
    n = max(f["details"], f["expandables"])
    if n < 1 or f["region"] != "main":
        return 0, "", {}
    faq = sum("?" in h["text"] for h in f["headings"]) >= 2 or "faq" in _classes(f).lower()
    score = 0.85 if n >= 2 else 0.66
    if _items(f).get("count", 0) >= 6 and f["inputs"]:
        score -= 0.3  # a filter dropdown on a listing
    return score, f"{n} collapsible element(s)", {"faq": faq}


def r_tabs(f, ctx):
    return (0.9, f"{f['tabs']} tabs", {"tabs": f["tabs"]}) if f["tabs"] >= 2 else (0, "", {})


def r_quote(f, ctx):
    if f["blockquotes"] and f["words"] <= 200:
        return 0.82, "blockquote", {"author": f["words"] > 0}
    longest_line = max((len(line.split()) for line in f["text_sample"].splitlines()), default=0)
    if re.search(r"quote|testimonial|review", _classes(f), re.I) and f["words"] <= 120 and longest_line >= 6 and f["media_frac"] < 0.6:
        return 0.75, "quote/testimonial class", {"author": True}
    if re.match(r'^\s*["“„«]', f["text_sample"]) and f["words"] <= 80:
        return 0.66, "text starts with a quotation mark", {"author": False}
    return 0, "", {}


def r_cta(f, ctx):
    actions = f["button_links"] + max(0, f["buttons"] - f["nav_buttons"] - f["dot_buttons"])
    if 1 <= actions <= 3 and f["words"] <= 8 and not f["headings"] and not f["images"] and f["links"] <= 3 and f["rect"]["h"] < 120:
        return 0.7, "button(s) on their own", {"background": False, "buttons": actions, "button_only": True}
    if actions < 1 or f["words"] > 60 or not f["headings"] or f["media_frac"] >= 0.5 or _items(f).get("count", 0) >= 3:
        return 0, "", {}
    score = 0.65 + (0.15 if f["background"] or f["bg_image"] else 0) + (0.05 if f["full_bleed"] else 0)
    if f["images"] and len(f["headings"]) >= 2:
        score -= 0.2  # section heading plus a titled item with an image: a card, not a call to action
    return score, "heading, short text and a button", {"background": bool(f["background"] or f["bg_image"]), "buttons": actions}


def r_stats(f, ctx):
    n = f["big_numbers"]
    if n >= 2 and f["words"] <= 20 * n + 30:
        return 0.85, f"{n} large numbers", {"figures": n}
    return 0, "", {}


def r_timeline(f, ctx):
    it = _items(f)
    if (it.get("count", 0) >= 3 and it.get("has_year_heading", 0) >= 0.6) or f["years"] >= 3:
        return 0.8 + (0.15 if "timeline" in _classes(f).lower() else 0), "items starting with years", {}
    return 0, "", {}


def r_form(f, ctx):
    if f["email_inputs"] and f["inputs"] <= 3 and (NEWSLETTER.search(f["text_sample"]) or f["inputs"] <= 2):
        return 0, "", {}  # handled by Newsletter
    if f["textareas"] or f["inputs"] >= 3 or (f["forms"] and f["inputs"] >= 2):
        return 0.82, f"{f['inputs']} input fields", {"contact": bool(f["textareas"]), "fields": f["inputs"]}
    return 0, "", {}


def r_embedded_form(f, ctx):
    other = [h for h in f["iframes"] if h and not VIDEO_HOSTS.search(h) and not MAP_HOSTS.search(h)]
    if CONFIGURATOR_HINT.search(_classes(f) + " " + " ".join(other)):
        return 0, "", {}
    if other and (re.search(r"form|contact|request|download|subscribe|newsletter|marketing|hubspot|pardot|typeform|jotform", " ".join(other), re.I)
                  or NEWSLETTER.search(f["text_sample"]) or re.search(r"contact|request|register|download", f["text_sample"], re.I)):
        return 0.74, f"form embedded from {other[0]}", {"embedded": other[0]}
    return 0, "", {}


def r_newsletter(f, ctx):
    if f["email_inputs"] and f["inputs"] <= 3:
        return 0.9, "email field", {}
    if NEWSLETTER.search(f["text_sample"]) and f["words"] <= 60 and (f["inputs"] or f["button_links"]):
        return 0.7, "newsletter wording with a sign-up action", {}
    return 0, "", {}


def r_configurator(f, ctx):
    hint = CONFIGURATOR_HINT.search(_classes(f) + " " + " ".join(f["iframes"]))
    if hint and (f["iframes"] or f["canvas"] or f["buttons"] + f["button_links"] >= 1 or f["words"] <= 30):
        return 0.88, f"configurator or 3D viewer ({hint.group(0)})", {"embedded": next(iter(f["iframes"]), "")}
    return 0, "", {}


def _has_map(f):
    return bool(f["canvas"] or any(MAP_HOSTS.search(h) for h in f["iframes"]) or MAP_HINT.search(_classes(f)))


def r_filterable_list(f, ctx):
    it = _items(f)
    controls = f["inputs"] + f["selects"] + (1 if f["checkboxes"] >= 2 else 0) \
        + (1 if re.search(r"\bfilter|currently showing|showing \d|results", f["text_sample"][:300], re.I) else 0) \
        + (1 if f["button_links"] + f["buttons"] >= 4 else 0)
    if controls and it.get("count", 0) >= 6 and (it["has_media"] >= 0.3 or it["has_heading"] >= 0.5):
        return (0.97 if it["count"] >= 12 else 0.86), f"search/filter controls with {it['count']} entries", {"map": _has_map(f), "entries": it["count"]}
    return 0, "", {}


def r_filter_bar(f, ctx):
    if FILTER_LABEL.search(f["text_sample"]) and f["rect"]["h"] < 150 and f["words"] <= 12:
        return 0.88, "filter/sort toggle", {}
    it = _items(f)
    if it.get("count", 0) >= 3 and it["avg_words"] <= 3 and it["has_media"] == 0 and f["button_links"] + f["buttons"] >= 3 \
            and it.get("rows", 9) <= 2:
        return 0.75, f"{it['count']} filter chips or category links", {"chips": it["count"]}
    # Dropdown filters ("All types", "Available finishes") built from buttons and lists
    if re.search(r"dropdown|dropbtn", _classes(f), re.I) and f["words"] <= 25 and f["media_frac"] < 0.2 \
            and not it.get("count", 0) >= 6:
        return 0.72, "dropdown filters", {}
    # A row of short uppercase terms or tags (categories above a listing), even without buttons
    if it.get("count", 0) >= 4 and it["avg_words"] <= 3 and it["has_media"] == 0 and it.get("per_row", 1) >= 4 \
            and it.get("rows", 9) <= 2 and f["words"] <= 4 * it["count"]:
        return 0.7, f"{it['count']} tags or category chips in a row", {"chips": it["count"]}
    if (f["selects"] or f["checkboxes"] >= 2 or f["inputs"]) and f["rect"]["h"] < 220 and f["words"] <= 30 and not _items(f).get("count", 0) >= 6:
        return 0.68, "filter or search controls", {}
    return 0, "", {}


def r_table(f, ctx):
    return (0.82, f"table with {f['table_rows']} rows", {"rows": f["table_rows"]}) if f["tables"] and f["table_rows"] >= 2 else (0, "", {})


def r_specs(f, ctx):
    return (0.82, f"{f['dl_terms']} key/value pairs", {}) if f["dl_terms"] >= 2 else (0, "", {})


def r_downloads(f, ctx):
    if f["file_links"] >= 2 and f["file_links"] / max(1, f["links"]) >= 0.4:
        return 0.85, f"{f['file_links']} file links", {"files": f["file_links"]}
    if f["file_links"] >= 1 and f["words"] <= 30:
        return 0.6, "file link", {"files": f["file_links"]}
    return 0, "", {}


def r_contact(f, ctx):
    contacts = f["tel_links"] + f["mail_links"]
    if contacts and (f["address"] or f["map_links"] or f["words"] <= 100):
        score = 0.7 + (0.1 if f["tel_links"] and f["mail_links"] else 0) + (0.05 if f["address"] or f["map_links"] else 0)
        return score, "phone/email links", {"map_link": bool(f["map_links"])}
    return 0, "", {}


def r_map(f, ctx):
    return (0.85, "map embed or map container", {}) if _has_map(f) and f["words"] <= 60 else (0, "", {})


def r_social(f, ctx):
    return (0.85, f"{f['social_links']} social links", {}) if f["social_links"] >= 2 and f["words"] <= 12 else (0, "", {})


def r_pagination(f, ctx):
    if re.search(r"pagination|pager", _classes(f), re.I) and f["words"] <= 20:
        return 0.85, "pagination class", {"style": "pages"}
    if LOAD_MORE.search(f["text_sample"]) and f["words"] <= 6:
        return 0.78, "'load more' button", {"style": "load more"}
    return 0, "", {}


def r_embed(f, ctx):
    other = [h for h in f["iframes"] if h and not VIDEO_HOSTS.search(h) and not MAP_HOSTS.search(h)]
    return (0.7, f"iframe from {other[0]}", {"source": other[0]}) if other else (0, "", {})


def r_divider(f, ctx):
    empty = f["words"] == 0 and f["images"] + f["bg_images"] == 0 and f["media_frac"] == 0 and not f["links"] and not f["iframes"]
    if empty and f["rect"]["h"] < 120:
        return (0.8 if f["hr"] else 0.7), "no content (separator or spacer)", {}
    if empty:
        return 0.55, "empty decorative element (animation or graphic)", {"decorative": True}
    return 0, "", {}


RULES = {
    "SiteHeader": r_site_header, "SiteFooter": r_site_footer, "Breadcrumb": r_breadcrumb, "Hero": r_hero,
    "PageHeader": r_page_header, "Heading": r_heading, "Carousel": r_carousel, "CardGrid": r_card_grid, "Gallery": r_gallery,
    "LogoStrip": r_logo_strip, "ImageText": r_image_text, "Columns": r_columns, "RichText": r_rich_text,
    "Image": r_image, "Video": r_video, "Accordion": r_accordion, "Tabs": r_tabs, "Quote": r_quote,
    "CallToAction": r_cta, "Stats": r_stats, "Timeline": r_timeline, "Form": r_form, "EmbeddedForm": r_embedded_form, "Newsletter": r_newsletter,
    "FilterableList": r_filterable_list, "FilterBar": r_filter_bar, "Table": r_table, "Specs": r_specs,
    "Downloads": r_downloads, "ContactDetails": r_contact, "Map": r_map, "Configurator": r_configurator, "SocialLinks": r_social,
    "Pagination": r_pagination, "Embed": r_embed, "Divider": r_divider,
}


# ---------------------------------------------------------------------------
# Content fields per component (candidates for the content model)
# ---------------------------------------------------------------------------

def item_fields(it: dict) -> list[str]:
    checks = [("image", "media", "has_media"), ("title", "text", "has_heading"), ("text", "text", "has_text"),
              ("link", "link", "has_link"), ("button", "link", "has_button"), ("date", "date", "has_date"),
              ("price", "number", "has_price")]
    return [f"{name}: {kind}" for name, kind, key in checks if it.get(key, 0) >= 0.5]


def fields_for(ctype: str, f: dict, variant: dict) -> list[str]:
    heading = ["title: text"] if f["headings"] else []
    it = _items(f)
    items = item_fields(it)
    button = ["button: link"] if f["button_links"] else []
    by_type = {
        "SiteHeader": ["logo: media", "navigation: list of link"] + button,
        "SiteFooter": ["navigation: list of link", "contact: text"] + (["social: list of link"] if f["social_links"] else []),
        "Breadcrumb": [],
        "Hero": ["title: text"] + (["intro: text"] if f["words"] > 15 else [])
                + (["slides: list of {media, title, link}"] if variant.get("slider") else ["media: media"]) + button,
        "PageHeader": ["title: text"] + (["intro: rich text"] if f["words"] > 15 else []),
        "Heading": ["title: text"] + (["subtitle: text"] if variant.get("subtitle") else []),
        "RichText": heading + ["body: rich text"],
        "ImageText": heading + ["body: rich text", "media: media", "media_position: enum(left, right)"] + button,
        "Image": ["image: media"] + (["caption: text"] if f["words"] else []),
        "Gallery": heading + ["images: list of media"],
        "Video": heading + ["video: media or url", "poster: media"],
        "CardGrid": heading + [f"items: list of {{{', '.join(items) or 'reference'}}}"] + ["source: manual or query"],
        "Carousel": heading + [f"slides: list of {{{', '.join(items) or 'media'}}}"],
        "Columns": heading + ["columns: list of {title: text, body: rich text}"],
        "Accordion": heading + ["items: list of {title: text, body: rich text}"],
        "Tabs": ["tabs: list of {label: text, content: rich text}"],
        "Quote": ["quote: text", "author: text"] + (["image: media"] if f["images"] else []),
        "CallToAction": (["buttons: list of link"] if variant.get("button_only") else ["title: text", "text: text", "buttons: list of link"]) + (["background: media or enum"] if variant.get("background") else []),
        "Stats": heading + ["figures: list of {value: text, label: text}"],
        "LogoStrip": heading + ["logos: list of {image: media, link: link}"],
        "Timeline": heading + ["items: list of {year: text, title: text, body: rich text, media: media}"],
        "Form": heading + ["form: reference (form definition)"],
        "EmbeddedForm": heading + ["form_url: url (third-party form)"],
        "Newsletter": ["title: text", "text: text", "list: reference (mailing list)"],
        "FilterableList": heading + ["source: query (content type + filters)", "filters: list of enum"]
                          + (["map: boolean"] if variant.get("map") else []),
        "FilterBar": ["filters: list of enum"],
        "Table": heading + ["table: table"],
        "Specs": heading + ["specs: list of {label: text, value: text}"],
        "Downloads": heading + ["files: list of {title: text, file: media}"],
        "ContactDetails": ["address: text", "phone: text", "email: text", "website: link"],
        "Map": ["location: geo point or address"],
        "Configurator": ["product: reference (product)", "configurator_url: url or embed id"],
        "SocialLinks": ["links: list of {network: enum, url: link}"],
        "Pagination": [],
        "Embed": ["embed: url or code"],
        "Divider": [],
        "Unknown": [],
    }
    return by_type.get(ctype, [])


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_page(blocks: list[dict]) -> list[dict]:
    out = []
    main_index = content_index = 0
    hero_seen = False
    for i, f in enumerate(blocks):
        ctx = {"index": main_index if f["region"] == "main" else -1, "content_index": content_index if f["region"] == "main" else -1}
        scored = []
        for ctype, rule in RULES.items():
            score, reason, variant = rule(f, ctx)
            if score > 0:
                scored.append((round(score, 2), ctype, reason, variant))
        scored.sort(key=lambda s: -s[0])
        if hero_seen:
            scored = [s for s in scored if s[1] != "Hero"]  # a page has at most one hero
        if scored and scored[0][0] >= 0.5:
            score, ctype, reason, variant = scored[0]
            # Confidence is about the choice: a modest score with no real alternative is still a safe call
            runner_up = scored[1][0] if len(scored) > 1 else 0
            if runner_up <= 0.45 and ctype in ("RichText", "ImageText", "Divider", "CardGrid", "Heading"):
                score = max(score, 0.7)
        else:
            score, ctype, reason, variant = (scored[0][0] if scored else 0), "Unknown", \
                (f"best guess {scored[0][1]} ({scored[0][0]})" if scored else "no rule matched"), {}
        hero_seen = hero_seen or ctype == "Hero"
        if f["region"] == "main":
            main_index += 1
            if ctype != "Breadcrumb":
                content_index += 1
        out.append({
            "index": i, "type": ctype, "confidence": score, "reason": reason, "variant": variant,
            "alternatives": [f"{t} {s}" for s, t, _, _ in scored[1:4] if s >= 0.4],
            "fields": fields_for(ctype, f, variant),
            "region": f["region"], "rect": f["rect"],
            "heading": f["headings"][0]["text"] if f["headings"] else None,
            "text": f["text_sample"][:140],
        })
    return merge_neighbours(page_level(out, blocks))


def page_level(out: list[dict], feats: list[dict]) -> list[dict]:
    """Decisions that need the whole page rather than one block.

    - An h1 with text as the last content block is SEO text written for search engines, not a page header.
    - Consecutive blocks with input fields are the rows of one form (themes that wrap every field in its own row).
    """
    main = [b for b in out if b["region"] == "main" and b["type"] not in ("Breadcrumb", "Divider")]
    if len(main) >= 3 and main[-1]["type"] == "PageHeader" and main[0] is not main[-1]:
        b = main[-1]
        f = feats[b["index"]]
        b.update(type="RichText", confidence=0.7, reason="h1 with text at the end of the page (SEO text)",
                 variant={"length": "short" if f["words"] <= 80 else "medium", "subheadings": len(f["headings"]), "lists": False,
                          "seo_text": True})
        b["fields"] = fields_for("RichText", f, b["variant"])

    merged, run, tail = [], [], []

    def flush():
        while run and not feats[run[-1]["index"]]["inputs"]:
            tail.append(run.pop())       # short blocks after the last field are not part of the form
        if sum(1 for b in run if feats[b["index"]]["inputs"]) >= 2:
            first = run[0]
            inputs = sum(feats[b["index"]]["inputs"] for b in run)
            emails = sum(feats[b["index"]]["email_inputs"] for b in run)
            textareas = sum(feats[b["index"]]["textareas"] for b in run)
            newsletter = emails and inputs <= 3 and not textareas
            first.update(type="Newsletter" if newsletter else "Form", confidence=0.8,
                         reason=f"{len(run)} consecutive blocks with {inputs} input fields: one form",
                         variant={} if newsletter else {"contact": bool(textareas), "fields": inputs})
            first["fields"] = ["title: text", "text: text", "list: reference (mailing list)"] if newsletter else \
                (["title: text"] if first.get("heading") else []) + ["form: reference (form definition)"]
            for b in run[1:]:
                _grow(first, b)
            merged.append(first)
        else:
            merged.extend(run)
        merged.extend(reversed(tail))
        run.clear()
        tail.clear()

    for b in out:
        f = feats[b["index"]]
        if b["region"] == "main" and (f["inputs"] or (run and f["words"] <= 12 and not f["images"] and f["buttons"] + f["button_links"] <= 2)):
            run.append(b)
            continue
        flush()
        merged.append(b)
    flush()
    return merged


def merge_neighbours(blocks: list[dict]) -> list[dict]:
    """Consecutive accordion items form one accordion; a short text right below an image is its caption;
    decorative empty elements join the block above; tiny labels ("CASES") join the block below."""
    blocks = _attach_labels(blocks)
    out = []
    for b in blocks:
        prev = out[-1] if out else None
        if prev and prev["type"] in ("FilterBar", "FilterableList") and b["type"] in ("CardGrid", "Gallery", "FilterableList"):
            if prev["type"] == "FilterBar":
                prev.update(type="FilterableList", confidence=max(0.8, b["confidence"]), variant={"map": False, "entries": 0},
                            reason="filter controls above a list of entries")
                prev["fields"] = ["source: query (content type + filters)", "filters: list of enum"]
            prev["variant"]["entries"] = prev["variant"].get("entries", 0) + (b["variant"].get("items") or b["variant"].get("images") or 0)
            _grow(prev, b)
            continue
        if prev and b["type"] == prev["type"] == "Accordion" and abs(b["rect"]["y"] - (prev["rect"]["y"] + prev["rect"]["h"])) < 80:
            prev["merged"] = prev.get("merged", 1) + 1
            prev["reason"] = f"{prev['merged']} consecutive collapsible items"
            prev["confidence"] = max(prev["confidence"], 0.85)
            _grow(prev, b)
            continue
        if prev and b["type"] == "Divider" and b["variant"].get("decorative") and prev["region"] == b["region"] == "main":
            prev.setdefault("variant", {})["decoration"] = True
            _grow(prev, b)
            continue
        if prev and prev["type"] in ("Image", "Gallery") and b["type"] == "RichText" and b["variant"].get("length") == "short" \
                and not b["heading"] and abs(b["rect"]["y"] - (prev["rect"]["y"] + prev["rect"]["h"])) < 120:
            prev["variant"]["caption"] = True
            if "caption: text" not in prev["fields"]:
                prev["fields"].append("caption: text")
            _grow(prev, b)
            continue
        out.append(b)
    return out


def _attach_labels(blocks: list[dict]) -> list[dict]:
    out, label = [], None
    for b in blocks:
        # One-line labels and eyebrows ("CASES", "TWO POINT O x CLIENT") above the real block
        tiny = b["region"] == "main" and b["rect"]["h"] < 60 and not b["heading"] and len(b["text"].split()) <= 8 \
            and (b["type"] == "Unknown" or (b["type"] == "RichText" and b["variant"].get("length") == "short"))
        if tiny:
            label = b
            continue
        if label is not None:
            if b["region"] == "main":
                b["label"] = label["text"].strip()
                r, l = b["rect"], label["rect"]
                r["h"] += r["y"] - l["y"] if l["y"] < r["y"] else 0
                r["y"] = min(r["y"], l["y"])
            else:
                out.append(label)
            label = None
        out.append(b)
    if label is not None:
        out.append(label)
    return out


def _grow(target: dict, extra: dict):
    r, e = target["rect"], extra["rect"]
    bottom = max(r["y"] + r["h"], e["y"] + e["h"])
    r["x"], r["w"] = min(r["x"], e["x"]), max(r["x"] + r["w"], e["x"] + e["w"]) - min(r["x"], e["x"])
    r["h"] = bottom - r["y"]


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------

def load_page_types(crawl_dir: Path) -> dict[str, str]:
    """page id -> page type name (from page-types.json via clusters.csv), when Phase 2 has run."""
    clusters_csv, types_json = crawl_dir / "clusters.csv", crawl_dir / "page-types.json"
    if not clusters_csv.exists():
        return {}
    names = {}
    if types_json.exists():
        names = {t["cluster"]: t["name"] for t in json.loads(types_json.read_text(encoding="utf-8"))["page_types"] if t.get("name")}
    with open(clusters_csv, encoding="utf-8") as f:
        return {row["id"]: names.get(int(row["cluster"]), f"cluster {row['cluster']}") for row in csv.DictReader(f)}


def variant_label(variant: dict) -> str:
    parts = [f"{k}={v}" for k, v in variant.items() if v not in (False, None, 0, "")]
    return ", ".join(parts) or "default"


def build_catalogue(pages: list[dict]) -> dict:
    cat = defaultdict(lambda: {"count": 0, "pages": set(), "page_types": Counter(), "variants": Counter(),
                               "fields": Counter(), "confidence": [], "examples": []})
    for p in pages:
        for b in p["blocks"]:
            c = cat[b["type"]]
            c["count"] += 1
            c["pages"].add(p["id"])
            c["page_types"][p["page_type"]] += 1
            c["variants"][variant_label(b["variant"])] += 1
            c["fields"].update(b["fields"])
            c["confidence"].append(b["confidence"])
            c["examples"].append((p, b))
    result = {}
    for ctype in COMPONENTS:
        if ctype not in cat:
            continue
        c = cat[ctype]
        result[ctype] = {
            "description": COMPONENTS[ctype],
            "count": c["count"],
            "pages": len(c["pages"]),
            "avg_confidence": round(sum(c["confidence"]) / len(c["confidence"]), 2),
            "page_types": dict(c["page_types"].most_common()),
            "variants": dict(c["variants"].most_common()),
            "fields": {k: round(v / c["count"], 2) for k, v in c["fields"].most_common()},
        }
    return result


def pick_examples(pages: list[dict], ctype: str, limit: int) -> list[tuple[dict, dict]]:
    """Highest-confidence instances, spread over different page types and variants first."""
    found = [(p, b) for p in pages for b in p["blocks"] if b["type"] == ctype]
    found.sort(key=lambda pb: -pb[1]["confidence"])
    chosen, seen = [], set()
    for p, b in found:
        key = (p["page_type"], variant_label(b["variant"]))
        if key not in seen:
            chosen.append((p, b)); seen.add(key)
        if len(chosen) >= limit:
            return chosen
    for pb in found:
        if pb not in chosen:
            chosen.append(pb)
        if len(chosen) >= limit:
            break
    return chosen


def crop(page: dict, block: dict, crops_dir: Path, max_height: int = 1600) -> str | None:
    shot = screenshot_path(page)
    if shot is None:
        return None
    name = f"{page['id']}__{block['index']}.jpg"
    target = crops_dir / name
    if not target.exists():
        r = block["rect"]
        with Image.open(shot) as img:
            box = (max(0, r["x"]), max(0, r["y"]), min(img.width, r["x"] + r["w"]), min(img.height, r["y"] + min(r["h"], max_height)))
            if box[2] - box[0] < 10 or box[3] - box[1] < 10:
                return None
            part = img.crop(box).convert("RGB")
            part.thumbnail((640, 640 * 4))
            part.save(target, "JPEG", quality=72)
    return f"components/crops/{name}"


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def page_structures(pages: list[dict]) -> dict[str, dict]:
    """Per page type: how often each component occurs on its pages, and the sequence on a typical page."""
    by_type = defaultdict(list)
    for p in pages:
        by_type[p["page_type"]].append(p)
    out = {}
    for ptype, members in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        presence = Counter(t for p in members for t in {b["type"] for b in p["blocks"] if b["region"] == "main"})
        sequences = Counter(" → ".join(b["type"] for b in p["blocks"] if b["region"] == "main") for p in members)
        out[ptype] = {
            "pages": len(members),
            "components": {t: round(n / len(members), 2) for t, n in presence.most_common()},
            "typical_sequence": sequences.most_common(1)[0][0] if sequences else "",
            "distinct_sequences": len(sequences),
        }
    return out


def write_markdown(path: Path, catalogue: dict, structures: dict, pages: list[dict]):
    md = ["# Component catalogue", "",
          f"Detected by rules (`components.py`) on {len(pages)} pages. "
          "Fields are candidates for the content model: check them against the screenshots in component-catalogue.html.", "",
          "| Component | Instances | Pages | Avg confidence | Main variant |", "| --- | --- | --- | --- | --- |"]
    for ctype, c in catalogue.items():
        md.append(f"| {ctype} | {c['count']} | {c['pages']} | {c['avg_confidence']} | {next(iter(c['variants']))} |")
    for ctype, c in catalogue.items():
        md += ["", f"## {ctype}", "", c["description"], "",
               f"**{c['count']} instances on {c['pages']} pages.** Page types: "
               + ", ".join(f"{t} ({n})" for t, n in c["page_types"].items()), ""]
        if c["fields"]:
            md += ["**Candidate fields** (share of instances):", ""] + [f"- `{k}` ({v:.0%})" for k, v in c["fields"].items()] + [""]
        md += ["**Variants:**", ""] + [f"- {k}: {v}" for k, v in c["variants"].items()]
    md += ["", "## Page structures", ""]
    for ptype, s in structures.items():
        md += [f"### {ptype} ({s['pages']} pages, {s['distinct_sequences']} distinct layouts)", "",
               f"Typical: {s['typical_sequence']}", "",
               "Components on its pages: " + ", ".join(f"{t} {v:.0%}" for t, v in s["components"].items()), ""]
    path.write_text("\n".join(md) + "\n", encoding="utf-8")


HTML_STYLE = """
:root { --bg:#f7f7f5; --panel:#fff; --text:#1d1d1b; --muted:#6b6b66; --line:#e3e3de; --accent:#2f5bd3; --warn:#b8641a; }
@media (prefers-color-scheme: dark) { :root { --bg:#161615; --panel:#20201e; --text:#ececea; --muted:#a3a39d; --line:#34342f; --accent:#8aa6ff; --warn:#f0a35e; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
header.top { padding:32px 24px 8px; max-width:1280px; margin:0 auto; }
h1 { font-size:28px; margin:0 0 4px; } h2 { font-size:22px; margin:0 0 6px; } h3 { font-size:16px; margin:16px 0 6px; }
.muted { color:var(--muted); }
nav.toc { position:sticky; top:0; z-index:2; background:var(--bg); border-bottom:1px solid var(--line); padding:10px 24px; overflow-x:auto; white-space:nowrap; }
nav.toc a { color:var(--text); text-decoration:none; margin-right:14px; font-size:13px; }
nav.toc a span { color:var(--muted); }
main { max-width:1280px; margin:0 auto; padding:8px 24px 64px; }
section { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:20px; margin:20px 0; }
table { border-collapse:collapse; width:100%; font-size:14px; }
td, th { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
code { font-size:13px; }
.meta { display:flex; flex-wrap:wrap; gap:6px 18px; font-size:14px; margin:8px 0; }
.chips span { display:inline-block; border:1px solid var(--line); border-radius:999px; padding:1px 9px; margin:2px 4px 2px 0; font-size:12px; }
.grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(280px, 1fr)); gap:14px; margin-top:12px; }
figure { margin:0; border:1px solid var(--line); border-radius:8px; overflow:hidden; background:var(--bg); }
figure img { display:block; width:100%; max-height:420px; object-fit:cover; object-position:top; }
figcaption { padding:8px 10px; font-size:12px; }
figcaption a { color:var(--accent); }
.low { color:var(--warn); }
.seq { font-size:13px; }
@media (max-width:600px) { header.top, main { padding-left:16px; padding-right:16px; } .grid { grid-template-columns:1fr; } }
"""


def write_html(path: Path, crawl_dir: Path, catalogue: dict, structures: dict, pages: list[dict], examples: int):
    crops_dir = crawl_dir / "components" / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)
    e = html.escape

    def figure(p, b):
        src = crop(p, b, crops_dir)
        img = f'<img loading="lazy" src="{e(src)}" alt="">' if src else '<p class="muted" style="padding:10px">no screenshot</p>'
        conf = f'<span class="{"low" if b["confidence"] < 0.65 else ""}">{b["confidence"]:.2f}</span>'
        alt = f"<br><span class=muted>also: {e(', '.join(b['alternatives']))}</span>" if b["alternatives"] else ""
        return (f"<figure>{img}<figcaption><b>{e(b['type'])}</b> {conf} · {e(variant_label(b['variant']))}<br>"
                f"<a href=\"{e(p['final_url'])}\" target=\"_blank\" rel=\"noopener\">{e(p['title'] or p['final_url'])}</a> "
                f"<span class=muted>({e(p['page_type'])}, block {b['index']})</span><br>"
                f"<span class=muted>{e(b['reason'])}</span>{alt}</figcaption></figure>")

    body = [f"<header class=top><h1>Component catalogue</h1><p class=muted>{len(pages)} pages · "
            f"{sum(c['count'] for c in catalogue.values())} blocks · {len(catalogue)} component types · "
            f"detected by rules, review before modelling</p></header>"]
    body.append("<nav class=toc>" + "".join(f'<a href="#{t}">{t} <span>{c["count"]}</span></a>' for t, c in catalogue.items())
                + '<a href="#structures">Page structures</a><a href="#review">Review</a></nav><main>')

    rows = "".join(f"<tr><td><a href=\"#{t}\">{t}</a></td><td>{c['count']}</td><td>{c['pages']}</td><td>{c['avg_confidence']}</td>"
                   f"<td>{e(', '.join(list(c['page_types'])[:4]))}</td></tr>" for t, c in catalogue.items())
    body.append(f"<section><h2>Overview</h2><table><tr><th>Component</th><th>Instances</th><th>Pages</th>"
                f"<th>Avg confidence</th><th>Main page types</th></tr>{rows}</table></section>")

    for ctype, c in catalogue.items():
        fields = "".join(f"<span><code>{e(k)}</code> {v:.0%}</span>" for k, v in c["fields"].items()) or "<span>none</span>"
        variants = "".join(f"<span>{e(k)} ×{v}</span>" for k, v in c["variants"].items())
        figs = "".join(figure(p, b) for p, b in pick_examples(pages, ctype, examples))
        body.append(f"<section id=\"{ctype}\"><h2>{ctype}</h2><p class=muted>{e(c['description'])}</p>"
                    f"<div class=meta><span><b>{c['count']}</b> instances</span><span><b>{c['pages']}</b> pages</span>"
                    f"<span>avg confidence <b>{c['avg_confidence']}</b></span>"
                    f"<span>page types: {e(', '.join(f'{t} ({n})' for t, n in c['page_types'].items()))}</span></div>"
                    f"<h3>Candidate fields</h3><div class=chips>{fields}</div>"
                    f"<h3>Variants</h3><div class=chips>{variants}</div><div class=grid>{figs}</div></section>")

    rows = "".join(f"<tr><td><b>{e(t)}</b><br><span class=muted>{s['pages']} pages, {s['distinct_sequences']} layouts</span></td>"
                   f"<td class=seq>{e(s['typical_sequence'])}</td>"
                   f"<td class=chips>{''.join(f'<span>{e(k)} {v:.0%}</span>' for k, v in s['components'].items())}</td></tr>"
                   for t, s in structures.items())
    body.append(f"<section id=structures><h2>Page structures</h2><p class=muted>Typical block sequence per page type "
                f"(site header and footer left out) and the share of its pages that contain each component.</p>"
                f"<table><tr><th>Page type</th><th>Typical sequence</th><th>Components</th></tr>{rows}</table></section>")

    review = [(p, b) for p in pages for b in p["blocks"] if b["type"] == "Unknown" or b["confidence"] < 0.65]
    review.sort(key=lambda pb: pb[1]["confidence"])
    body.append(f"<section id=review><h2>Review: unknown and low-confidence blocks</h2>"
                f"<p class=muted>{len(review)} blocks; showing up to 60, lowest confidence first.</p>"
                f"<div class=grid>{''.join(figure(p, b) for p, b in review[:60])}</div></section></main>")

    path.write_text(f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content=\"width=device-width, initial-scale=1\">"
                    f"<title>Component catalogue</title><style>{HTML_STYLE}</style></head><body>{''.join(body)}</body></html>",
                    encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Detect visual components in crawled pages.")
    ap.add_argument("crawl_dir", type=Path, help="Crawl output folder, e.g. output/extremis")
    ap.add_argument("--examples", type=int, default=8, help="Screenshot examples per component in the HTML catalogue (default: 8)")
    args = ap.parse_args()

    page_types = load_page_types(args.crawl_dir)
    pages = []
    for p in load_pages(args.crawl_dir):
        blocks_file = p["dir"] / "blocks.json"
        if not blocks_file.exists():
            continue
        p["page_type"] = page_types.get(p["id"], "unclassified")
        p["blocks"] = classify_page(json.loads(blocks_file.read_text(encoding="utf-8"))["blocks"])
        pages.append(p)
    if not pages:
        sys.exit(f"No blocks.json files in {args.crawl_dir}/pages: re-crawl with the current crawl.py")

    catalogue = build_catalogue(pages)
    structures = page_structures(pages)

    (args.crawl_dir / "components.json").write_text(json.dumps({
        "catalogue": catalogue, "page_structures": structures,
        "pages": [{"id": p["id"], "url": p["final_url"], "page_type": p["page_type"], "blocks": p["blocks"]} for p in pages],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    with open(args.crawl_dir / "components.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["page_id", "url", "page_type", "block", "type", "confidence", "variant", "heading", "y", "height", "reason"])
        for p in pages:
            for b in p["blocks"]:
                w.writerow([p["id"], p["final_url"], p["page_type"], b["index"], b["type"], b["confidence"],
                            variant_label(b["variant"]), b["heading"] or "", b["rect"]["y"], b["rect"]["h"], b["reason"]])
    write_markdown(args.crawl_dir / "component-catalogue.md", catalogue, structures, pages)
    write_html(args.crawl_dir / "component-catalogue.html", args.crawl_dir, catalogue, structures, pages, args.examples)

    total = sum(c["count"] for c in catalogue.values())
    print(f"{len(pages)} pages, {total} blocks\n")
    for ctype, c in catalogue.items():
        print(f"  {ctype:<16} {c['count']:>4} blocks on {c['pages']:>3} pages   avg confidence {c['avg_confidence']:.2f}")
    print(f"\nWrote components.json, components.csv, component-catalogue.md and component-catalogue.html in {args.crawl_dir}")


if __name__ == "__main__":
    main()
