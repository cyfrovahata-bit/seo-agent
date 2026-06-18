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
import json
import os
import re
import uuid

import anthropic
from bs4 import BeautifulSoup

from lib.google_seo import get_search_console_data, get_ga4_data
from lib.metrics import find_page_metrics, IMPACT_REVIEW_DAYS
from lib.state import load_json, save_json
from lib.telegram import get_updates, send_message
from lib.wordpress import WordPressClient

MODEL = "claude-sonnet-4-6"

TEXTS_REPLACEMENT_PROMPT = """\
Ти — SEO-копірайтер. Тобі потрібно написати нові тексти для статті на тему:

Завдання: {title}
Опис: {description}

Нижче — список текстових рядків з існуючої статті-зразка. Для КОЖНОГО рядка
напиши новий текст відповідно до нової теми, зберігаючи приблизно ту ж довжину
і стиль (діловий, українська мова).

Поверни ТІЛЬКИ валідний JSON масив об'єктів:
[{{"old": "оригінальний текст", "new": "новий текст"}}, ...]

Жодних пояснень, тільки JSON.

Тексти для заміни:
{texts_json}
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


def replace_block_ids(markup: str) -> str:
    """Генерує нові унікальні block_id для всіх UAGB-блоків."""
    def new_id(_):
        return f'"block_id":"{uuid.uuid4().hex[:8]}"'
    return re.sub(r'"block_id":"[a-f0-9]+"', new_id, markup)


def extract_texts(markup: str) -> list[str]:
    """Витягує текстові рядки з HTML-розмітки (h1-h4, p, li), довші за 10 символів."""
    soup = BeautifulSoup(markup, "html.parser")
    seen, texts = set(), []
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
        t = tag.get_text(strip=True)
        if len(t) > 10 and t not in seen:
            seen.add(t)
            texts.append(t)
    return texts


def apply_replacements(markup: str, replacements: list[dict]) -> str:
    """Замінює тексти в розмітці за списком {old, new}."""
    for item in replacements:
        old, new = item.get("old", ""), item.get("new", "")
        if old and new and old != new:
            markup = markup.replace(old, new)
    return markup


def process_create_new(rec, client, wp, telegram_token, chat_id):
    """Створює новий запис: копіює Gutenberg-розмітку зразка,
    замінює тільки тексти через Claude, структуру блоків не чіпає."""
    # Беремо перший опублікований пост як зразок
    posts = wp._get("posts", {"per_page": 5, "status": "publish"})
    if not posts:
        send_message(telegram_token, chat_id,
                     f"⚠️ #{rec['id']}: не знайшов жодного опублікованого запису-зразка.")
        rec["status"] = "needs_manual_review"
        return

    reference_id = posts[0]["id"]
    reference_markup = wp.get_raw_content(reference_id, "posts")

    # Витягуємо тексти для заміни
    texts = extract_texts(reference_markup)
    texts_json = json.dumps(texts, ensure_ascii=False)

    # Claude генерує тільки заміни текстів
    raw_response = call_claude(client, TEXTS_REPLACEMENT_PROMPT.format(
        title=rec["title"], description=rec["description"], texts_json=texts_json,
    ), max_tokens=4000)

    # Парсимо JSON з відповіді
    json_match = re.search(r"\[.*\]", raw_response, re.DOTALL)
    replacements = json.loads(json_match.group()) if json_match else []

    # Застосовуємо заміни і нові block_id
    new_markup = apply_replacements(reference_markup, replacements)
    new_markup = replace_block_ids(new_markup)

    # Заголовок — перший new текст що замінював h1/h2
    new_title = rec["title"]
    for item in replacements:
        if len(item.get("new", "")) > 20:
            new_title = item["new"]
            break

    draft = wp.create_draft(new_title, new_markup, "posts")

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
