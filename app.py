# app.py - Mangatek Scraper API (with anti-block protections)
# Features added vs your original file:
# - cloudscraper fallback when httpx returns 403 (Cloudflare)
# - retry loop with exponential backoff
# - rotating User-Agents
# - optional proxy support (env: HTTP_PROXIES or HTTP_PROXY)
# - simple in-memory TTL cache (cachetools)
# - slowapi rate limiting (requires Request param on endpoints)
# - clearer logging for diagnostics

import os
import re
import json
import random
import asyncio
import logging
from typing import List, Optional
from urllib.parse import urljoin, unquote, urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from bs4 import BeautifulSoup
from cachetools import TTLCache

# slowapi
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

# optional cloudscraper (blocking) - used via asyncio.to_thread
try:
    import cloudscraper
    _HAS_CLOUDSCRAPER = True
except Exception:
    _HAS_CLOUDSCRAPER = False

# ---------- logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mangatek_scraper")

# ---------- app & limiter ----------
app = FastAPI(title="Mangatek Scraper API", version="0.4.0")
RATE_LIMIT = os.getenv("RATE_LIMIT", "20/minute")
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(status_code=429, content={"detail": "Too many requests, slow down."})

# ---------- config ----------
BASE = os.getenv("MANGATEK_BASE", "https://mangatek.com")
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
]
DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    "Referer": BASE + "/",
}

# caching
CACHE_TTL = int(os.getenv("CACHE_TTL", "900"))
cache = TTLCache(maxsize=2000, ttl=CACHE_TTL)

# proxies: can supply HTTP_PROXIES as comma-separated list or HTTP_PROXY/HTTPS_PROXY single
PROXIES_ENV = os.getenv("HTTP_PROXIES", "")
PROXY_LIST = [p.strip() for p in PROXIES_ENV.split(",") if p.strip()]
SINGLE_PROXY = os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY") or None

# if true, when failing upstream returns empty results instead of raising 502
RETURN_EMPTY_ON_UPSTREAM_FAIL = os.getenv("RETURN_EMPTY_ON_UPSTREAM_FAIL", "false").lower() in ("1","true","yes")

# ---------- helpers ----------
from fastapi.responses import JSONResponse


def try_soup(html: str):
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def extract_slug_from_href(href: str) -> str:
    if not href:
        return ""
    href = href.strip("/")
    parts = href.split("/")
    if "manga" in parts:
        try:
            return parts[parts.index("manga") + 1]
        except Exception:
            pass
    if "reader" in parts:
        try:
            return parts[parts.index("reader") + 1]
        except Exception:
            pass
    return parts[-1]


def find_json_arrays_in_text(text: str) -> List:
    found = []
    arrays = re.findall(r"\[\s*(?:\"https?://[^\"]+\"(?:\s*,\s*\"https?://[^\"]+\")*)\s*\]", text)
    for a in arrays:
        try:
            parsed = json.loads(a)
            if isinstance(parsed, list):
                found.extend(parsed)
        except Exception:
            continue
    m = re.findall(r'(["\']?images["\']?\s*:\s*\[.*?\])', text, flags=re.DOTALL)
    for group in m:
        try:
            obj = "{" + group + "}"
            parsed = json.loads(obj)
            imgs = parsed.get("images") or []
            found.extend(imgs)
        except Exception:
            continue
    m2 = re.findall(r'=\s*\[.*?https?://.*?\]', text, flags=re.DOTALL)
    for g in m2:
        try:
            parsed = json.loads(g.strip().lstrip("=").strip())
            if isinstance(parsed, list):
                found.extend(parsed)
        except Exception:
            continue
    seen = set()
    uniq = []
    for u in found:
        if isinstance(u, str) and u not in seen:
            uniq.append(u); seen.add(u)
    return uniq


def choose_proxy_for_request() -> Optional[str]:
    if SINGLE_PROXY:
        return SINGLE_PROXY
    if PROXY_LIST:
        return random.choice(PROXY_LIST)
    return None


# ---------- fetch helpers with anti-block logic ----------
async def fetch_page_httpx(url: str, max_retries: int = 3, timeout: int = 20) -> str:
    last_exc = None
    for attempt in range(1, max_retries + 1):
        ua = random.choice(UA_POOL)
        headers = {**DEFAULT_HEADERS, "User-Agent": ua}
        proxy = choose_proxy_for_request()
        proxies_mapping = None
        if proxy:
            proxies_mapping = {"http://": proxy, "https://": proxy}
        try:
            logger.info("httpx try %s -> %s (UA=%s proxy=%s)", attempt, url, ua, proxy)
            timeout_cfg = httpx.Timeout(timeout)
            async with httpx.AsyncClient(timeout=timeout_cfg, follow_redirects=True, proxies=proxies_mapping) as client:
                resp = await client.get(url, headers=headers)
                # handle Cloudflare-ish responses: 403 or challenge pages
                if resp.status_code == 403 or resp.status_code == 429:
                    last_exc = httpx.HTTPStatusError(f"{resp.status_code} for {url}", request=resp.request, response=resp)
                    logger.warning("httpx got %s for %s (attempt %s)", resp.status_code, url, attempt)
                    # small wait then retry (maybe proxy rotate)
                    await asyncio.sleep(1 + attempt)
                    continue
                resp.raise_for_status()
                if resp.text and len(resp.text) > 50:
                    return resp.text
                last_exc = Exception("Empty or too short response")
        except Exception as e:
            last_exc = e
            logger.warning("httpx request error for %s: %s (attempt %s)", url, str(e), attempt)
        await asyncio.sleep(min(4, 1.5 ** attempt))
    raise HTTPException(status_code=502, detail=f"Upstream fetch failed (httpx). last_error={str(last_exc)}")


