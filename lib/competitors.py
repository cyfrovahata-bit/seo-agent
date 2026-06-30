"""
Моніторинг конкурентів — аналіз контенту конкурентних сайтів через їх sitemap
і реальний fetch сторінок (H1, H2, meta description, текст).
Запускається раз на тиждень.
"""

import requests
from collections import Counter
from xml.etree import ElementTree
from bs4 import BeautifulSoup


COMPETITOR_DOMAINS = [
    "https://artjoker.ua",
    "https://webstudio2u.net",
    "https://seotm.ua",
    "https://impulse.guru",
    "https://agem.com.ua",
]

SITEMAP_PATHS = ["/sitemap.xml", "/sitemap_index.xml", "/sitemap-posts.xml"]
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SEO-Agent/1.0)"}


def _fetch_sitemap_urls(domain: str) -> list[str]:
    urls = []
    for path in SITEMAP_PATHS:
        try:
            resp = requests.get(domain.rstrip("/") + path, timeout=15, headers=HEADERS)
            if resp.status_code != 200:
                continue
            root = ElementTree.fromstring(resp.content)
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            for sitemap_tag in root.findall("sm:sitemap/sm:loc", ns):
                try:
                    sub = requests.get(sitemap_tag.text.strip(), timeout=15, headers=HEADERS)
                    sub_root = ElementTree.fromstring(sub.content)
                    for loc in sub_root.findall("sm:url/sm:loc", ns):
                        urls.append(loc.text.strip())
                    if urls:
                        break
                except Exception:
                    continue
            for loc in root.findall("sm:url/sm:loc", ns):
                urls.append(loc.text.strip())
            if urls:
                break
        except Exception:
            continue
    return urls[:300]


def _fetch_page_content(url: str) -> dict:
    """Завантажує сторінку і витягує SEO-елементи."""
    try:
        resp = requests.get(url, timeout=15, headers=HEADERS)
        if resp.status_code != 200:
            return {}
        soup = BeautifulSoup(resp.text, "html.parser")

        title = soup.title.string.strip() if soup.title and soup.title.string else ""
        meta_desc = ""
        meta_tag = soup.find("meta", attrs={"name": "description"})
        if meta_tag:
            meta_desc = meta_tag.get("content", "").strip()

        h1 = soup.find("h1")
        h1_text = h1.get_text(strip=True) if h1 else ""

        h2_tags = soup.find_all("h2")
        h2_texts = [h.get_text(strip=True) for h in h2_tags[:6]]

        # Перший абзац тексту
        paragraphs = soup.find_all("p")
        intro = ""
        for p in paragraphs:
            text = p.get_text(strip=True)
            if len(text) > 80:
                intro = text[:300]
                break

        return {
            "url": url,
            "title": title[:100],
            "meta_description": meta_desc[:200],
            "h1": h1_text[:150],
            "h2s": h2_texts,
            "intro": intro,
        }
    except Exception:
        return {}


def _extract_slug_keywords(url: str) -> str:
    path = url.split("//", 1)[-1].split("/", 1)[-1].rstrip("/")
    return path.replace("-", " ").replace("_", " ").replace("/", " › ")


def analyze_competitors(our_pages: list[str]) -> dict:
    """
    1. Порівнює теми сторінок конкурентів з нашими (через sitemap)
    2. Для топ-5 сервісних сторінок кожного конкурента — завантажує реальний контент
    """
    our_slugs = set()
    for p in our_pages:
        slug = p.strip("/").split("/")[-1].replace("-", " ").lower()
        our_slugs.add(slug)

    results = []
    all_competitor_topics = []
    deep_content = []  # реальний контент сторінок

    for domain in COMPETITOR_DOMAINS:
        urls = _fetch_sitemap_urls(domain)
        blog_urls = [u for u in urls if any(
            seg in u for seg in ["/blog/", "/statti/", "/news/", "/uk/", "/post/"]
        )]
        service_urls = [u for u in urls if not any(
            seg in u for seg in ["/blog/", "/statti/", "/news/", "/uk/", "/post/", "?", "#"]
        ) and u.rstrip("/") != domain.rstrip("/")]

        # Завантажуємо контент топ-3 сервісних сторінок і топ-2 статей
        pages_to_fetch = service_urls[:3] + blog_urls[:2]
        fetched = []
        for url in pages_to_fetch:
            content = _fetch_page_content(url)
            if content:
                fetched.append(content)

        if fetched:
            deep_content.append({
                "domain": domain,
                "pages": fetched,
            })

        results.append({
            "domain": domain,
            "total_pages": len(urls),
            "blog_posts": len(blog_urls),
            "service_pages": len(service_urls),
            "sample_topics": [_extract_slug_keywords(u) for u in (blog_urls + service_urls)[:20]],
        })

        for u in blog_urls[:50]:
            slug = u.strip("/").split("/")[-1].replace("-", " ").lower()
            all_competitor_topics.append({"domain": domain, "slug": slug, "url": u})

    slug_count: Counter = Counter()
    slug_to_url: dict = {}
    for t in all_competitor_topics:
        slug_count[t["slug"]] += 1
        slug_to_url[t["slug"]] = t["url"]

    gaps = [
        {"topic": slug, "competitor_count": cnt, "example_url": slug_to_url[slug]}
        for slug, cnt in slug_count.most_common(20)
        if cnt >= 2 and not any(word in our_slugs for word in slug.split()[:2])
    ]

    return {
        "competitors": results,
        "content_gaps": gaps[:10],
        "deep_content": deep_content,  # реальний контент для аналізу Claude
    }
