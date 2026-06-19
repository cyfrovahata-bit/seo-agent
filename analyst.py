"""
АГЕНТ 1: SEO-АНАЛІТИК (тільки читання)

Режими запуску:
  python analyst.py --mode daily   # щоденний звіт (порівнює день до дня)
  python analyst.py --mode weekly  # щотижневий звіт (порівнює тиждень до тижня)
"""

import argparse
import datetime
import os
import re
import json

import anthropic

from lib.google_seo import get_search_console_data, get_ga4_data
from lib.metrics import aggregate_site_totals, find_page_metrics, IMPACT_REVIEW_DAYS
from lib.state import load_json, save_json
from lib.telegram import send_message, send_recommendations_buttons
from lib.wordpress import WordPressClient

MODEL = "claude-sonnet-4-6"
MAX_HISTORY_ENTRIES = 90  # зберігати до 90 записів (~3 місяці щоденних)

SYSTEM_PROMPT = """\
Ти — постійний SEO-аналітик сайту cyfrovahata.com.ua — української компанії,
що займається розробкою сайтів, SEO-просуванням і технічною підтримкою сайтів.
Ти ведеш цей сайт від самого початку і пам'ятаєш всю історію: тренди за тижні
й місяці, а також які зміни вже були застосовані і чи вони подіяли.

ВАЖЛИВИЙ КОНТЕКСТ:
- Сайт запущено наприкінці квітня 2026 року — йому ~2 місяці. Це дуже молодий сайт.
- Сайт орієнтований на ВСЮ УКРАЇНУ — не на конкретні міста.
- Google ще активно індексує і "вивчає" його — різкі коливання позицій і трафіку є нормою.
- Для сайту такого віку позиції 20-100 і низький CTR — очікуваний початковий стан, не проблема.
- Не роби висновки типу "сайт має проблеми" через низькі абсолютні показники.
- Фокусуйся на тому, що зростає чи падає відносно попередніх даних, а не на порівнянні з "нормою" для зрілого сайту.
- Пріоритет рекомендацій: технічне SEO і контент важливіші за тонкі оптимізації title/meta на цьому етапі.

ПРАВИЛА:
- Дивись на ДОВГОСТРОКОВІ тренди (весь наданий ряд), а не лише на останній день/тиждень окремо.
- Якщо в даних є розділ "ОЦІНКА ЕФЕКТУ ЗМІН" — обов'язково прокоментуй КОЖНУ зміну: допомогло чи ні, конкретні цифри.
- Якщо нових суттєвих змін немає — прямо скажи що все стабільно, не вигадуй штучні рекомендації.
- НІКОЛИ не пропонуй окремі сторінки під конкретні міста чи регіони України (Ужгород, Львів, Київ тощо) — сайт охоплює всю Україну.
- НІКОЛИ не пропонуй "переписати сторінку" без конкретної причини — рекомендації завжди точкові.
- Не повторюй рекомендації, які вже є у відкритому бэклозі.
- ОБОВ'ЯЗКОВО перевіряй розділ "РЕАЛЬНИЙ ВМІСТ СТОРІНОК" перед будь-якою контентною рекомендацією. Якщо елемент вже є — НЕ пропонуй його.
- Сторінки із розділу "⛔ НЕ ПРОПОНУЙ нових змін" — повністю пропускай.
- Пиши українською, по-діловому, без зайвої води.

ФОРМАТ ВІДПОВІДІ (рівно два блоки):

1) Текст звіту для Telegram (до 1500 символів, без markdown-заголовків,
   емодзі для структури — ок).

2) Рядок "---JSON---" і далі JSON-масив НОВИХ рекомендацій:
[{"title": "...", "description": "...", "type": "content|technical|onpage",
  "priority": "high|medium|low", "action": "edit_existing|create_new",
  "target_page_path": "/шлях/ або null"}]
   "action": "edit_existing" — обов'язково з "target_page_path".
   "create_new" — для сторінки загальноукраїнської тематики (НЕ міської/регіональної).
   Порожній масив [], якщо нових пропозицій немає.
"""