def fetch_page_cloudscraper_sync(url: str, timeout: int = 20) -> str:
    if not _HAS_CLOUDSCRAPER:
        raise RuntimeError("cloudscraper not installed")
    headers = {**DEFAULT_HEADERS, "User-Agent": random.choice(UA_POOL)}
    scr = cloudscraper.create_scraper()
    proxy = choose_proxy_for_request()
    if proxy:
        scr.proxies.update({"http": proxy, "https": proxy})
    r = scr.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text


async def fetch_html(url: str, timeout: int = 20) -> str:
    # Try httpx first, but if 403 detected then try cloudscraper (blocking) as fallback
    try:
        return await fetch_page_httpx(url, max_retries=2, timeout=timeout)
    except HTTPException as e:
        # If it's a 403/Cloudflare-like, attempt cloudscraper
        logger.warning("httpx failed; trying cloudscraper for %s; reason=%s", url, e.detail)
        if _HAS_CLOUDSCRAPER:
            try:
                html = await asyncio.to_thread(fetch_page_cloudscraper_sync, url, timeout)
                return html
            except Exception as ce:
                logger.exception("cloudscraper also failed for %s", url)
                raise HTTPException(status_code=502, detail=f"Both httpx and cloudscraper failed. last={str(ce)}")
        else:
            raise


# -------------------- Endpoints (adapted) --------------------

@app.get("/_health")
async def health():
    return {"ok": True}


@app.get("/manga-list")
@limiter.limit(RATE_LIMIT)
async def manga_list(request: Request, sort: str = Query("views"), page: int = Query(1, ge=1)):
    cache_key = f"list::{sort}::{page}"
    if cache_key in cache:
        return cache[cache_key]

    url = f"{BASE}/manga-list?sort={sort}"
    if page > 1:
        url += f"&page={page}"

    try:
        html = await fetch_html(url)
    except HTTPException as e:
        logger.exception("Failed fetching manga-list: %s", e.detail)
        if RETURN_EMPTY_ON_UPSTREAM_FAIL:
            return {"items": [], "pagination": {"current": page, "pages": []}}
        raise

    soup = try_soup(html)
    items = []
    seen_slugs = set()

    for a in soup.select("a.manga-card"):
        href = a.get("href") or ""
        slug = extract_slug_from_href(href)
        if not slug or slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        img = a.select_one("img")
        title = (img.get("alt") if img and img.get("alt") else a.get_text(strip=True)) or slug
        cover = img.get("data-src") or (img.get("src") if img else None)
        items.append({"title": title.strip(), "slug": slug, "url": urljoin(BASE, href), "cover": cover})

    if not items:
        for a in soup.select("a[href*='/manga/']"):
            href = a.get("href") or ""
            slug = extract_slug_from_href(href)
            if not slug or slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            title_el = a.select_one("h3") or a.select_one(".title") or a.select_one("h2")
            title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
            img = a.select_one("img")
            cover = img.get("data-src") or (img.get("src") if img else None)
            items.append({"title": title.strip(), "slug": slug, "url": urljoin(BASE, href), "cover": cover})

    pagination = {"current": page, "pages": []}
    pager = soup.select_one("nav[aria-label='الصفحات']") or soup.select_one(".pagination") or soup.select_one(".pagenavi")
    if pager:
        for a in pager.select("a[href]"):
            href = a.get("href")
            text = a.get_text(strip=True)
            pagination["pages"].append({"page": text, "url": urljoin(BASE, href)})

    result = {"items": items, "pagination": pagination}
    cache[cache_key] = result
    return result


