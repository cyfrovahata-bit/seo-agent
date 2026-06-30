"""
АГЕНТ 3: ЩОМІСЯЧНИЙ СТРАТЕГІЧНИЙ ЗВІТ

На відміну від тижневого тактичного звіту (analyst.py), цей запускається
раз на місяць і робить глибший, стратегічний синтез:
- Повна історія всіх тижневих метрик від самого старту (не тільки 12 тижнів)
- Технічний SEO-аудит (PageSpeed + перевірка title/meta/H1/canonical)
- Безкоштовний проксі для лідів/конверсій (підрахунок подій GA4)
- Підсумок змін, застосованих за останній місяць, і їх ефект (де відомо)

Використовує Claude Opus — найсильнішу модель — саме для цього глибокого
синтезу. Тижневі тактичні звіти лишаються на швидшій/дешевшій Sonnet —
це і відповідає тому, як працює реальний SEO-консультант: оперативка
щотижня, стратегія раз на місяць з повним зануренням.
"""

import datetime
import os

import anthropic

from lib.google_seo import get_search_console_data, get_ga4_data, get_ga4_events, get_ga4_page_conversions, get_ga4_traffic_channels
from lib.technical_seo import run_technical_audit
from lib.wordpress import WordPressClient
from lib.state import load_json
from lib.telegram import send_message
from lib.explain_why import build_explain_why_full
from lib.decision_engine import build_decision_plan
from lib.metrics import aggregate_site_totals

MODEL = "claude-opus-4-7"

SYSTEM_PROMPT = """\
Ти — провідний (senior) SEO-консультант, що веде сайт cyfrovahata.com.ua —
української компанії з розробки сайтів, SEO-просування і технічної підтримки
сайтів. Це твій щомісячний стратегічний звіт власнику бізнесу — пиши як
консультант, якому платять за глибину думки, а не за обсяг тексту.

Тобі надається:
- Повна історія тижневих метрик від самого старту проєкту
- Технічний SEO-аудит сайту (Lighthouse/PageSpeed + перевірка сторінок)
- Дані про події в GA4 (безкоштовний проксі для лідів — офіційних "Key events"
  ще не налаштовано, тож сам визнач зі списку подій, що виглядає як лід:
  форма, дзвінок, заявка тощо)
- Журнал змін, застосованих за останній місяць, і відомий ефект

ЗАВДАННЯ:
1. Дай стратегічну оцінку напрямку руху сайту за місяць — трафік, видимість
   у пошуку, технічний стан, ймовірні ліди.
2. Виділи 2-4 НАЙВАЖЛИВІШІ пріоритети на наступний місяць — не список з
   20 дрібниць, а те, що реально матиме найбільший вплив (думай в категоріях
   "вплив відносно зусиль").
3. Прокоментуй технічні проблеми з аудиту, якщо є, і чи варто реагувати
   зараз, чи це може почекати.
4. Якщо дані про ліди виглядають слабко відстеженими — порадь одною фразою
   налаштувати в GA4 "Key events" для відповідних подій (це ручна дія в
   інтерфейсі GA4, кілька кліків, коду не потребує).
5. Пиши українською, як консультант клієнту — стратегічно, орієнтуючись на
   бізнес-результат, без зайвої води навколо сирих цифр.

Формат — звичайний текст для Telegram (без markdown-заголовків), до 3500 символів.
"""


def main():
    today = datetime.date.today()
    month_start = (today - datetime.timedelta(days=30)).isoformat()
    today_str = today.isoformat()

    gsc_data = get_search_console_data(
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], os.environ["GSC_SITE_URL"],
        month_start, today_str, row_limit=300,
    )
    ga4_data = get_ga4_data(
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], os.environ["GA4_PROPERTY_ID"],
        month_start, today_str,
    )
    ga4_events = get_ga4_events(
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], os.environ["GA4_PROPERTY_ID"],
        month_start, today_str,
    )
    try:
        page_conversions = get_ga4_page_conversions(
            os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], os.environ["GA4_PROPERTY_ID"],
            month_start, today_str,
        )
    except Exception:
        page_conversions = {}
    try:
        traffic_channels = get_ga4_traffic_channels(
            os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], os.environ["GA4_PROPERTY_ID"],
            month_start, today_str,
        )
    except Exception:
        traffic_channels = {}

    wp = WordPressClient(
        os.environ["WP_BASE_URL"], os.environ["WP_USERNAME"], os.environ["WP_APP_PASSWORD"],
    )
    technical_audit = run_technical_audit(
        wp, os.environ["WP_BASE_URL"], os.environ.get("PAGESPEED_API_KEY"),
    )

    full_history = load_json("metrics_history.json", default=[])
    backlog = load_json("recommendations.json", default=[])
    recent_changes = [
        r for r in backlog
        if r.get("published_date")
        and datetime.date.fromisoformat(r["published_date"]) >= today - datetime.timedelta(days=30)
    ]

    keyword_history = load_json("keyword_history.json", default={})
    current_totals = aggregate_site_totals(gsc_data, ga4_data)

    learning_log = load_json("learning_log.json", default=[])

    explain_why_section, explain_result = build_explain_why_full(
        current_totals=current_totals,
        current_gsc=gsc_data,
        current_ga4=ga4_data,
        history=full_history,
        keyword_history=keyword_history,
        backlog=backlog,
        ga4_events=ga4_events,
        technical_data=technical_audit,
        mode="monthly",
        today=today,
        learning_log=learning_log,
        page_conversions=page_conversions,
        traffic_channels=traffic_channels,
    )
    decision_section = build_decision_plan(
        explain_result=explain_result,
        backlog=backlog,
        learning_log=learning_log,
        today=today,
        mode="monthly",
    )

    user_message = f"""
ПОВНА ІСТОРІЯ ТИЖНЕВИХ МЕТРИК ВІД СТАРТУ ПРОЄКТУ:
{full_history}

ДАНІ ЗА ОСТАННІ 30 ДНІВ — Search Console:
{gsc_data}

ДАНІ ЗА ОСТАННІ 30 ДНІВ — Google Analytics (трафік по сторінках):
{ga4_data}

ПОДІЇ В GA4 ЗА ОСТАННІ 30 ДНІВ (проксі для лідів/конверсій):
{ga4_events}

{explain_why_section}

{decision_section}

ТЕХНІЧНИЙ SEO-АУДИТ:
{technical_audit}

ЗМІНИ, ЗАСТОСОВАНІ ЗА ОСТАННІЙ МІСЯЦЬ (і відомий ефект, де є):
{recent_changes if recent_changes else "За цей місяць підтверджених публікацій немає."}
"""

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=MODEL, max_tokens=3000, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    report_text = "".join(b.text for b in response.content if b.type == "text").strip()

    send_message(
        os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"],
        f"📈 ЩОМІСЯЧНИЙ СТРАТЕГІЧНИЙ ЗВІТ — {today_str}\n\n{report_text}",
    )


if __name__ == "__main__":
    main()
