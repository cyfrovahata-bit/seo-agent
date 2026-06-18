"""
АГЕНТ 2: SEO-ВИКОНАВЕЦЬ (обмежений запис)

Запускається за розкладом (.github/workflows/seo-executor.yml), кожні кілька годин.
1. Читає нові команди з Telegram: "/do <id>" — виконати рекомендацію з бэклогу.
2. Якщо рекомендація — НОВИЙ контент (action=create_new): знаходить схожу
   існуючу сторінку як зразок стилю і створює новий запис як DRAFT
   (безпечно, бо живої версії ще немає).
3. Якщо рекомендація — правка ВЖЕ ОПУБЛІКОВАНОЇ сторінки (action=edit_existing):
   НЕ змінює статус і НЕ чіпає живий контент, а створює autosave-ревізію,
   яку людина сама підвантажує і підтверджує в редакторі WordPress.
4. У будь-якому випадку — жодна зміна не йде на сайт без ручного підтвердження.
"""

import datetime
import os
import re

import anthropic

from lib.google_seo import get_search_console_data, get_ga4_data
from lib.metrics import find_page_metrics, IMPACT_REVIEW_DAYS
from lib.state import load_json, save_json
from lib.telegram import get_updates, send_message
from lib.wordpress import WordPressClient

MODEL = "claude-sonnet-4-6"

PICK_REFERENCE_PROMPT = """\
Ти — SEO-агент, що готується створити НОВИЙ контент на сайті cyfrovahata.com.ua
за завданням нижче. Перед генерацією тобі потрібен зразок існуючого дизайну/стилю.

Завдання: {title}
Опис: {description}

Ось короткий список існуючих сторінок/постів сайту (id, заголовок, slug):
{content_list}

Поверни ОДНЕ слово чи коротку фразу — пошуковий запит, за яким можна знайти
найбільш схожу за тематикою/структурою сторінку для пошуку через WordPress search
(наприклад: "оренда ноутбук" або "блог техніка"). Нічого, крім цього запиту, не пиши.
"""

GENERATE_NEW_PROMPT = """\
Ти — SEO-копірайтер і верстальник сайту cyfrovahata.com.ua.

Завдання: {title}
Опис: {description}

Ось сирий HTML/Gutenberg-контент ІСНУЮЧОЇ схожої сторінки сайту — використай його
як еталон стилю, структури блоків і тону:

---ЗРАЗОК---
{reference_content}
---КІНЕЦЬ ЗРАЗКА---

Згенеруй новий контент для поставленого завдання у ТОЧНО такій же розмітці.
Не додавай пояснень — у відповіді має бути ЛИШЕ готова розмітка контенту.
Перший рядок: TITLE: <заголовок>, з другого рядка — сам контент.
"""

EDIT_EXISTING_PROMPT = """\
Ти — SEO-редактор сайту cyfrovahata.com.ua. Потрібно ВНЕСТИ ТОЧКОВУ ПРАВКУ
в УЖЕ ОПУБЛІКОВАНУ сторінку, не ламаючи решту розмітки.

Завдання: {title}
Опис: {description}

Ось ПОВНИЙ поточний контент сторінки (Gutenberg/HTML розмітка):
---ПОТОЧНИЙ КОНТЕНТ---
{current_content}
---КІНЕЦЬ---

Зроби ЛИШЕ ту правку, що описана в завданні (наприклад: додай один блок,
поправ один абзац, додай FAQ-блок тощо). Решту розмітки, стилів, класів,
порядку існуючих блоків — залиш АБСОЛЮТНО без змін.

У відповіді поверни ПОВНИЙ оновлений контент сторінки (з усіма старими
блоками + внесеною правкою), без пояснень, готовий для прямого збереження.
"""


def call_claude(client, prompt: str, max_tokens: int = 3000) -> str:
    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in response.content if b.type == "text").strip()


def slug_from_path(path: str) -> str:
    return path.strip("/").split("/")[-1]


def process_edit_existing(rec, client, wp, telegram_token, chat_id):
    """Правка вже опублікованої сторінки: ЖИВИЙ контент не чіпаємо,
    зміну кладемо як autosave-ревізію, прив'язану саме до цього запису."""
    slug = slug_from_path(rec["target_page_path"])

    target, post_type = None, None
    for candidate_type in ("pages", "posts"):
        found = wp.find_by_slug(slug, candidate_type)
        if found:
            target, post_type = found, candidate_type
            break

    if target is None:
        send_message(telegram_token, chat_id,
                      f"⚠️ #{rec['id']}: не знайшов сторінку за шляхом "
                      f"{rec['target_page_path']}. Потрібно зробити вручну.")
        rec["status"] = "needs_manual_review"
        return

    current_content = wp.get_raw_content(target["id"], post_type)

    updated_content = call_claude(client, EDIT_EXISTING_PROMPT.format(
        title=rec["title"], description=rec["description"], current_content=current_content,
    ))

    revision = wp.propose_revision(target["id"], updated_content, post_type)

    send_message(telegram_token, chat_id,
                  f"✅ #{rec['id']}: правку для \"{rec['title']}\" підготовано як "
                  f"чернетку-ревізію вже опублікованої сторінки.\n"
                  f"Жива сторінка НЕ змінена. Щоб побачити й застосувати правку:\n"
                  f"1. Відкрий {revision['edit_link']}\n"
                  f"2. WordPress покаже банер \"Є новіша автозбережена версія\" — "
                  f"натисни його, щоб завантажити запропоновану правку в редактор.\n"
                  f"3. Перевір і натисни \"Оновити\", якщо все добре.")

    rec["status"] = "revision_ready"
    rec["edit_link"] = revision["edit_link"]


