"""
Технічний SEO-аудит — безкоштовні джерела:
- PageSpeed Insights API (Lighthouse: performance/SEO/accessibility, Core Web Vitals)
- Власний легкий аудит сторінок: title, meta description, H1, canonical, noindex
Запускається раз на місяць (важче навантаження, ніж тижневий звіт).
"""

import datetime
import json
import os

import requests
from bs4 import BeautifulSoup

PAGESPEED_API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
MAX_PAGES_TO_CHECK = 30  # обмеження, щоб не перевантажувати GitHub Actions runner
PAGESPEED_CACHE_FILE = "data/pagespeed_cache.json"
PAGESPEED_CACHE_TTL_DAYS = 7


def _load_pagespeed_cache() -> dict:
    try:
        with open(PAGESPEED_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_pagespeed_cache(cache: dict) -> None:
    os.makedirs("data", exist_ok=True)
    with open(PAGESPEED_CACHE_FILE, "w") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def check_pagespeed(url: str, api_key: str, strategy: str = "mobile") -> dict:
    resp = requests.get(PAGESPEED_API, params={
        "url": url,
        "key": api_key,
        "strategy": strategy,
        "category": ["performance", "seo", "accessibility", "best-practices"],
    }, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    categories = data.get("lighthouseResult", {}).get("categories", {})
    audits = data.get("lighthouseResult", {}).get("audits", {})
    return {
        "url": url,
        "performance_score": round(categories.get("performance", {}).get("score", 0) * 100),
        "seo_score": round(categories.get("seo", {}).get("score", 0) * 100),
        "accessibility_score": round(categories.get("accessibility", {}).get("score", 0) * 100),
        "largest_contentful_paint": audits.get("largest-contentful-paint", {}).get("displayValue"),
        "cumulative_layout_shift": audits.get("cumulative-layout-shift", {}).get("displayValue"),
        "total_blocking_time": audits.get("total-blocking-time", {}).get("displayValue"),
    }


def check_page_seo(url: str) -> dict:
    """Легка перевірка on-page технічних факторів однієї сторінки."""
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (SEO-Agent-Audit)"})
    except requests.RequestException as e:
        return {"url": url, "issues": [f"Не вдалось завантажити: {e}"]}

    issues = []
    if resp.status_code != 200:
        issues.append(f"HTTP статус {resp.status_code}")

    soup = BeautifulSoup(resp.text, "html.parser")

    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    if not title:
        issues.append("Відсутній <title>")
    elif not (30 <= len(title) <= 65):
        issues.append(f"Довжина title {len(title)} символів (рекомендовано 30-65)")

    meta_desc = soup.find("meta", attrs={"name": "description"})
    desc_content = meta_desc.get("content", "").strip() if meta_desc else ""
    if not desc_content:
        issues.append("Відсутній meta description")
    elif len(desc_content) > 160:
        issues.append(f"Meta description {len(desc_content)} символів (більше 160)")

    h1_tags = soup.find_all("h1")
    if len(h1_tags) == 0:
        issues.append("Відсутній H1")
    elif len(h1_tags) > 1:
        issues.append(f"{len(h1_tags)} тегів H1 на сторінці (рекомендовано рівно 1)")

    if not soup.find("link", attrs={"rel": "canonical"}):
        issues.append("Відсутній canonical тег")

    robots_meta = soup.find("meta", attrs={"name": "robots"})
    if robots_meta and "noindex" in robots_meta.get("content", "").lower():
        issues.append("⚠️ Сторінка позначена noindex — не індексується Google")

    return {"url": url, "title": title, "issues": issues}


def check_robots_and_sitemap(base_url: str) -> dict:
    result = {}
    for path in ("/robots.txt", "/sitemap.xml", "/sitemap_index.xml"):
        try:
            resp = requests.get(base_url.rstrip("/") + path, timeout=10)
            result[path] = resp.status_code
        except requests.RequestException:
            result[path] = "помилка з'єднання"
    return result


def run_technical_audit(wp_client, base_url: str, pagespeed_api_key: str | None) -> dict:
    pages = wp_client.list_content("pages") + wp_client.list_content("posts")
    page_results = [check_page_seo(p["link"]) for p in pages[:MAX_PAGES_TO_CHECK]]

    pagespeed = None
    if pagespeed_api_key:
        cache = _load_pagespeed_cache()
        cache_key = base_url.rstrip("/")
        cached_entry = cache.get(cache_key, {})
        cached_date = cached_entry.get("cached_date", "")
        cache_age_days = (datetime.date.today() - datetime.date.fromisoformat(cached_date)).days if cached_date else 999
        if cache_age_days < PAGESPEED_CACHE_TTL_DAYS:
            pagespeed = cached_entry.get("data")
        else:
            try:
                pagespeed = check_pagespeed(base_url, pagespeed_api_key)
                cache[cache_key] = {"cached_date": datetime.date.today().isoformat(), "data": pagespeed}
                _save_pagespeed_cache(cache)
            except Exception as e:
                pagespeed = cached_entry.get("data") or {"error": str(e)}

    return {
        "robots_and_sitemap": check_robots_and_sitemap(base_url),
        "pagespeed_homepage": pagespeed,
        "pages_with_issues": [p for p in page_results if p.get("issues")],
        "pages_checked_total": len(page_results),
    }
