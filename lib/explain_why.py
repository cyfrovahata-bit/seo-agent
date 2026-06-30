"""
Explain Why Engine — детермінований аналіз причин змін SEO-метрик.

Не використовує Claude API і не вигадує причини.
Кожне пояснення має рівень впевненості (1-5 зірок) і посилання на конкретні дані.
Повертає форматований текстовий блок для інжекції в Claude-промпт.
"""

import datetime
from urllib.parse import urlparse


# --- Допоміжні функції ---

def _pct(new_val: float, old_val: float) -> float | None:
    if not old_val:
        return None
    return round((new_val - old_val) / old_val * 100, 1)


def _stars(confidence: int) -> str:
    """1-5 → ★★★★★ / ★★★★☆ тощо"""
    confidence = max(1, min(5, confidence))
    return "★" * confidence + "☆" * (5 - confidence)


def _path(url: str) -> str:
    p = urlparse(url).path
    return p.rstrip("/") or "/"


def _days_ago(date_iso: str, today: datetime.date) -> int:
    try:
        return (today - datetime.date.fromisoformat(date_iso)).days
    except Exception:
        return 9999


# --- Підфункції аналізу ---

def _analyze_site_totals(current: dict, history: list[dict], mode: str) -> list[dict]:
    """Знаходить суттєві зміни на рівні сайту (кліки, покази, сесії)."""
    relevant = [e for e in history if e.get("mode") == mode]
    if not relevant:
        return []
    prev = relevant[-1]["site_totals"]
    findings = []
    for key, label in [
        ("clicks", "кліки"),
        ("impressions", "покази"),
        ("sessions", "сесії"),
        ("users", "користувачі"),
    ]:
        pct = _pct(current.get(key, 0), prev.get(key, 0))
        if pct is None or abs(pct) < 15:
            continue
        findings.append({
            "metric": key,
            "label": label,
            "prev": prev.get(key, 0),
            "curr": current.get(key, 0),
            "pct": pct,
        })
    return findings


def _analyze_keyword_changes(keyword_history: dict) -> list[dict]:
    """Аналізує значні зміни позицій ключових слів."""
    changes = []
    for query, entries in keyword_history.items():
        if len(entries) < 2:
            continue
        prev = entries[-2]
        curr = entries[-1]
        diff = round(curr["position"] - prev["position"], 1)
        if abs(diff) < 3:
            continue
        changes.append({
            "query": query,
            "prev_pos": prev["position"],
            "curr_pos": curr["position"],
            "diff": diff,
            "clicks": curr["clicks"],
            "direction": "down" if diff > 0 else "up",
        })
    changes.sort(key=lambda x: abs(x["diff"]), reverse=True)
    return changes[:10]


def _find_recent_published(backlog: list[dict], today: datetime.date, window_days: int = 30) -> list[dict]:
    """Знаходить рекомендації, опубліковані за останні N днів."""
    recent = []
    for rec in backlog:
        if rec.get("status") == "published" and rec.get("published_date"):
            days = _days_ago(rec["published_date"], today)
            if days <= window_days:
                recent.append({**rec, "_days_ago": days})
    return recent


def _analyze_page_conversions(ga4_data: list[dict], ga4_events: list[dict]) -> list[dict]:
    """Сторінки з трафіком але без конверсій (phone_click, telegram_click, form_submit)."""
    # Загальна кількість конверсій по сайту
    conversion_events = {
        e["name"]: e["count"]
        for e in ga4_events
        if e["name"] in ("phone_click", "telegram_click", "form_submit")
    }
    total_conversions = sum(conversion_events.values())

    issues = []
    for page in ga4_data:
        sessions = page.get("sessions", 0)
        if sessions < 10:
            continue
        avg_duration = page.get("avg_session_duration", 0)
        issues.append({
            "page": page.get("page", ""),
            "sessions": sessions,
            "avg_duration": round(avg_duration, 0),
            "total_site_conversions": total_conversions,
            "conversion_events": conversion_events,
        })
    issues.sort(key=lambda x: -x["sessions"])
    return issues[:5]


def _analyze_pagespeed(technical_data: dict | None) -> list[dict]:
    """Знаходить сторінки з критично низьким PageSpeed."""
    if not technical_data or "error" in technical_data:
        return []
    slow_pages = []
    for page_url, data in technical_data.items():
        if not isinstance(data, dict):
            continue
        perf = data.get("performance", 100)
        if perf < 50:
            slow_pages.append({
                "page": _path(page_url),
                "performance": perf,
                "lcp": data.get("lcp"),
            })
    return slow_pages


