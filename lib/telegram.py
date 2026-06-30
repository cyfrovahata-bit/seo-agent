"""
Telegram Bot API: відправка звітів і читання команд від тебе.
Polling (getUpdates) достатньо, окремий сервер з webhook не потрібен —
це і дає змогу обійтись cron-завданнями в GitHub Actions.
"""

import json
import requests

API_BASE = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE_LEN = 4000  # запас від ліміту Telegram 4096


def send_message(token: str, chat_id: str, text: str) -> None:
    """Відправляє повідомлення, розбиваючи довгі тексти на частини."""
    for i in range(0, len(text), MAX_MESSAGE_LEN):
        chunk = text[i:i + MAX_MESSAGE_LEN]
        resp = requests.post(
            API_BASE.format(token=token, method="sendMessage"),
            data={"chat_id": chat_id, "text": chunk},
            timeout=30,
        )
        if not resp.ok:
            print(f"Telegram API error {resp.status_code}: {resp.text}")
        resp.raise_for_status()


def send_recommendations_buttons(token: str, chat_id: str, pending: list[dict]) -> None:
    """Відправляє кожну рекомендацію окремим повідомленням з кнопками."""
    if not pending:
        return
    for r in pending:
        priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(r.get("priority", ""), "⚪")
        text = (
            f"📋 Рекомендація #{r['id']} {priority_emoji}\n\n"
            f"<b>{r['title']}</b>\n\n"
            f"{r.get('description', '')}"
        )
        buttons = [[
            {"text": f"▶️ Виконати", "callback_data": f"do_{r['id']}"},
            {"text": f"❌ Відхилити", "callback_data": f"reject_{r['id']}"},
        ]]
        resp = requests.post(
            API_BASE.format(token=token, method="sendMessage"),
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": buttons},
            },
            timeout=30,
        )
        if not resp.ok:
            print(f"Telegram buttons error {resp.status_code}: {resp.text}")


def answer_callback_query(token: str, callback_query_id: str, text: str = "") -> None:
    """Підтверджує натискання inline-кнопки (прибирає "годинник" на кнопці)."""
    requests.post(
        API_BASE.format(token=token, method="answerCallbackQuery"),
        data={"callback_query_id": callback_query_id, "text": text},
        timeout=10,
    )


def edit_message_reply_markup(token: str, chat_id: str, message_id: int, text: str) -> None:
    """Замінює inline-клавіатуру на текст після того, як кнопку натиснули."""
    requests.post(
        API_BASE.format(token=token, method="editMessageText"),
        data={"chat_id": chat_id, "message_id": message_id, "text": text},
        timeout=10,
    )


def get_updates(token: str, offset: int | None = None) -> list[dict]:
    """Повертає нові повідомлення та callback_query після останнього offset."""
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(
        API_BASE.format(token=token, method="getUpdates"),
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("result", [])
