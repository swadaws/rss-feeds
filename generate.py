#!/usr/bin/env python3
"""Generate RSS 2.0 feeds for sites that do not provide one.

Site definitions live in sites.json. Each site names a parser; the UOMO
parser below works off the document structure of the "what's new" list
(fixed card layout), not on CSS class names, so minor markup changes are
less likely to break it.

Safety rule: a site's feed file is only rewritten when at least one item
was extracted. On any failure the previous feed file is left untouched,
so a broken run never publishes a broken or empty feed.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape

import requests
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))
USER_AGENT = "Mozilla/5.0 (compatible; personal-rss-generator/1.0)"
DATE_RE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})")
ARTICLE_PATH_RE = re.compile(
    r"^/(?:uomolist/[^/]+|[^/]+/(?:news|topic|matome|special)/[^/]+)/$"
)
SECTION_ROOT_RE = re.compile(r"^/[^/]+/$")
SECTION_SUB_RE = re.compile(r"^/[^/]+/[^/]+/$")


def fetch(url):
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def parse_uomo_whatsnew(html, base_url):
    """Extract articles from a UOMO 'what's new' list page.

    Cards sit between the <h1> and the pagination. Each card is, in
    order: image link, category link, optional sub-category link, date
    text (YYYY.MM.DD), an optional series link, the title link, and an
    optional brand/series link. The title link is the anchor whose href
    matches an article URL pattern; fields are gathered from the events
    surrounding it, so the parser does not depend on CSS class names.
    """
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main") or soup

    # Flatten the list body into an event stream of anchors and text
    # nodes in document order. Anchor events carry the URL *path* (the
    # site may use absolute or relative hrefs). The stream ends at the
    # pagination (the list body is over there) or at the UOMO'S CHOICE /
    # RANKING headings, whichever comes first, so side sections never
    # leak in.
    events = []  # ("a", path, text) or ("text", text)
    for el in main.descendants:
        if getattr(el, "name", None) in ("h2", "h3"):
            label = el.get_text(strip=True)
            if "CHOICE" in label or "RANKING" in label:
                break
        if getattr(el, "name", None) == "a":
            parsed = urlparse(el.get("href") or "")
            if "whatsnew" in parsed.path and "page=" in parsed.query:
                break  # pagination: end of the list body
            events.append(("a", parsed.path, el.get_text(strip=True)))
        elif isinstance(el, str):
            text = el.strip()
            if text:
                events.append(("text", text))

    def is_title(i):
        ev = events[i]
        return (
            ev[0] == "a"
            and ARTICLE_PATH_RE.match(ev[1])
            and len(ev[2]) >= 6
        )

    # Walk the stream card by card. A card starts at its image link (an
    # anchor with no text); everything up to the title link is the card's
    # own category / sub-category / date / series, and the first text
    # anchor after the title link (before the next card starts) is its
    # brand label. Bounding the windows this way keeps the previous
    # card's brand link from leaking into the next card.
    items = []
    pending = {"category": None, "subcategory": None, "date": None, "series": None}
    i = 0
    while i < len(events):
        ev = events[i]
        if ev[0] == "a" and ev[2] == "":
            pending = {"category": None, "subcategory": None, "date": None, "series": None}
            i += 1
            continue
        if is_title(i):
            _, href, raw_title = ev
            brand = None
            j = i + 1
            while j < len(events):
                nxt = events[j]
                if nxt[0] == "a":
                    if nxt[2] == "" or is_title(j):
                        break
                    path = nxt[1]
                    if SECTION_ROOT_RE.match(path) or SECTION_SUB_RE.match(path):
                        break
                    brand = nxt[2]
                    break
                j += 1
            items.append(
                {
                    "title": re.sub(r"\s+PR$", "", raw_title).strip(),
                    "url": urljoin(base_url, href),
                    "category": pending["category"],
                    "date": pending["date"],
                    "sub": brand or pending["series"],
                    "subcategory": pending["subcategory"],
                }
            )
            pending = {"category": None, "subcategory": None, "date": None, "series": None}
            i += 1
            continue
        if ev[0] == "text":
            m = DATE_RE.search(ev[1])
            if m:
                pending["date"] = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        else:
            path = ev[1]
            if SECTION_ROOT_RE.match(path) and 0 < len(ev[2]) <= 24:
                pending["category"] = ev[2]
            elif SECTION_SUB_RE.match(path) and 0 < len(ev[2]) <= 10:
                # Sub-category labels are short taxonomy words (まとめ,
                # ニュース, ...). A longer two-segment link is a series
                # name, not a sub-category.
                pending["subcategory"] = ev[2]
            elif ev[2] and not ARTICLE_PATH_RE.match(path):
                pending["series"] = ev[2]
        i += 1

    # Deduplicate by article URL (the list repeats UOMO LIST articles once
    # per featured item). Keep the newest date; first occurrence wins ties.
    by_url = {}
    order = []
    for it in items:
        if it["date"] is None or not it["title"]:
            continue
        key = it["url"]
        if key not in by_url:
            by_url[key] = it
            order.append(key)
        elif it["date"] > by_url[key]["date"]:
            by_url[key] = it
    result = [by_url[k] for k in order]
    result.sort(key=lambda it: it["date"], reverse=True)
    return result


PARSERS = {
    "uomo_whatsnew": parse_uomo_whatsnew,
}


def build_rss(site, items):
    now = datetime.now(JST)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        f"<title>{escape(site['name'])}</title>",
        f"<link>{escape(site['site_url'])}</link>",
        f"<description>{escape(site.get('description', site['name']))}</description>",
        "<language>ja</language>",
        f"<lastBuildDate>{format_datetime(now)}</lastBuildDate>",
    ]
    for it in items[: site.get("max_items", 30)]:
        y, m, d = it["date"]
        pub = datetime(y, m, d, 0, 0, 0, tzinfo=JST)
        desc_parts = []
        if it.get("category"):
            desc_parts.append(it["category"])
        if it.get("subcategory"):
            desc_parts.append(it["subcategory"])
        if it.get("sub"):
            desc_parts.append(it["sub"])
        lines.append("<item>")
        lines.append(f"<title>{escape(it['title'])}</title>")
        lines.append(f"<link>{escape(it['url'])}</link>")
        lines.append(f'<guid isPermaLink="true">{escape(it["url"])}</guid>')
        lines.append(f"<pubDate>{format_datetime(pub)}</pubDate>")
        if it.get("category"):
            lines.append(f"<category>{escape(it['category'])}</category>")
        if desc_parts:
            lines.append(
                f"<description>{escape(' / '.join(desc_parts))}</description>"
            )
        lines.append("</item>")
    lines.append("</channel>")
    lines.append("</rss>")
    return "\n".join(lines) + "\n"


def dump_diagnostics(site):
    """Print what the list page actually looked like, for the run log.

    Called only when extraction produced nothing, so a failed run shows
    whether the site changed shape, blocked the fetch, or moved content
    behind scripts.
    """
    try:
        html = fetch(site["list_url"])
    except Exception as exc:
        print(f"diagnostics: refetch failed: {exc}", file=sys.stderr)
        return
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.find_all("a")
    print(f"diagnostics: html {len(html)} chars, {len(anchors)} anchors, "
          f"main={'yes' if soup.find('main') else 'no'}, "
          f"date matches={len(DATE_RE.findall(html))}", file=sys.stderr)
    print(f"diagnostics: html head: {html[:300]!r}", file=sys.stderr)
    for a in anchors[:40]:
        print(f"diagnostics: a href={a.get('href')!r} text={a.get_text(strip=True)[:50]!r}",
              file=sys.stderr)


def main():
    with open("sites.json", encoding="utf-8") as f:
        config = json.load(f)
    failures = 0
    for site in config["sites"]:
        try:
            parser = PARSERS[site["parser"]]
            items = []
            for page in range(1, site.get("pages", 1) + 1):
                url = site["list_url"] if page == 1 else f"{site['list_url']}?page={page}"
                items.extend(parser(fetch(url), site["site_url"]))
            # Re-deduplicate across pages, newest date wins.
            by_url = {}
            order = []
            for it in items:
                if it["url"] not in by_url:
                    by_url[it["url"]] = it
                    order.append(it["url"])
                elif it["date"] > by_url[it["url"]]["date"]:
                    by_url[it["url"]] = it
            items = [by_url[k] for k in order]
            items.sort(key=lambda it: it["date"], reverse=True)
            if not items:
                dump_diagnostics(site)
                raise RuntimeError("no items extracted; keeping previous feed")
            xml = build_rss(site, items)
            out_dir = os.path.dirname(site["output"])
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            with open(site["output"], "w", encoding="utf-8") as f:
                f.write(xml)
            print(f"[{site['id']}] wrote {site['output']} with {len(items)} items")
        except Exception as exc:  # keep the previous feed file untouched
            failures += 1
            print(f"[{site['id']}] FAILED: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
