"""
Моніторинг конкурентів — аналіз контенту конкурентних сайтів через їх sitemap.
Запускається раз на тиждень разом з тижневим звітом.
"""

import requests
from xml.etree import ElementTree


COMPETITOR_DOMAINS = [
    "https://artjoker.ua",
    "https://webstudio2u.net",
    "https://seotm.ua",
    "https://impulse.guru",
    "https://agem.com.ua",
]

SITEMAP_PATHS = ["/sitemap.xml", "/sitemap_index.xml", "/sitemap-posts.xml"]


def _fetch_sitemap_urls(domain: str) -> list[str]:
    urls = []
    for path in SITEMAP_PATHS:
        try:
            resp = requests.get(domain.rstrip("/") + path, timeout=15,
                                headers={"User-Agent": "Mozilla/5.0 (SEO-Agent)"})
            if resp.status_code != 200:
                continue
            root = ElementTree.fromstring(resp.content)
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            # sitemap index — рекурсивно беремо перший дочірній sitemap
            for sitemap_tag in root.findall("sm:sitemap/sm:loc", ns):
                try:
                    sub = requests.get(sitemap_tag.text.strip(), timeout=15,
                                       headers={"User-Agent": "Mozilla/5.0 (SEO-Agent)"})
                    sub_root = ElementTree.fromstring(sub.content)
                    for loc in sub_root.findall("sm:url/sm:loc", ns):
                        urls.append(loc.text.strip())
                    if urls:
                        break
                except Exception:
                    continue
            # звичайний sitemap
            for loc in root.findall("sm:url/sm:loc", ns):
                urls.append(loc.text.strip())
            if urls:
                break
        except Exception:
            continue
    return urls[:300]


def _extract_slug_keywords(url: str) -> str:
    """Витягує слова зі slug URL для розуміння теми сторінки."""
    path = url.split("//", 1)[-1].split("/", 1)[-1].rstrip("/")
    return path.replace("-", " ").replace("_", " ").replace("/", " › ")


def analyze_competitors(our_pages: list[str]) -> dict:
    """
    Порівнює теми сторінок конкурентів з нашими.
    Повертає: список конкурентів з кількістю сторінок і список тем яких у нас немає.
    """
    our_slugs = set()
    for p in our_pages:
        slug = p.strip("/").split("/")[-1].replace("-", " ").lower()
        our_slugs.add(slug)

    results = []
    all_competitor_topics = []

    for domain in COMPETITOR_DOMAINS:
        urls = _fetch_sitemap_urls(domain)
        blog_urls = [u for u in urls if any(
            seg in u for seg in ["/blog/", "/statti/", "/news/", "/uk/", "/post/"]
        )]
        service_urls = [u for u in urls if not any(
            seg in u for seg in ["/blog/", "/statti/", "/news/", "/uk/", "/post/", "?", "#"]
        ) and u.rstrip("/") != domain.rstrip("/")]

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

    # теми які є у 2+ конкурентів але нема у нас
    from collections import Counter
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
    }
