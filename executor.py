"""
АГЕНТ 2: SEO-ВИКОНАВЕЦЬ (обмежений запис)

Запускається за розкладом (.github/workflows/seo-executor.yml), кожні кілька годин.
1. Читає нові команди з Telegram: "/do <id>" — виконати рекомендацію з бэклогу.
2. Якщо рекомендація — НОВИЙ контент (action=create_new): знаходить схожу
   існуючу сторінку як зразок стилю, генерує текст і одразу ПУБЛІКУЄ статтю
   (безпечно — нова сторінка не може зламати існуючий контент).
   Надсилає URL опублікованої статті і команду /revert <id> для відкату.
3. Якщо рекомендація — правка ВЖЕ ОПУБЛІКОВАНОЇ сторінки (action=edit_existing):
   НЕ змінює статус і НЕ чіпає живий контент, а надсилає точну інструкцію
   що змінити вручну в редакторі WordPress.
4. /revert <id> — повертає автоматично опубліковану статтю в чернетку.
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
from lib.telegram import get_updates, send_message, send_message_with_buttons, answer_callback_query
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

MANUAL_EDIT_PROMPT = """\
Ти — SEO-консультант. Людина буде вносити правку ВРУЧНУ в WordPress редакторі.
Поясни ЧІТКО і КОРОТКО що саме потрібно зробити.

Завдання: {title}
Опис: {description}
Сторінка: {page_url}

Поточний контент сторінки (скорочено):
{content_preview}

Напиши інструкцію у форматі:
1. Що знайти на сторінці (конкретний текст, блок, елемент)
2. Що змінити / додати (точний текст або посилання)
3. Де саме в редакторі це зробити

Максимум 5 коротких пунктів. Лише конкретні дії, без теорії.
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
    """Правка вже опублікованої сторінки: надсилає точну інструкцію що змінити вручну."""
    page_url = f"{wp.base_url}{rec['target_page_path']}" if rec.get("target_page_path") else ""

    # Отримуємо поточний контент для контексту
    content_preview = ""
    if rec.get("target_page_path"):
        slug = slug_from_path(rec["target_page_path"])
        for candidate_type in ("pages", "posts"):
            found = wp.find_by_slug(slug, candidate_type)
            if found:
                try:
                    raw = wp.get_raw_content(found["id"], candidate_type)
                    soup_import = __import__("bs4", fromlist=["BeautifulSoup"])
                    from bs4 import BeautifulSoup
                    text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
                    content_preview = text[:2000]
                except Exception:
                    pass
                break

    instruction = call_claude(client, MANUAL_EDIT_PROMPT.format(
        title=rec["title"],
        description=rec.get("description", ""),
        page_url=page_url or rec.get("target_page_path", ""),
        content_preview=content_preview or "(контент недоступний)",
    ), max_tokens=1000)

    edit_link = f"{wp.base_url}/wp-admin/post.php" if page_url else wp.base_url + "/wp-admin/"

    send_message(telegram_token, chat_id,
                 f"✏️ #{rec['id']} — потрібна ручна правка\n\n"
                 f"<b>{rec['title']}</b>\n\n"
                 f"Сторінка: {page_url}\n\n"
                 f"{instruction}\n\n"
                 f"Після виконання — натисни <b>✅ Зроблено вручну</b> на кнопці вище.")

    rec["status"] = "awaiting_manual"
    rec["manual_instruction"] = instruction


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
    """Створює і одразу публікує новий запис: копіює Gutenberg-розмітку зразка,
    замінює тільки тексти через Claude, структуру блоків не чіпає.
    Нові статті — безризикові (живої версії ще немає), тому публікуються автоматично."""
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

    # Зберігаємо як чернетку — відправляємо preview для підтвердження
    post = wp.create_draft(new_title, new_markup, "posts", status="draft")

    rec["status"] = "draft_ready"
    rec["wp_post_id"] = post["id"]
    rec["edit_link"] = post["edit_link"]
    rec["draft_title"] = new_title

    # Перший абзац для preview
    from bs4 import BeautifulSoup as _BS
    _soup = _BS(new_markup, "html.parser")
    preview_text = ""
    for _p in _soup.find_all("p"):
        _t = _p.get_text(strip=True)
        if len(_t) > 60:
            preview_text = _t[:300]
            break

    from lib.telegram import send_message_with_buttons
    send_message_with_buttons(
        telegram_token, chat_id,
        f"📝 Стаття #{rec['id']} готова до публікації:\n\n"
        f"<b>{new_title}</b>\n\n"
        f"{preview_text}\n\n"
        f"Переглянути у WP: {post['edit_link']}",
        buttons=[
            [{"text": "🚀 Опублікувати", "callback_data": f"publish_{rec['id']}"},
             {"text": "✏️ Редагувати", "callback_data": f"noop_{rec['id']}"}],
        ],
    )


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


