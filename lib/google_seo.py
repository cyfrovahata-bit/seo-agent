"""
Зчитування даних з Google Search Console API та GA4 Data API.
Авторизація — через service account (JSON-ключ), якому надано
ТІЛЬКИ права на читання (Restricted user у Search Console,
Viewer у GA4 Property Access Management). Запис у Google-сервіси
цьому агенту не потрібен взагалі.
"""

import json

from google.oauth2 import service_account
from googleapiclient.discovery import build
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange, Dimension, Metric, RunReportRequest,
    FilterExpression, Filter,
)

SCOPES = [
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/analytics.readonly",
]


def _credentials(service_account_json: str):
    info = json.loads(service_account_json)
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def get_search_console_data(service_account_json: str, site_url: str,
                             start_date: str, end_date: str, row_limit: int = 100) -> list[dict]:
    """Топ запитів за період: запит, кліки, показники, CTR, середня позиція."""
    creds = _credentials(service_account_json)
    service = build("searchconsole", "v1", credentials=creds)
    body = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": ["query", "page"],
        "rowLimit": row_limit,
    }
    try:
        response = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
    except Exception as e:
        print(f"Search Console API error: {e}")
        return []
    rows = response.get("rows", [])
    return [
        {
            "query": r["keys"][0],
            "page": r["keys"][1],
            "clicks": r["clicks"],
            "impressions": r["impressions"],
            "ctr": round(r["ctr"] * 100, 2),
            "position": round(r["position"], 1),
        }
        for r in rows
    ]


def get_ga4_events(service_account_json: str, property_id: str,
                    start_date: str, end_date: str) -> list[dict]:
    """Підрахунок усіх подій GA4 за період. Це безкоштовний спосіб бачити
    сигнали про ліди/конверсії (form_submit, click, generate_lead тощо),
    навіть якщо в GA4 ще не позначені офіційні 'Key events' — Claude сам
    визначає зі списку назв подій, що виглядає як лід."""
    creds = _credentials(service_account_json)
    client = BetaAnalyticsDataClient(credentials=creds)
    request = RunReportRequest(
        property=f"properties/{property_id}",
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        dimensions=[Dimension(name="eventName")],
        metrics=[Metric(name="eventCount")],
        limit=50,
    )
    response = client.run_report(request)
    return [
        {"name": row.dimension_values[0].value, "count": int(row.metric_values[0].value)}
        for row in response.rows
    ]
def get_ga4_data(service_account_json: str, property_id: str,
                  start_date: str, end_date: str) -> list[dict]:
    """Трафік по сторінках: сесії, користувачі, показник відмов, середній час."""
    creds = _credentials(service_account_json)
    client = BetaAnalyticsDataClient(credentials=creds)
    request = RunReportRequest(
        property=f"properties/{property_id}",
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        dimensions=[Dimension(name="pagePath")],
        metrics=[
            Metric(name="sessions"),
            Metric(name="activeUsers"),
            Metric(name="bounceRate"),
            Metric(name="averageSessionDuration"),
        ],
        limit=100,
    )
    try:
        response = client.run_report(request)
    except Exception as e:
        print(f"GA4 API error: {e}")
        return []
    result = []
    for row in response.rows:
        result.append({
            "page": row.dimension_values[0].value,
            "sessions": int(row.metric_values[0].value),
            "users": int(row.metric_values[1].value),
            "bounce_rate": round(float(row.metric_values[2].value) * 100, 1),
            "avg_duration_sec": round(float(row.metric_values[3].value), 1),
        })
    return result


def get_ga4_page_conversions(service_account_json: str, property_id: str,
                              start_date: str, end_date: str) -> dict:
    """Конверсійні події по кожній сторінці.
    Повертає: {"/page/path/": {"phone_click": N, "telegram_click": N, "form_submit": N, "total": N}}
    """
    creds = _credentials(service_account_json)
    client = BetaAnalyticsDataClient(credentials=creds)
    request = RunReportRequest(
        property=f"properties/{property_id}",
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        dimensions=[Dimension(name="pagePath"), Dimension(name="eventName")],
        metrics=[Metric(name="eventCount")],
        dimension_filter=FilterExpression(
            filter=Filter(
                field_name="eventName",
                in_list_filter=Filter.InListFilter(
                    values=["phone_click", "telegram_click", "form_submit"]
                ),
            )
        ),
        limit=500,
    )
    try:
        response = client.run_report(request)
    except Exception as e:
        print(f"GA4 page conversions error: {e}")
        return {}
    result: dict = {}
    for row in response.rows:
        page = row.dimension_values[0].value
        event = row.dimension_values[1].value
        count = int(row.metric_values[0].value)
        if page not in result:
            result[page] = {"phone_click": 0, "telegram_click": 0, "form_submit": 0, "total": 0}
        result[page][event] = result[page].get(event, 0) + count
        result[page]["total"] += count
    return result


def get_ga4_traffic_channels(service_account_json: str, property_id: str,
                              start_date: str, end_date: str) -> dict:
    """Канали трафіку по всьому сайту.
    Повертає: {"Organic Search": {"sessions": N, "users": N}, "Direct": {...}, ...}
    """
    creds = _credentials(service_account_json)
    client = BetaAnalyticsDataClient(credentials=creds)
    request = RunReportRequest(
        property=f"properties/{property_id}",
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        dimensions=[Dimension(name="sessionDefaultChannelGrouping")],
        metrics=[Metric(name="sessions"), Metric(name="activeUsers")],
        limit=20,
    )
    try:
        response = client.run_report(request)
    except Exception as e:
        print(f"GA4 traffic channels error: {e}")
        return {}
    result: dict = {}
    for row in response.rows:
        channel = row.dimension_values[0].value
        result[channel] = {
            "sessions": int(row.metric_values[0].value),
            "users": int(row.metric_values[1].value),
        }
    return result
