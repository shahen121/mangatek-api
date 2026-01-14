# app.py - FastAPI mangatek scraper with Playwright fallback
import os
import re
import json
import asyncio
import logging
from typing import List, Optional
from urllib.parse import urljoin, unquote, urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.middleware import SlowAPIMiddleware

import httpx
import cloudscraper
from bs4 import BeautifulSoup
from cachetools import TTLCache, cached

# Playwright async
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ---------- config ----------
BASE = os.getenv("MANGATEK_BASE", "https://mangatek.com")
USE_PLAYWRIGHT = os.getenv("USE_PLAYWRIGHT", "1") == "1"  # default on
PLAYWRIGHT_PROXY = os.getenv("PLAYWRIGHT_PROXY")  # e.g. http://user:pass@host:port
HTTP_PROXIES = os.getenv("HTTP_PROXIES")  # optional proxies for httpx/cloudscraper (not parsed here)
CACHE_TTL = int(os.getenv("CACHE_TTL", "300"))
RATE_LIMIT = os.getenv("RATE_LIMIT", "15/minute")  # default
PLAYWRIGHT_HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "1") == "1"

HEADERS = {
    "User-Agent": os.getenv("DEFAULT_UA", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------- logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mangatek_scraper")

# ---------- app & rate limiter ----------
app = FastAPI(title="Mangatek Scraper (Playwright enabled)", version="0.4.0")
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(429, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# ---------- cache ----------
cache = TTLCache(maxsize=1024, ttl=CACHE_TTL)

# ---------- helpers ----------
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

def find_json_arrays_in_text(text: str) -> List[str]:
    found = []
    arrays = re.findall(r'\[\s*(?:"https?://[^"]+"(?:\s*,\s*"https?://[^"]+")*)\s*\]', text)
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

# ---------- HTTP fetch helpers ----------
async def fetch_page_playwright(url: str, timeout: int = 20) -> str:
    """
    Use Playwright to retrieve fully-rendered HTML. Returns HTML string.
    """
    logger.info("Playwright fetching: %s", url)
    # Playwright launch options
    chromium_args = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
    # Proxy config for Playwright if provided
    pw_proxy = None
    if PLAYWRIGHT_PROXY:
        pw_proxy = {"server": PLAYWRIGHT_PROXY}

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=PLAYWRIGHT_HEADLESS, args=chromium_args, proxy=pw_proxy)
            context = await browser.new_context(user_agent=HEADERS.get("User-Agent"), locale="en-US")
            page = await context.new_page()
            # navigate
            try:
                await page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
            except PlaywrightTimeoutError:
                logger.warning("Playwright timeout on goto; trying 'load' then wait")
                try:
                    await page.goto(url, wait_until="load", timeout=timeout * 1000)
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
            # optionally wait small time for dynamic content
            await asyncio.sleep(0.3)
            content = await page.content()
            await context.close()
            await browser.close()
            return content
    except Exception as e:
        logger.exception("Playwright fetch failed for %s: %s", url, str(e))
        raise

async def fetch_page_httpx(url: str, timeout: int = 20) -> str:
    """
    Async httpx fallback (no JS rendering).
    """
    logger.info("httpx fetching: %s", url)
    proxies = None
    # httpx expects "proxies" on client init as dict (optional)
    if HTTP_PROXIES:
        # user may set a single proxy string
        proxies = {"all://": HTTP_PROXIES}
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=HEADERS, proxies=proxies) as client:
            r = await client.get(url, follow_redirects=True)
            r.raise_for_status()
            return r.text
    except Exception as e:
        logger.exception("httpx fetch failed for %s: %s", url, str(e))
        raise

def fetch_page_cloudscraper_sync(url: str, timeout: int = 20) -> str:
    """
    Synchronous cloudscraper fallback (blocking). Run via to_thread.
    """
    logger.info("cloudscraper fetching: %s", url)
    try:
        s = cloudscraper.create_scraper()
        r = s.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.exception("cloudscraper fetch failed for %s: %s", url, str(e))
        raise

async def fetch_html(url: str, timeout: int = 20) -> str:
    """
    Try Playwright -> httpx -> cloudscraper (blocking) with logging.
    Raises HTTPException on total failure.
    """
    last_exc = None
    # 1) Playwright (preferred when enabled)
    if USE_PLAYWRIGHT:
        try:
            html = await fetch_page_playwright(url, timeout=timeout)
            if html and len(html) > 50:
                return html
        except Exception as e:
            last_exc = e
            logger.warning("Playwright attempt failed: %s", str(e))

    # 2) httpx async
    try:
        html = await fetch_page_httpx(url, timeout=timeout)
        if html and len(html) > 50:
            return html
    except Exception as e:
        last_exc = e
        logger.warning("httpx attempt failed: %s", str(e))

    # 3) cloudscraper (blocking)
    try:
        html = await asyncio.to_thread(fetch_page_cloudscraper_sync, url, timeout)
        if html and len(html) > 50:
            return html
    except Exception as e:
        last_exc = e
        logger.warning("cloudscraper attempt failed: %s", str(e))

    logger.error("Both Playwright/httpx/cloudscraper failed. last=%s", str(last_exc))
    raise HTTPException(status_code=502, detail=f"Fetching failed. last={str(last_exc)}")

# ---------- parsing helpers ----------
def soup_from_html(html: str):
    return try_soup(html)

# ---------- endpoints ----------
@cached(cache)
@limiter.limit(RATE_LIMIT)
@app.get("/manga-list")
async def manga_list(request: Request, sort: str = Query("views"), page: int = Query(1, ge=1)):
    url = f"{BASE}/manga-list?sort={sort}"
    if page > 1:
        url += f"&page={page}"
    try:
        html = await fetch_html(url)
    except HTTPException as e:
        logger.error("Failed fetching manga-list: %s", e.detail)
        raise
    except Exception as e:
        logger.exception("Unexpected error fetching manga-list")
        raise HTTPException(status_code=502, detail="Failed to fetch source")

    soup = soup_from_html(html)
    items = []
    seen_slugs = set()

    # strategy A
    for a in soup.select("a.manga-card"):
        href = a.get("href") or ""
        slug = extract_slug_from_href(href)
        if not slug or slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        img = a.select_one("img")
        title = (img.get("alt") if img else a.get_text(strip=True)) or slug
        cover = img.get("src") if img else None
        items.append({"title": title.strip(), "slug": slug, "url": urljoin(BASE, href), "cover": cover})

    # strategy B
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
            cover = img.get("data-src") or img.get("src") if img else None
            items.append({"title": title.strip(), "slug": slug, "url": urljoin(BASE, href), "cover": cover})

    pagination = {"current": page, "pages": []}
    pager = soup.select_one("nav[aria-label='الصفحات']") or soup.select_one(".pagination") or soup.select_one(".pagenavi")
    if pager:
        for a in pager.select("a[href]"):
            href = a.get("href")
            text = a.get_text(strip=True)
            pagination["pages"].append({"page": text, "url": urljoin(BASE, href)})

    return {"items": items, "pagination": pagination, "note": None if items else "No items found; check logs."}

@cached(cache)
@limiter.limit(RATE_LIMIT)
@app.get("/manga/{slug}")
async def manga_detail(request: Request, slug: str):
    url = f"{BASE}/manga/{slug}"
    try:
        html = await fetch_html(url)
    except HTTPException as e:
        logger.error("Upstream status or failure for %s: %s", url, e.detail)
        raise
    except Exception:
        logger.exception("Failed to fetch manga detail")
        raise HTTPException(status_code=502, detail="Failed to fetch source")

    soup = soup_from_html(html)
    title_el = soup.select_one("h1") or soup.select_one(".title") or soup.select_one(".entry-title")
    title = title_el.get_text(strip=True) if title_el else slug

    desc = None
    desc_el = soup.select_one("p.text-gray-300") or soup.select_one(".description") or soup.select_one(".entry-content p") or soup.select_one("meta[name='description']")
    if desc_el:
        if desc_el.name == "meta":
            desc = desc_el.get("content")
        else:
            desc = desc_el.get_text(strip=True)

    cover = None
    cov = soup.select_one("img.cover") or soup.select_one(".cover img") or soup.select_one(".thumb img")
    if cov:
        cover = cov.get("data-src") or cov.get("src")

    tags = [t.get_text(strip=True) for t in soup.select(".tags span, .genres span, .tag a")]

    chapters = []
    for a in soup.select("a[href*='/reader/']"):
        href = a.get("href") or ""
        chap_match = re.search(r"/reader/([^/]+)/(\d+)", href)
        if chap_match:
            chap_slug = chap_match.group(1)
            chap_num = chap_match.group(2)
            title_text = a.get_text(strip=True)
            chapters.append({"chapter_number": chap_num, "url": urljoin(BASE, href), "title": title_text})
    if not chapters:
        for a in soup.select("a[href*='/manga/']"):
            href = a.get("href") or ""
            m = re.search(r"/manga/([^/]+)/(\d+)", href)
            if m:
                chapters.append({"chapter_number": m.group(2), "url": urljoin(BASE, href), "title": a.get_text(strip=True)})

    return {
        "title": title,
        "slug": slug,
        "url": url,
        "description": desc,
        "cover": cover,
        "tags": tags,
        "rating": None,
        "chapters": chapters
    }

@cached(cache)
@limiter.limit(RATE_LIMIT)
@app.get("/reader/{slug}/{chapter}")
async def reader(request: Request, slug: str, chapter: int, debug: Optional[bool] = Query(False)):
    url = f"{BASE}/reader/{slug}/{chapter}"
    try:
        html = await fetch_html(url)
    except HTTPException as e:
        logger.error("Failed fetching reader page %s: %s", url, e.detail)
        raise
    except Exception:
        logger.exception("Failed to fetch reader page")
        raise HTTPException(status_code=502, detail="Failed to fetch source")

    soup = soup_from_html(html)
    images = []

    # Strategy 1: reader containers
    for sel in [".reader", ".reader-container", ".chapter-images", "#reader", ".rdminimal", ".page"]:
        container = soup.select_one(sel)
        if container:
            imgs = container.select("img")
            for img in imgs:
                src = img.get("data-src") or img.get("data-lazy-src") or img.get("src")
                if src and not src.startswith("data:"):
                    images.append(src)
            if images:
                break

    # Strategy 2: fallback image scan
    if not images:
        for img in soup.select("article img, .page img, .chapter img, img"):
            src = img.get("data-src") or img.get("data-lazy-src") or img.get("src")
            if src and not src.startswith("data:"):
                images.append(src)

    # Strategy 3: parse scripts for image arrays
    if not images:
        for s in soup.find_all("script"):
            text = s.string or s.get_text() or ""
            if not text or len(text) < 20:
                continue
            found = find_json_arrays_in_text(text)
            if found:
                images.extend(found)
                break

    # cleanup & heuristics
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

    return {"slug": slug, "chapter": chapter, "images": clean, "note": None if clean else "No images found; maybe JS-rendered or blocked."}

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
    m = re.search(r"/reader/([^/]+)/(\d+)", decoded)
    if m:
        slug = m.group(1)
        chapter = int(m.group(2))
        return await reader(Request(scope={"type":"http"}), slug, chapter)
    m2 = re.search(r"/manga/([^/]+)", decoded)
    if m2:
        slug = m2.group(1)
        mnum = re.search(r"/(\d+)(?:/?)$", decoded)
        if mnum:
            chapter = int(mnum.group(1))
            return await reader(Request(scope={"type":"http"}), slug, chapter)
    raise HTTPException(status_code=400, detail="Could not extract slug/chapter from URL")

@app.get("/_health")
def health():
    return {"ok": True}
