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
        return {"clicks": 0, "impressions": 0, "sessions": 0}
    # Точний збіг шляху (не підрядковий) щоб /seo/ не матчило /seo-prosuvanya/
    def _path_matches(row_page: str) -> bool:
        from urllib.parse import urlparse
        row_path = urlparse(row_page).path.rstrip("/") or "/"
        target = page_path.rstrip("/") or "/"
        return row_path == target

    gsc_rows = [r for r in gsc_data if _path_matches(r.get("page", ""))]
    ga4_rows = [r for r in ga4_data if _path_matches(r.get("page", ""))]
    return {
        "clicks": sum(r["clicks"] for r in gsc_rows),
        "impressions": sum(r["impressions"] for r in gsc_rows),
        "sessions": sum(r["sessions"] for r in ga4_rows),
    }
