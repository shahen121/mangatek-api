# app.py - Fully Resilient Mangatek Scraper with Enhanced Proxy & UA Rotation, Rate Limiting, and Backoff
from fastapi import FastAPI, HTTPException, Query
from typing import List, Optional, Dict, Any
import httpx
from bs4 import BeautifulSoup
import re
import logging
import json
import random
from urllib.parse import urljoin, unquote, urlparse
import time
import backoff  # Add this library for exponential backoff (pip install backoff)

# إعداد السجلات (Logging)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mangatek_scraper")

app = FastAPI(title="Mangatek Scraper API (Resilient Edition)", version="0.5.0")
BASE = "https://mangatek.com"

# 1. قائمة هويات المتصفح (User-Agent Rotation) 🎭 - Expanded list for better variety
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36 Edg/118.0.2088.46",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/119.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/119.0",
    "Mozilla/5.0 (iPad; CPU OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Android 14; Mobile; rv:109.0) Gecko/20100101 Firefox/119.0"
]

# 2. قائمة البروكسيات (Proxy Rotation) 🌐
# ملاحظة: استبدل هذه العناوين ببروكسيات تعمل لديك.
# الصيغة: http://username:password@ip:port أو http://ip:port
# لتحسين: استخدم خدمة مثل Bright Data أو ScrapingBee لروتيشن تلقائي، لكن هنا نستخدم قائمة محلية.
PROXIES_LIST = [
    # "http://user:pass@ip:port",
    # مثال لبروكسي بدون كلمة سر: "http://1.2.3.4:8080"
]

# 3. إضافة تأخير أساسي بين الطلبات (Rate Limiting) ⏳
MIN_DELAY = 2  # ثواني minimun
MAX_DELAY = 5  # ثواني maximum

# ---------- helpers ----------
def try_soup(html: str):
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")

@backoff.on_exception(backoff.expo, (httpx.RequestError, httpx.HTTPStatusError), max_tries=5, max_time=60)
async def fetch_html(url: str, timeout: int = 20) -> str:
    """
    جلب HTML مع تدوير البروكسي والهوية، نظام محاولات متكررة مع backoff exponential، وتأخير عشوائي.
    """
    # تأخير عشوائي لتجنب الكشف عن نمط
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
    
    # اختيار هوية عشوائية مع headers أكثر شمولاً لتبدو كمتصفح حقيقي
    current_headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": random.choice(["https://www.google.com/", "https://www.bing.com/", BASE]),
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1"
    }
    
    # اختيار بروكسي عشوائي (إذا كانت القائمة غير فارغة)
    current_proxy = random.choice(PROXIES_LIST) if PROXIES_LIST else None
    
    logger.info(f"Fetching: {url} | Proxy: {current_proxy} | UA: {current_headers['User-Agent']}")
    
    async with httpx.AsyncClient(
        timeout=timeout,
        headers=current_headers,
        proxy=current_proxy,
        follow_redirects=True
    ) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.text

def extract_slug_from_href(href: str) -> str:
    if not href: return ""
    href = href.strip("/")
    parts = href.split("/")
    if "manga" in parts:
        try: return parts[parts.index("manga") + 1]
        except: pass
    if "reader" in parts:
        try: return parts[parts.index("reader") + 1]
        except: pass
    return parts[-1]

def find_json_arrays_in_text(text: str) -> List:
    found = []
    arrays = re.findall(r'\[\s*(?:"https?://[^"]+"(?:\s*,\s*"https?://[^"]+")*)\s*\]', text)
    for a in arrays:
        try:
            parsed = json.loads(a)
            if isinstance(parsed, list): found.extend(parsed)
        except: continue
    m = re.findall(r'(["\']?images["\']?\s*:\s*\[.*?\])', text, flags=re.DOTALL)
    for group in m:
        try:
            obj = "{" + group + "}"
            parsed = json.loads(obj)
            imgs = parsed.get("images") or []
            found.extend(imgs)
        except: continue
    m2 = re.findall(r'=\s*\[.*?https?://.*?\]', text, flags=re.DOTALL)
    for g in m2:
        try:
            parsed = json.loads(g.strip().lstrip("=").strip())
            if isinstance(parsed, list): found.extend(parsed)
        except: continue
    seen = set(); uniq = []
    for u in found:
        if isinstance(u, str) and u not in seen:
            uniq.append(u); seen.add(u)
    return uniq

