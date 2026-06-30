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

from lib.google_seo import get_search_console_data, get_ga4_data, get_ga4_events
from lib.metrics import aggregate_site_totals, find_page_metrics, IMPACT_REVIEW_DAYS
from lib.explain_why import build_explain_why, record_outcome
from lib.state import load_json, save_json
from lib.telegram import send_message, send_recommendations_buttons
from lib.wordpress import WordPressClient
from lib.competitors import analyze_competitors
from lib.technical_seo import run_technical_audit
from lib.backlinks import get_backlink_report
from lib.target_keywords import build_target_keyword_report, build_cluster_summary

MODEL = "claude-sonnet-4-6"
MAX_HISTORY_ENTRIES = 90  # зберігати до 90 записів (~3 місяці щоденних)

SYSTEM_PROMPT = """\
Ти — SEO-помічник для власника малого бізнесу. Твій читач — не технічний спеціаліст,
він займається своєю справою і хоче розуміти що відбувається з сайтом простими словами.

КОНТЕКСТ САЙТУ:
- cyfrovahata.com.ua — українська компанія, розробка сайтів і SEO-просування
- Сайт запущено наприкінці квітня 2026 року — йому ~2 місяці, це дуже молодий сайт
- Сайт орієнтований на всю Україну, не на конкретні міста
- Для такого віку позиції 20-100 і малий трафік — абсолютно нормально

ПРАВИЛА ЗМІСТУ:
- Якщо нових суттєвих змін немає — так і пиши, не вигадуй проблем
- Дивись на тренд за весь наданий період, а не тільки на останній день
- Якщо є розділ "ОЦІНКА ЕФЕКТУ ЗМІН" — обов'язково прокоментуй кожну зміну: допомогло чи ні
- Сторінки із розділу "⛔ НЕ ПРОПОНУЙ нових змін" — повністю пропускай
- ОБОВ'ЯЗКОВО перевіряй "РЕАЛЬНИЙ ВМІСТ СТОРІНОК" — не пропонуй те, що вже є
- Не повторюй рекомендації з відкритого бэклогу
- НІКОЛИ не пропонуй сторінки під конкретні міста (Ужгород, Львів тощо)
- Блог-статті: не більше 2 пропозицій на тиждень; якщо в бэклозі вже є 2+ — не пропонуй нових
- Якщо є "АНАЛІЗ КОНКУРЕНТІВ" — використовуй "прогалини в контенті" як ідеї для статей (не більше 1-2 на тиждень)
- Якщо є "ТЕХНІЧНИЙ АУДИТ" — обов'язково прокоментуй критичні проблеми (noindex, відсутній H1, битий title) простими словами
- Якщо є "МОЖЛИВОСТІ ДЛЯ ЗОВНІШНІХ ПОСИЛАНЬ" — запропонуй 1-2 конкретні майданчики як рекомендацію з type="technical", action="create_new", target_page_path=null; title має починатись з "Розмістити посилання на"
- Якщо є "МОЖЛИВИЙ ВІДКАТ ЗМІН" — обов'язково запропонуй рекомендацію повернути оригінал, поясни що зміна не спрацювала
- "ДИНАМІКА ПОЗИЦІЙ ЗАПИТІВ" — якщо якийсь запит різко впав (position_change > +5) — прокоментуй і запропонуй дії
- "ЦІЛЬОВІ КЛЮЧОВІ СЛОВА" — коментуй тільки ті де є дані або значне відставання; не перераховуй всі 30 запитів
- "СЕМАНТИЧНІ КЛАСТЕРИ" — вкажи який кластер найслабший і що можна зробити
- "Реальний контент конкурентів" — порівняй їх H1/H2 з нашими сторінками, знайди ідеї яких нам бракує
- Рекомендації завжди точкові: один заголовок, один блок, одна стаття — не "переписати сторінку"

ПОЯСНЕННЯ ПРИЧИН (обов'язково):
- Якщо є блок "🧠 АНАЛІЗ ПРИЧИН ЗМІН" — використовуй ці дані щоб пояснювати ЧОМУ щось змінилось
- Після кожної суттєвої зміни (впало / зросло) додавай блок: "🧠 Чому: [причина з даних]"
- НІКОЛИ не вигадуй причини — тільки ті, що підкріплені даними в секції "АНАЛІЗ ПРИЧИН ЗМІН"
- Рівень впевненості ★★★★★ = дуже ймовірно, ★★☆☆☆ = можливо але не точно
- Якщо причина невідома — так і пиши: "точна причина невідома, але..."

СТИЛЬ — людська мова, не технічна:
- Замість "CTR 2.3%" → "з кожних 100 людей які бачать сайт у пошуку, 2 заходять"
- Замість "bounce rate 100%" → "всі хто зайшов — одразу пішли, не погортали навіть"
- Замість "позиція 97.8" → "сайт з'являється десь на 10-й сторінці Google"
- Замість "impressions 17" → "Google показав сайт 17 разів у пошуку"
- Порівняння: "вчора було X, сьогодні Y — це добре/погано/нормально тому що..."
- Жодних технічних термінів: CTR, bounce rate, impressions, canonical, meta — не використовуй

ФОРМАТ ВІДПОВІДІ (рівно два блоки):

1) Текст звіту для Telegram — живою мовою, з емодзі. До 1800 символів.
   Обов'язкова структура:

   📅 SEO звіт | [дата або період]

   Що відбулось:
   [2-4 речення про зміни — що виросло, що впало, чому, що це означає]

   Які запити працюють:
   [перелік топ-запитів: 🔍 "запит" — позиція словами (1-ша сторінка / 5-та сторінка тощо), скільки разів показувався, чи клікали]

   Які сторінки відвідують:
   [перелік сторінок: 🏠/📄 назва — скільки людей, скільки часу провели, чи є проблеми]

   Загалом:
   [1-2 речення загальна оцінка — добре/погано/нормально для цього віку сайту]

2) Рядок "---JSON---" і далі JSON-масив НОВИХ рекомендацій.
   Кожна рекомендація — окрема точкова дія (один заголовок, один блок, одна стаття).
   title: коротко що зробити, людською мовою (напр. "Поміняти заголовок на сторінці SEO-просування")
   description: 1-2 речення чому це потрібно і що зміниться після — як пояснення другу, без жаргону.

[{"title": "...", "description": "...", "type": "content|technical|onpage",
  "priority": "high|medium|low", "action": "edit_existing|create_new",
  "target_page_path": "/шлях/ або null"}]
   "action": "edit_existing" — обов'язково з "target_page_path".
   "create_new" — тільки для загальноукраїнської тематики.
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
    try:
        ga4_events = get_ga4_events(
            os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], os.environ["GA4_PROPERTY_ID"],
            start_date, end_date,
        )
    except Exception:
        ga4_events = []

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

    competitor_data = None
    technical_data = None
    backlink_data = None
    if mode == "weekly":
        our_page_paths = list(page_snapshots.keys())
        try:
            competitor_data = analyze_competitors(our_page_paths)
        except Exception as e:
            competitor_data = {"error": str(e)}
        try:
            pagespeed_key = os.environ.get("PAGESPEED_API_KEY")
            technical_data = run_technical_audit(wp, os.environ["WP_BASE_URL"], pagespeed_key)
        except Exception as e:
            technical_data = {"error": str(e)}
        completed_backlinks = load_json("backlinks_done.json", default=[])
        backlink_data = get_backlink_report(completed_backlinks)

    # Трекінг позицій цільових запитів
    keyword_history = load_json("keyword_history.json", default={})
    if gsc_data:
        for row in gsc_data:
            query = row.get("query", "")
            if not query:
                continue
            if query not in keyword_history:
                keyword_history[query] = []
            keyword_history[query].append({
                "date": today.isoformat(),
                "position": round(row.get("position", 0), 1),
                "clicks": row.get("clicks", 0),
                "impressions": row.get("impressions", 0),
            })
            keyword_history[query] = keyword_history[query][-90:]
    save_json("keyword_history.json", keyword_history)

    # Топ запити з динамікою позиції (порівняння з попереднім тижнем)
    keyword_trends = []
    for query, entries in keyword_history.items():
        if len(entries) >= 2:
            prev_pos = entries[-2]["position"]
            curr_pos = entries[-1]["position"]
            diff = round(curr_pos - prev_pos, 1)
            keyword_trends.append({
                "query": query,
                "position_now": curr_pos,
                "position_change": diff,
                "clicks": entries[-1]["clicks"],
            })
    keyword_trends.sort(key=lambda x: x["clicks"], reverse=True)

    # Перевіряємо чи треба запропонувати revert для погіршених title-змін
    revert_candidates = []
    for rec in backlog:
        if (rec.get("status") == "published"
                and rec.get("impact_checked")
                and rec.get("action") == "edit_existing"
                and rec.get("original_title_backup")
                and not rec.get("revert_proposed")):
            baseline = rec.get("baseline_metrics", {})
            current = find_page_metrics(rec.get("target_page_path"), gsc_data, ga4_data)
            if (baseline.get("clicks", 0) > 0
                    and current.get("clicks", 0) < baseline.get("clicks", 0) * 0.7):
                revert_candidates.append({
                    "id": rec["id"],
                    "title": rec["title"],
                    "page": rec.get("target_page_path"),
                    "clicks_before": baseline.get("clicks"),
                    "clicks_now": current.get("clicks"),
                    "original_backup": rec["original_title_backup"],
                })

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

    revert_section = ""
    if revert_candidates:
        revert_section = (
            "\n⚠️ МОЖЛИВИЙ ВІДКАТ ЗМІН (кліки впали більш ніж на 30% після правки):\n"
            + str(revert_candidates)
            + "\nЯкщо це підтверджується — запропонуй рекомендацію з action=edit_existing щоб повернути оригінал."
        )

    technical_section = ""
    if technical_data:
        technical_section = "\nТЕХНІЧНИЙ АУДИТ САЙТУ:\n" + str(technical_data)

    competitor_section = ""
    if competitor_data:
        domains = [c["domain"] + " (" + str(c["total_pages"]) + " стор.)" for c in competitor_data.get("competitors", [])]
        deep = competitor_data.get("deep_content", [])
        deep_lines = []
        for comp in deep:
            deep_lines.append(f"\n  {comp['domain']}:")
            for page in comp["pages"]:
                deep_lines.append(f"    [{page.get('url','')}]")
                deep_lines.append(f"    H1: {page.get('h1','')}")
                deep_lines.append(f"    H2s: {'; '.join(page.get('h2s',[]))}")
                deep_lines.append(f"    Meta: {page.get('meta_description','')}")
        competitor_section = (
            "\nАНАЛІЗ КОНКУРЕНТІВ:\n"
            "Конкуренти: " + str(domains) + "\n"
            "Прогалини в контенті: " + str(competitor_data.get("content_gaps", [])) + "\n"
            "Реальний контент конкурентів (H1/H2/meta):" + "".join(deep_lines)
        )

    backlink_section = ""
    if backlink_data and backlink_data["total_remaining"] > 0:
        backlink_section = (
            "\nМОЖЛИВОСТІ ДЛЯ ЗОВНІШНІХ ПОСИЛАНЬ"
            " (зроблено " + str(backlink_data["total_done"])
            + ", залишилось " + str(backlink_data["total_remaining"]) + "):\n"
            + str(backlink_data["opportunities"][:3])
            + "\nЗапропонуй 1-2 конкретні майданчики з інструкцією як розмістити посилання."
        )

    learning_log = load_json("learning_log.json", default=[])

    explain_why_section = build_explain_why(
        current_totals=current_totals,
        current_gsc=gsc_data,
        current_ga4=ga4_data,
        history=history,
        keyword_history=keyword_history,
        backlog=backlog,
        ga4_events=ga4_events,
        technical_data=technical_data,
        mode=mode,
        today=today,
        learning_log=learning_log,
    )

    user_message = (
        f"{report_type_hint}\n"
        f"Режим: {period_label} | Період: {start_date} – {end_date}\n\n"
        f"{comparison_text}\n\n"
        f"ДОВГОСТРОКОВИЙ ТРЕНД ({period_label}):\n{trend_table}\n\n"
        f"ДАНІ ПОШУКУ — Search Console (запит, сторінка, кліки, покази, CTR, позиція):\n{gsc_data}\n\n"
        f"ДАНІ ВІДВІДУВАНОСТІ — Google Analytics (сторінка, сесії, користувачі, відмови, тривалість):\n{ga4_data}\n\n"
        f"ПОДІЇ GA4 (конверсії по сайту):\n{ga4_events}\n\n"
        f"{explain_why_section}\n\n"
        f"{snapshots_text}\n\n"
        f"{frozen_text}\n\n"
        f"ВІДКРИТИЙ БЭКЛОГ (вже запропоновані, ще НЕ виконані рекомендації — не дублюй):\n"
        f"{open_backlog if open_backlog else 'Порожньо.'}\n\n"
        f"ОЦІНКА ЕФЕКТУ ЗМІН (раніше застосовані правки, час підбити підсумок):\n"
        f"{impact_reviews if impact_reviews else 'Немає правок, готових до оцінки ефекту.'}\n\n"
        f"ДИНАМІКА ПОЗИЦІЙ ЗАПИТІВ (порівняно з попереднім звітом):\n"
        f"{keyword_trends[:20] if keyword_trends else 'Ще немає даних для порівняння.'}\n\n"
        f"{build_cluster_summary(gsc_data)}\n\n"
        f"{build_target_keyword_report(keyword_history)}"
        f"{revert_section}{technical_section}{competitor_section}{backlink_section}"
    )

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

    # Оновлюємо learning_log на основі impact reviews
    ACTION_TO_CAUSE = {
        "edit_existing": "edit_caused_position_drop",
        "create_new": "content_improvement_effect",
    }
    for review in impact_reviews:
        m_before = review.get("metrics_before") or {}
        m_now = review.get("metrics_now") or {}
        clicks_before = m_before.get("clicks", 0) or 0
        clicks_now = m_now.get("clicks", 0) or 0
        # Для нових сторінок (clicks_before==0): worked якщо є хоч 1 клік
        if clicks_before > 0:
            worked = clicks_now >= clicks_before * 1.1
        elif clicks_now > 0:
            worked = True
        else:
            worked = False
        rec_match = next((r for r in backlog if r["id"] == review["id"]), {})
        cause_type = ACTION_TO_CAUSE.get(rec_match.get("action", ""), "content_improvement_effect")
        learning_log = record_outcome(review["id"], cause_type, worked, learning_log, today)
    save_json("learning_log.json", learning_log[-500:])

    next_id = (max((r["id"] for r in backlog), default=0)) + 1
    for rec in new_recommendations:
        rec["id"] = next_id
        rec["status"] = "pending"
        rec["created"] = today.isoformat()
        backlog.append(rec)
        next_id += 1
    # Архівуємо старі виконані/відхилені рекомендації (тримаємо не більше 200)
    active = [r for r in backlog if r.get("status") in ("pending", "draft_ready", "revision_ready")]
    closed = [r for r in backlog if r.get("status") not in ("pending", "draft_ready", "revision_ready")]
    save_json("recommendations.json", active + closed[-150:])

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
