"""
Аномальний моніторинг — запускається кожні 4 години.
Якщо кліки або покази змінились >30% відносно середнього за останні 7 днів — надсилає алерт.
"""

import datetime
import os

from lib.google_seo import get_search_console_data
from lib.state import load_json, save_json
from lib.telegram import send_message

DELTA_THRESHOLD = 0.30   # 30%
LOOKBACK_DAYS = 7
ALERT_COOLDOWN_HOURS = 8  # не більше одного алерту за 8 годин на одну метрику


def _get_today_totals(gsc_data: list[dict]) -> dict:
    clicks = sum(r.get("clicks", 0) for r in gsc_data)
    impressions = sum(r.get("impressions", 0) for r in gsc_data)
    return {"clicks": clicks, "impressions": impressions}


def _daily_avg(history: list[dict], days: int) -> dict:
    if not history:
        return {"clicks": 0, "impressions": 0}
    recent = sorted(history, key=lambda h: h.get("date", ""), reverse=True)[:days]
    if not recent:
        return {"clicks": 0, "impressions": 0}
    return {
        "clicks": sum(h.get("clicks", 0) for h in recent) / len(recent),
        "impressions": sum(h.get("impressions", 0) for h in recent) / len(recent),
    }


def _cooldown_ok(last_alerts: dict, metric: str) -> bool:
    last = last_alerts.get(metric)
    if not last:
        return True
    try:
        last_dt = datetime.datetime.fromisoformat(last)
        return (datetime.datetime.utcnow() - last_dt).total_seconds() > ALERT_COOLDOWN_HOURS * 3600
    except Exception:
        return True


def main():
    telegram_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    gsc_site = os.environ.get("GSC_SITE_URL", "")
    today = datetime.date.today()

    # Завантажуємо дані GSC за сьогодні (останній день)
    try:
        gsc_data = get_search_console_data(days=1)
    except Exception as e:
        print(f"GSC error: {e}")
        return

    totals = _get_today_totals(gsc_data)
    history = load_json("history.json", default=[])
    last_alerts = load_json("data/anomaly_last_alert.json", default={})

    avg = _daily_avg(history, LOOKBACK_DAYS)

    alerts = []
    for metric in ("clicks", "impressions"):
        curr = totals[metric]
        baseline = avg[metric]
        if baseline < 1:
            continue
        delta = (curr - baseline) / baseline
        if abs(delta) >= DELTA_THRESHOLD and _cooldown_ok(last_alerts, metric):
            direction = "📈 Зріст" if delta > 0 else "📉 Падіння"
            label = "кліків" if metric == "clicks" else "показів"
            alerts.append(
                f"{direction} {label}: {curr:.0f} vs середнє {baseline:.0f} ({delta:+.0%})"
            )
            last_alerts[metric] = datetime.datetime.utcnow().isoformat()

    if alerts:
        msg = "🚨 SEO-АНОМАЛІЯ ({}):\n\n{}".format(
            today.isoformat(), "\n".join(alerts)
        )
        send_message(telegram_token, chat_id, msg)
        save_json("data/anomaly_last_alert.json", last_alerts)
    else:
        print(f"No anomaly detected. clicks={totals['clicks']}, impressions={totals['impressions']}")


if __name__ == "__main__":
    main()
