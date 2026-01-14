# app.py - improved resilient Mangatek scraper
from fastapi import FastAPI, HTTPException, Query
from typing import List, Optional, Dict, Any
import httpx
from bs4 import BeautifulSoup
import re
import logging
import json
from urllib.parse import urljoin, unquote, urlparse

# logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mangatek_scraper")

app = FastAPI(title="Mangatek Scraper API", version="0.3.0")

BASE = "https://mangatek.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9"
}

# ---------- helpers ----------
def try_soup(html: str):
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")

async def fetch_html(url: str, timeout: int = 20) -> str:
    """
    Fetch HTML with headers; raises httpx.HTTPStatusError on non-200.
    """
    logger.info("Fetching: %s", url)
    async with httpx.AsyncClient(timeout=timeout, headers=HEADERS) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.text

def extract_slug_from_href(href: str) -> str:
    if not href:
        return ""
    href = href.strip("/")
    parts = href.split("/")
    # common forms: manga/slug or reader/slug/num
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
    # fallback last part
    return parts[-1]

def find_json_arrays_in_text(text: str) -> List:
    """
    حاول إيجاد أي array من روابط داخل نص السكربت مثل ["https://...","..."]
    أو {"images":[...]} أو var imgs = [...]
    """
    found = []
    # أولاً اي مصفوفة JSON من روابط
    arrays = re.findall(r'\[\s*(?:"https?://[^"]+"(?:\s*,\s*"https?://[^"]+")*)\s*\]', text)
    for a in arrays:
        try:
            parsed = json.loads(a)
            if isinstance(parsed, list):
                found.extend(parsed)
        except Exception:
            continue

    # ثانياً ابحث عن key: images: [...]
    m = re.findall(r'(["\']?images["\']?\s*:\s*\[.*?\])', text, flags=re.DOTALL)
    for group in m:
        try:
            # add braces to make valid JSON
            obj = "{" + group + "}"
            parsed = json.loads(obj)
            imgs = parsed.get("images") or []
            found.extend(imgs)
        except Exception:
            continue

    # ثالثاً متغيرات JS بسيطة: var pics = ["...","..."]
    m2 = re.findall(r'=\s*\[.*?https?://.*?\]', text, flags=re.DOTALL)
    for g in m2:
        try:
            parsed = json.loads(g.strip().lstrip("=").strip())
            if isinstance(parsed, list):
                found.extend(parsed)
        except Exception:
            continue

    # dedupe and return
    seen = set()
    uniq = []
    for u in found:
        if isinstance(u, str) and u not in seen:
            uniq.append(u); seen.add(u)
    return uniq

# ---------- endpoints ----------

@app.get("/manga-list")
async def manga_list(sort: str = Query("views"), page: int = Query(1, ge=1)):
    """
    Returns list of manga cards. resilient selectors.
    """
    url = f"{BASE}/manga-list?sort={sort}"
    if page > 1:
        url += f"&page={page}"

    try:
        html = await fetch_html(url)
    except Exception as e:
        logger.exception("Failed fetching manga-list")
        raise HTTPException(status_code=502, detail="Failed to fetch source")

    soup = try_soup(html)

    items = []
    seen_slugs = set()

    # Strategy A: site uses cards with class 'manga-card'
    for a in soup.select("a.manga-card"):
        href = a.get("href") or ""
        slug = extract_slug_from_href(href)
        if not slug or slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        title = (a.select_one("img").get("alt") if a.select_one("img") else a.get_text(strip=True)) or slug
        cover = a.select_one("img").get("src") if a.select_one("img") else None
        items.append({"title": title.strip(), "slug": slug, "url": urljoin(BASE, href), "cover": cover})

    # Strategy B: generic anchors that contain /manga/
    if not items:
        for a in soup.select("a[href*='/manga/']"):
            href = a.get("href") or ""
            slug = extract_slug_from_href(href)
            if not slug or slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            # prefer title inside child elements
            title_el = a.select_one("h3") or a.select_one(".title") or a.select_one("h2")
            title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
            img = a.select_one("img")
            cover = img.get("data-src") or img.get("src") if img else None
            items.append({"title": title.strip(), "slug": slug, "url": urljoin(BASE, href), "cover": cover})

    # Strategy C: older theme markup (list of items)
    if not items:
        # try common selectors seen in some WP themes
        for card in soup.select(".bs .bsx, .manga-item, .item, .post"):
            a = card.select_one("a[href*='/manga/']")
            if not a:
                continue
            href = a.get("href") or ""
            slug = extract_slug_from_href(href)
            if not slug or slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            title = (card.select_one(".tt") or card.select_one(".title") or a).get_text(strip=True)
            img = card.select_one("img")
            cover = img.get("src") if img else None
            items.append({"title": title, "slug": slug, "url": urljoin(BASE, href), "cover": cover})

    # pagination: try aria nav or .pagination
    pagination = {"current": page, "pages": []}
    pager = soup.select_one("nav[aria-label='الصفحات']") or soup.select_one(".pagination") or soup.select_one(".pagenavi")
    if pager:
        for a in pager.select("a[href]"):
            href = a.get("href")
            text = a.get_text(strip=True)
            pagination["pages"].append({"page": text, "url": urljoin(BASE, href)})

    return {"items": items, "pagination": pagination, "note": None if items else "No items found with current selectors; check logs for details."}

