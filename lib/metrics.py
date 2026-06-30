"""
Спільні функції для аналітика й виконавця: агрегація метрик по всьому сайту
і пошук метрик конкретної сторінки (потрібно для оцінки "до/після" ефекту змін).
"""

IMPACT_REVIEW_DAYS = 14  # скільки чекати після /published, перш ніж оцінювати ефект


def aggregate_site_totals(gsc_data: list[dict], ga4_data: list[dict]) -> dict:
    return {
        "clicks": sum(r["clicks"] for r in gsc_data),
        "impressions": sum(r["impressions"] for r in gsc_data),
        "sessions": sum(r["sessions"] for r in ga4_data),
        "users": sum(r["users"] for r in ga4_data),
    }


def find_page_metrics(page_path: str, gsc_data: list[dict], ga4_data: list[dict]) -> dict:
    """Метрики конкретної сторінки за період (для baseline і для порівняння ефекту)."""
    if not page_path:
        return {"clicks": 0, "impressions": 0, "sessions": 0, "users": 0,
                "avg_duration_sec": 0.0, "bounce_rate": 0.0}

    def _path_matches(row_page: str) -> bool:
        from urllib.parse import urlparse
        row_path = urlparse(row_page).path.rstrip("/") or "/"
        target = page_path.rstrip("/") or "/"
        return row_path == target

    gsc_rows = [r for r in gsc_data if _path_matches(r.get("page", ""))]
    ga4_rows = [r for r in ga4_data if _path_matches(r.get("page", ""))]
    avg_dur = (sum(r.get("avg_duration_sec", 0) for r in ga4_rows) / len(ga4_rows)) if ga4_rows else 0.0
    avg_bounce = (sum(r.get("bounce_rate", 0) for r in ga4_rows) / len(ga4_rows)) if ga4_rows else 0.0
    return {
        "clicks": sum(r["clicks"] for r in gsc_rows),
        "impressions": sum(r["impressions"] for r in gsc_rows),
        "sessions": sum(r["sessions"] for r in ga4_rows),
        "users": sum(r.get("users", 0) for r in ga4_rows),
        "avg_duration_sec": round(avg_dur, 1),
        "bounce_rate": round(avg_bounce, 1),
    }


def build_page_funnel(page_path: str, gsc_data: list[dict], ga4_data: list[dict],
                      page_conversions: dict, channels_for_page: dict) -> dict:
    """Повна воронка для однієї сторінки: GSC → GA4 → Конверсії."""
    base = find_page_metrics(page_path, gsc_data, ga4_data)
    # GA4 може повертати шляхи з або без trailing slash — пробуємо обидва
    conv = page_conversions.get(page_path) or page_conversions.get(page_path.rstrip("/") + "/") or page_conversions.get(page_path.rstrip("/")) or {}
    total_conv = conv.get("total", 0)
    sessions = base["sessions"]
    impressions = base["impressions"]
    clicks = base["clicks"]
    return {
        "page": page_path,
        "impressions": impressions,
        "ctr": round(clicks / impressions * 100, 2) if impressions else 0.0,
        "clicks": clicks,
        "sessions": sessions,
        "users": base["users"],
        "avg_duration_sec": base["avg_duration_sec"],
        "bounce_rate": base["bounce_rate"],
        "phone_click": conv.get("phone_click", 0),
        "telegram_click": conv.get("telegram_click", 0),
        "form_submit": conv.get("form_submit", 0),
        "total_conversions": total_conv,
        "conversion_rate": round(total_conv / sessions * 100, 2) if sessions else 0.0,
        "channels": channels_for_page,
    }


def score_page_priority(funnel: dict) -> float:
    """Оцінює бізнес-цінність сторінки. Більше = важливіша."""
    conv = funnel.get("total_conversions", 0)
    sessions = funnel.get("sessions", 0)
    avg_dur = funnel.get("avg_duration_sec", 0)
    return conv * 10 + sessions * 1 + min(avg_dur / 60, 5) * 0.5
