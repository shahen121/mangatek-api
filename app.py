# app.py - Robust Mangatek scraper API (fixed proxies usage)
import os
import re
import json
import random
import asyncio
import logging
from typing import List, Optional, Tuple

import httpx
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import JSONResponse
from bs4 import BeautifulSoup
from cachetools import TTLCache

# slowapi
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

# optional fallback
try:
    import cloudscraper
    _HAS_CLOUDSCRAPER = True
except Exception:
    _HAS_CLOUDSCRAPER = False

# -------- logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mangatek")

# -------- app & limiter ----------
app = FastAPI(title="Mangatek Scraper", version="1.0.0")
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)


@app.exception_handler(RateLimitExceeded)
def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(status_code=429, content={"detail": "Too many requests, slow down."})

# -------- config ----------
# Try these domains (order matters)
DOMAINS = [os.getenv("MANGATEK_DOMAIN", "https://mangatek.com"), "https://mangatek.net"]
# Cache: ttl seconds (default 1 hour)
CACHE_TTL = int(os.getenv("CACHE_TTL", 3600))
cache = TTLCache(maxsize=2000, ttl=CACHE_TTL)

# Proxy from env (optional). Example: http://user:pass@host:port
PROXY = os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY") or None

# If you're using a SOCKS proxy (socks5://...), install httpx with socks:
# pip install "httpx[socks]"
# and ensure PROXY is like: socks5://user:pass@host:port

# User-Agent pool
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
]

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    "Referer": "https://mangatek.com/",
}

# -------- helpers ----------
def make_abs(src: Optional[str], base: str) -> Optional[str]:
    if not src:
        return None
    src = src.strip()
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("http"):
        return src
    return base.rstrip("/") + "/" + src.lstrip("/")

def find_image_array_in_scripts(soup: BeautifulSoup) -> List[str]:
    out = []
    scripts = soup.find_all("script")
    for s in scripts:
        txt = s.string or s.get_text() or ""
        if not txt or len(txt) < 50:
            continue
        # find JSON arrays of strings
        arrs = re.findall(r'\[\s*(?:"https?://[^"\]]+"(?:\s*,\s*"https?://[^"\]]+")*)\s*\]', txt)
        for a in arrs:
            try:
                parsed = json.loads(a)
                if isinstance(parsed, list):
                    out.extend(parsed)
            except Exception:
                continue
        # images: [...]
        m = re.search(r'["\']?images["\']?\s*:\s*(\[[^\]]+\])', txt, flags=re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(1))
                if isinstance(parsed, list):
                    out.extend(parsed)
            except Exception:
                continue
    # dedupe
    seen = set()
    res = []
    for u in out:
        if u not in seen:
            seen.add(u); res.append(u)
    return res