def _publish_draft(rec_id: int, by_id: dict, telegram_token: str, chat_id: str) -> None:
    """Публікує чернетку яку створив process_create_new після підтвердження користувачем."""
    rec = by_id.get(rec_id)
    if not rec:
        send_message(telegram_token, chat_id, f"⚠️ #{rec_id} не знайдено.")
        return
    if rec.get("status") != "draft_ready" or not rec.get("wp_post_id"):
        send_message(telegram_token, chat_id, f"ℹ️ #{rec_id} не є чернеткою для публікації (статус: {rec.get('status')}).")
        return
    wp = WordPressClient(
        os.environ["WP_BASE_URL"], os.environ["WP_USERNAME"], os.environ["WP_APP_PASSWORD"],
    )
    try:
        result = wp._post(f"posts/{rec['wp_post_id']}", {"status": "publish"})
        slug = result.get("slug", "")
        public_url = f"{wp.base_url}/{slug}/" if slug else rec["edit_link"]
        rec["status"] = "published"
        rec["auto_published"] = True
        rec["published_date"] = datetime.date.today().isoformat()
        rec["baseline_metrics"] = {"clicks": 0, "impressions": 0, "sessions": 0, "users": 0}
        rec["impact_checked"] = False
        send_message(telegram_token, chat_id,
                     f"🚀 Статтю #{rec_id} опубліковано!\n\n"
                     f"<b>{rec.get('draft_title', rec['title'])}</b>\n"
                     f"🔗 {public_url}\n\n"
                     f"Щоб відкатити — надішли /revert {rec_id}")
    except Exception as e:
        send_message(telegram_token, chat_id, f"⚠️ Не вдалось опублікувати #{rec_id}: {e}")


def _send_status(telegram_token: str, chat_id: str, backlog: list[dict], learning_log: list[dict]) -> None:
    """Відповідає на /status — короткий дайджест стану бэклогу і learning_log."""
    from collections import Counter
    statuses = Counter(r.get("status", "pending") for r in backlog)
    pending = statuses.get("pending", 0)
    in_progress = statuses.get("in_progress", 0)
    published = statuses.get("published", 0)
    done = statuses.get("done", 0)
    rejected = statuses.get("rejected", 0)
    reverted = statuses.get("reverted", 0)

    log_total = len(learning_log)
    log_worked = sum(1 for e in learning_log if e.get("worked"))
    log_pct = round(log_worked / log_total * 100) if log_total else 0

    pending_items = [r for r in backlog if r.get("status") == "pending"]
    top3 = pending_items[:3]
    top3_lines = "\n".join(
        f"  #{r['id']} [{r.get('action','?')}] {r['title'][:55]}"
        for r in top3
    )

    msg = (
        f"📊 Статус SEO-агента:\n\n"
        f"Бэклог:\n"
        f"  ⏳ Очікують: {pending}\n"
        f"  🔄 В роботі: {in_progress}\n"
        f"  ✅ Опубліковано: {published + done}\n"
        f"  ↩️ Відкочено: {reverted}\n"
        f"  ❌ Відхилено: {rejected}\n\n"
        f"Learning log: {log_total} записів, {log_worked} успішних ({log_pct}%)\n\n"
        f"Топ-3 очікують виконання:\n{top3_lines or '  (порожньо)'}\n\n"
        f"Команди: /do <id> | /reject <id> | /published <id> | /revert <id>"
    )
    send_message(telegram_token, chat_id, msg)