@app.get("/manga/{slug}")
async def manga_detail(slug: str):
    url = f"{BASE}/manga/{slug}"
    try:
        html = await fetch_html(url)
    except httpx.HTTPStatusError as e:
        logger.error("Upstream status %s for %s", e.response.status_code, url)
        raise HTTPException(status_code=502, detail=f"Upstream returned {e.response.status_code}")
    except Exception:
        logger.exception("Failed to fetch manga detail")
        raise HTTPException(status_code=502, detail="Failed to fetch source")

    soup = try_soup(html)

    # title
    title_el = soup.select_one("h1") or soup.select_one(".title") or soup.select_one(".entry-title")
    title = title_el.get_text(strip=True) if title_el else slug

    # description
    desc = None
    desc_el = soup.select_one("p.text-gray-300") or soup.select_one(".description") or soup.select_one(".entry-content p") or soup.select_one("meta[name='description']")
    if desc_el:
        if desc_el.name == "meta":
            desc = desc_el.get("content")
        else:
            desc = desc_el.get_text(strip=True)

    # cover
    cover = None
    cov = soup.select_one("img.cover") or soup.select_one(".cover img") or soup.select_one(".thumb img")
    if cov:
        cover = cov.get("data-src") or cov.get("src")

    # tags
    tags = [t.get_text(strip=True) for t in soup.select(".tags span, .genres span, .tag a")]

    # chapters (try reader links)
    chapters = []
    # first try: links to /reader/<slug>/<num>
    for a in soup.select("a[href*='/reader/']"):
        href = a.get("href") or ""
        chap_match = re.search(r"/reader/([^/]+)/(\d+)", href)
        if chap_match:
            chap_slug = chap_match.group(1)
            chap_num = chap_match.group(2)
            title_text = a.get_text(strip=True)
            chapters.append({"chapter_number": chap_num, "url": urljoin(BASE, href), "title": title_text})
    # fallback: links that contain "/manga/<slug>/" with numbers
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

@app.get("/reader/{slug}/{chapter}")
async def reader(slug: str, chapter: int):
    url = f"{BASE}/reader/{slug}/{chapter}"
    try:
        html = await fetch_html(url)
    except httpx.HTTPStatusError as e:
        logger.error("Upstream status %s for %s", e.response.status_code, url)
        raise HTTPException(status_code=502, detail=f"Upstream returned {e.response.status_code}")
    except Exception:
        logger.exception("Failed to fetch reader page")
        raise HTTPException(status_code=502, detail="Failed to fetch source")

    soup = try_soup(html)

    images = []

    # Strategy 1: dedicated reader containers
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

    # Strategy 2: generic article/figure imgs
    if not images:
        for img in soup.select("article img, .page img, .chapter img, img"):
            src = img.get("data-src") or img.get("data-lazy-src") or img.get("src")
            if src and not src.startswith("data:"):
                images.append(src)
        # may include other site images; we try to filter those that contain '/uploads' or '/images' heuristically later

    # Strategy 3: search scripts for arrays/JSON that list images
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

    # final cleanup & heuristics: make absolute & dedupe & filter
    clean = []
    seen = set()
    for src in images:
        if not src:
            continue
        src = src.strip()
        # sometimes relative
        if src.startswith("//"):
            src = "https:" + src
        if src.startswith("/"):
            src = urljoin(BASE, src)
        # filter obvious non-chapter images (logo etc) using heuristics - keep if contains 'manga', 'reader', '/uploads', '/covers', '/images', 'api.mangatek'
        ok = any(k in src for k in ["/reader/", "/manga/", "/uploads/", "/covers/", "api.mangatek", "/images/"])
        # if images list came from script it may be direct links - accept them
        if not ok and len(images) > 0 and any(src.startswith("http") for src in images):
            ok = True
        if not ok:
            # keep small chance: if filename ends with typical image ext and contains numbers (page)
            if re.search(r'\.(jpe?g|png|webp)(?:\?|$)', src) and re.search(r'\d', src):
                ok = True
        if not ok:
            # skip
            continue
        if src not in seen:
            seen.add(src)
            clean.append(src)

    return {"slug": slug, "chapter": chapter, "images": clean, "note": None if clean else "No images found; check if content is JS-rendered or blocked. See logs."}

# endpoint to accept full URL (encoded or not) and proxy to reader
@app.get("/reader/from-url")
async def reader_from_url(url: str):
    # decode
    try:
        decoded = unquote(url)
    except:
        decoded = url
    # quick domain check to avoid SSRF
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
        return await reader(slug, chapter)
    m2 = re.search(r"/manga/([^/]+)", decoded)
    if m2:
        slug = m2.group(1)
        # try to find final number
        mnum = re.search(r"/(\d+)(?:/?)$", decoded)
        if mnum:
            chapter = int(mnum.group(1))
            return await reader(slug, chapter)
    raise HTTPException(status_code=400, detail="Could not extract slug/chapter from URL")

@app.get("/_health")
def health():
    return {"ok": True}

