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
import time
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


HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en;q=0.5",
}


def decode(resp):
    """Decode a response as UTF-8 when possible. apparent_encoding
    can guess Latin-1 for Japanese pages, which decodes without
    error but produces mojibake, so it is only the fallback for
    pages that are not valid UTF-8 (e.g. Shift_JIS)."""
    try:
        return resp.content.decode("utf-8")
    except UnicodeDecodeError:
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text


def fetch(url):
    last_exc = None
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            return decode(resp)
        except Exception as exc:  # transient errors: retry with backoff
            last_exc = exc
            time.sleep(2 * (attempt + 1))
    raise last_exc


def fetch_once(url):
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return decode(resp)


def parse_article_meta(html):
    """Pull a thumbnail and a lead paragraph from an article page's
    Open Graph / meta tags. Returns (image_url, lead_text); either
    may be None."""
    soup = BeautifulSoup(html, "html.parser")

    def meta(*keys):
        for key in keys:
            tag = soup.find("meta", attrs={"property": key})
            if tag is None:
                tag = soup.find("meta", attrs={"name": key})
            if tag is not None and tag.get("content"):
                return tag["content"].strip()
        return None

    img = meta("og:image", "twitter:image")
    lead = meta("og:description", "description", "twitter:description")
    if lead:
        lead = re.sub(r"\s+", " ", lead).strip()
        if len(lead) > 300:
            lead = lead[:300].rstrip() + "…"
    if not lead or looks_boilerplate(lead):
        # Some sites give every article the same site boilerplate as
        # its description (or none at all); fall back to the first
        # real paragraph of the article body.
        para = first_paragraph(soup)
        if para:
            lead = para
        elif lead and looks_boilerplate(lead):
            lead = None
    return img, lead or None


BOILERPLATE_MARKERS = ("公式ウェブサイト", "に関する記事一覧ページです")


def looks_boilerplate(text):
    return any(m in text for m in BOILERPLATE_MARKERS)


def first_paragraph(soup):
    # Scan the whole page in document order: some sites wrap the
    # article in <article> tags that are really list cards, so a
    # container-first search can land in the wrong block. Paragraphs
    # inside header / footer / nav / aside are chrome, not body.
    for p in soup.find_all("p"):
        if p.find_parent(["header", "footer", "nav", "aside"]):
            continue
        text = re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()
        if len(text) >= 40 and not looks_boilerplate(text):
            if len(text) > 300:
                text = text[:300].rstrip() + "…"
            return text
    return None


def escape_attr(text):
    return escape(text, {'"': "&quot;"})


def enrich_items(items, cache):
    """Attach a thumbnail (img) and lead text (lead) to items, taken
    from each article page's meta tags, so feed readers show more
    than a bare title. Results are cached in the state file keyed by
    article URL, so each article page is fetched only until its meta
    is captured (at most 3 attempts). Best-effort: any failure just
    leaves the item plain, never fails the feed."""
    changed = False
    for it in items:
        url = it["url"]
        entry = cache.get(url)
        if entry and (entry.get("img") or entry.get("desc")):
            it["img"] = entry.get("img")
            it["lead"] = entry.get("desc")
            continue
        if entry and entry.get("tries", 0) >= 3:
            continue
        try:
            img, lead = parse_article_meta(fetch_once(url))
        except Exception:
            img, lead = None, None
        tries = (entry.get("tries", 0) if entry else 0)
        if not (img or lead):
            tries += 1
        cache[url] = {"img": img, "desc": lead, "tries": tries}
        changed = True
        it["img"] = img
        it["lead"] = lead
        time.sleep(0.3)
    return changed


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


