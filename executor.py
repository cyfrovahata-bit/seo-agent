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
from lib.telegram import get_updates, send_message, answer_callback_query
from lib.wordpress import WordPressClient

MODEL = "claude-sonnet-4-6"

TEXTS_REPLACEMENT_PROMPT = """\
Ти — SEO-копірайтер. Тобі потрібно написати нові тексти для статті на тему:

Завдання: {title}
Опис: {description}

Нижче — пронумерований список текстів із статті-зразка (можуть містити HTML-теги).
Для КОЖНОГО тексту напиши новий відповідно до нової теми, зберігаючи приблизно ту ж
довжину і стиль (діловий, українська мова). Якщо в тексті є <strong> або <br> — збережи.

Поверни ТІЛЬКИ валідний JSON масив — тільки нові тексти за їх номером:
[{{"i": 0, "new": "новий текст"}}, {{"i": 1, "new": "..."}}]

Жодних пояснень, тільки JSON.

Тексти:
{texts_json}
"""

LAYOUT_PROMPT = """\
Ти — SEO-архітектор контенту. Тема нової статті:

Завдання: {title}
Опис: {description}

Зразок має такі info-box блоки (назва + поточна іконка + опис):
{infoboxes_json}

Зразок має {count} блоків — поверни рівно {count} об'єктів.

Твоє завдання:
1. Для кожного блоку написати назву і короткий опис (1 рядок) відповідно до нової теми
2. Підібрати іконку Font Awesome яка ТОЧНО відповідає змісту блоку
3. Всі {count} іконок ПОВИННІ бути РІЗНИМИ між собою

Доступні Font Awesome іконки (використовуй ТІЛЬКИ ці точні назви — вони вже перевірені):
- SEO/пошук: searchengin, chart-line, chart-bar, bullseye
- Гроші/ціна: money-bill-wave, dollar-sign, coins, tags
- Швидкість/час: gauge-high, clock, stopwatch, hourglass-half
- Технічне: gear, wrench, code, laptop-code
- Контент/текст: file-lines, pencil, align-left, newspaper
- Посилання/трафік: link, share-nodes, diagram-project
- Користувачі/клієнти: users, user-check, user-tie, handshake
- Перевірка/успіх: circle-check, clipboard-check, list-check
- Зріст/результат: arrow-trend-up, rocket, trophy, medal
- Локальне SEO: location-dot, map, globe
- Аудит: microscope, eye, binoculars
- Ключові слова: key, keyboard, text-height
- Інші: shield, chart-column, mobile-screen-button, computer-mouse, text-width

Поверни ТІЛЬКИ валідний JSON масив:
[{{"title": "Назва блоку", "desc": "короткий опис 1 речення", "icon": "назва-іконки"}}, ...]

Жодних пояснень, тільки JSON.
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

{block_template_section}

Зроби ЛИШЕ ту правку, що описана в завданні. Решту розмітки, стилів, класів,
порядку існуючих блоків — залиш АБСОЛЮТНО без змін.

Якщо потрібно додати новий блок — використовуй ЗРАЗОК БЛОКУ (якщо наданий):
скопіюй його розмітку, змін тільки тексти під нову тему, не чіпай класи, атрибути, структуру.
Якщо зразку немає — вставляй стандартний Gutenberg wp:paragraph або wp:heading.

У відповіді поверни ПОВНИЙ оновлений контент сторінки (з усіма старими
блоками + внесеною правкою), без пояснень, готовий для прямого збереження.
"""