# ---------- endpoints ----------
@app.get("/manga-list")
async def manga_list(sort: str = Query("views"), page: int = Query(1, ge=1)):
    url = f"{BASE}/manga-list?sort={sort}"
    if page > 1: url += f"&page={page}"
    html = await fetch_html(url)
    soup = try_soup(html)
    items = []
    seen_slugs = set()
    for a in soup.select("a.manga-card"):
        href = a.get("href") or ""
        slug = extract_slug_from_href(href)
        if not slug or slug in seen_slugs: continue
        seen_slugs.add(slug)
        title = (a.select_one("img").get("alt") if a.select_one("img") else a.get_text(strip=True)) or slug
        cover = a.select_one("img").get("src") if a.select_one("img") else None
        items.append({"title": title.strip(), "slug": slug, "url": urljoin(BASE, href), "cover": cover})
    if not items:
        for a in soup.select("a[href*='/manga/']"):
            href = a.get("href") or ""
            slug = extract_slug_from_href(href)
            if not slug or slug in seen_slugs: continue
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
            pagination["pages"].append({"page": a.get_text(strip=True), "url": urljoin(BASE, a.get("href"))})
    return {"items": items, "pagination": pagination}

@app.get("/manga/{slug}")
async def manga_detail(slug: str):
    url = f"{BASE}/manga/{slug}"
    html = await fetch_html(url)
    soup = try_soup(html)
    title_el = soup.select_one("h1") or soup.select_one(".title") or soup.select_one(".entry-title")
    title = title_el.get_text(strip=True) if title_el else slug
    desc = None
    desc_el = soup.select_one("p.text-gray-300") or soup.select_one(".description") or soup.select_one(".entry-content p") or soup.select_one("meta[name='description']")
    if desc_el:
        desc = desc_el.get("content") if desc_el.name == "meta" else desc_el.get_text(strip=True)
    cover = None
    cov = soup.select_one("img.cover") or soup.select_one(".cover img") or soup.select_one(".thumb img")
    if cov: cover = cov.get("data-src") or cov.get("src")
    chapters = []
    for a in soup.select("a[href*='/reader/']"):
        href = a.get("href") or ""
        m = re.search(r"/reader/([^/]+)/(\d+)", href)
        if m: chapters.append({"chapter_number": m.group(2), "url": urljoin(BASE, href), "title": a.get_text(strip=True)})
    return {"title": title, "slug": slug, "description": desc, "cover": cover, "chapters": chapters}

@app.get("/reader/{slug}/{chapter}")
async def reader(slug: str, chapter: int):
    url = f"{BASE}/reader/{slug}/{chapter}"
    html = await fetch_html(url)
    soup = try_soup(html)
    images = []
    for sel in [".reader", ".reader-container", ".chapter-images", "#reader", ".rdminimal", ".page"]:
        container = soup.select_one(sel)
        if container:
            for img in container.select("img"):
                src = img.get("data-src") or img.get("data-lazy-src") or img.get("src")
                if src and not src.startswith("data:"): images.append(src)
            if images: break
    if not images:
        scripts = soup.find_all("script")
        for s in scripts:
            found = find_json_arrays_in_text(s.string or s.get_text() or "")
            if found: images.extend(found); break
    clean = []
    seen = set()
    for src in images:
        src = urljoin(BASE, src.strip().replace("//", "https://") if src.startswith("//") else src.strip())
        if src not in seen:
            seen.add(src)
            clean.append(src)
    return {"slug": slug, "chapter": chapter, "images": clean}

@app.get("/_health")
def health():
    return {"ok": True, "proxies_loaded": len(PROXIES_LIST)}
