import asyncio
import httpx
from fastapi import FastAPI, Request, HTTPException
from bs4 import BeautifulSoup
from cachetools import TTLCache

app = FastAPI()

# Cache لمدة ساعة
cache = TTLCache(maxsize=1000, ttl=3600)

# HEADERS لتجاوز Cloudflare و 403
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://mangatek.com/",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

BASE_URL = "https://mangatek.com"


# --------------------------
# 🟦 Helper: Get Page (with retry)
# --------------------------
async def fetch_page(url: str):
    async with httpx.AsyncClient(headers=HEADERS, timeout=20) as client:
        try:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

        except httpx.HTTPStatusError:
            print("Retrying after 3 seconds…")
            await asyncio.sleep(3)
            response = await client.get(url)
            response.raise_for_status()
            return response.text


# --------------------------
# 🟩 Manga List Endpoint
# --------------------------
@app.get("/manga-list")
async def manga_list(request: Request, sort: str = "views", page: int = 1):

    cache_key = f"list-{sort}-{page}"
    if cache_key in cache:
        return cache[cache_key]

    url = f"{BASE_URL}/manga-list?sort={sort}&page={page}"

    html = await fetch_page(url)
    soup = BeautifulSoup(html, "lxml")

    items = []
    blocks = soup.select(".manga-slider-item, .manga-item, .item")

    for a in blocks:
        link = a.select_one("a")
        img = a.select_one("img")

        href = link.get("href") if link else None
        slug = href.replace("/manga/", "").strip("/") if href else None

        title = (
            (a.select_one(".title") and a.select_one(".title").get_text(strip=True))
            or (img and img.get("alt"))
            or (link and link.get("title"))
            or a.get_text(strip=True)
        )

        items.append({
            "title": title,
            "slug": slug,
            "url": BASE_URL + href if href else None
        })

    pagination = {
        "current": page,
        "pages": []
    }

    cache[cache_key] = {"items": items, "pagination": pagination}
    return cache[cache_key]


# --------------------------
# 🟩 Manga Info by Slug
# --------------------------
@app.get("/manga/{slug}")
async def manga_info(slug: str):

    cache_key = f"info-{slug}"
    if cache_key in cache:
        return cache[cache_key]

    url = f"{BASE_URL}/manga/{slug}"
    html = await fetch_page(url)

    soup = BeautifulSoup(html, "lxml")

    title = soup.select_one(".post-title h1")
    cover = soup.select_one(".summary_image img")

    chapters_list = []

    for li in soup.select(".wp-manga-chapter"):
        ch_link = li.select_one("a")
        if ch_link:
            chapters_list.append({
                "name": ch_link.get_text(strip=True),
                "url": ch_link.get("href")
            })

    result = {
        "title": title.get_text(strip=True) if title else slug,
        "cover": cover.get("src") if cover else None,
        "chapters": chapters_list
    }

    cache[cache_key] = result
    return result


# --------------------------
# 🟩 Reader (Read chapter pages)
# --------------------------
@app.get("/reader/{slug}/{chapter}")
async def manga_reader(slug: str, chapter: int):

    cache_key = f"reader-{slug}-{chapter}"
    if cache_key in cache:
        return cache[cache_key]

    url = f"{BASE_URL}/reader/{slug}/{chapter}"

    html = await fetch_page(url)
    soup = BeautifulSoup(html, "lxml")

    images = []

    for img in soup.select("img"):
        src = img.get("src") or img.get("data-src")
        if src and "manga" in src:
            images.append(src)

    result = {
        "slug": slug,
        "chapter": chapter,
        "images": images
    }

    cache[cache_key] = result
    return result


# --------------------------
# 🔵 Health check
# --------------------------
@app.get("/_health")
async def health():
    return {"status": "ok"}

