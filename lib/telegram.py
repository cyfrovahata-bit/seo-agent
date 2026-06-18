"""
Telegram Bot API: відправка звітів і читання команд (/approve, /do) від тебе.
Polling (getUpdates) достатньо, окремий сервер з webhook не потрібен —
це і дає змогу обійтись cron-завданнями в GitHub Actions.
"""

import requests

API_BASE = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE_LEN = 4000  # запас від лімітy Telegram 4096


def send_message(token: str, chat_id: str, text: str) -> None:
    """Відправляє повідомлення, розбиваючи довгі тексти на частини.
    Без parse_mode: символи типу < > & у згенерованому тексті
    ламали HTML-парсер Telegram і викликали 400 Bad Request."""
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


def get_updates(token: str, offset: int | None = None) -> list[dict]:
    """Повертає нові повідомлення, надіслані боту після останнього offset."""
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