def main():
    telegram_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    offset_data = load_json("telegram_offset.json", default={"offset": None})
    updates = get_updates(telegram_token, offset_data["offset"])

    backlog = load_json("recommendations.json", default=[])
    by_id = {r["id"]: r for r in backlog}

    requested_do, requested_published, requested_reject, requested_done, requested_revert, requested_publish_draft = [], [], [], [], [], []
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
            m_done = re.match(r"done_(\d+)", data)
            m_rej = re.match(r"reject_(\d+)", data)
            m_pub_draft = re.match(r"publish_(\d+)", data)
            if m_pub_draft:
                rec_id = int(m_pub_draft.group(1))
                requested_publish_draft.append(rec_id)
                callbacks_to_answer.append((cq_id, cq_chat, cq_msg_id, f"🚀 Публікую #{rec_id}..."))
            elif m_do:
                rec_id = int(m_do.group(1))
                requested_do.append(rec_id)
                callbacks_to_answer.append((cq_id, cq_chat, cq_msg_id, f"▶️ Виконую #{rec_id}..."))
            elif m_done:
                rec_id = int(m_done.group(1))
                requested_done.append(rec_id)
                callbacks_to_answer.append((cq_id, cq_chat, cq_msg_id, f"✅ #{rec_id} зафіксовано як виконане"))
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
        m_rev = re.match(r"/revert\s+(\d+)", text)
        if text.strip() == "/status":
            _send_status(telegram_token, chat_id, backlog, load_json("learning_log.json", default=[]))
        elif m_do:
            requested_do.append(int(m_do.group(1)))
        elif m_pub:
            requested_published.append(int(m_pub.group(1)))
        elif m_rej:
            requested_reject.append(int(m_rej.group(1)))
        elif m_rev:
            requested_revert.append(int(m_rev.group(1)))

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

    # "Зроблено вручну" — фіксуємо як published
    for rec_id in requested_done:
        rec = by_id.get(rec_id)
        if rec is None:
            send_message(telegram_token, chat_id, f"⚠️ Рекомендацію #{rec_id} не знайдено.")
            continue
        # Якщо це draft_ready (create_new чернетка) — спочатку публікуємо у WP
        if rec.get("status") == "draft_ready" and rec.get("wp_post_id"):
            _publish_draft(rec_id, by_id, telegram_token, chat_id)
            continue
        rec["status"] = "published"
        rec["published_date"] = datetime.date.today().isoformat()
        rec["impact_checked"] = False
        rec["baseline_metrics"] = rec.get("baseline_metrics") or {"clicks": 0, "impressions": 0, "sessions": 0, "users": 0}
        send_message(telegram_token, chat_id,
                     f"✅ #{rec_id} «{rec['title']}» зафіксовано як виконане вручну.\n"
                     f"Через ~14 днів у звіті побачимо чи це дало результат.")

    # Відкат автоматично опублікованих статей (/revert <id>)
    if requested_revert:
        wp_revert = WordPressClient(
            os.environ["WP_BASE_URL"], os.environ["WP_USERNAME"], os.environ["WP_APP_PASSWORD"],
        )
        for rec_id in requested_revert:
            rec = by_id.get(rec_id)
            if rec is None:
                send_message(telegram_token, chat_id, f"⚠️ Рекомендацію #{rec_id} не знайдено.")
                continue
            if not rec.get("auto_published"):
                send_message(telegram_token, chat_id,
                             f"ℹ️ #{rec_id} не є автоматично опублікованою статтею — "
                             f"відкат не підтримується через бот.")
                continue
            # Знаходимо пост за edit_link
            post_id_match = re.search(r"post=(\d+)", rec.get("edit_link", ""))
            if not post_id_match:
                send_message(telegram_token, chat_id,
                             f"⚠️ Не вдалось знайти ID поста для #{rec_id}.")
                continue
            post_id = int(post_id_match.group(1))
            ok = wp_revert.unpublish_post(post_id)
            if ok:
                rec["status"] = "reverted"
                rec["reverted_date"] = datetime.date.today().isoformat()
                send_message(telegram_token, chat_id,
                             f"↩️ Статтю #{rec_id} «{rec['title']}» переведено в чернетку.\n"
                             f"Перевірити: {rec['edit_link']}")
            else:
                send_message(telegram_token, chat_id,
                             f"⚠️ Не вдалось відкатити #{rec_id} через API — "
                             f"поверніть вручну: {rec.get('edit_link', '')}")

    for rec_id in requested_publish_draft:
        _publish_draft(rec_id, by_id, telegram_token, chat_id)

    if requested_do or requested_published or requested_reject or requested_done or requested_revert or requested_publish_draft:
        save_json("recommendations.json", list(by_id.values()))

    # Автоматичний impact review: шукаємо published без перевірки ефекту (14+ днів)
    _auto_impact_review(backlog, telegram_token, chat_id)