@app.get("/manga/{slug}")
@limiter.limit(RATE_LIMIT)
async def manga_detail(request: Request, slug: str):
    cache_key = f"detail::{slug}"
    if cache_key in cache:
        return cache[cache_key]

    url = f"{BASE}/manga/{slug}"
    try:
        html = await fetch_html(url)
    except HTTPException as e:
        logger.exception("manga_detail fetch failed: %s", e.detail)
        if RETURN_EMPTY_ON_UPSTREAM_FAIL:
            return {"title": slug, "slug": slug, "url": None, "description": None, "cover": None, "tags": [], "rating": None, "chapters": []}
        raise

    soup = try_soup(html)
    title_el = soup.select_one("h1") or soup.select_one(".title") or soup.select_one(".entry-title")
    title = title_el.get_text(strip=True) if title_el else slug

    desc = None
    desc_el = soup.select_one("p.text-gray-300") or soup.select_one(".description") or soup.select_one(".entry-content p") or soup.select_one("meta[name='description']")
    if desc_el:
        desc = desc_el.get("content") if desc_el.name == "meta" else desc_el.get_text(strip=True)

    cover = None
    cov = soup.select_one("img.cover") or soup.select_one(".cover img") or soup.select_one(".thumb img")
    if cov:
        cover = cov.get("data-src") or cov.get("src")

    tags = [t.get_text(strip=True) for t in soup.select(".tags span, .genres span, .tag a")]

    chapters = []
    for a in soup.select("a[href*='/reader/']"):
        href = a.get("href") or ""
        chap_match = re.search(r"/reader/([^/]+)/([0-9]+)", href)
        if chap_match:
            chap_num = chap_match.group(2)
            chapters.append({"chapter_number": chap_num, "url": urljoin(BASE, href), "title": a.get_text(strip=True)})

    if not chapters:
        for a in soup.select("a[href*='/manga/']"):
            href = a.get("href") or ""
            m = re.search(r"/manga/([^/]+)/([0-9]+)", href)
            if m:
                chapters.append({"chapter_number": m.group(2), "url": urljoin(BASE, href), "title": a.get_text(strip=True)})

    res = {"title": title, "slug": slug, "url": url, "description": desc, "cover": cover, "tags": tags, "rating": None, "chapters": chapters}
    cache[cache_key] = res
    return res


@app.get("/reader/{slug}/{chapter}")
@limiter.limit(RATE_LIMIT)
async def reader(request: Request, slug: str, chapter: int):
    cache_key = f"reader::{slug}::{chapter}"
    if cache_key in cache:
        return cache[cache_key]

    url = f"{BASE}/reader/{slug}/{chapter}"
    try:
        html = await fetch_html(url)
    except HTTPException as e:
        logger.exception("reader fetch failed: %s", e.detail)
        if RETURN_EMPTY_ON_UPSTREAM_FAIL:
            return {"slug": slug, "chapter": chapter, "images": []}
        raise

    soup = try_soup(html)
    images = []

    for sel in [".reader", ".reader-container", ".chapter-images", "#reader", ".rdminimal", ".page"]:
        container = soup.select_one(sel)
        if container:
            for img in container.select("img"):
                src = img.get("data-src") or img.get("data-lazy-src") or img.get("src")
                if src and not src.startswith("data:"):
                    images.append(src)
            if images:
                break

    if not images:
        for img in soup.select("article img, .page img, .chapter img, img"):
            src = img.get("data-src") or img.get("data-lazy-src") or img.get("src")
            if src and not src.startswith("data:"):
                images.append(src)

    if not images:
        scripts = soup.find_all("script")
        for s in scripts:
            text = s.string or s.get_text() or ""
            if not text or len(text) < 20:
                continue
            found = find_json_arrays_in_text(text)
            if found:
                images.extend(found)
                break

    clean = []
    seen = set()
    for src in images:
        if not src:
            continue
        src = src.strip()
        if src.startswith("//"):
            src = "https:" + src
        if src.startswith("/"):
            src = urljoin(BASE, src)
        ok = any(k in src for k in ["/reader/", "/manga/", "/uploads/", "/covers/", "api.mangatek", "/images/"])
        if not ok and re.search(r'\.(jpe?g|png|webp)(?:\?|$)', src) and re.search(r'\d', src):
            ok = True
        if not ok:
            continue
        if src not in seen:
            seen.add(src)
            clean.append(src)

    result = {"slug": slug, "chapter": chapter, "images": clean}
    cache[cache_key] = result
    return result


@app.get("/reader/from-url")
async def reader_from_url(url: str):
    try:
        decoded = unquote(url)
    except:
        decoded = url
    try:
        parsed = urlparse(decoded)
        if parsed.netloc and "mangatek.com" not in parsed.netloc:
            raise HTTPException(status_code=400, detail="Only mangatek.com URLs allowed")
    except Exception:
        pass
    m = re.search(r"/reader/([^/]+)/([0-9]+)", decoded)
    if m:
        slug = m.group(1)
        chapter = int(m.group(2))
        return await reader(Request({}), slug, chapter)
    m2 = re.search(r"/manga/([^/]+)", decoded)
    if m2:
        slug = m2.group(1)
        mnum = re.search(r"/(\d+)(?:/?)$", decoded)
        if mnum:
            chapter = int(mnum.group(1))
            return await reader(Request({}), slug, chapter)
    raise HTTPException(status_code=400, detail="Could not extract slug/chapter from URL")
