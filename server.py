#!/usr/bin/env python3
"""
Local backend, media stream proxy, Turbo Downloader, and static server for PH Explorer.
Endpoints:
  - GET  /api/health           : Health check
  - GET  /api/hottest          : Hottest videos in IP location or chosen country (?cc=...)
  - GET  /api/search           : Full Pornhub /video/search with relevance/views/rating/newest/longest
  - GET  /api/autocomplete     : Live search & performer autocomplete suggestions
  - GET  /api/performer        : Dedicated Pornstar / Model / Channel profile & video feed
  - GET  /api/video            : Watch-page streams (HLS + MP4), chapters, peaks, related, comments
  - GET  /api/comments         : Popular / Newest comment fetching & pagination
  - GET  /api/downloads        : Active Turbo Download jobs + Offline Library items
  - POST /api/downloads/start  : Start multi-threaded Turbo MP4 download to Chores/Downloads
  - POST /api/downloads/cancel : Cancel an active Turbo Download job
  - POST /api/downloads/delete : Delete a local downloaded file
  - GET  /api/local_video      : HTTP 206 Range streaming for downloaded local .mp4 files
  - GET  /api/hls              : Same-origin HLS (.m3u8 / .ts) proxy with Referer injection
  - GET  /api/stream           : Range-enabled (HTTP 206) CDN MP4 stream & browser-download proxy
  - GET  /api/thumb            : Thumbnail image proxy fallback
"""

