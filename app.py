# app.py - Mangatek Scraper API (final)
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

# slowapi for rate limit
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

# optional cloudscraper fallback (for pages protected by Cloudflare)
try:
    import cloudscraper
    _HAS_CLOUDSCRAPER = True
except Exception:
    _HAS_CLOUDSCRAPER = False

# ---------- logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mangatek")

# ---------- app & limiter ----------
app = FastAPI(title="Mangatek Scraper", version="1.0.0")
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)


@app.exception_handler(RateLimitExceeded)
def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(status_code=429, content={"detail": "Too many requests, slow down."})


# ---------- config ----------
DOMAINS = [os.getenv("MANGATEK_DOMAIN", "https://mangatek.com")]
CACHE_TTL = int(os.getenv("CACHE_TTL", 3600))  # default 1 hour
cache = TTLCache(maxsize=3000, ttl=CACHE_TTL)

# Proxy env (optional)
PROXY = os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY") or None
# if using SOCKS proxies make sure httpx[socks] is installed and the PROXY value starts with socks5://

# user-agent pool
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


# ---------- helpers ----------
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
    """Try to extract arrays of image urls embedded in scripts"""
    out = []
    scripts = soup.find_all("script")
    for s in scripts:
        txt = s.string or s.get_text() or ""
        if not txt or len(txt) < 50:
            continue
        # find arrays like ["https://...","https://..."]
        arrs = re.findall(r'\[\s*(?:"https?://[^"\]]+"(?:\s*,\s*"https?://[^"\]]+")*)\s*\]', txt)
        for a in arrs:
            try:
                parsed = json.loads(a)
                if isinstance(parsed, list):
                    out.extend(parsed)
            except Exception:
                continue
        # try to find images: [...] inside JS objects
        m = re.search(r'["\']?images["\']?\s*:\s*(\[[^\]]+\])', txt, flags=re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(1))
                if isinstance(parsed, list):
                    out.extend(parsed)
            except Exception:
                continue
    # dedupe preserve order
    seen = set()
    res = []
    for u in out:
        if u not in seen:
            seen.add(u)
            res.append(u)
    return res


# ---------- fetch helpers ----------
async def fetch_page_httpx(url: str, max_retries: int = 2, timeout: int = 20) -> str:
    """
    Async fetch using httpx.AsyncClient.
    Pass proxies to AsyncClient constructor if PROXY set.
    """
    last_exc = None
    proxies_mapping = {"http://": PROXY, "https://": PROXY} if PROXY else None

    for attempt in range(1, max_retries + 2):
        ua = random.choice(UA_POOL)
        headers = {**DEFAULT_HEADERS, "User-Agent": ua}
        try:
            logger.info("HTTPX try %s -> %s (UA=%s)", attempt, url, ua)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, proxies=proxies_mapping) as client:
                resp = await client.get(url, headers=headers)
                # don't raise immediately; allow fallback logic
                if resp.status_code == 200 and resp.text and len(resp.text) > 50:
                    return resp.text
                # handle 403 specially (let retries continue and maybe fallback)
                if resp.status_code == 403:
                    last_exc = httpx.HTTPStatusError("403 Forbidden", request=resp.request, response=resp)
                    logger.warning("Received 403 for %s (attempt %s)", url, attempt)
                else:
                    last_exc = httpx.HTTPStatusError(f"{resp.status_code}", request=resp.request, response=resp)
                    logger.warning("HTTP %s for %s (attempt %s)", resp.status_code, url, attempt)
        except httpx.RequestError as e:
            last_exc = e
            logger.warning("httpx request error for %s: %s (attempt %s)", url, str(e), attempt)

        await asyncio.sleep(min(3, 1.5 ** attempt))

    # if we get here, httpx failed -> raise last or generic
    logger.error("httpx all attempts failed for %s", url)
    if isinstance(last_exc, Exception):
        raise HTTPException(status_code=502, detail=f"Upstream fetch failed: {str(last_exc)}")
    raise HTTPException(status_code=502, detail="Upstream fetch failed")


def fetch_page_cloudscraper_sync(url: str, timeout: int = 20) -> str:
    """
    Blocking fetch using cloudscraper (suitable for Cloudflare-protected pages).
    Intended to be used inside asyncio.to_thread().
    """
    if not _HAS_CLOUDSCRAPER:
        raise RuntimeError("cloudscraper not installed")

    headers = {**DEFAULT_HEADERS, "User-Agent": random.choice(UA_POOL)}
    scr = cloudscraper.create_scraper()
    if PROXY:
        scr.proxies.update({"http": PROXY, "https": PROXY})
    r = scr.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text