# -------- robust fetch_page (async) ----------
async def fetch_page(url: str, max_retries: int = 2, timeout: int = 20) -> str:
    """
    Robust fetch:
      - rotates UA
      - retries with backoff
      - supports proxy via env var (passed to AsyncClient constructor)
      - fallback to cloudscraper if 403 and cloudscraper present
    """
    last_exc = None
    # build proxies mapping for httpx constructor if PROXY set
    proxies_mapping = None
    if PROXY:
        proxies_mapping = {"http://": PROXY, "https://": PROXY}

    for attempt in range(1, max_retries + 2):
        ua = random.choice(UA_POOL)
        headers = {**DEFAULT_HEADERS, "User-Agent": ua}
        try:
            logger.info("HTTPX try %s -> %s (UA=%s)", attempt, url, ua)
            # pass proxies to AsyncClient constructor (not to .get)
            async with httpx.AsyncClient(timeout=timeout, proxies=proxies_mapping) as client:
                r = await client.get(url, headers=headers, follow_redirects=True)
                if r.status_code == 200 and r.text and len(r.text) > 50:
                    return r.text
                elif r.status_code == 403:
                    last_exc = httpx.HTTPStatusError("403 Forbidden", request=r.request, response=r)
                    logger.warning("403 for %s (attempt %s)", url, attempt)
                else:
                    last_exc = httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
                    logger.warning("HTTP %s for %s (attempt %s)", r.status_code, url, attempt)
        except httpx.RequestError as e:
            last_exc = e
            logger.warning("httpx request error for %s: %s (attempt %s)", url, str(e), attempt)

        await asyncio.sleep(min(3, 1.5 ** attempt))

    # fallback: cloudscraper (blocking) if installed
    if _HAS_CLOUDSCRAPER:
        try:
            logger.info("Trying cloudscraper fallback for %s", url)
            def _cs_get(u, headers, proxy):
                scr = cloudscraper.create_scraper()
                if proxy:
                    scr.proxies.update({"http": proxy, "https": proxy})
                return scr.get(u, headers=headers, timeout=timeout)
            ua = random.choice(UA_POOL)
            headers = {**DEFAULT_HEADERS, "User-Agent": ua}
            resp = await asyncio.to_thread(_cs_get, url, headers, PROXY)
            if resp.status_code == 200 and resp.text and len(resp.text) > 50:
                logger.info("cloudscraper succeeded for %s", url)
                return resp.text
            else:
                logger.warning("cloudscraper returned %s for %s", getattr(resp, "status_code", None), url)
        except Exception as e:
            logger.exception("cloudscraper fallback failed: %s", e)

    logger.error("All fetch attempts failed for %s", url)
    if isinstance(last_exc, Exception):
        raise HTTPException(status_code=502, detail=f"Upstream fetch failed: {str(last_exc)}")
    raise HTTPException(status_code=502, detail="Upstream fetch failed")

# -------- try domains helper ----------
async def fetch_html_try_domains(path_or_url: str) -> Tuple[str, str]:
    """
    Try DOMAINS list; returns (base_used, html)
    path_or_url may be full url or path like '/manga-list?...'
    """
    for base in DOMAINS:
        if path_or_url.startswith("http"):
            url = path_or_url
        else:
            url = base.rstrip("/") + "/" + path_or_url.lstrip("/")
        try:
            html = await fetch_page(url)
            return base, html
        except HTTPException as e:
            logger.warning("Attempt failed for base %s: %s", base, e.detail)
            continue
    logger.error("All domain attempts failed for path: %s", path_or_url)
    raise HTTPException(status_code=502, detail="Failed to fetch source from upstream (tried domains)")

# -------------------- Endpoints --------------------

@app.get("/_health")
async def health():
    return {"ok": True}

# ---------- manga-list ----------
@app.get("/manga-list")
@limiter.limit("10/minute")
async def manga_list(request: Request, sort: str = Query("views"), page: int = Query(1, ge=1)):
    cache_key = f"list::{sort}::{page}"
    if cache_key in cache:
        logger.info("cache hit %s", cache_key)
        return cache[cache_key]

    path = f"/manga-list?sort={sort}&page={page}"
    base, html = await fetch_html_try_domains(path)
    soup = BeautifulSoup(html, "lxml")

    items = []
    # try multiple selectors
    selectors = [".ml-item", ".manga-item", ".manga-card", ".bsx", ".item", "a.manga-card", ".manga-slider-item"]
    found = False
    for sel in selectors:
        cards = soup.select(sel)
        if cards:
            found = True
            logger.info("manga-list using selector %s (%d items)", sel, len(cards))
            for card in cards:
                a = card if card.name == "a" else (card.select_one("a") or card)
                href = a.get("href") or a.get("data-href") or ""
                img = a.select_one("img")
                cover = img.get("data-src") or img.get("src") if img else None

                title_el = a.select_one(".title") or a.select_one("h3") or a.select_one(".tt")
                if title_el and title_el.get_text(strip=True):
                    title = title_el.get_text(strip=True)
                else:
                    title = img.get("alt") if img and img.get("alt") else (a.get("title") or a.get_text(strip=True) or "Unknown")

                slug = href.rstrip("/").split("/")[-1] if href else None
                items.append({
                    "title": title,
                    "slug": slug,
                    "url": make_abs(href, base) if href else None,
                    "cover": make_abs(cover, base) if cover else None
                })
            break

    if not found:
        # fallback: anchors with /manga/
        for a in soup.select("a[href*='/manga/']"):
            href = a.get("href")
            img = a.select_one("img")
            cover = img.get("data-src") or img.get("src") if img else None
            title = a.get_text(strip=True) or (img.get("alt") if img and img.get("alt") else "Unknown")
            slug = href.rstrip("/").split("/")[-1]
            items.append({
                "title": title,
                "slug": slug,
                "url": make_abs(href, base),
                "cover": make_abs(cover, base) if cover else None
            })

    result = {"items": items, "pagination": {"current": page, "pages": []}}
    cache[cache_key] = result
    return result