# Відповідність ключових слів → типи блоків для пошуку на сайті
BLOCK_HINT_MAP = [
    (["faq", "питань", "відповід", "запитань"],
     ["wp:uagb/faq", "wp:yoast/faq-block", "wp:rank-math/faq-block"]),
    (["список", "перелік", "переваг", "пункти", "кроки", "етапи"],
     ["wp:uagb/icon-list", "wp:list"]),
    (["cta", "заклик", "кнопк", "замовити", "зв'яжіться"],
     ["wp:uagb/call-to-action", "wp:buttons"]),
    (["відгук", "кейс", "приклад"],
     ["wp:uagb/testimonial", "wp:pullquote"]),
    (["таблиц", "порівнян"],
     ["wp:table"]),
    (["іконк", "блок переваг", "info-box", "infobox"],
     ["wp:uagb/info-box"]),
]


def _detect_block_hints(title: str, description: str) -> list[str] | None:
    """Визначає який тип блоку потрібен за текстом рекомендації."""
    hint = (title + " " + description).lower()
    for keywords, block_types in BLOCK_HINT_MAP:
        if any(kw in hint for kw in keywords):
            return block_types
    return None


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

    # Зберігаємо оригінальний заголовок для можливого відкату
    if "заголовок" in rec.get("title", "").lower() or "title" in rec.get("title", "").lower():
        rec["original_title_backup"] = target.get("title", {}).get("rendered", "")

    # Шукаємо зразок блоку на сайті якщо рекомендація про додавання блоку
    block_template_section = ""
    block_hints = _detect_block_hints(rec["title"], rec.get("description", ""))
    if block_hints:
        found_block = wp.find_block_on_site(block_hints)
        if found_block:
            block_template_section = (
                "ЗРАЗОК БЛОКУ З САЙТУ (використай цю розмітку як основу, змін тільки тексти):\n"
                "---ЗРАЗОК---\n" + found_block + "\n---КІНЕЦЬ ЗРАЗКА---"
            )

    updated_content = call_claude(client, EDIT_EXISTING_PROMPT.format(
        title=rec["title"], description=rec["description"],
        current_content=current_content,
        block_template_section=block_template_section,
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


def get_stacked_infobox_comment_positions(markup: str) -> list[tuple[int, int]]:
    """Знаходить позиції (start, end) коментарів <!-- wp:uagb/info-box --> з iconView=Stacked."""
    results = []
    i = 0
    marker = "<!-- wp:uagb/info-box "
    while True:
        pos = markup.find(marker, i)
        if pos == -1:
            break
        end = markup.find(" -->", pos)
        if end == -1:
            break
        end += 4
        if '"iconView":"Stacked"' in markup[pos:end]:
            results.append((pos, end))
        i = end
    return results


def extract_infoboxes(markup: str) -> list[dict]:
    """Витягує іконки зі stacked info-box Gutenberg-коментарів."""
    results = []
    for start, end in get_stacked_infobox_comment_positions(markup):
        m = re.search(r'"icon":"([^"]+)"', markup[start:end])
        if m:
            results.append({"icon": m.group(1), "title": "", "desc": ""})
    return results


def adjust_infobox_blocks(markup: str, new_infoboxes: list[dict]) -> str:
    """Замінює іконки у stacked info-box блоках (тільки у Gutenberg-коментарях)."""
    positions = get_stacked_infobox_comment_positions(markup)
    if not positions:
        return markup
    # Замінюємо з кінця, щоб не зміщувати позиції
    for i, (start, end) in enumerate(reversed(positions)):
        icon_idx = len(positions) - 1 - i
        if icon_idx >= len(new_infoboxes):
            continue
        new_icon = new_infoboxes[icon_idx]["icon"]
        comment = markup[start:end]
        comment = re.sub(r'"icon":"[^"]+"', f'"icon":"{new_icon}"', comment, count=1)
        markup = markup[:start] + comment + markup[end:]
    return markup


def strip_hero_background(markup: str) -> str:
    """Прибирає фонове зображення з hero-контейнера — користувач додасть своє."""
    # Знаходимо backgroundImageDesktop і замінюємо значення на {} (балансування дужок)
    key = '"backgroundImageDesktop":'
    result = markup
    search_from = 0
    while True:
        pos = result.find(key, search_from)
        if pos == -1:
            break
        val_start = pos + len(key)
        if val_start < len(result) and result[val_start] == "{":
            depth, i = 0, val_start
            while i < len(result):
                if result[i] == "{":
                    depth += 1
                elif result[i] == "}":
                    depth -= 1
                    if depth == 0:
                        result = result[:val_start] + "{}" + result[i + 1:]
                        break
                i += 1
        search_from = pos + len(key)
    return result


def replace_block_ids(markup: str) -> str:
    """Генерує нові унікальні block_id для всіх UAGB-блоків."""
    def new_id(_):
        return f'"block_id":"{uuid.uuid4().hex[:8]}"'
    return re.sub(r'"block_id":"[a-f0-9]+"', new_id, markup)


def extract_texts(markup: str) -> list[str]:
    """Витягує ТОЧНІ рядки з розмітки через regex — тільки основний контент."""
    texts, seen = [], set()
    # h1/h2 заголовки
    for m in re.finditer(r'<h[12][^>]*>(.*?)</h[12]>', markup, re.DOTALL):
        t = m.group(1).strip()
        plain = re.sub(r'<[^>]+>', '', t)
        if len(plain) > 5 and t not in seen:
            seen.add(t); texts.append(t)
    # Лише параграфи з uagb-desc-text (підзаголовки під h2) та uagb-ifb-desc
    for cls in ('uagb-desc-text', 'uagb-ifb-desc', 'uagb-heading-text'):
        for m in re.finditer(rf'<p class="{cls}">(.*?)</p>', markup, re.DOTALL):
            t = m.group(1).strip()
            plain = re.sub(r'<[^>]+>', '', t)
            if len(plain) > 10 and t not in seen:
                seen.add(t); texts.append(t)
    # Мітки icon-list
    for m in re.finditer(r'<span class="uagb-icon-list__label">(.*?)</span>', markup):
        t = m.group(1).strip()
        if len(t) > 3 and t not in seen:
            seen.add(t); texts.append(t)
    return texts


def apply_replacements(markup: str, originals: list[str], new_texts: list[dict]) -> str:
    """Замінює тексти в розмітці за індексованим списком {i, new}."""
    for item in new_texts:
        idx = item.get("i", -1)
        new = item.get("new", "")
        if 0 <= idx < len(originals) and new and originals[idx] != new:
            markup = markup.replace(originals[idx], new)
    return markup


def process_create_new(rec, client, wp, telegram_token, chat_id):
    """Створює новий запис: копіює Gutenberg-розмітку зразка,
    замінює тільки тексти через Claude, структуру блоків не чіпає."""
    # Знаходимо найкращий шаблон серед опублікованих постів
    reference_id, template_type = wp.find_best_template(
        rec["title"], rec.get("description", ""), fallback_id=1751
    )
    reference_markup = wp.get_raw_content(reference_id, "posts")

    # Витягуємо тексти для заміни
    texts = extract_texts(reference_markup)
    texts_json = json.dumps(texts, ensure_ascii=False)

    # Витягуємо тексти — ТОЧНІ рядки з розмітки (Claude тільки генерує нові, не відтворює старі)
    texts = extract_texts(reference_markup)
    texts_indexed = [{"i": i, "text": t} for i, t in enumerate(texts)]
    texts_json = json.dumps(texts_indexed, ensure_ascii=False)

    # Claude генерує нові тексти за індексом
    raw_response = call_claude(client, TEXTS_REPLACEMENT_PROMPT.format(
        title=rec["title"], description=rec["description"], texts_json=texts_json,
    ), max_tokens=8000)
    json_match = re.search(r"\[.*\]", raw_response, re.DOTALL)
    if json_match:
        new_texts = json.loads(json_match.group())
    else:
        # Якщо JSON обрізано — закриваємо масив і парсимо що є
        raw_arr = re.search(r"\[.*", raw_response, re.DOTALL)
        try:
            new_texts = json.loads((raw_arr.group() if raw_arr else "[]") + "]")
        except Exception:
            new_texts = []

    # Claude генерує нові info-box іконки
    infoboxes = extract_infoboxes(reference_markup)
    if infoboxes:
        raw_layout = call_claude(client, LAYOUT_PROMPT.format(
            title=rec["title"], description=rec["description"],
            infoboxes_json=json.dumps(infoboxes, ensure_ascii=False),
            count=len(infoboxes),
        ), max_tokens=1000)
        layout_match = re.search(r"\[.*\]", raw_layout, re.DOTALL)
        new_infoboxes = json.loads(layout_match.group()) if layout_match else infoboxes
    else:
        new_infoboxes = []

    # Застосовуємо: спочатку іконки, потім тексти, потім видаляємо фон, потім block_id
    new_markup = reference_markup
    if new_infoboxes:
        new_markup = adjust_infobox_blocks(new_markup, new_infoboxes)
    new_markup = apply_replacements(new_markup, texts, new_texts)
    new_markup = strip_hero_background(new_markup)
    new_markup = replace_block_ids(new_markup)

    # Заголовок — перший довгий новий текст
    new_title = rec["title"]
    for item in new_texts:
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

    requested_do, requested_published, requested_reject = [], [], []
    callbacks_to_answer = []  # (callback_query_id, chat_id, message_id, text)

    for update in updates:
        offset_data["offset"] = update["update_id"] + 1

        # Inline кнопка (callback_query)
        if "callback_query" in update:
            cq = update["callback_query"]
            data = cq.get("data", "")
            cq_id = cq["id"]
            cq_chat = str(cq["message"]["chat"]["id"])
            cq_msg_id = cq["message"]["message_id"]
            m_do = re.match(r"do_(\d+)", data)
            m_rej = re.match(r"reject_(\d+)", data)
            if m_do:
                rec_id = int(m_do.group(1))
                requested_do.append(rec_id)
                callbacks_to_answer.append((cq_id, cq_chat, cq_msg_id, f"▶️ Виконую #{rec_id}..."))
            elif m_rej:
                rec_id = int(m_rej.group(1))
                requested_reject.append(rec_id)
                callbacks_to_answer.append((cq_id, cq_chat, cq_msg_id, f"❌ Рекомендацію #{rec_id} відхилено"))
            continue

        # Текстові команди (/do, /published, /reject)
        text = update.get("message", {}).get("text", "").strip()
        m_do = re.match(r"/do\s+(\d+)", text)
        m_pub = re.match(r"/published\s+(\d+)", text)
        m_rej = re.match(r"/reject\s+(\d+)", text)
        if m_do:
            requested_do.append(int(m_do.group(1)))
        elif m_pub:
            requested_published.append(int(m_pub.group(1)))
        elif m_rej:
            requested_reject.append(int(m_rej.group(1)))

    save_json("telegram_offset.json", offset_data)

    # Відповідаємо на callback і прибираємо кнопки
    for cq_id, cq_chat, cq_msg_id, cq_text in callbacks_to_answer:
        answer_callback_query(telegram_token, cq_id, cq_text)

    # Відхилення рекомендацій
    for rec_id in requested_reject:
        rec = by_id.get(rec_id)
        if rec is None:
            send_message(telegram_token, chat_id, f"⚠️ Рекомендацію #{rec_id} не знайдено.")
            continue
        if rec["status"] != "pending":
            send_message(telegram_token, chat_id, f"ℹ️ #{rec_id} вже має статус \"{rec['status']}\".")
            continue
        rec["status"] = "rejected"
        send_message(telegram_token, chat_id, f"❌ Рекомендацію #{rec_id} «{rec['title']}» відхилено.")

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

    if requested_do or requested_published or requested_reject:
        save_json("recommendations.json", list(by_id.values()))


if __name__ == "__main__":
    main()