async def fetch_html_try_domains(path_or_url: str, prefer_cloudscraper: bool = False) -> Tuple[str, str]:
    """
    Try configured DOMAINS. If path_or_url is a full url, try it directly.
    prefer_cloudscraper=True will try cloudscraper first (good for pages that often return 403).
    Returns (base_used, html_text).
    """
    candidates = []
    if path_or_url.startswith("http"):
        candidates = [path_or_url]
    else:
        for base in DOMAINS:
            candidates.append(base.rstrip("/") + "/" + path_or_url.lstrip("/"))

    last_exc = None
    for url in candidates:
        try:
            if prefer_cloudscraper and _HAS_CLOUDSCRAPER:
                # run blocking cloudscraper in thread
                logger.info("Using cloudscraper for %s", url)
                html = await asyncio.to_thread(fetch_page_cloudscraper_sync, url)
                # return base part
                base = "/".join(url.split("/")[:3])
                return base, html
            else:
                html = await fetch_page_httpx(url)
                base = "/".join(url.split("/")[:3])
                return base, html
        except HTTPException as e:
            logger.warning("fetch attempt failed for %s -> %s", url, e.detail)
            last_exc = e
        except Exception as e:
            logger.warning("unexpected fetch error for %s -> %s", url, str(e))
            last_exc = e
            continue

    logger.error("All domain attempts failed for %s", path_or_url)
    if isinstance(last_exc, HTTPException):
        raise last_exc
    raise HTTPException(status_code=502, detail="Failed to fetch source from upstream (tried domains)")


# -------------------- Endpoints --------------------

@app.get("/_health")
async def health():
    return {"ok": True}


# ---------- manga-list (use cloudscraper fallback because this endpoint often blocked) ----------
@app.get("/manga-list")
@limiter.limit("10/minute")
async def manga_list(request: Request, sort: str = Query("views"), page: int = Query(1, ge=1)):
    cache_key = f"list::{sort}::{page}"
    if cache_key in cache:
        logger.info("cache hit %s", cache_key)
        return cache[cache_key]

    path = f"/manga-list?sort={sort}&page={page}"

    # use cloudscraper for manga-list because site often returns 403 to simple requests
    prefer_cloud = True if _HAS_CLOUDSCRAPER else False
    base, html = await fetch_html_try_domains(path, prefer_cloudscraper=prefer_cloud)

    soup = BeautifulSoup(html, "html.parser")

    items = []
    # common card selectors (mangatek markup)
    selectors = [".manga-card", ".manga-chapter", ".ml-item", ".manga-item", "a.manga-card", ".item"]
    used_sel = None
    for sel in selectors:
        cards = soup.select(sel)
        if cards:
            used_sel = sel
            logger.info("manga-list: found selector %s (%d)", sel, len(cards))
            for card in cards:
                a = card if card.name == "a" else (card.select_one("a") or card)
                href = a.get("href") or a.get("data-href") or ""
                img = a.select_one("img")
                cover = img.get("data-src") or img.get("src") if img else None
                # title: try elements then attributes
                title_el = a.select_one(".title") or a.select_one("h3") or a.select_one(".tt")
                if title_el and title_el.get_text(strip=True):
                    title = title_el.get_text(strip=True)
                else:
                    title = (img.get("alt") if img and img.get("alt") else (a.get("title") or a.get_text(strip=True) or "Unknown"))
                slug = href.rstrip("/").split("/")[-1] if href else None
                items.append({
                    "title": title,
                    "slug": slug,
                    "url": make_abs(href, base) if href else None,
                    "cover": make_abs(cover, base) if cover else None
                })
            break

    # fallback generic anchors
    if not items:
        logger.info("manga-list: fallback anchor scan")
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
    base, html = await fetch_html_try_domains(path, prefer_cloudscraper=False)
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.select_one("h1.entry-title") or soup.select_one("h1") or soup.select_one(".post-title")
    title = title_el.get_text(strip=True) if title_el else slug

    desc_el = soup.select_one(".description p") or soup.select_one(".entry-content p") or soup.select_one("meta[name='description']")
    if desc_el:
        description = desc_el.get("content") if desc_el.name == "meta" else desc_el.get_text(" ", strip=True)
    else:
        description = None

    cov_el = soup.select_one(".thumb img") or soup.select_one(".summary_image img") or soup.select_one(".cover img")
    cover = cov_el.get("data-src") or cov_el.get("src") if cov_el else None

    tags = [t.get_text(strip=True) for t in soup.select(".mgen a, .genres a, .tags a")]

    # chapters
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
    # reader pages usually not heavily protected, try httpx first
    base, html = await fetch_html_try_domains(path, prefer_cloudscraper=False)
    soup = BeautifulSoup(html, "html.parser")

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
        found = find_image_array_in_scripts(soup)
        if found:
            images = [make_abs(u, base) for u in found]

    # dedupe and filter
    seen = set()
    final_images = []
    for u in images:
        if u and u not in seen:
            seen.add(u)
            final_images.append(u)

    result = {"slug": slug, "chapter": chapter, "images": final_images}
    if debug:
        result["debug_html_snippet"] = html[:4000]

    cache[cache_key] = result
    return result
