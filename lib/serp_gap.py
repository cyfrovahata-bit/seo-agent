"""
Пробиття в топ-10: для запитів, що вже мають покази на позиціях 11–50,
порівнює нашу сторінку з релевантними сторінками конкурентів
і генерує конкретний список правок «що на що поміняти».
Правки НЕ застосовуються автоматично — надсилаються в Telegram,
власник вносить їх вручну.
"""

from lib.competitors import (
    COMPETITOR_DOMAINS,
    _fetch_page_content,
    _fetch_sitemap_urls,
)

MAX_QUERIES_PER_RUN = 3        # скільки запитів розбираємо за один тижневий запуск
MIN_IMPRESSIONS = 5            # менше — ще зарано оптимізувати
POSITION_RANGE = (11, 50)      # «близько, але не в топ-10»
MAX_COMPETITOR_PAGES = 3       # скільки сторінок конкурентів порівнюємо на запит


def _pick_opportunity_queries(gsc_data: list[dict]) -> list[dict]:
    """Запити з найбільшим потенціалом: багато показів, позиція 11–50."""
    lo, hi = POSITION_RANGE
    candidates = [
        r for r in gsc_data
        if r.get("impressions", 0) >= MIN_IMPRESSIONS and lo <= r.get("position", 999) <= hi
    ]
    # Один запит може мати кілька сторінок — беремо рядок з найкращою позицією
    best_by_query: dict[str, dict] = {}
    for r in candidates:
        q = r["query"]
        if q not in best_by_query or r["position"] < best_by_query[q]["position"]:
            best_by_query[q] = r
    ranked = sorted(best_by_query.values(), key=lambda r: -r["impressions"])
    return ranked[:MAX_QUERIES_PER_RUN]


def _find_competitor_pages(query: str, sitemap_cache: dict) -> list[dict]:
    """Шукає у sitemap конкурентів сторінки, чиї slug перетинаються зі словами запиту."""
    words = {w for w in query.lower().split() if len(w) >= 4}
    if not words:
        return []
    # транслітеровані відповідники для найчастіших SEO-слів у слагах
    translit = {
        "просування": "prosuv", "сайту": "sait", "сайтів": "sait", "сайт": "sait",
        "розробка": "rozrobka", "створення": "stvorennia", "ціна": "cina",
        "вартість": "vartist", "інтернет": "internet", "магазин": "mahazyn",
    }
    slug_needles = {translit.get(w, w) for w in words}
    if "seo" in query.lower():
        slug_needles.add("seo")

    matches = []
    for domain in COMPETITOR_DOMAINS:
        if domain not in sitemap_cache:
            try:
                sitemap_cache[domain] = _fetch_sitemap_urls(domain)
            except Exception:
                sitemap_cache[domain] = []
        for url in sitemap_cache[domain]:
            slug = url.lower()
            hits = sum(1 for n in slug_needles if n in slug)
            if hits >= 1:
                matches.append({"url": url, "hits": hits})
    matches.sort(key=lambda m: -m["hits"])
    pages = []
    for m in matches:
        content = _fetch_page_content(m["url"])
        if content:
            pages.append(content)
        if len(pages) >= MAX_COMPETITOR_PAGES:
            break
    return pages


SERP_GAP_PROMPT = """Ти — SEO-редактор. Наша сторінка ранжується за запитом «{query}» на позиції {position} ({impressions} показів/тиждень, 0 кліків). Мета — пробитися в топ-10.

НАША СТОРІНКА:
{our_page}

СТОРІНКИ КОНКУРЕНТІВ за схожою темою:
{competitor_pages}

Порівняй і дай КОНКРЕТНИЙ список правок для власника сайту у форматі «що → на що поміняти». Правила:
- Кожна правка: елемент (Title / H1 / H2 / мета-опис / новий розділ), поточний текст (якщо є) і точний новий текст, який треба вставити.
- Максимум 5 правок, від найважливішої.
- Нові тексти пиши українською, природно, без перенасичення ключами.
- Якщо бракує цілого розділу (наприклад FAQ, таблиця цін, приклади робіт) — дай готовий заголовок розділу і 2-3 речення тез, що в ньому має бути.
- Формат — простий текст для Telegram, без markdown-таблиць. Починай одразу зі списку правок."""


def build_serp_gap_plan(gsc_data: list[dict], client, model: str) -> str:
    """Повертає текст плану пробиття в топ-10 для Telegram (порожній рядок якщо нема кандидатів)."""
    opportunities = _pick_opportunity_queries(gsc_data)
    if not opportunities:
        return ""

    sitemap_cache: dict = {}
    sections = []
    for opp in opportunities:
        query = opp["query"]
        our_url = opp.get("page", "")
        our_page = _fetch_page_content(our_url) if our_url else {}
        competitor_pages = _find_competitor_pages(query, sitemap_cache)
        if not our_page:
            continue

        prompt = SERP_GAP_PROMPT.format(
            query=query,
            position=round(opp["position"]),
            impressions=opp["impressions"],
            our_page=our_page,
            competitor_pages=competitor_pages if competitor_pages else "не знайдено — орієнтуйся на загальні вимоги топ-10 для цього запиту",
        )
        try:
            response = client.messages.create(
                model=model, max_tokens=1200,
                messages=[{"role": "user", "content": prompt}],
            )
            plan = response.content[0].text.strip()
        except Exception as e:
            plan = f"(не вдалось згенерувати план: {e})"

        sections.append(
            f"🎯 «{query}» — позиція {round(opp['position'])}, {opp['impressions']} показів\n"
            f"Сторінка: {our_url}\n\n{plan}"
        )

    if not sections:
        return ""
    return (
        "🚀 ПЛАН ПРОБИТТЯ В ТОП-10\n"
        "Правки внось вручну — нижче конкретно що і на що поміняти.\n\n"
        + "\n\n————————————\n\n".join(sections)
    )