def build_trend_table(history: list[dict], mode: str) -> str:
    relevant = [e for e in history if e.get("mode") == mode]
    if not relevant:
        return "Історії ще немає — це перший запуск."
    lines = ["дата | кліки | покази | сесії | користувачі"]
    for entry in relevant[-14:]:
        t = entry["site_totals"]
        lines.append(
            f"{entry['date']} | {t['clicks']} | {t['impressions']} | {t['sessions']} | {t['users']}"
        )
    return "\n".join(lines)


def build_comparison(history: list[dict], mode: str, current_totals: dict) -> str:
    relevant = [e for e in history if e.get("mode") == mode]
    if not relevant:
        return ""
    prev = relevant[-1]["site_totals"]
    lines = ["ПОРІВНЯННЯ З ПОПЕРЕДНІМ ПЕРІОДОМ:"]
    for key in ("clicks", "impressions", "sessions", "users"):
        diff = current_totals.get(key, 0) - prev.get(key, 0)
        sign = "+" if diff >= 0 else ""
        lines.append(f"  {key}: {prev.get(key, 0)} → {current_totals.get(key, 0)} ({sign}{diff})")
    return "\n".join(lines)


def build_page_snapshots(wp: WordPressClient) -> dict:
    from bs4 import BeautifulSoup
    snapshots = {}
    for post_type in ("pages", "posts"):
        items = wp._get(post_type, {"per_page": 100, "status": "publish"})
        for item in items:
            link = item.get("link", "")
            base = wp.base_url.rstrip("/")
            path = link.replace(base, "") or "/"
            raw_html = item["content"].get("rendered", "")
            soup = BeautifulSoup(raw_html, "html.parser")
            text = " ".join(soup.get_text(" ", strip=True).split())[:2000]
            seo_tags = wp._fetch_seo_tags(link)
            snapshots[path] = {
                "title": item["title"].get("rendered", ""),
                "seo_title": seo_tags["seo_title"],
                "meta_description": seo_tags["meta_description"],
                "text_content": text,
            }
    return snapshots


def build_frozen_pages(backlog: list[dict], today: datetime.date) -> dict:
    frozen = {}
    for rec in backlog:
        if rec.get("status") == "published" and not rec.get("impact_checked"):
            published_date = datetime.date.fromisoformat(rec["published_date"])
            days_left = IMPACT_REVIEW_DAYS - (today - published_date).days
            if days_left > 0:
                page = rec.get("target_page_path") or "/"
                if page not in frozen:
                    frozen[page] = []
                frozen[page].append({"title": rec["title"], "days_left": days_left})
    return frozen


def build_impact_reviews(backlog, gsc_data, ga4_data, today):
    reviews = []
    for rec in backlog:
        if rec.get("status") == "published" and not rec.get("impact_checked"):
            published_date = datetime.date.fromisoformat(rec["published_date"])
            if (today - published_date).days >= IMPACT_REVIEW_DAYS:
                current = find_page_metrics(rec.get("target_page_path"), gsc_data, ga4_data)
                reviews.append({
                    "id": rec["id"],
                    "title": rec["title"],
                    "page": rec.get("target_page_path"),
                    "days_since_published": (today - published_date).days,
                    "metrics_before": rec.get("baseline_metrics"),
                    "metrics_now": current,
                })
    return reviews


