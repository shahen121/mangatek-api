# app.py - resilient Mangatek scraper with Playwright fallback
return await fetch_page_playwright(url, timeout=timeout)
except Exception as e:
last_exc = e
logger.warning("playwright failed for %s -> %s", url, repr(e))


# 3) cloudscraper
try:
html = await asyncio.to_thread(fetch_page_cloudscraper_sync, url, timeout)
return html
except Exception as e:
last_exc = e
logger.warning("cloudscraper failed for %s -> %s", url, repr(e))


logger.error("Both httpx and cloudscraper failed. last=%s", str(last_exc))
raise HTTPException(status_code=502, detail=f"Both httpx and cloudscraper failed. last={str(last_exc)}")




# -------- cache helper for async
async def cached_fetch(key: str, coro_func, *args, **kwargs):
if key in CACHE:
logger.debug("cache hit: %s", key)
return CACHE[key]
logger.debug("cache miss: %s", key)
result = await coro_func(*args, **kwargs)
CACHE[key] = result
return result




# ---------- endpoints ----------


@limiter.limit("30/minute")
@app.get("/manga-list")
async def manga_list(request: Request, sort: str = Query("views"), page: int = Query(1, ge=1)):
"""Returns list of manga cards. resilient selectors + caching."""
key = f"manga-list::{sort}::p{page}"
try:
html = await cached_fetch(key, fetch_html, f"{BASE}/manga-list?sort={sort}" + (f"&page={page}" if page > 1 else ""))
except HTTPException as e:
logger.exception("Failed fetching manga-list")
raise e


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
cover = img.get("src") if img else None
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
cover = img.get("data-src") or img.get("src") if img else None
items.append({"title": title.strip(), "slug": slug, "url": urljoin(BASE, href), "cover": cover})


pagination = {"current": page, "pages": []}
pager = soup.select_one("nav[aria-label='الصفحات']") or soup.select_one(".pagination") or soup.select_one(".pagenavi")
if pager:
for a in pager.select("a[href]"):
href = a.get("href")
text = a.get_text(strip=True)
pagination["pages"].append({"page": text, "url": urljoin(BASE, href)})


return {"it