def _auto_impact_review(backlog: list[dict], telegram_token: str, chat_id: str) -> None:
    """Надсилає нагадування в Telegram для published рекомендацій що чекають impact review."""
    today = datetime.date.today()
    from lib.metrics import IMPACT_REVIEW_DAYS
    due = []
    for rec in backlog:
        if rec.get("status") != "published":
            continue
        if rec.get("impact_checked"):
            continue
        pub_date = rec.get("published_date")
        if not pub_date:
            continue
        try:
            days_passed = (today - datetime.date.fromisoformat(pub_date)).days
        except Exception:
            continue
        if days_passed >= IMPACT_REVIEW_DAYS:
            due.append((rec, days_passed))

    if not due:
        return

    # Отримуємо GSC/GA4 для порівняння
    try:
        end = today
        start = end - datetime.timedelta(days=7)
        gsc_now = get_search_console_data(
            os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], os.environ["GSC_SITE_URL"],
            start.isoformat(), end.isoformat(),
        )
        ga4_now = get_ga4_data(
            os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], os.environ["GA4_PROPERTY_ID"],
            start.isoformat(), end.isoformat(),
        )
    except Exception as e:
        print(f"Impact review fetch error: {e}")
        return

    for rec, days_passed in due[:3]:  # не більше 3 за раз
        baseline = rec.get("baseline_metrics") or {}
        page = rec.get("target_page_path")
        current = find_page_metrics(page, gsc_now, ga4_now) if page else {}

        lines = [f"📊 Impact review #{rec['id']} «{rec['title']}» ({days_passed} днів):"]
        if baseline and current:
            for key, label in [("clicks", "кліки"), ("impressions", "покази"), ("sessions", "сесії")]:
                b = baseline.get(key, 0) or 0
                c = current.get(key, 0) or 0
                if b == 0 and c == 0:
                    continue
                diff = c - b
                pct = f" ({diff:+d}, {diff/b*100:+.0f}%)" if b > 0 else f" (було 0 → {c})"
                lines.append(f"  {label}: {b} → {c}{pct}")
        else:
            lines.append("  (немає даних для порівняння)")

        lines.append(f"\nЯкщо результат задовільний — натисни /done_{rec['id']}")
        send_message(telegram_token, chat_id, "\n".join(lines))


if __name__ == "__main__":
    main()