def _analyze_query_appearance(keyword_history: dict, current_gsc: list[dict]) -> list[dict]:
    """Знаходить нові запити, яких раніше не було в history."""
    current_queries = {r["query"] for r in current_gsc}
    known_queries = set(keyword_history.keys())
    # Запити які з'явились в поточному звіті і мають мало історичних записів (≤1)
    new_queries = []
    for q in current_queries:
        entries = keyword_history.get(q, [])
        if len(entries) <= 1:
            row = next((r for r in current_gsc if r["query"] == q), {})
            impressions = row.get("impressions", 0)
            if impressions >= 3:
                new_queries.append({"query": q, "impressions": impressions, "clicks": row.get("clicks", 0)})
    new_queries.sort(key=lambda x: -x["impressions"])
    return new_queries[:8]


# --- Основна функція ---

def build_explain_why(
    current_totals: dict,
    current_gsc: list[dict],
    current_ga4: list[dict],
    history: list[dict],
    keyword_history: dict,
    backlog: list[dict],
    ga4_events: list[dict],
    technical_data: dict | None,
    mode: str,
    today: datetime.date,
) -> str:
    """
    Повертає текстовий блок з поясненнями причин змін для інжекції в Claude-промпт.
    Всі твердження базуються на наявних даних, кожне має рівень впевненості.
    """
    lines = ["\n🧠 АНАЛІЗ ПРИЧИН ЗМІН (дані для пояснення «чому так сталося»):"]
    has_content = False

    # 1. Зміни на рівні сайту
    site_changes = _analyze_site_totals(current_totals, history, mode)
    kw_changes = _analyze_keyword_changes(keyword_history)
    recent_published = _find_recent_published(backlog, today, window_days=30)
    new_queries = _analyze_query_appearance(keyword_history, current_gsc)
    slow_pages = _analyze_pagespeed(technical_data)

    for change in site_changes:
        has_content = True
        direction = "↑" if change["pct"] > 0 else "↓"
        lines.append(
            f"\n  {direction} {change['label'].capitalize()} "
            f"{'зросли' if change['pct'] > 0 else 'впали'} "
            f"({change['pct']:+.0f}%): {change['prev']} → {change['curr']}"
        )
        causes = []

        if change["metric"] in ("clicks", "impressions"):
            # Позиції впали → кліків менше
            drops = [k for k in kw_changes if k["direction"] == "down" and k["clicks"] > 0]
            if drops and change["pct"] < 0:
                for kw in drops[:3]:
                    causes.append((5, f"позиція за «{kw['query']}» впала: {kw['prev_pos']} → {kw['curr_pos']} ({kw['diff']:+.1f} позицій)"))

            # Позиції зросли → більше кліків
            gains = [k for k in kw_changes if k["direction"] == "up"]
            if gains and change["pct"] > 0:
                for kw in gains[:3]:
                    causes.append((5, f"позиція за «{kw['query']}» покращилась: {kw['prev_pos']} → {kw['curr_pos']} ({kw['diff']:+.1f} позицій)"))

            # Нові запити → покази зросли
            if new_queries and change["metric"] == "impressions" and change["pct"] > 0:
                q_list = ", ".join(f"«{q['query']}»" for q in new_queries[:3])
                causes.append((5, f"Google почав показувати сайт за новими запитами: {q_list}"))

            # Недавня публікація → ефект
            if recent_published and change["pct"] > 0:
                for pub in recent_published[:2]:
                    if pub.get("type") == "content" and pub.get("action") == "create_new":
                        causes.append((4, f"стаття «{pub['title']}» опублікована {pub['_days_ago']} днів тому — може давати покази"))
                    elif pub.get("action") == "edit_existing":
                        causes.append((3, f"правка «{pub['title']}» ({pub['_days_ago']} днів тому) — очікуємо реакцію Google"))

            # Технічні проблеми → падіння
            if slow_pages and change["pct"] < 0:
                causes.append((2, f"низька швидкість завантаження ({len(slow_pages)} сторінок з PageSpeed < 50) — може впливати на ранжування"))

        if change["metric"] in ("sessions", "users"):
            if change["pct"] < 0:
                drops = [k for k in kw_changes if k["direction"] == "down"]
                if drops:
                    causes.append((4, f"позиції за {len(drops)} запитами впали — менше органічного трафіку"))
                if slow_pages:
                    causes.append((3, f"повільне завантаження ({len(slow_pages)} сторінок < 50 балів) — частина відвідувачів не чекає"))
            if change["pct"] > 0:
                if new_queries:
                    causes.append((4, f"нові запити дають додаткові переходи ({len(new_queries)} нових)"))
                for pub in recent_published[:1]:
                    if pub.get("type") == "content":
                        causes.append((4, f"нова стаття «{pub['title']}» ({pub['_days_ago']} днів) приводить відвідувачів"))

        for stars, reason in causes:
            lines.append(f"    {_stars(stars)} {reason}")

        if not causes:
            lines.append("    (недостатньо даних для визначення конкретної причини)")

    # 2. Значні зміни позицій окремих запитів
    if kw_changes:
        has_content = True
        lines.append("\n  📍 Значні зміни позицій запитів:")
        for kw in kw_changes[:5]:
            arrow = "↓" if kw["direction"] == "down" else "↑"
            lines.append(
                f"    {arrow} «{kw['query']}»: {kw['prev_pos']} → {kw['curr_pos']} "
                f"({kw['diff']:+.1f}) | кліків: {kw['clicks']}"
            )
            # Причини зміни позиції
            if kw["direction"] == "down":
                for pub in recent_published:
                    if pub.get("target_page_path") and kw.get("query", "") in pub.get("title", "").lower():
                        lines.append(f"      {_stars(3)} правка «{pub['title']}» — можлива реакція Google на зміни")
                if slow_pages:
                    lines.append(f"      {_stars(2)} технічні проблеми можуть впливати на позиції")

    # 3. Нові запити (чому ростуть покази)
    if new_queries:
        has_content = True
        lines.append(f"\n  🆕 Нові запити (Google почав показувати за ними вперше):")
        for q in new_queries[:5]:
            lines.append(f"    • «{q['query']}» — {q['impressions']} показів, {q['clicks']} кліків")
        # Пов'язати з нещодавніми публікаціями
        for pub in recent_published:
            if pub.get("type") == "content":
                lines.append(
                    f"    {_stars(4)} {pub['_days_ago']} днів тому опубліковано «{pub['title']}» — "
                    f"нові запити можуть бути з цієї статті"
                )

    # 4. Сторінки з трафіком але проблемами конверсії
    conv_issues = _analyze_page_conversions(current_ga4, ga4_events)
    if conv_issues:
        has_content = True
        lines.append("\n  🎯 Аналіз конверсій по сторінках:")
        total_conv = conv_issues[0]["total_site_conversions"] if conv_issues else 0
        conv_events = conv_issues[0]["conversion_events"] if conv_issues else {}
        lines.append(
            f"    По всьому сайту за період: "
            f"заявок={conv_events.get('form_submit', 0)}, "
            f"Telegram-кліків={conv_events.get('telegram_click', 0)}, "
            f"телефон={conv_events.get('phone_click', 0)}"
        )
        if total_conv == 0:
            lines.append(f"    {_stars(5)} 0 конверсій за весь період — CTA не клікають або форма не відстежується")
        for page in conv_issues[:3]:
            if page["avg_duration"] < 30:
                lines.append(
                    f"    {_stars(4)} {page['page']} ({page['sessions']} сесій, "
                    f"{page['avg_duration']}с) — люди не читають, "
                    f"можлива невідповідність контенту запиту або повільне завантаження"
                )
            elif page["avg_duration"] > 120 and total_conv == 0:
                lines.append(
                    f"    {_stars(4)} {page['page']} ({page['sessions']} сесій, "
                    f"{page['avg_duration']}с) — читають довго але не конвертують, "
                    f"CTA може бути занадто низько або недостатньо помітний"
                )

    # 5. Технічні проблеми
    if slow_pages:
        has_content = True
        lines.append("\n  ⚡ Технічні причини:")
        for sp in slow_pages[:3]:
            lines.append(
                f"    {_stars(4)} {sp['page']}: PageSpeed {sp['performance']}/100 — "
                f"повільне завантаження впливає на відмови та ранжування"
            )

    # 6. Ефект нещодавніх змін
    if recent_published:
        has_content = True
        lines.append("\n  📌 Нещодавні зміни та їх можливий ефект:")
        for pub in recent_published:
            days = pub["_days_ago"]
            page = pub.get("target_page_path") or "новий контент"
            if days < 7:
                conf, comment = 2, "надто рано — Google ще не переіндексував"
            elif days < 14:
                conf, comment = 3, "рано для оцінки — зазвичай ефект видно після 14-30 днів"
            elif days < 30:
                conf, comment = 4, "оптимальний час для оцінки ефекту"
            else:
                conf, comment = 5, "ефект вже повністю проявився"
            lines.append(
                f"    {_stars(conf)} «{pub['title']}» ({page}) — {days} днів тому. {comment}"
            )

    if not has_content:
        lines.append("  (недостатньо даних для аналізу причин — потрібна більша historія)")

    lines.append(
        "\n  ⚠️ Інструкція для Claude: використовуй ці дані у звіті, щоб пояснювати ЧОМУ "
        "відбулись зміни. Не вигадуй причини — тільки ті що підкріплені даними вище. "
        "Додавай блок «🧠 Чому це сталося» після пояснення кожної суттєвої зміни."
    )

    return "\n".join(lines)