import html as html_lib
import http.cookiejar
import json
import os
import re
import math
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8765"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "Downloads")
DOWNLOADS_INDEX_PATH = os.path.join(DOWNLOADS_DIR, "downloads_index.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_CACHE = {}
CACHE_TTL_SECONDS = 300
_WATCH_SESSIONS = {}
_AUTOCOMPLETE_SESSION = {"ts": 0, "opener": None, "token": ""}

_DOWNLOAD_LOCK = threading.Lock()
_DOWNLOAD_JOBS = {}  # job_id -> dict


def cache_get(key, ttl=CACHE_TTL_SECONDS):
    entry = _CACHE.get(key)
    if not entry:
        return None
    ts, val = entry
    if time.time() - ts > ttl:
        _CACHE.pop(key, None)
        return None
    return val


def cache_set(key, val):
    if len(_CACHE) > 600:
        _CACHE.clear()
    _CACHE[key] = (time.time(), val)


def is_allowed_media_host(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return (
        host == "pornhub.com"
        or host.endswith(".pornhub.com")
        or host == "phncdn.com"
        or host.endswith(".phncdn.com")
    )


def make_opener():
    cj = http.cookiejar.CookieJar()
    cookie = http.cookiejar.Cookie(
        version=0,
        name="accessAgeDisclaimerPH",
        value="1",
        port=None,
        port_specified=False,
        domain=".pornhub.com",
        domain_specified=True,
        domain_initial_dot=True,
        path="/",
        path_specified=True,
        secure=False,
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={},
    )
    cj.set_cookie(cookie)
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def http_get_text(url, timeout=12, opener=None, extra_headers=None):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    }
    if opener is None:
        headers["Cookie"] = "accessAgeDisclaimerPH=1"
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    open_fn = opener.open if opener else urllib.request.urlopen
    with open_fn(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def http_get_json(url, timeout=10, opener=None, extra_headers=None):
    return json.loads(
        http_get_text(url, timeout=timeout, opener=opener, extra_headers=extra_headers)
    )


def parse_duration_seconds(dur_str):
    parts = [p for p in str(dur_str or "").strip().split(":") if p.isdigit()]
    if not parts:
        return 0
    sec = 0
    for p in parts:
        sec = sec * 60 + int(p)
    return sec


def parse_compact_views(v_str):
    if not v_str:
        return 0
    s = str(v_str).strip().upper().replace(",", "")
    mult = 1
    if s.endswith("M"):
        mult = 1_000_000
        s = s[:-1]
    elif s.endswith("K"):
        mult = 1_000
        s = s[:-1]
    elif s.endswith("B"):
        mult = 1_000_000_000
        s = s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def slugify_performer(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s


def sanitize_profile_path(raw_path):
    if not raw_path:
        return ""
    p = urllib.parse.urlsplit(raw_path.strip()).path
    m = re.match(r"^/(pornstar|model|channels|users)/([a-zA-Z0-9._-]+)", p)
    return f"/{m.group(1)}/{m.group(2)}" if m else ""


def normalize_api_video(v, listing_hint=None):
    """Normalize a Pornhub Webmasters video object + optional HTML listing hints."""
    listing_hint = listing_hint or {}
    vid = str(v.get("video_id") or listing_hint.get("id") or "").strip()

    raw_title = v.get("title") or listing_hint.get("title") or "Untitled"
    title = html_lib.unescape(str(raw_title)).strip()

    duration = str(v.get("duration") or listing_hint.get("duration") or "0:00").strip()

    thumb = (
        listing_hint.get("thumb")
        or v.get("thumb")
        or v.get("default_thumb")
        or ""
    )
    thumb = html_lib.unescape(thumb).strip()

    try:
        rating_pct = float(v.get("rating") or listing_hint.get("rating_pct") or 0.0)
    except (TypeError, ValueError):
        rating_pct = 0.0

    try:
        ratings_count = int(v.get("ratings") or 0)
    except (TypeError, ValueError):
        ratings_count = 0

    likes = int(round(ratings_count * (rating_pct / 100.0))) if ratings_count > 0 else 0
    dislikes = max(0, ratings_count - likes) if ratings_count > 0 else 0

    try:
        views = int(v.get("views") or 0)
    except (TypeError, ValueError):
        views = 0
    if not views and listing_hint.get("views_text"):
        views = parse_compact_views(listing_hint["views_text"])

    stars = []
    for p in v.get("pornstars") or []:
        name = (p.get("pornstar_name") if isinstance(p, dict) else str(p)).strip()
        if name and name not in stars:
            stars.append(html_lib.unescape(name))

    tags = []
    for t in v.get("tags") or []:
        tname = (t.get("tag_name") if isinstance(t, dict) else str(t)).strip()
        if tname and tname not in tags:
            tags.append(html_lib.unescape(tname))

    categories = []
    for c in v.get("categories") or []:
        cname = (c.get("category") if isinstance(c, dict) else str(c)).strip()
        if cname and cname not in categories:
            categories.append(html_lib.unescape(cname))

    url = v.get("url") or (
        f"https://www.pornhub.com/view_video.php?viewkey={vid}"
        if vid
        else "https://www.pornhub.com"
    )

    return {
        "id": vid,
        "title": title,
        "thumbnail": thumb,
        "preview_video": listing_hint.get("preview_video") or "",
        "duration": duration,
        "duration_seconds": parse_duration_seconds(duration),
        "stars": stars,
        "star_links": listing_hint.get("star_links") or [],
        "uploader": listing_hint.get("uploader") or "",
        "uploader_path": listing_hint.get("uploader_path") or "",
        "tags": tags,
        "categories": categories,
        "likes": likes,
        "dislikes": dislikes,
        "rating_percent": round(rating_pct, 1),
        "ratings_count": ratings_count,
        "views": views,
        "views_text": listing_hint.get("views_text") or "",
        "added": listing_hint.get("added") or "",
        "publish_date": v.get("publish_date") or "",
        "url": url,
    }


def extract_listing_items_from_ul(ul_html):
    """Parse <li> video cards inside any Pornhub listing <ul>."""
    items = []
    seen = set()
    for li_match in re.finditer(
        r'<li[^>]*data-video-vkey="([^"]+)"[^>]*>(.*?)</li>', ul_html, re.S
    ):
        vkey, body = li_match.group(1).strip(), li_match.group(2)
        if not vkey or vkey in seen:
            continue
        seen.add(vkey)

        thumb_m = re.search(r'data-mediumthumb="([^"]+)"', body) or re.search(
            r'<img[^>]+src="([^"]+)"', body
        )
        preview_m = re.search(r'data-mediabook="([^"]+)"', body)
        title_m = re.search(r'<a[^>]+title="([^"]+)"', body) or re.search(
            r'<img[^>]+alt="([^"]+)"', body
        )
        dur_m = re.search(r'<var[^>]*class="[^"]*duration[^"]*"[^>]*>([^<]+)</var>', body)
        user_m = re.search(r'class="usernameWrap"[^>]*>(.*?)</div>', body, re.S)
        views_m = re.search(r'<span[^>]*class="views"[^>]*>.*?<var>([^<]+)</var>', body, re.S)
        added_m = re.search(r'<var[^>]*class="added"[^>]*>([^<]+)</var>', body)
        rating_m = re.search(r'<div[^>]*class="value"[^>]*>\s*(\d+)%\s*</div>', body)

        uploader = ""
        uploader_path = ""
        if user_m:
            u_inner = user_m.group(1)
            uploader = html_lib.unescape(re.sub(r"<[^>]+>", "", u_inner)).strip()
            href_m = re.search(r'href="([^"]+)"', u_inner)
            if href_m:
                uploader_path = sanitize_profile_path(href_m.group(1))

        items.append(
            {
                "id": vkey,
                "title": html_lib.unescape(title_m.group(1)).strip() if title_m else "",
                "thumb": html_lib.unescape(thumb_m.group(1)).strip() if thumb_m else "",
                "preview_video": html_lib.unescape(preview_m.group(1)).strip() if preview_m else "",
                "duration": dur_m.group(1).strip() if dur_m else "",
                "uploader": uploader,
                "uploader_path": uploader_path,
                "views_text": views_m.group(1).strip() if views_m else "",
                "added": added_m.group(1).strip() if added_m else "",
                "rating_pct": float(rating_m.group(1)) if rating_m else 0.0,
            }
        )
    return items


def parse_hottest_listing_html(page_html):
    """Extract country info and video listing items from Pornhub's ?o=ht HTML."""
    country = None
    m1 = re.search(r"<span>\s*Hottest in ([^<]+?)\s*</span>", page_html, re.I)
    if m1:
        country = html_lib.unescape(m1.group(1)).strip()
    if not country:
        m2 = re.search(r"Hottest Porn Videos in ([^<\n\r\"]+)", page_html, re.I)
        if m2:
            country = html_lib.unescape(m2.group(1)).strip()

    cc_match = re.search(r"round-flag-([a-z]{2})", page_html, re.I)
    country_code = cc_match.group(1).upper() if cc_match else ""

    ul_match = re.search(r'<ul[^>]*id="videoCategory"[^>]*>(.*?)</ul>', page_html, re.S)
    search_scope = ul_match.group(1) if ul_match else page_html

    return {
        "country": country or "Worldwide",
        "country_code": country_code,
        "items": extract_listing_items_from_ul(search_scope),
    }


def fetch_video_by_id_cached(vkey):
    ck = f"vid:{vkey}"
    cached = cache_get(ck, ttl=600)
    if cached is not None:
        return cached
    url = f"https://www.pornhub.com/webmasters/video_by_id?id={urllib.parse.quote(vkey)}"
    try:
        data = http_get_json(url, timeout=9)
        vid_obj = data.get("video")
        if vid_obj:
            cache_set(ck, vid_obj)
            return vid_obj
    except Exception:
        pass
    return {}


COUNTRY_NAMES = {
    "US": "United States",
    "GB": "United Kingdom",
    "DE": "Germany",
    "FR": "France",
    "JP": "Japan",
    "BR": "Brazil",
    "CA": "Canada",
    "AU": "Australia",
    "ES": "Spain",
    "IT": "Italy",
    "NL": "Netherlands",
    "PL": "Poland",
    "MX": "Mexico",
    "RU": "Russia",
    "EG": "Egypt",
}


def get_hottest_videos(page=1, cc=""):
    page = max(1, int(page))
    raw_cc = (cc or "").strip().lower()
    if raw_cc == "world":
        cc_clean = "world"
    else:
        cc_clean = re.sub(r"[^a-z]", "", raw_cc)[:2]

    ck = f"hottest:{cc_clean}:{page}"
    cached = cache_get(ck)
    if cached is not None:
        return cached

    if cc_clean == "world":
        params = {"o": "mv", "t": "w", "page": str(page)}
    else:
        params = {"o": "ht", "page": str(page)}
        if cc_clean:
            params["cc"] = cc_clean

    url = "https://www.pornhub.com/video?" + urllib.parse.urlencode(params)
    page_html = http_get_text(url, timeout=14)
    parsed = parse_hottest_listing_html(page_html)
    items = parsed["items"]

    with ThreadPoolExecutor(max_workers=12) as pool:
        api_objs = list(pool.map(lambda it: fetch_video_by_id_cached(it["id"]), items))

    videos = []
    for idx, (hint, api_v) in enumerate(zip(items, api_objs), start=1):
        norm = normalize_api_video(api_v or {}, listing_hint=hint)
        norm["rank"] = (page - 1) * max(len(items), 30) + idx
        videos.append(norm)

    if cc_clean == "world":
        country_name = "Worldwide"
        country_code = "WW"
    elif cc_clean:
        country_code = cc_clean.upper()
        country_name = COUNTRY_NAMES.get(country_code) or parsed["country"]
    else:
        country_name = parsed["country"]
        country_code = parsed["country_code"]

    result = {
        "mode": "hottest",
        "country": country_name,
        "country_code": country_code,
        "selected_cc": cc_clean,
        "page": page,
        "count": len(videos),
        "videos": videos,
    }
    cache_set(ck, result)
    return result


def search_videos(query, ordering="relevant", period="alltime", page=1):
    page = max(1, int(page))
    q = (query or "").strip()

    order_map = {
        "relevant": "",
        "featured": "",
        "newest": "mr",
        "recent": "mr",
        "mostviewed": "mv",
        "rating": "tr",
        "longest": "lg",
    }
    if ordering not in order_map:
        ordering = "relevant"
    o_code = order_map[ordering]

    ck = f"search_v2:{q.lower()}:{ordering}:{period}:{page}"
    cached = cache_get(ck)
    if cached is not None:
        return cached

    vk_match = re.search(r"(?:viewkey=|/embed/)([a-zA-Z0-9]+)", q)
    if vk_match and page == 1:
        direct_vk = vk_match.group(1)
        api_v = fetch_video_by_id_cached(direct_vk)
        if api_v:
            norm = normalize_api_video(api_v)
            norm["rank"] = 1
            res = {
                "mode": "search",
                "query": q,
                "ordering": ordering,
                "page": 1,
                "count": 1,
                "related_searches": [],
                "videos": [norm],
            }
            cache_set(ck, res)
            return res

    params = {"search": q}
    if o_code:
        params["o"] = o_code
        if o_code in ("mv", "tr") and period in ("daily", "weekly", "monthly", "alltime"):
            t_map = {"daily": "t", "weekly": "w", "monthly": "m", "alltime": "a"}
            if period != "alltime":
                params["t"] = t_map[period]
    if page > 1:
        params["page"] = str(page)

    url = "https://www.pornhub.com/video/search?" + urllib.parse.urlencode(params)
    try:
        page_html = http_get_text(url, timeout=14)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {
                "mode": "search",
                "query": q,
                "ordering": ordering,
                "page": page,
                "count": 0,
                "related_searches": [],
                "videos": [],
            }
        raise

    ul_match = re.search(
        r'<ul[^>]*id="videoSearchResult"[^>]*>(.*?)</ul>', page_html, re.S
    )
    items = extract_listing_items_from_ul(ul_match.group(1)) if ul_match else []

    with ThreadPoolExecutor(max_workers=12) as pool:
        api_objs = list(pool.map(lambda it: fetch_video_by_id_cached(it["id"]), items))

    videos = []
    for idx, (hint, api_v) in enumerate(zip(items, api_objs), start=1):
        norm = normalize_api_video(api_v or {}, listing_hint=hint)
        norm["rank"] = (page - 1) * max(len(items), 30) + idx
        videos.append(norm)

    rel_searches = []
    rel_sec = re.search(
        r'class="[^"]*relatedSearch[^"]*"[^>]*>(.*?)</ul>', page_html, re.S | re.I
    )
    if rel_sec:
        for rm in re.finditer(r"<a[^>]*>(.*?)</a>", rel_sec.group(1), re.S):
            term = html_lib.unescape(re.sub(r"<[^>]+>", "", rm.group(1))).strip()
            if term and term.lower() != q.lower() and term not in rel_searches:
                rel_searches.append(term)

    result = {
        "mode": "search",
        "query": q,
        "ordering": ordering,
        "period": period,
        "page": page,
        "count": len(videos),
        "related_searches": rel_searches[:12],
        "videos": videos,
    }
    cache_set(ck, result)
    return result


def get_search_autocomplete(query):
    q = (query or "").strip()
    if len(q) < 2:
        return {"queries": [], "performers": [], "channels": []}

    ck = f"ac:{q.lower()}"
    cached = cache_get(ck, ttl=300)
    if cached is not None:
        return cached

    now = time.time()
    if (
        not _AUTOCOMPLETE_SESSION["opener"]
        or not _AUTOCOMPLETE_SESSION["token"]
        or now - _AUTOCOMPLETE_SESSION["ts"] > 600
    ):
        op = make_opener()
        home_html = http_get_text("https://www.pornhub.com/", timeout=10, opener=op)
        tok_m = re.search(r'data-token="([^"]+)"', home_html) or re.search(
            r"token=([a-zA-Z0-9._-]+)", home_html
        )
        _AUTOCOMPLETE_SESSION["opener"] = op
        _AUTOCOMPLETE_SESSION["token"] = tok_m.group(1) if tok_m else ""
        _AUTOCOMPLETE_SESSION["ts"] = now

    params = {
        "q": q,
        "pornstars": "true",
        "token": _AUTOCOMPLETE_SESSION["token"],
        "alt": "0",
    }
    ac_url = "https://www.pornhub.com/api/v1/video/search_autocomplete?" + urllib.parse.urlencode(
        params
    )
    try:
        raw = http_get_json(
            ac_url,
            timeout=6,
            opener=_AUTOCOMPLETE_SESSION["opener"],
            extra_headers={
                "Referer": "https://www.pornhub.com/",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
    except Exception:
        # Reset session once on failure
        _AUTOCOMPLETE_SESSION["ts"] = 0
        return {"queries": [], "performers": [], "channels": []}

    queries = [html_lib.unescape(str(x)) for x in (raw.get("queries") or [])[:8]]
    performers = []
    for kind_key, prefix, label in [
        ("pornstars", "/pornstar/", "Pornstar"),
        ("models", "/model/", "Model"),
        ("channels", "/channels/", "Channel"),
    ]:
        for item in (raw.get(kind_key) or [])[:4]:
            if isinstance(item, dict) and item.get("slug") and item.get("name"):
                performers.append(
                    {
                        "name": html_lib.unescape(str(item["name"])),
                        "path": f"{prefix}{item['slug']}",
                        "kind": label,
                        "rank": item.get("rank"),
                    }
                )

    res = {"queries": queries, "performers": performers[:8]}
    cache_set(ck, res)
    return res


def get_performer_profile(path="", name="", sort="newest", page=1):
    page = max(1, int(page))
    sort = sort if sort in ("newest", "mostviewed", "rating", "longest") else "newest"
    clean_path = sanitize_profile_path(path)

    candidates = []
    if clean_path:
        candidates.append(clean_path)
    elif name:
        slug = slugify_performer(name)
        if slug:
            candidates.extend(
                [f"/pornstar/{slug}", f"/model/{slug}", f"/channels/{slug}"]
            )

    if not candidates:
        raise ValueError("Missing performer path or name")

    ck = f"perf:{candidates[0]}:{sort}:{page}"
    cached = cache_get(ck, ttl=300)
    if cached is not None:
        return cached

    chosen_base = None
    page_html = None

    for base in candidates:
        is_channel = base.startswith("/channels/")
        if is_channel:
            ch_map = {"newest": "", "mostviewed": "vi", "rating": "ra", "longest": "lg"}
            o_val = ch_map.get(sort, "")
        else:
            ps_map = {"newest": "mr", "mostviewed": "mv", "rating": "tr", "longest": "lg"}
            o_val = ps_map.get(sort, "mr")

        q_params = {}
        if o_val:
            q_params["o"] = o_val
        if page > 1:
            q_params["page"] = str(page)

        q_str = ("?" + urllib.parse.urlencode(q_params)) if q_params else ""

        # Try /videos endpoint first, then base profile URL if /videos 404s
        for sub in ["/videos" + q_str, q_str]:
            test_url = "https://www.pornhub.com" + base + sub
            try:
                html_try = http_get_text(test_url, timeout=12)
                chosen_base = base
                page_html = html_try
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    continue
                raise
        if page_html:
            break

    if not page_html or not chosen_base:
        # Graceful fallback to keyword search if performer has no dedicated page
        fallback_q = name or clean_path.split("/")[-1].replace("-", " ")
        srch = search_videos(fallback_q, ordering="mostviewed" if sort == "mostviewed" else "relevant", page=page)
        return {
            "mode": "performer",
            "name": fallback_q.title(),
            "path": "",
            "kind": "Performer",
            "ph_url": f"https://www.pornhub.com/video/search?search={urllib.parse.quote(fallback_q)}",
            "stats": [],
            "sort": sort,
            "page": page,
            "count": srch["count"],
            "videos": srch["videos"],
        }

    # Parse Performer / Channel Title & Stats
    h1_m = re.search(r"<h1[^>]*>(.*?)</h1>", page_html, re.S)
    disp_name = (
        html_lib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", h1_m.group(1)))).strip()
        if h1_m
        else (name or chosen_base.split("/")[-1].replace("-", " ").title())
    )
    # Trim trailing "Porn Videos" if h1 says "Alex Adams Porn Videos"
    disp_name = re.sub(r"\s+(?:Porn\s+)?Videos.*$", "", disp_name, flags=re.I).strip()

    if chosen_base.startswith("/channels/"):
        kind = "Channel"
    elif chosen_base.startswith("/model/"):
        kind = "Model"
    elif chosen_base.startswith("/pornstar/"):
        kind = "Pornstar"
    else:
        kind = "Creator"

    stats = []
    for box_m in re.finditer(
        r'class="[^"]*infoPiece[^"]*"[^>]*>(.*?)</div>', page_html, re.S
    ):
        txt = html_lib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", box_m.group(1)))).strip()
        if txt and len(txt) < 48 and txt not in stats:
            stats.append(txt)

    # Strip header/menu ULs before extracting profile video cards
    cleaned_html = re.sub(
        r'<ul[^>]*id="(?:headerMainMenu|hottestMenuSection|recommMenuSection|playListHeaderSection)"[^>]*>.*?</ul>',
        "",
        page_html,
        flags=re.S,
    )
    items = extract_listing_items_from_ul(cleaned_html)

    enrich_slice = items[:18]
    with ThreadPoolExecutor(max_workers=12) as pool:
        api_objs = list(pool.map(lambda it: fetch_video_by_id_cached(it["id"]), enrich_slice))

    videos = []
    for idx, hint in enumerate(items):
        if not hint.get("uploader"):
            hint["uploader"] = disp_name
            hint["uploader_path"] = chosen_base
        api_v = api_objs[idx] if idx < len(api_objs) else {}
        norm = normalize_api_video(api_v or {}, listing_hint=hint)
        norm["rank"] = (page - 1) * max(len(items), 36) + idx + 1
        videos.append(norm)

    res = {
        "mode": "performer",
        "name": disp_name,
        "path": chosen_base,
        "kind": kind,
        "ph_url": f"https://www.pornhub.com{chosen_base}",
        "stats": stats[:4],
        "sort": sort,
        "page": page,
        "count": len(videos),
        "videos": videos,
    }
    cache_set(ck, res)
    return res


def parse_action_tags_and_html(raw_tags, whtml):
    """Parse both flashvars actionTags and HTML actionTagList items."""
    chapters = []
    seen_sec = set()

    if raw_tags and isinstance(raw_tags, str):
        for part in raw_tags.split(","):
            part = part.strip()
            if ":" not in part:
                continue
            label, sec_str = part.rsplit(":", 1)
            try:
                sec = int(float(sec_str))
                if label.strip() and sec >= 0 and sec not in seen_sec:
                    seen_sec.add(sec)
                    chapters.append(
                        {"title": html_lib.unescape(label.strip()), "seconds": sec}
                    )
            except ValueError:
                continue

    for ul_m in re.finditer(r'<ul[^>]*actionTagList[^>]*>(.*?)</ul>', whtml, re.S):
        for li_m in re.finditer(r"<li[^>]*>(.*?)</li>", ul_m.group(1), re.S):
            li_body = li_m.group(1)
            sec_m = re.search(r"(?:seekTo|jumpTo|data-seconds=[\"']?)(\d+)", li_body)
            lbl_txt = html_lib.unescape(re.sub(r"<[^>]+>", " ", li_body)).strip()
            lbl_txt = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", "", lbl_txt).strip(" -·\t\n")
            if sec_m and lbl_txt:
                sec = int(sec_m.group(1))
                if sec not in seen_sec:
                    seen_sec.add(sec)
                    chapters.append({"title": lbl_txt, "seconds": sec})

    chapters.sort(key=lambda x: x["seconds"])
    return chapters


def compute_hotspot_peaks(hotspots, duration_seconds, max_peaks=4):
    """Identify prominent 'Most Replayed' local maxima from hotspots when chapters are absent."""
    if not hotspots or len(hotspots) < 10 or duration_seconds < 45:
        return []

    n = len(hotspots)
    # Smooth with a 3-point moving average and ignore first 6% (auto-start spike) & last 4%
    smoothed = []
    for i in range(n):
        lo = max(0, i - 1)
        hi = min(n, i + 2)
        smoothed.append(sum(hotspots[lo:hi]) / (hi - lo))

    start_idx = max(2, int(n * 0.06))
    end_idx = min(n - 2, int(n * 0.96))
    if start_idx >= end_idx:
        return []

    candidates = []
    for i in range(start_idx, end_idx):
        if smoothed[i] >= smoothed[i - 1] and smoothed[i] >= smoothed[i + 1]:
            candidates.append((smoothed[i], i))

    candidates.sort(reverse=True)
    min_gap_idx = max(3, int(n * 0.14))
    chosen_indices = []
    for _, idx in candidates:
        if all(abs(idx - c) >= min_gap_idx for c in chosen_indices):
            chosen_indices.append(idx)
            if len(chosen_indices) >= max_peaks:
                break

    chosen_indices.sort()
    peaks = []
    for rank_num, idx in enumerate(chosen_indices, start=1):
        sec = int(round((idx / max(1, n - 1)) * duration_seconds))
        peaks.append({"title": f"Peak #{rank_num}", "seconds": sec, "is_peak": True})
    return peaks


def parse_comments_from_html(raw_html):
    comments = []
    seen_ids = set()

    nested_ranges = [
        (m.start(), m.end())
        for m in re.finditer(
            r'<div[^>]*class="[^"]*nestedBlock\s+childrenOf[^"]*"[^>]*>.*?(?=<div[^>]*class="commentBlock\s+closestComment|$)',
            raw_html,
            re.S,
        )
    ]

    for m in re.finditer(
        r'<div[^>]*class="([^"]*commentBlock[^"]*)"[^>]*>(.*?)(?=<div[^>]*class="[^"]*commentBlock\b|<div[^>]*class="[^"]*moreComments|$)',
        raw_html,
        re.S,
    ):
        cls_attr = m.group(1)
        block = m.group(2)
        pos = m.start()

        if "[[id]]" in cls_attr or "commentTag[[" in cls_attr:
            continue

        cid_m = re.search(r"commentTag([a-zA-Z0-9_-]+)", cls_attr)
        cid = cid_m.group(1) if cid_m else f"pos-{pos}"
        if cid in seen_ids:
            continue
        seen_ids.add(cid)

        user_m = re.search(r'class="[^"]*usernameLink[^"]*"[^>]*>([^<]+)<', block)
        if not user_m:
            user_m = re.search(r'<span[^>]*class="[^"]*username[^"]*"[^>]*>([^<]+)<', block)
        if not user_m:
            user_m = re.search(r'class="commentAvatarImg[^"]*"[^>]*alt="([^"]+)"', block)
        username = html_lib.unescape(user_m.group(1)).strip() if user_m else "Anonymous"
        if not username or username == "[[username]]":
            continue

        date_m = re.search(r'<div[^>]*class="date"[^>]*>\s*([^<]+?)\s*</div>', block)
        date_str = html_lib.unescape(date_m.group(1)).strip() if date_m else ""

        msg_m = re.search(
            r'<div[^>]*class="commentMessage"[^>]*>\s*<span>(.*?)</span>', block, re.S
        )
        if not msg_m:
            continue
        msg_raw = msg_m.group(1)
        msg_raw = re.sub(r"<br\s*/?>", "\n", msg_raw, flags=re.I)
        msg_clean = html_lib.unescape(re.sub(r"<[^>]+>", "", msg_raw)).strip()
        if not msg_clean:
            continue

        up_m = re.search(r'thumbsUpVote[^"]*"[^>]*>\s*([+-]?\d+)\s*<', block)
        upvotes = int(up_m.group(1)) if up_m else 0

        down_m = re.search(r'thumbsDownVote[^"]*"[^>]*>\s*([+-]?\d+)\s*<', block)
        downvotes = int(down_m.group(1)) if down_m else None

        is_reply = (
            "reply" in cls_attr.lower()
            or "closestComment" not in cls_attr
            or any(start <= pos <= end for start, end in nested_ranges)
        )

        comments.append(
            {
                "id": cid,
                "username": username,
                "date": date_str,
                "text": msg_clean,
                "upvotes": upvotes,
                "downvotes": downvotes,
                "is_reply": is_reply,
            }
        )

    return comments


def get_video_watch_details(vkey):
    vkey = (vkey or "").strip()
    if not vkey:
        raise ValueError("Missing video id")

    ck = f"watch:{vkey}"
    cached = cache_get(ck, ttl=600)
    if cached is not None:
        return cached

    opener = make_opener()
    watch_url = f"https://www.pornhub.com/view_video.php?viewkey={urllib.parse.quote(vkey)}"

    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_html = ex.submit(http_get_text, watch_url, 14, opener)
        fut_api = ex.submit(fetch_video_by_id_cached, vkey)
        whtml = fut_html.result()
        api_v = fut_api.result() or {}

    fv = {}
    fv_match = re.search(r"var\s+flashvars_\d+\s*=\s*(\{.*?\});\s*\n", whtml, re.S)
    if not fv_match:
        fv_match = re.search(r"flashvars_\d+\s*=\s*(\{.*?\});", whtml, re.S)
    if fv_match:
        try:
            fv = json.loads(fv_match.group(1))
        except Exception:
            fv = {}

    up_a = (
        re.search(
            r'<a[^>]*href="([^"]+)"[^>]*data-event="video_underplayer"[^>]*data-label="(?:channel|model|pornstar|user)"[^>]*>([^<]+)</a>',
            whtml,
        )
        or re.search(
            r'<a[^>]*data-event="video_underplayer"[^>]*href="([^"]+)"[^>]*class="[^"]*bolded[^"]*"[^>]*>([^<]+)</a>',
            whtml,
        )
        or re.search(
            r'<a[^>]*href="([^"]+)"[^>]*data-event="video_underplayer"[^>]*class="[^"]*bolded[^"]*"[^>]*>([^<]+)</a>',
            whtml,
        )
        or re.search(
            r'class="userInfoBlock"[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>([^<]+)</a>',
            whtml,
            re.S,
        )
    )
    uploader = html_lib.unescape(up_a.group(2)).strip() if up_a else ""
    uploader_path = sanitize_profile_path(up_a.group(1)) if up_a else ""
    if not uploader_path:
        av_m = re.search(r'<a[^>]+href="([^"]+)"[^>]*class="[^"]*userAvatarLink', whtml)
        if av_m:
            uploader_path = sanitize_profile_path(av_m.group(1))

    # Extract exact star profile paths from watch page pornstarsWrapper
    star_links = []
    seen_spaths = set()
    ps_wrap = re.search(r'class="[^"]*pornstarsWrapper[^"]*"[^>]*>(.*?)</div>', whtml, re.S)
    if ps_wrap:
        for sm in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', ps_wrap.group(1), re.S):
            sp = sanitize_profile_path(sm.group(1))
            sname = html_lib.unescape(re.sub(r"<[^>]+>", "", sm.group(2))).strip()
            if sp and sname and sp not in seen_spaths:
                seen_spaths.add(sp)
                star_links.append({"name": sname, "path": sp})

    hint = {
        "id": vkey,
        "title": fv.get("video_title") or "",
        "thumb": fv.get("image_url") or "",
        "duration": "",
        "uploader": uploader,
        "uploader_path": uploader_path,
        "star_links": star_links,
    }
    if fv.get("video_duration") and not api_v.get("duration"):
        secs = int(fv["video_duration"])
        m, s = divmod(secs, 60)
        h, m = divmod(m, 60)
        hint["duration"] = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    video_info = normalize_api_video(api_v, listing_hint=hint)
    video_info["poster"] = fv.get("image_url") or video_info["thumbnail"]

    hls_streams = []
    mp4_streams = []
    raw_mp4_map = {}
    mp4_endpoint = None

    for md in fv.get("mediaDefinitions") or []:
        fmt = md.get("format")
        vurl = md.get("videoUrl")
        if not vurl:
            continue
        if fmt == "hls" and isinstance(md.get("quality"), (str, int)):
            try:
                q_int = int(md["quality"])
            except ValueError:
                continue
            hls_streams.append(
                {
                    "quality": q_int,
                    "label": f"{q_int}p" + (" HD" if q_int >= 720 else ""),
                    "url": f"/api/hls?url={urllib.parse.quote(vurl, safe='')}",
                }
            )
        elif fmt == "mp4" and isinstance(vurl, str) and "get_media" in vurl:
            mp4_endpoint = vurl
        elif fmt == "mp4" and isinstance(md.get("quality"), (str, int)):
            try:
                q_int = int(md["quality"])
                raw_mp4_map[q_int] = vurl
                mp4_streams.append(
                    {
                        "quality": q_int,
                        "label": f"{q_int}p" + (" HD" if q_int >= 720 else ""),
                        "url": f"/api/stream?url={urllib.parse.quote(vurl, safe='')}",
                        "raw_url": vurl,
                    }
                )
            except ValueError:
                pass

    if mp4_endpoint:
        try:
            mp4_list = http_get_json(
                mp4_endpoint,
                timeout=8,
                opener=opener,
                extra_headers={"Referer": watch_url},
            )
            for item in mp4_list or []:
                if item.get("format") == "mp4" and item.get("videoUrl"):
                    q_int = int(item.get("quality") or item.get("height") or 0)
                    if q_int > 0:
                        raw_mp4_map[q_int] = item["videoUrl"]
                        mp4_streams.append(
                            {
                                "quality": q_int,
                                "label": f"{q_int}p" + (" HD" if q_int >= 720 else ""),
                                "url": f"/api/stream?url={urllib.parse.quote(item['videoUrl'], safe='')}",
                                "raw_url": item["videoUrl"],
                            }
                        )
        except Exception:
            pass

    seen_hls = set()
    dedup_hls = []
    for s in sorted(hls_streams, key=lambda x: x["quality"], reverse=True):
        if s["quality"] not in seen_hls:
            seen_hls.add(s["quality"])
            dedup_hls.append(s)

    seen_mp4 = set()
    dedup_mp4 = []
    for s in sorted(mp4_streams, key=lambda x: x["quality"], reverse=True):
        if s["quality"] not in seen_mp4:
            seen_mp4.add(s["quality"])
            dedup_mp4.append(s)

    preview_scrub_url = dedup_mp4[-1]["url"] if dedup_mp4 else ""

    raw_hotspots = fv.get("hotspots") or []
    hotspots = []
    for h in raw_hotspots:
        try:
            hotspots.append(int(h))
        except (TypeError, ValueError):
            pass

    dur_secs = video_info["duration_seconds"] or int(fv.get("video_duration") or 0)
    chapters = parse_action_tags_and_html(fv.get("actionTags") or "", whtml)
    peak_moments = compute_hotspot_peaks(hotspots, dur_secs) if not chapters else []

    rel_ul = re.search(r'<ul[^>]*id="relatedVideosListing"[^>]*>(.*?)</ul>', whtml, re.S)
    rec_ul = re.search(r'<ul[^>]*id="recommendedVideosListing"[^>]*>(.*?)</ul>', whtml, re.S)

    rel_hints = extract_listing_items_from_ul(rel_ul.group(1)) if rel_ul else []
    rec_hints = extract_listing_items_from_ul(rec_ul.group(1)) if rec_ul else []

    enrich_slice = rel_hints[:12]
    with ThreadPoolExecutor(max_workers=12) as pool:
        enriched_objs = list(
            pool.map(lambda it: fetch_video_by_id_cached(it["id"]), enrich_slice)
        )

    related_videos = []
    for idx, hint_item in enumerate(rel_hints):
        api_item = enriched_objs[idx] if idx < len(enriched_objs) else {}
        related_videos.append(normalize_api_video(api_item or {}, listing_hint=hint_item))

    recommended_videos = [
        normalize_api_video({}, listing_hint=hint_item) for hint_item in rec_hints[:20]
    ]

    total_cmt_m = re.search(r"All Comments\s*<span>\((\d+)\)</span>", whtml, re.I)
    total_comments = int(total_cmt_m.group(1)) if total_cmt_m else 0
    comments = parse_comments_from_html(whtml)
    if not total_comments:
        total_comments = len(comments)

    tok_m = re.search(r'/comment/show\?id=(\d+)[^"\']*?[&;]token=([^"\'&\s]+)', whtml)
    can_sort_comments = False
    if tok_m:
        numeric_id, comment_token = tok_m.group(1), html_lib.unescape(tok_m.group(2))
        _WATCH_SESSIONS[vkey] = {
            "ts": time.time(),
            "opener": opener,
            "numeric_id": numeric_id,
            "token": comment_token,
            "watch_url": watch_url,
        }
        can_sort_comments = True

    cc_path = fv.get("closedCaptionsFile") or ""
    result = {
        "_cc_path": cc_path if isinstance(cc_path, str) else "",
        "subtitles": {
            "ph_available": bool(cc_path),
            "ai_available": ai_tools_ready(),
        },
        "video": video_info,
        "streams": {
            "hls": dedup_hls,
            "mp4": [{"quality": x["quality"], "label": x["label"], "url": x["url"]} for x in dedup_mp4],
            "_raw_mp4_map": raw_mp4_map,
            "scrub_preview_mp4": preview_scrub_url,
            "embed_url": f"https://www.pornhub.com/embed/{urllib.parse.quote(vkey)}",
        },
        "hotspots": hotspots,
        "chapters": chapters,
        "peak_moments": peak_moments,
        "related_videos": related_videos,
        "recommended_videos": recommended_videos,
        "comments": {
            "total": total_comments,
            "can_sort": can_sort_comments,
            "downvotes_public": False,
            "items": comments,
        },
    }
    cache_set(ck, result)
    return result


def get_video_comments(vkey, popular=1, page=1):
    vkey = (vkey or "").strip()
    popular = 1 if str(popular) in ("1", "true", "True") else 0
    page = max(1, int(page))

    ck = f"cmts:{vkey}:{popular}:{page}"
    cached = cache_get(ck, ttl=180)
    if cached is not None:
        return cached

    sess = _WATCH_SESSIONS.get(vkey)
    if not sess or time.time() - sess["ts"] > 900:
        _CACHE.pop(f"watch:{vkey}", None)
        get_video_watch_details(vkey)
        sess = _WATCH_SESSIONS.get(vkey)

    if not sess:
        return {"items": [], "popular": popular, "page": page}

    params = {
        "id": sess["numeric_id"],
        "popular": str(popular),
        "page": str(page),
        "what": "video",
        "token": sess["token"],
    }
    curl = "https://www.pornhub.com/comment/show?" + urllib.parse.urlencode(params)
    body = http_get_text(
        curl,
        timeout=10,
        opener=sess["opener"],
        extra_headers={
            "Referer": sess["watch_url"],
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    items = parse_comments_from_html(body)
    res = {"items": items, "popular": popular, "page": page}
    cache_set(ck, res)
    return res


# ============================================================================
# TURBO MULTI-THREADED DOWNLOADER & OFFLINE LIBRARY
# ============================================================================
def ensure_downloads_dir():
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)


def load_downloads_index():
    ensure_downloads_dir()
    if not os.path.exists(DOWNLOADS_INDEX_PATH):
        return []
    try:
        with open(DOWNLOADS_INDEX_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_downloads_index(entries):
    ensure_downloads_dir()
    tmp_p = DOWNLOADS_INDEX_PATH + ".tmp"
    with open(tmp_p, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    os.replace(tmp_p, DOWNLOADS_INDEX_PATH)


def list_downloads_and_jobs():
    ensure_downloads_dir()
    with _DOWNLOAD_LOCK:
        index_entries = load_downloads_index()
        by_file = {e.get("filename"): e for e in index_entries if e.get("filename")}

        completed = []
        for fname in sorted(os.listdir(DOWNLOADS_DIR)):
            if not fname.lower().endswith(".mp4"):
                continue
            fpath = os.path.join(DOWNLOADS_DIR, fname)
            if not os.path.isfile(fpath):
                continue
            st = os.stat(fpath)
            meta = by_file.get(fname) or {}
            completed.append(
                {
                    "filename": fname,
                    "id": meta.get("id") or f"local-{fname}",
                    "title": meta.get("title") or os.path.splitext(fname)[0],
                    "quality": meta.get("quality") or 0,
                    "duration": meta.get("duration") or "",
                    "thumbnail": meta.get("thumbnail") or "",
                    "uploader": meta.get("uploader") or "Offline Library",
                    "stars": meta.get("stars") or [],
                    "tags": meta.get("tags") or [],
                    "size_bytes": st.st_size,
                    "completed_at": meta.get("completed_at") or int(st.st_mtime),
                    "local_stream_url": f"/api/local_video?file={urllib.parse.quote(fname, safe='')}",
                }
            )

        completed.sort(key=lambda x: x["completed_at"], reverse=True)
        jobs = [
            {k: v for k, v in j.items() if not k.startswith("_")}
            for j in _DOWNLOAD_JOBS.values()
        ]
        jobs.sort(key=lambda x: x["started_at"], reverse=True)

    return {"jobs": jobs, "files": completed, "folder": DOWNLOADS_DIR}


def start_turbo_download(payload):
    vid = str(payload.get("id") or "").strip()
    req_q = int(payload.get("quality") or 0)
    if not vid:
        raise ValueError("Missing video id")

    # Prevent duplicate active job for same video + quality
    with _DOWNLOAD_LOCK:
        for existing in _DOWNLOAD_JOBS.values():
            if (
                existing["video_id"] == vid
                and existing["quality"] == req_q
                and existing["status"] in ("starting", "downloading")
            ):
                return {k: v for k, v in existing.items() if not k.startswith("_")}

    # Fetch fresh signed MP4 URLs
    _CACHE.pop(f"watch:{vid}", None)
    w = get_video_watch_details(vid)
    vinfo = w["video"]
    raw_map = w["streams"].get("_raw_mp4_map") or {}
    if not raw_map:
        raise RuntimeError("No downloadable MP4 stream found for this video")

    chosen_q = req_q if req_q in raw_map else max(raw_map.keys())
    cdn_url = raw_map[chosen_q]

    ensure_downloads_dir()
    safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", vinfo["title"]).strip(" .")[:85] or vid
    filename = f"{safe_title} ({chosen_q}p).mp4"
    final_path = os.path.join(DOWNLOADS_DIR, filename)
    part_path = final_path + ".part"

    job_id = uuid.uuid4().hex[:10]
    job = {
        "job_id": job_id,
        "video_id": vid,
        "title": vinfo["title"],
        "quality": chosen_q,
        "filename": filename,
        "thumbnail": vinfo.get("thumbnail") or "",
        "duration": vinfo.get("duration") or "",
        "status": "starting",
        "downloaded_bytes": 0,
        "total_bytes": 0,
        "percent": 0.0,
        "speed_bps": 0,
        "error": "",
        "started_at": time.time(),
        "_cancel": False,
    }

    with _DOWNLOAD_LOCK:
        _DOWNLOAD_JOBS[job_id] = job

    t = threading.Thread(
        target=_run_turbo_download_worker,
        args=(job, cdn_url, part_path, final_path, vinfo),
        daemon=True,
    )
    t.start()
    return {k: v for k, v in job.items() if not k.startswith("_")}


def _run_turbo_download_worker(job, cdn_url, part_path, final_path, vinfo):
    # 1.4 MB chunks stay inside CDN burst window while maximizing 12-thread parallelism
    CHUNK_SIZE = 1_400_000
    WORKERS = 12

    try:
        probe_req = urllib.request.Request(
            cdn_url,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": "https://www.pornhub.com/",
                "Range": "bytes=0-0",
            },
        )
        with urllib.request.urlopen(probe_req, timeout=14) as pr:
            cr = pr.headers.get("Content-Range") or ""
            m = re.search(r"/(\d+)$", cr)
            total_bytes = int(m.group(1)) if m else int(pr.headers.get("Content-Length") or 0)

        if total_bytes <= 0:
            raise RuntimeError("Could not determine video byte size from CDN")

        with _DOWNLOAD_LOCK:
            job["total_bytes"] = total_bytes
            job["status"] = "downloading"

        with open(part_path, "wb") as fp:
            fp.truncate(total_bytes)

        file_lock = threading.Lock()
        t_start = time.time()

        ranges = []
        offset = 0
        while offset < total_bytes:
            end = min(total_bytes - 1, offset + CHUNK_SIZE - 1)
            ranges.append((offset, end))
            offset = end + 1

        def download_range(byte_range):
            start_b, end_b = byte_range
            if job.get("_cancel"):
                return 0
            last_err = None
            for attempt in range(3):
                if job.get("_cancel"):
                    return 0
                try:
                    rq = urllib.request.Request(
                        cdn_url,
                        headers={
                            "User-Agent": USER_AGENT,
                            "Referer": "https://www.pornhub.com/",
                            "Range": f"bytes={start_b}-{end_b}",
                        },
                    )
                    with urllib.request.urlopen(rq, timeout=20) as resp:
                        data = resp.read()
                    if job.get("_cancel"):
                        return 0
                    with file_lock:
                        with open(part_path, "r+b") as out_f:
                            out_f.seek(start_b)
                            out_f.write(data)
                    with _DOWNLOAD_LOCK:
                        job["downloaded_bytes"] += len(data)
                        elapsed = max(0.15, time.time() - t_start)
                        job["speed_bps"] = int(job["downloaded_bytes"] / elapsed)
                        job["percent"] = round(
                            min(99.9, (job["downloaded_bytes"] / total_bytes) * 100.0), 1
                        )
                    return len(data)
                except Exception as exc:
                    last_err = exc
                    time.sleep(0.4 * (attempt + 1))
            raise last_err or RuntimeError("Chunk download failed")

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futs = [pool.submit(download_range, r) for r in ranges]
            for fut in as_completed(futs):
                fut.result()
                if job.get("_cancel"):
                    break

        if job.get("_cancel"):
            with _DOWNLOAD_LOCK:
                job["status"] = "cancelled"
                job["speed_bps"] = 0
            try:
                if os.path.exists(part_path):
                    os.remove(part_path)
            except OSError:
                pass
            return

        os.replace(part_path, final_path)

        with _DOWNLOAD_LOCK:
            job["status"] = "completed"
            job["percent"] = 100.0
            job["downloaded_bytes"] = total_bytes
            job["speed_bps"] = 0

            entries = [
                e for e in load_downloads_index() if e.get("filename") != job["filename"]
            ]
            entries.insert(
                0,
                {
                    "filename": job["filename"],
                    "id": vinfo["id"],
                    "title": vinfo["title"],
                    "quality": job["quality"],
                    "duration": vinfo.get("duration") or "",
                    "thumbnail": vinfo.get("thumbnail") or "",
                    "uploader": vinfo.get("uploader") or "",
                    "stars": vinfo.get("stars") or [],
                    "tags": vinfo.get("tags") or [],
                    "size_bytes": total_bytes,
                    "completed_at": int(time.time()),
                },
            )
            save_downloads_index(entries)

    except Exception as exc:
        with _DOWNLOAD_LOCK:
            job["status"] = "error"
            job["error"] = str(exc)
            job["speed_bps"] = 0
        try:
            if os.path.exists(part_path):
                os.remove(part_path)
        except OSError:
            pass


def cancel_turbo_download(job_id):
    with _DOWNLOAD_LOCK:
        job = _DOWNLOAD_JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": "Job not found"}
        job["_cancel"] = True
        if job["status"] not in ("completed", "error"):
            job["status"] = "cancelled"
    return {"ok": True}


def delete_downloaded_file(filename):
    safe = os.path.basename(filename or "")
    if not safe or not safe.lower().endswith(".mp4"):
        raise ValueError("Invalid filename")
    fpath = os.path.join(DOWNLOADS_DIR, safe)
    with _DOWNLOAD_LOCK:
        if os.path.isfile(fpath):
            os.remove(fpath)
        entries = [e for e in load_downloads_index() if e.get("filename") != safe]
        save_downloads_index(entries)
        # Also remove completed job entries for this file
        to_pop = [
            jid
            for jid, j in _DOWNLOAD_JOBS.items()
            if j.get("filename") == safe and j.get("status") in ("completed", "cancelled", "error")
        ]
        for jid in to_pop:
            _DOWNLOAD_JOBS.pop(jid, None)
    return {"ok": True}


# ============================================================================
# SUBTITLES: PORNHUB CLOSED CAPTIONS + LOCAL WHISPER AI (whisper.cpp)
# ============================================================================
TOOLS_DIR = os.path.join(BASE_DIR, "tools")
FFMPEG_EXE = os.path.join(TOOLS_DIR, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
WHISPER_CLI = os.path.join(TOOLS_DIR, "whisper", "whisper-cli.exe" if os.name == "nt" else "whisper-cli")
WHISPER_MODELS = {
    "base": os.path.join(TOOLS_DIR, "models", "ggml-base.en-q5_1.bin"),
    "tiny": os.path.join(TOOLS_DIR, "models", "ggml-tiny.en-q5_1.bin"),
}
AI_LOCAL_WINDOW = 30.0
AI_HLS_MIN_WINDOW = 24.0
_WHISPER_LOCK = threading.Lock()
_AI_SUBS_CACHE = {}
_HLS_PLAN_CACHE = {}
_SUB_JUNK = {"", "[blank_audio]", "[silence]", "(silence)", "[ silence ]", "[no speech]", "(no speech)"}


def _resolve_ffmpeg():
    if os.path.isfile(FFMPEG_EXE):
        return FFMPEG_EXE
    return shutil.which("ffmpeg") or ""


def _resolve_whisper_cli():
    if os.path.isfile(WHISPER_CLI):
        return WHISPER_CLI
    return shutil.which("whisper-cli") or ""


def ai_tools_ready():
    return bool(
        _resolve_ffmpeg()
        and _resolve_whisper_cli()
        and any(os.path.isfile(p) for p in WHISPER_MODELS.values())
    )


def _quiet_subprocess_kwargs():
    kw = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if os.name == "nt":
        # Hidden window + below-normal priority so video playback stays smooth
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0
        )
    return kw


def _sub_ts_to_sec(ts):
    m = re.match(r"(?:(\d+):)?(\d+):(\d+)(?:[,.](\d+))?", ts.strip())
    if not m:
        return 0.0
    h, mi, s, frac = m.groups()
    val = int(h or 0) * 3600 + int(mi) * 60 + int(s)
    if frac:
        val += int(frac) / (10 ** len(frac))
    return float(val)


def parse_srt_or_vtt(text):
    text = (text or "").replace("\ufeff", "").replace("\r", "")
    cues = []
    for block in re.split(r"\n\s*\n", text):
        lines = [l for l in block.split("\n") if l.strip()]
        for i, line in enumerate(lines):
            if "-->" in line:
                a, b = line.split("-->", 1)
                b = b.strip().split(" ")[0]
                body = "\n".join(lines[i + 1:])
                body = html_lib.unescape(re.sub(r"<[^>]+>", "", body)).strip()
                if body:
                    st, en = _sub_ts_to_sec(a), _sub_ts_to_sec(b)
                    cues.append({"start": round(st, 3), "end": round(max(en, st + 0.5), 3), "text": body})
                break
    cues.sort(key=lambda c: c["start"])
    return cues


def get_ph_subtitles(vkey):
    vkey = (vkey or "").strip()
    if not vkey:
        return {"available": False, "cues": []}
    ck = f"phsubs:{vkey}"
    cached = cache_get(ck, ttl=3600)
    if cached is not None:
        return cached

    w = get_video_watch_details(vkey)
    cc_path = w.get("_cc_path") or ""
    if not cc_path:
        res = {"available": False, "cues": []}
        cache_set(ck, res)
        return res

    url = urllib.parse.urljoin("https://www.pornhub.com/", cc_path)
    raw = http_get_text(
        url,
        timeout=12,
        extra_headers={"Referer": f"https://www.pornhub.com/view_video.php?viewkey={vkey}"},
    )
    cues = parse_srt_or_vtt(raw)
    res = {"available": bool(cues), "source": "pornhub", "label": "English (Pornhub)", "cues": cues}
    cache_set(ck, res)
    return res


def _fetch_media_bytes(url, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Referer": "https://www.pornhub.com/"}
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read()
        except Exception as exc:
            last = exc
            time.sleep(0.4 * (attempt + 1))
    raise last


def get_hls_audio_plan(vkey, force=False):
    """Lowest-quality HLS rendition -> list of timed segments (audio is identical across qualities)."""
    ent = _HLS_PLAN_CACHE.get(vkey)
    if ent and not force and time.time() - ent["ts"] < 1200:
        return ent
    if force:
        _CACHE.pop(f"watch:{vkey}", None)

    w = get_video_watch_details(vkey)
    hls = w["streams"].get("hls") or []
    if not hls:
        raise RuntimeError("No HLS stream available for audio extraction")
    master = urllib.parse.parse_qs(urllib.parse.urlsplit(hls[-1]["url"]).query)["url"][0]
    hdrs = {"Referer": "https://www.pornhub.com/"}
    mt = http_get_text(master, timeout=12, extra_headers=hdrs)

    child, ct = master, mt
    if "#EXTINF" not in mt:
        rel = [l.strip() for l in mt.splitlines() if l.strip() and not l.startswith("#")]
        if not rel:
            raise RuntimeError("Empty HLS master playlist")
        child = urllib.parse.urljoin(master, rel[0])
        ct = http_get_text(child, timeout=12, extra_headers=hdrs)

    init_m = re.search(r'#EXT-X-MAP:URI="([^"]+)"', ct)
    init_url = urllib.parse.urljoin(child, init_m.group(1)) if init_m else None

    segs, t, pending = [], 0.0, None
    for ln in ct.splitlines():
        ln = ln.strip()
        if ln.startswith("#EXTINF:"):
            try:
                pending = float(ln[8:].split(",")[0])
            except ValueError:
                pending = None
        elif ln and not ln.startswith("#") and pending is not None:
            segs.append({"start": t, "end": t + pending, "url": urllib.parse.urljoin(child, ln)})
            t += pending
            pending = None

    if not segs:
        raise RuntimeError("No HLS segments found")
    ent = {"ts": time.time(), "init": init_url, "segments": segs, "duration": t}
    _HLS_PLAN_CACHE[vkey] = ent
    return ent


def _run_whisper(wav_path, model_key, out_prefix):
    whisper_bin = _resolve_whisper_cli()
    if not whisper_bin:
        raise RuntimeError("whisper-cli not found. Run python setup_ai.py first.")
    model = WHISPER_MODELS.get(model_key)
    if not model or not os.path.isfile(model):
        model = next(p for p in WHISPER_MODELS.values() if os.path.isfile(p))
    threads = max(2, min(6, (os.cpu_count() or 4) - 2))
    cmd = [
        whisper_bin, "-m", model, "-f", wav_path, "-t", str(threads),
        "-oj", "-of", out_prefix, "-np", "-ml", "72", "-sow",
    ]
    r = subprocess.run(cmd, timeout=240, **_quiet_subprocess_kwargs())
    if r.returncode != 0:
        raise RuntimeError("Whisper failed: " + r.stderr.decode("utf-8", "ignore")[-300:])
    with open(out_prefix + ".json", "r", encoding="utf-8", errors="ignore") as f:
        data = json.load(f)
    return data.get("transcription") or []


def _decode_to_wav(src_args, wav_path):
    ffmpeg_bin = _resolve_ffmpeg()
    if not ffmpeg_bin:
        raise RuntimeError("ffmpeg not found. Run python setup_ai.py first.")
    cmd = [ffmpeg_bin, "-y", "-loglevel", "error"] + src_args + [
        "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav_path,
    ]
    r = subprocess.run(cmd, timeout=120, **_quiet_subprocess_kwargs())
    if r.returncode != 0 or not os.path.isfile(wav_path):
        raise RuntimeError("FFmpeg audio decode failed: " + r.stderr.decode("utf-8", "ignore")[-300:])
    return max(0.0, (os.path.getsize(wav_path) - 44) / 32000.0)


def _clean_whisper_cues(trans, offset, window_end):
    cues, last_txt = [], None
    for seg in trans:
        txt = re.sub(r"\s+", " ", (seg.get("text") or "")).strip()
        low = txt.lower()
        if low in _SUB_JUNK or "blank_audio" in low:
            continue
        o = seg.get("offsets") or {}
        st = offset + (o.get("from") or 0) / 1000.0
        en = offset + (o.get("to") or 0) / 1000.0
        if en <= st:
            en = st + 1.5
        en = min(en, window_end + 0.3)
        if cues and txt == last_txt:
            cues[-1]["end"] = round(en, 3)
            continue
        last_txt = txt
        cues.append({"start": round(st, 3), "end": round(en, 3), "text": txt})
    return cues


def _cache_ai_result(key, res):
    if len(_AI_SUBS_CACHE) > 3000:
        _AI_SUBS_CACHE.clear()
    _AI_SUBS_CACHE[key] = res


def get_ai_subtitle_window(vkey="", local_file="", start=0.0, model="base"):
    if not ai_tools_ready():
        raise RuntimeError("Local AI subtitle tools are not installed in Chores/tools")
    model = model if model in WHISPER_MODELS else "base"
    start = max(0.0, float(start))
    t0 = time.time()

    # ---------- Offline downloaded file ----------
    if local_file:
        safe = os.path.basename(local_file)
        fpath = os.path.join(DOWNLOADS_DIR, safe)
        if not os.path.isfile(fpath):
            raise RuntimeError("Local file not found")
        win_start = math.floor(start / AI_LOCAL_WINDOW) * AI_LOCAL_WINDOW
        key = f"file:{safe}:{model}:{win_start}"
        if key in _AI_SUBS_CACHE:
            return _AI_SUBS_CACHE[key]
        with _WHISPER_LOCK:
            if key in _AI_SUBS_CACHE:
                return _AI_SUBS_CACHE[key]
            with tempfile.TemporaryDirectory() as td:
                wav = os.path.join(td, "a.wav")
                actual = _decode_to_wav(
                    ["-ss", f"{win_start:.3f}", "-t", f"{AI_LOCAL_WINDOW:.3f}", "-i", fpath], wav
                )
                trans = _run_whisper(wav, model, os.path.join(td, "out")) if actual > 0.3 else []
            win_end = win_start + max(actual, 0.1)
            res = {
                "window_start": win_start,
                "window_end": round(win_end, 3),
                "is_last": actual < AI_LOCAL_WINDOW - 0.5,
                "model": model,
                "cues": _clean_whisper_cues(trans, win_start, win_end),
                "elapsed": round(time.time() - t0, 2),
            }
            _cache_ai_result(key, res)
            return res

    # ---------- Online video via HLS segments ----------
    vkey = (vkey or "").strip()
    if not vkey:
        raise ValueError("Missing vkey or file")

    for attempt in range(2):
        plan = get_hls_audio_plan(vkey, force=(attempt == 1))
        segs = plan["segments"]
        idx = len(segs) - 1
        for i, s in enumerate(segs):
            if s["end"] > start + 0.01:
                idx = i
                break
        j, acc = idx, 0.0
        while j < len(segs):
            acc += segs[j]["end"] - segs[j]["start"]
            if acc >= AI_HLS_MIN_WINDOW:
                break
            j += 1
        j = min(j, len(segs) - 1)
        win_start, win_end = segs[idx]["start"], segs[j]["end"]
        key = f"hls:{vkey}:{model}:{idx}"
        if key in _AI_SUBS_CACHE:
            return _AI_SUBS_CACHE[key]

        try:
            urls = ([plan["init"]] if plan["init"] else []) + [s["url"] for s in segs[idx:j + 1]]
            with ThreadPoolExecutor(max_workers=4) as pool:
                blobs = list(pool.map(_fetch_media_bytes, urls))
        except Exception:
            if attempt == 0:
                continue  # signed URLs may have expired -> refresh plan once
            raise

        with _WHISPER_LOCK:
            if key in _AI_SUBS_CACHE:
                return _AI_SUBS_CACHE[key]
            with tempfile.TemporaryDirectory() as td:
                media = os.path.join(td, "slice.mp4")
                with open(media, "wb") as f:
                    for b in blobs:
                        f.write(b)
                wav = os.path.join(td, "a.wav")
                actual = _decode_to_wav(["-i", media], wav)
                trans = _run_whisper(wav, model, os.path.join(td, "out")) if actual > 0.3 else []
            res = {
                "window_start": round(win_start, 3),
                "window_end": round(win_end, 3),
                "is_last": j >= len(segs) - 1,
                "model": model,
                "cues": _clean_whisper_cues(trans, win_start, win_end),
                "elapsed": round(time.time() - t0, 2),
            }
            _cache_ai_result(key, res)
            return res

    raise RuntimeError("Could not transcribe this window")


def rewrite_m3u8_manifest(manifest_text, base_url, api_prefix):
    def wrap(u):
        resolved = urllib.parse.urljoin(base_url, u.strip())
        return f"{api_prefix}/api/hls?url={urllib.parse.quote(resolved, safe='')}"

    out_lines = []
    for line in manifest_text.splitlines():
        stripped = line.strip()
        if not stripped:
            out_lines.append("")
            continue
        if stripped.startswith("#"):
            rewritten_tag = re.sub(
                r'URI="([^"]+)"',
                lambda m: f'URI="{wrap(m.group(1))}"',
                stripped,
            )
            out_lines.append(rewritten_tag)
        else:
            out_lines.append(wrap(stripped))
    return "\n".join(out_lines) + "\n"


class AppHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Range")
        self.send_header(
            "Access-Control-Expose-Headers",
            "Content-Length, Content-Range, Accept-Ranges",
        )
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self):
        clen = int(self.headers.get("Content-Length") or "0")
        if clen <= 0:
            return {}
        raw = self.rfile.read(clen).decode("utf-8", "ignore")
        return json.loads(raw) if raw else {}

    def handle_local_video_stream(self, filename):
        safe = os.path.basename(filename or "")
        fpath = os.path.join(DOWNLOADS_DIR, safe)
        if not safe or not os.path.isfile(fpath):
            return self.send_json({"error": "Local file not found"}, status=404)

        total = os.path.getsize(fpath)
        range_hdr = self.headers.get("Range")
        start_b, end_b = 0, total - 1
        status = 200

        if range_hdr and range_hdr.startswith("bytes="):
            m = re.match(r"bytes=(\d*)-(\d*)", range_hdr)
            if m:
                s_str, e_str = m.groups()
                if s_str:
                    start_b = int(s_str)
                if e_str:
                    end_b = min(total - 1, int(e_str))
                if start_b <= end_b < total:
                    status = 206

        length = (end_b - start_b) + 1
        try:
            self.send_response(status)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start_b}-{end_b}/{total}")
            self.end_headers()

            with open(fpath, "rb") as f:
                f.seek(start_b)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def handle_hls_proxy(self, target):
        if not is_allowed_media_host(target):
            return self.send_json({"error": "Disallowed media host"}, status=400)

        req = urllib.request.Request(
            target,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": "https://www.pornhub.com/",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=14) as resp:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                path_lower = urllib.parse.urlsplit(target).path.lower()
                is_m3u8 = "mpegurl" in ctype or path_lower.endswith(".m3u8")

                if is_m3u8:
                    raw = resp.read().decode("utf-8", "ignore")
                    host_hdr = self.headers.get("Host") or f"127.0.0.1:{PORT}"
                    api_prefix = f"http://{host_hdr}"
                    rewritten = rewrite_m3u8_manifest(raw, target, api_prefix).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(rewritten)))
                    self.end_headers()
                    self.wfile.write(rewritten)
                else:
                    clen = resp.headers.get("Content-Length")
                    self.send_response(200)
                    self.send_header("Content-Type", ctype or "video/mp2t")
                    self.send_header("Cache-Control", "public, max-age=600")
                    if clen:
                        self.send_header("Content-Length", clen)
                    else:
                        self.send_header("Connection", "close")
                        self.close_connection = True
                    self.end_headers()
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except urllib.error.HTTPError as exc:
            return self.send_json({"error": f"Upstream HTTP {exc.code}"}, status=exc.code)
        except Exception as exc:
            return self.send_json({"error": str(exc)}, status=502)

    def handle_mp4_stream(self, target, download_name=""):
        if not is_allowed_media_host(target):
            return self.send_json({"error": "Disallowed media host"}, status=400)

        headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.pornhub.com/",
        }
        range_hdr = self.headers.get("Range")
        if range_hdr:
            headers["Range"] = range_hdr

        req = urllib.request.Request(target, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=16) as resp:
                status = resp.status
                self.send_response(status)
                self.send_header(
                    "Content-Type", resp.headers.get("Content-Type") or "video/mp4"
                )
                self.send_header("Accept-Ranges", "bytes")
                clen = resp.headers.get("Content-Length")
                if clen:
                    self.send_header("Content-Length", clen)
                else:
                    self.send_header("Connection", "close")
                    self.close_connection = True
                crange = resp.headers.get("Content-Range")
                if crange:
                    self.send_header("Content-Range", crange)
                if download_name:
                    safe_name = re.sub(r'[^a-zA-Z0-9._ ()-]', "_", download_name)[:95]
                    self.send_header(
                        "Content-Disposition", f'attachment; filename="{safe_name}.mp4"'
                    )
                self.end_headers()
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except urllib.error.HTTPError as exc:
            return self.send_json({"error": f"Upstream HTTP {exc.code}"}, status=exc.code)
        except Exception as exc:
            return self.send_json({"error": str(exc)}, status=502)

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        try:
            payload = self.read_json_body()
        except Exception as exc:
            return self.send_json({"error": f"Invalid JSON: {exc}"}, status=400)

        if path == "/api/downloads/start":
            try:
                job = start_turbo_download(payload)
                return self.send_json({"ok": True, "job": job})
            except Exception as exc:
                return self.send_json({"error": str(exc)}, status=500)

        if path == "/api/downloads/cancel":
            jid = str(payload.get("job_id") or "")
            return self.send_json(cancel_turbo_download(jid))

        if path == "/api/downloads/delete":
            fname = str(payload.get("filename") or "")
            try:
                return self.send_json(delete_downloaded_file(fname))
            except Exception as exc:
                return self.send_json({"error": str(exc)}, status=400)

        return self.send_json({"error": "Not found"}, status=404)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/api/health":
            return self.send_json({"ok": True, "port": PORT})

        if path == "/api/hottest":
            page = int(qs.get("page", ["1"])[0] or "1")
            cc = qs.get("cc", [""])[0]
            try:
                return self.send_json(get_hottest_videos(page=page, cc=cc))
            except Exception as exc:
                return self.send_json({"error": str(exc)}, status=502)

        if path == "/api/search":
            q = qs.get("q", [""])[0]
            ordering = qs.get("ordering", ["relevant"])[0]
            period = qs.get("period", ["alltime"])[0]
            page = int(qs.get("page", ["1"])[0] or "1")
            try:
                return self.send_json(
                    search_videos(query=q, ordering=ordering, period=period, page=page)
                )
            except Exception as exc:
                return self.send_json({"error": str(exc)}, status=502)

        if path == "/api/autocomplete":
            q = qs.get("q", [""])[0]
            try:
                return self.send_json(get_search_autocomplete(q))
            except Exception as exc:
                return self.send_json({"error": str(exc)}, status=502)

        if path == "/api/performer":
            p_path = qs.get("path", [""])[0]
            p_name = qs.get("name", [""])[0]
            sort = qs.get("sort", ["newest"])[0]
            page = int(qs.get("page", ["1"])[0] or "1")
            try:
                return self.send_json(
                    get_performer_profile(path=p_path, name=p_name, sort=sort, page=page)
                )
            except Exception as exc:
                return self.send_json({"error": str(exc)}, status=502)

        if path == "/api/video":
            vid = qs.get("id", [""])[0]
            try:
                res = get_video_watch_details(vid)
                # Strip internal _raw_mp4_map before sending to client
                public_res = {k: v for k, v in res.items() if not k.startswith("_")}
                public_streams = {
                    k: v for k, v in res["streams"].items() if not k.startswith("_")
                }
                public_res["streams"] = public_streams
                return self.send_json(public_res)
            except Exception as exc:
                return self.send_json({"error": str(exc)}, status=502)

        if path == "/api/comments":
            vkey = qs.get("vkey", [""])[0]
            popular = int(qs.get("popular", ["1"])[0] or "1")
            page = int(qs.get("page", ["1"])[0] or "1")
            try:
                return self.send_json(get_video_comments(vkey, popular=popular, page=page))
            except Exception as exc:
                return self.send_json({"error": str(exc)}, status=502)

        if path == "/api/subs/ph":
            vkey = qs.get("vkey", [""])[0]
            try:
                return self.send_json(get_ph_subtitles(vkey))
            except Exception as exc:
                return self.send_json({"available": False, "cues": [], "error": str(exc)}, status=502)

        if path == "/api/subs/ai":
            vkey = qs.get("vkey", [""])[0]
            local_file = qs.get("file", [""])[0]
            model = qs.get("model", ["base"])[0]
            try:
                start = float(qs.get("start", ["0"])[0] or "0")
            except ValueError:
                start = 0.0
            try:
                return self.send_json(
                    get_ai_subtitle_window(vkey=vkey, local_file=local_file, start=start, model=model)
                )
            except Exception as exc:
                return self.send_json({"error": str(exc)}, status=502)

        if path == "/api/downloads":
            return self.send_json(list_downloads_and_jobs())

        if path == "/api/local_video":
            fname = qs.get("file", [""])[0]
            return self.handle_local_video_stream(fname)

        if path == "/api/hls":
            target = qs.get("url", [""])[0]
            return self.handle_hls_proxy(target)

        if path == "/api/stream":
            target = qs.get("url", [""])[0]
            dl = qs.get("download", [""])[0]
            return self.handle_mp4_stream(target, download_name=dl)

        if path == "/api/thumb":
            target = qs.get("url", [""])[0]
            if not is_allowed_media_host(target):
                return self.send_json({"error": "Disallowed image host"}, status=400)
            try:
                req = urllib.request.Request(
                    target,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Referer": "https://www.pornhub.com/",
                    },
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    img = resp.read()
                    ctype = resp.headers.get("Content-Type", "image/jpeg")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "public, max-age=3600")
                self.send_header("Content-Length", str(len(img)))
                self.end_headers()
                self.wfile.write(img)
                return
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return
            except Exception as exc:
                return self.send_json({"error": str(exc)}, status=502)

        return super().do_GET()

    def log_message(self, fmt, *args):
        try:
            msg = fmt % args
            if "/api/hls" in msg or "/api/thumb" in msg or "/api/downloads" in msg:
                return
            sys.stderr.write("[Server] " + msg + "\n")
            sys.stderr.flush()
        except Exception:
            pass


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


if __name__ == "__main__":
    os.chdir(BASE_DIR)
    server = ExclusiveThreadingHTTPServer(("127.0.0.1", PORT), AppHandler)
    print(f"Serving Video Explorer at http://127.0.0.1:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.", flush=True)
        server.server_close()