def parse_claude_response(text: str):
    if "---JSON---" not in text:
        return text.strip(), []
    report_text, json_part = text.split("---JSON---", 1)
    json_part = re.sub(r"^```(json)?|```$", "", json_part.strip(), flags=re.MULTILINE).strip()
    try:
        recommendations = json.loads(json_part)
    except Exception:
        recommendations = []
    return report_text.strip(), recommendations


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["daily", "weekly"], default="daily")
    args = parser.parse_args()
    mode = args.mode

    today = datetime.date.today()
    if mode == "daily":
        days = 1
        period_label = "щоденний"
        emoji = "📅"
    else:
        days = 7
        period_label = "щотижневий"
        emoji = "📊"

    start_date = (today - datetime.timedelta(days=days)).isoformat()
    end_date = today.isoformat()

    gsc_data = get_search_console_data(
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], os.environ["GSC_SITE_URL"],
        start_date, end_date, row_limit=200,
    )
    ga4_data = get_ga4_data(
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], os.environ["GA4_PROPERTY_ID"],
        start_date, end_date,
    )

    history = load_json("metrics_history.json", default=[])
    backlog = load_json("recommendations.json", default=[])

    current_totals = aggregate_site_totals(gsc_data, ga4_data)
    comparison_text = build_comparison(history, mode, current_totals)
    trend_table = build_trend_table(history, mode)

    wp = WordPressClient(os.environ["WP_BASE_URL"], os.environ["WP_USERNAME"], os.environ["WP_APP_PASSWORD"])
    page_snapshots = build_page_snapshots(wp)
    frozen_pages = build_frozen_pages(backlog, today)
    impact_reviews = build_impact_reviews(backlog, gsc_data, ga4_data, today)
    open_backlog = [r for r in backlog if r["status"] == "pending"]

    frozen_text = ""
    if frozen_pages:
        lines = ["⛔ НЕ ПРОПОНУЙ нових змін для цих сторінок — зміни вже внесені, чекаємо результату:"]
        for page, changes in frozen_pages.items():
            for ch in changes:
                lines.append(f"  {page} → «{ch['title']}» (ще {ch['days_left']} днів до оцінки)")
        frozen_text = "\n".join(lines)

    snapshots_text = ""
    if page_snapshots:
        lines = ["РЕАЛЬНИЙ ВМІСТ СТОРІНОК (що вже є на сайті — НЕ пропонуй те, що вже присутнє):"]
        for path, snap in page_snapshots.items():
            lines.append(f"\n--- {path} ---")
            lines.append(f"Title: {snap['title']}")
            lines.append(f"SEO title: {snap['seo_title']}")
            lines.append(f"Meta description: {snap['meta_description']}")
            lines.append(f"Текст: {snap['text_content'][:1000]}")
        snapshots_text = "\n".join(lines)

    report_type_hint = (
        "Це ЩОДЕННИЙ звіт — коротко, тільки суттєві зміни за день порівняно з вчорашнім."
        if mode == "daily"
        else "Це ЩОТИЖНЕВИЙ звіт — детальніший аналіз тренду, рекомендації якщо є підстави."
    )

    user_message = f"""
{report_type_hint}
Режим: {period_label} | Період: {start_date} – {end_date}

{comparison_text}

ДОВГОСТРОКОВИЙ ТРЕНД ({period_label}):
{trend_table}

ДАНІ ПОШУКУ — Search Console (запит, сторінка, кліки, покази, CTR, позиція):
{gsc_data}

ДАНІ ВІДВІДУВАНОСТІ — Google Analytics (сторінка, сесії, користувачі, відмови, тривалість):
{ga4_data}

{snapshots_text}

{frozen_text}

ВІДКРИТИЙ БЭКЛОГ (вже запропоновані, ще НЕ виконані рекомендації — не дублюй):
{open_backlog if open_backlog else "Порожньо."}

ОЦІНКА ЕФЕКТУ ЗМІН (раніше застосовані правки, час підбити підсумок):
{impact_reviews if impact_reviews else "Немає правок, готових до оцінки ефекту."}
"""

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=MODEL, max_tokens=2000, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    full_text = "".join(b.text for b in response.content if b.type == "text")
    report_text, new_recommendations = parse_claude_response(full_text)

    reviewed_ids = {r["id"] for r in impact_reviews}
    for rec in backlog:
        if rec["id"] in reviewed_ids:
            rec["impact_checked"] = True

    next_id = (max((r["id"] for r in backlog), default=0)) + 1
    for rec in new_recommendations:
        rec["id"] = next_id
        rec["status"] = "pending"
        rec["created"] = today.isoformat()
        backlog.append(rec)
        next_id += 1
    save_json("recommendations.json", backlog)

    history.append({
        "date": today.isoformat(),
        "mode": mode,
        "site_totals": current_totals,
        "top_queries": sorted(gsc_data, key=lambda x: -x["clicks"])[:15],
        "top_pages": sorted(ga4_data, key=lambda x: -x["sessions"])[:15],
    })
    save_json("metrics_history.json", history[-MAX_HISTORY_ENTRIES:])

    pending = [r for r in backlog if r["status"] == "pending"]

    send_message(
        os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"],
        f"{emoji} SEO {period_label} звіт | {start_date} – {end_date}\n\n{report_text}",
    )

    if pending:
        send_recommendations_buttons(
            os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"], pending
        )


if __name__ == "__main__":
    main()