# ---------- manga detail ----------
@app.get("/manga/{slug}")
@limiter.limit("10/minute")
async def manga_detail(request: Request, slug: str):
    cache_key = f"detail::{slug}"
    if cache_key in cache:
        return cache[cache_key]

    path = f"/manga/{slug}"
    base, html = await fetch_html_try_domains(path)
    soup = BeautifulSoup(html, "lxml")

    title_el = soup.select_one("h1.entry-title") or soup.select_one("h1") or soup.select_one(".post-title")
    title = title_el.get_text(strip=True) if title_el else slug

    # description/meta
    desc_el = soup.select_one(".description p") or soup.select_one(".entry-content p") or soup.select_one("meta[name='description']")
    if desc_el:
        description = desc_el.get("content") if desc_el.name == "meta" else desc_el.get_text(" ", strip=True)
    else:
        description = None

    cov_el = soup.select_one(".thumb img") or soup.select_one(".summary_image img") or soup.select_one(".cover img")
    cover = cov_el.get("data-src") or cov_el.get("src") if cov_el else None

    tags = [t.get_text(strip=True) for t in soup.select(".mgen a, .genres a, .tags a")]

    # chapters: try multiple selectors
    chapters = []
    for sel in [".chapter-list a", ".chapters a", ".eplister li a", ".wp-manga-chapter a", ".chapter a", "a[href*='/reader/']"]:
        els = soup.select(sel)
        if els:
            for a in els:
                href = a.get("href") or ""
                chap_num = href.rstrip("/").split("/")[-1] if href else None
                chapters.append({"chapter_number": chap_num, "url": make_abs(href, base), "title": a.get_text(strip=True)})
            break

    result = {
        "title": title,
        "slug": slug,
        "url": make_abs(path, base),
        "description": description,
        "cover": make_abs(cover, base) if cover else None,
        "tags": tags,
        "rating": None,
        "chapters": chapters
    }
    cache[cache_key] = result
    return result

# ---------- reader ----------
@app.get("/reader/{slug}/{chapter}")
@limiter.limit("20/minute")
async def reader(request: Request, slug: str, chapter: int, debug: Optional[bool] = Query(False)):
    cache_key = f"reader::{slug}::{chapter}"
    if cache_key in cache:
        return cache[cache_key]

    path = f"/reader/{slug}/{chapter}"
    base, html = await fetch_html_try_domains(path)
    soup = BeautifulSoup(html, "lxml")

    images: List[str] = []
    selectors = [".reading-content img", ".reader-area img", ".chapter-images img", ".rdminimal img", ".page img", ".reader img", "article img"]
    for sel in selectors:
        imgs = soup.select(sel)
        if imgs:
            logger.info("reader: using selector %s (%d imgs)", sel, len(imgs))
            for im in imgs:
                src = im.get("data-src") or im.get("data-lazy-src") or im.get("src")
                if src:
                    images.append(make_abs(src, base))
            if images:
                break

    if not images:
        # try to pull from JS arrays
        found = find_image_array_in_scripts(soup)
        if found:
            images = [make_abs(u, base) for u in found]

    # dedupe
    seen = set()
    final_images = []
    for u in images:
        if u and u not in seen:
            seen.add(u); final_images.append(u)

    result = {"slug": slug, "chapter": chapter, "images": final_images}
    if debug:
        snippet = html[:4000]
        result["debug_html_snippet"] = snippet

    cache[cache_key] = result
    return result