def process_create_new(rec, client, wp, telegram_token, chat_id):
    """Створення нового контенту, якого ще немає на сайті — це безпечно
    оформити як звичайну чернетку (draft), бо живої версії ще не існує."""
    content_list = wp.list_content("posts") + wp.list_content("pages")
    listing_text = "\n".join(f"#{c['id']} {c['title']} ({c['slug']})" for c in content_list)

    search_query = call_claude(client, PICK_REFERENCE_PROMPT.format(
        title=rec["title"], description=rec["description"], content_list=listing_text,
    ), max_tokens=50)

    reference, post_type = None, None
    for candidate_type in ("posts", "pages"):
        found = wp.search_content(search_query, candidate_type)
        if found:
            reference, post_type = found[0], candidate_type
            break

    if reference is None:
        send_message(telegram_token, chat_id,
                      f"⚠️ #{rec['id']}: не знайшов схожої сторінки-зразка для \"{rec['title']}\". "
                      f"Потрібно зробити вручну.")
        rec["status"] = "needs_manual_review"
        return

    reference_content = wp.get_raw_content(reference["id"], post_type)

    generated = call_claude(client, GENERATE_NEW_PROMPT.format(
        title=rec["title"], description=rec["description"], reference_content=reference_content,
    ))

    title_match = re.match(r"TITLE:\s*(.+)", generated)
    if title_match:
        new_title = title_match.group(1).strip()
        new_content = generated[title_match.end():].strip()
    else:
        new_title = rec["title"]
        new_content = generated

    draft = wp.create_draft(new_title, new_content, post_type)

    send_message(telegram_token, chat_id,
                  f"✅ Нову чернетку за рекомендацією #{rec['id']} (\"{rec['title']}\") створено.\n"
                  f"Перевір і опублікуй вручну: {draft['edit_link']}")

    rec["status"] = "draft_ready"
    rec["edit_link"] = draft["edit_link"]


def capture_baseline(rec: dict) -> dict | None:
    """Фіксує поточні метрики сторінки в момент підтвердження публікації —
    це точка відліку "до", з якою аналітик через 14+ днів порівняє "після"."""
    if not rec.get("target_page_path"):
        return None
    end = datetime.date.today()
    start = end - datetime.timedelta(days=7)
    gsc_data = get_search_console_data(
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], os.environ["GSC_SITE_URL"],
        start.isoformat(), end.isoformat(),
    )
    ga4_data = get_ga4_data(
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], os.environ["GA4_PROPERTY_ID"],
        start.isoformat(), end.isoformat(),
    )
    return find_page_metrics(rec["target_page_path"], gsc_data, ga4_data)


def process_published(rec_id: int, by_id: dict, telegram_token: str, chat_id: str):
    rec = by_id.get(rec_id)
    if rec is None:
        send_message(telegram_token, chat_id, f"⚠️ Рекомендацію #{rec_id} не знайдено в бэклозі.")
        return
    if rec["status"] not in ("draft_ready", "revision_ready"):
        send_message(telegram_token, chat_id,
                      f"ℹ️ #{rec_id} має статус \"{rec['status']}\" — спершу потрібно /do {rec_id}.")
        return

    rec["baseline_metrics"] = capture_baseline(rec)
    rec["published_date"] = datetime.date.today().isoformat()
    rec["status"] = "published"
    rec["impact_checked"] = False

    send_message(telegram_token, chat_id,
                  f"📌 Зафіксував #{rec_id} \"{rec['title']}\" як опубліковане сьогодні.\n"
                  f"Через ~{IMPACT_REVIEW_DAYS} днів у тижневому звіті покажу, чи це реально допомогло.")


def process_recommendation(rec, client, wp, telegram_token, chat_id):
    if rec.get("action") == "edit_existing" and rec.get("target_page_path"):
        process_edit_existing(rec, client, wp, telegram_token, chat_id)
    else:
        process_create_new(rec, client, wp, telegram_token, chat_id)


def main():
    telegram_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    offset_data = load_json("telegram_offset.json", default={"offset": None})
    updates = get_updates(telegram_token, offset_data["offset"])

    backlog = load_json("recommendations.json", default=[])
    by_id = {r["id"]: r for r in backlog}

    requested_do, requested_published = [], []
    for update in updates:
        offset_data["offset"] = update["update_id"] + 1
        text = update.get("message", {}).get("text", "").strip()
        m_do = re.match(r"/do\s+(\d+)", text)
        m_pub = re.match(r"/published\s+(\d+)", text)
        if m_do:
            requested_do.append(int(m_do.group(1)))
        elif m_pub:
            requested_published.append(int(m_pub.group(1)))

    save_json("telegram_offset.json", offset_data)

    for rec_id in requested_published:
        process_published(rec_id, by_id, telegram_token, chat_id)

    if requested_do:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        wp = WordPressClient(
            os.environ["WP_BASE_URL"], os.environ["WP_USERNAME"], os.environ["WP_APP_PASSWORD"],
        )
        for rec_id in requested_do:
            rec = by_id.get(rec_id)
            if rec is None:
                send_message(telegram_token, chat_id, f"⚠️ Рекомендацію #{rec_id} не знайдено в бэклозі.")
                continue
            if rec["status"] != "pending":
                send_message(telegram_token, chat_id, f"ℹ️ #{rec_id} вже має статус \"{rec['status']}\".")
                continue
            process_recommendation(rec, client, wp, telegram_token, chat_id)

    if requested_do or requested_published:
        save_json("recommendations.json", list(by_id.values()))


if __name__ == "__main__":
    main()