BT_ARTICLE_RE = re.compile(r"^/magazine/news/(?:([^/]+)/)?(\d+)$")
TFP_ARTICLE_RE = re.compile(r"^/news/(\d+)$")
TFP_DATE_RE = re.compile(
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+(\d{1,2}),\s+(\d{4})",
    re.IGNORECASE,
)
MONTHS = {name: i + 1 for i, name in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"])}
JP_CHARS_RE = re.compile(r"[぀-ヿ一-鿿]")


NS_ARTICLE_RE = re.compile(r"^/p/(\d+)$")
MQ_ARTICLE_RE = re.compile(r"^/news/([0-9a-f]{40})/$")
MQ_SECTION_RE = re.compile(r"^/news/[^/]+/$")
JP_DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")


def flatten_events(container, break_check):
    """Flatten a container into ("a", path, query, text) / ("text", text)
    events in document order. break_check(path, query) ends the stream
    (used to stop at pagination)."""
    events = []
    for el in container.descendants:
        if getattr(el, "name", None) == "a":
            parsed = urlparse(el.get("href") or "")
            if break_check(parsed.path, parsed.query):
                break
            # Segments inside one anchor (category label, date line,
            # title lines) are kept apart with newlines; some sites wrap
            # a whole card in a single anchor.
            events.append(("a", parsed.path, parsed.query,
                           el.get_text("\n", strip=True)))
        elif isinstance(el, str):
            text = el.strip()
            if text:
                events.append(("text", text))
    return events


def parse_bijutsutecho_news(html, base_url):
    """美術手帖 news list: thumbnail, category + date (YYYY.MM.DD),
    title, excerpt. The article URL /magazine/news/<category>/<id>
    carries the category, so it is read from the URL itself."""
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main") or soup
    events = flatten_events(
        main,
        lambda path, query: path.rstrip("/") == "/magazine/news"
        and "page=" in query,
    )
    items = []
    pending_date = None
    for ev in events:
        if ev[0] == "text":
            m = DATE_RE.search(ev[1])
            if m:
                pending_date = (int(m.group(1)), int(m.group(2)),
                                int(m.group(3)))
            continue
        m = BT_ARTICLE_RE.match(ev[1])
        if m and len(ev[3]) >= 6 and pending_date is not None:
            items.append({
                "title": ev[3],
                "url": urljoin(base_url, ev[1]),
                "category": (m.group(1) or "news").upper(),
                "date": pending_date,
                "sub": None,
                "subcategory": None,
            })
    by_url = {}
    order = []
    for it in items:
        if it["url"] not in by_url:
            by_url[it["url"]] = it
            order.append(it["url"])
    result = [by_url[k] for k in order]
    result.sort(key=lambda it: it["date"], reverse=True)
    return result


def parse_fashionpost_news(html, base_url):
    """The Fashion Post /news list: a 'news | nov 25, 2026 7:40 pm'
    line, then the English title, the Japanese title and a thumbnail,
    all linking to /news/<id>. Consecutive anchors to the same article
    are grouped; the Japanese title wins as the feed title."""
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main") or soup
    events = flatten_events(
        main,
        lambda path, query: path.startswith("/news/page/"),
    )
    items = []
    pending_date = None
    current = None  # {"url", "date", "texts": [...]}

    def flush():
        nonlocal current
        if current and current["date"] is not None and current["texts"]:
            texts = current["texts"]
            jp = [t for t in texts if JP_CHARS_RE.search(t)]
            # The Japanese title may be split over several nodes inside
            # the card anchor; join its segments back together.
            title = "".join(jp) if jp else max(texts, key=len)
            items.append({
                "title": title,
                "url": current["url"],
                "category": "news",
                "date": current["date"],
                "sub": None,
                "subcategory": None,
            })
        current = None

    for ev in events:
        if ev[0] == "text":
            m = TFP_DATE_RE.search(ev[1])
            if m:
                flush()
                pending_date = (int(m.group(3)),
                                MONTHS[m.group(1).lower()],
                                int(m.group(2)))
            continue
        if TFP_ARTICLE_RE.match(ev[1]):
            url = urljoin(base_url, ev[1])
            if current is None or current["url"] != url:
                flush()
                current = {"url": url, "date": pending_date, "texts": []}
            # One card may be a single anchor holding the category
            # label, the date line and both titles as segments.
            for seg in ev[3].split("\n"):
                seg = seg.strip()
                if not seg:
                    continue
                dm = TFP_DATE_RE.search(seg)
                if dm:
                    current["date"] = (int(dm.group(3)),
                                       MONTHS[dm.group(1).lower()],
                                       int(dm.group(2)))
                elif seg.lower() == "news":
                    continue
                elif len(seg) >= 4:
                    current["texts"].append(seg)
    flush()
    result = []
    seen = set()
    for it in items:
        if it["url"] not in seen:
            seen.add(it["url"])
            result.append(it)
    result.sort(key=lambda it: it["date"], reverse=True)
    return result


def parse_newswitch_latest(html, base_url):
    """newswitch /articles/latest: each div.article card holds an
    image link and a title link to /p/<id>, plus keyword tags. The
    list shows no dates, so items carry date=None and main() stamps
    them with their first-seen time from the state file."""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for card in soup.find_all("div", class_="article"):
        title = None
        url = None
        tags = []
        # The title link lives in div.title; the image link to the
        # same article only carries the "NEW" badge as text, so it
        # must not win. Fall back to the longest link text.
        title_div = card.find("div", class_="title")
        if title_div is not None:
            a = title_div.find("a", href=True)
            if a is not None and NS_ARTICLE_RE.match(urlparse(a["href"]).path):
                url = urljoin(base_url, urlparse(a["href"]).path)
                title = a.get_text(strip=True) or None
        for a in card.find_all("a", href=True):
            path = urlparse(a["href"]).path
            if NS_ARTICLE_RE.match(path):
                text = a.get_text(strip=True)
                if url is None and text and text != "NEW":
                    url = urljoin(base_url, path)
                    title = text
            elif "/keyword/detail/" in path:
                tag = a.get_text(strip=True).lstrip("#").strip()
                if tag:
                    tags.append(tag)
        if url and title:
            items.append({
                "title": title,
                "url": url,
                "category": tags[0] if tags else None,
                "date": None,
                "sub": " ".join("#" + t for t in tags[1:]) or None,
                "subcategory": None,
            })
    return items


def parse_monoque_news(html, base_url):
    """MonoQue /news/ list: each card is an image link and a title
    link to /news/<40-hex>/, followed by the company name, a date
    (YYYY年M月D日) and hashtag links to /news/<section>/."""
    soup = BeautifulSoup(html, "html.parser")
    container = soup
    for div in soup.find_all("div"):
        classes = " ".join(div.get("class") or [])
        if "c-archive__lists" in classes:
            container = div
            break
    events = flatten_events(container, lambda path, query: False)
    items = []
    current = None

    def flush():
        nonlocal current
        if current and current["date"] is not None:
            items.append(current)
        current = None

    for ev in events:
        if ev[0] == "text":
            m = JP_DATE_RE.search(ev[1])
            if m and current is not None and current["date"] is None:
                current["date"] = (int(m.group(1)), int(m.group(2)),
                                   int(m.group(3)))
            continue
        path, text = ev[1], ev[3]
        if MQ_ARTICLE_RE.match(path):
            if len(text) >= 6:
                flush()
                current = {
                    "title": text,
                    "url": urljoin(base_url, path),
                    "category": None,
                    "date": None,
                    "sub": None,
                    "subcategory": None,
                }
            continue
        if (current is not None and MQ_SECTION_RE.match(path)
                and text.startswith("#")):
            tag = text.lstrip("#").strip()
            if tag and current["category"] is None:
                current["category"] = tag
    flush()
    result = []
    seen = set()
    for it in items:
        if it["url"] not in seen:
            seen.add(it["url"])
            result.append(it)
    result.sort(key=lambda it: it["date"], reverse=True)
    return result


PARSERS = {
    "uomo_whatsnew": parse_uomo_whatsnew,
    "bijutsutecho_news": parse_bijutsutecho_news,
    "fashionpost_news": parse_fashionpost_news,
    "newswitch_latest": parse_newswitch_latest,
    "monoque_news": parse_monoque_news,
}


def build_rss(site, items):
    now = datetime.now(JST)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">',
        "<channel>",
        f"<title>{escape(site['name'])}</title>",
        f"<link>{escape(site['site_url'])}</link>",
        f"<description>{escape(site.get('description', site['name']))}</description>",
        "<language>ja</language>",
        f"<lastBuildDate>{format_datetime(now)}</lastBuildDate>",
    ]
    for it in items[: site.get("max_items", 30)]:
        pub = it.get("pub")
        if pub is None:
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
        clean_title = re.sub(r"\s+", " ", it["title"]).strip()
        lines.append(f"<title>{escape(clean_title)}</title>")
        lines.append(f"<link>{escape(it['url'])}</link>")
        lines.append(f'<guid isPermaLink="true">{escape(it["url"])}</guid>')
        lines.append(f"<pubDate>{format_datetime(pub)}</pubDate>")
        if it.get("category"):
            lines.append(f"<category>{escape(it['category'])}</category>")
        # The description carries the thumbnail and lead text as
        # escaped HTML so readers render them in the article view;
        # the category line stays as a trailing paragraph.
        html_parts = []
        if it.get("img"):
            html_parts.append(
                f'<img src="{escape_attr(it["img"])}" alt="">'
            )
        if it.get("lead"):
            html_parts.append(f"<p>{escape(it['lead'])}</p>")
        if desc_parts:
            html_parts.append(f"<p>{escape(' / '.join(desc_parts))}</p>")
        if html_parts:
            lines.append(
                f"<description>{escape(''.join(html_parts))}</description>"
            )
        if it.get("img"):
            img = escape_attr(it["img"])
            lines.append(f'<media:content url="{img}" medium="image"/>')
            lines.append(f'<media:thumbnail url="{img}"/>')
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
    state = load_state()
    enrich_cache = state.setdefault("__enrich__", {})
    all_urls = set()
    failures = 0
    for site in config["sites"]:
        try:
            parser = PARSERS[site["parser"]]
            items = []
            for page in range(1, site.get("pages", 1) + 1):
                if page == 1:
                    url = site["list_url"]
                elif site.get("page_url"):
                    url = site["page_url"].format(page=page)
                else:
                    url = f"{site['list_url']}?page={page}"
                items.extend(parser(fetch(url), site["site_url"]))
            # Re-deduplicate across pages, newest date wins.
            by_url = {}
            order = []
            for it in items:
                if it["url"] not in by_url:
                    by_url[it["url"]] = it
                    order.append(it["url"])
                elif (it["date"] is not None
                        and by_url[it["url"]]["date"] is not None
                        and it["date"] > by_url[it["url"]]["date"]):
                    by_url[it["url"]] = it
            items = [by_url[k] for k in order]
            # Dateless items (newswitch) are stamped with their
            # first-seen time, kept in the state file so the stamp
            # stays stable across runs.
            site_state = state.setdefault(site["id"], {})
            used_state = False
            now = datetime.now(JST)
            for it in items:
                if it["date"] is None:
                    used_state = True
                    ts = site_state.get(it["url"])
                    if ts:
                        it["pub"] = datetime.fromisoformat(ts)
                    else:
                        it["pub"] = now
                        site_state[it["url"]] = now.isoformat()
            items.sort(key=item_dt, reverse=True)
            if not items:
                dump_diagnostics(site)
                raise RuntimeError("no items extracted; keeping previous feed")
            all_urls.update(it["url"] for it in items)
            if enrich_items(items[: site.get("max_items", 30)],
                            enrich_cache):
                used_state = True
            xml = build_rss(site, items)
            out_dir = os.path.dirname(site["output"])
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            with open(site["output"], "w", encoding="utf-8") as f:
                f.write(xml)
            print(f"[{site['id']}] wrote {site['output']} with {len(items)} items")
            if used_state:
                state[site["id"]] = {
                    it["url"]: site_state[it["url"]]
                    for it in items if it["url"] in site_state
                }
                save_state(state)
        except Exception as exc:  # keep the previous feed file untouched
            failures += 1
            print(f"[{site['id']}] FAILED: {exc}", file=sys.stderr)
    if failures == 0:
        # Drop enrichment entries for articles that have rotated out
        # of every feed, so the state file stays small.
        stale = [u for u in enrich_cache if u not in all_urls]
        if stale:
            for u in stale:
                del enrich_cache[u]
            save_state(state)
    return 1 if failures else 0


STATE_PATH = os.path.join("feeds", "state.json")


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


def item_dt(it):
    if it.get("pub") is not None:
        return it["pub"]
    y, m, d = it["date"]
    return datetime(y, m, d, tzinfo=JST)


if __name__ == "__main__":
    sys.exit(main())
