"""
Decision Engine — центральний шар прийняття рішень SEO-агента.

Отримує результати аналізу (ExplainResult + стан системи) і перетворює їх
на пріоритезований план дій з оцінками Priority Score, ROI та впевненості.

НЕ аналізує сирі дані — лише синтезує:
  - ExplainResult (з explain_why.py)
  - backlog (recommendations.json)
  - learning_log (learning_log.json)
  - funnel_pages (вже в ExplainResult)
"""

import datetime
from dataclasses import dataclass, field


# ─────────────────────────────────────────────
# Константи
# ─────────────────────────────────────────────

# Орієнтовний час виконання (хвилини) для кожного типу причини
EFFORT_MINUTES: dict[str, int] = {
    "low_ctr_title": 15,
    "title_change_ctr": 15,
    "telegram_preferred": 20,
    "cta_too_low": 20,
    "engagement_no_cta": 25,
    "content_improvement_effect": 20,
    "position_improvement": 20,
    "new_content_indexed": 25,
    "no_cta_clicks": 30,
    "ctr_organic_drop": 30,
    "clicks_no_sessions": 45,
    "new_queries": 45,
    "edit_caused_position_drop": 45,
    "content_mismatch": 60,
    "low_engagement_bounce": 60,
    "organic_traffic_drop": 60,
    "position_drop": 90,
    "pagespeed_low": 120,
    "pagespeed_bounce": 120,
    "algorithm_fluctuation": 0,  # нічого робити — чекати
}

RISK_LEVEL: dict[str, str] = {
    "low_ctr_title": "low",
    "title_change_ctr": "low",
    "telegram_preferred": "low",
    "engagement_no_cta": "low",
    "cta_too_low": "low",
    "new_content_indexed": "low",
    "content_improvement_effect": "low",
    "position_improvement": "low",
    "no_cta_clicks": "low",
    "new_queries": "medium",
    "low_engagement_bounce": "medium",
    "content_mismatch": "medium",
    "ctr_organic_drop": "medium",
    "position_drop": "medium",
    "edit_caused_position_drop": "medium",
    "organic_traffic_drop": "medium",
    "algorithm_fluctuation": "low",
    "clicks_no_sessions": "medium",
    "pagespeed_low": "high",
    "pagespeed_bounce": "high",
}

RISK_PENALTY: dict[str, float] = {"low": 0.0, "medium": 5.0, "high": 15.0}

# Альтернативні дії якщо основна рекомендація неефективна
ALTERNATIVES: dict[str, list[str]] = {
    "low_ctr_title": ["Покращити H1 + перший абзац", "Оновити meta description", "Додати FAQ-блок"],
    "title_change_ctr": ["Повернути попередній title", "Додати ключове слово в H1"],
    "engagement_no_cta": ["Перемістити CTA вище на сторінці", "Додати блок з відгуками", "Додати FAQ"],
    "cta_too_low": ["Перемістити CTA вище на сторінці", "Додати Telegram-кнопку"],
    "content_mismatch": ["Переписати вступний абзац", "Додати структуровані дані", "Покращити H2/H3"],
    "position_drop": ["Наростити внутрішні посилання", "Оновити контент +500 слів", "Покращити H2-структуру"],
    "pagespeed_low": ["Оптимізувати зображення", "Увімкнути кешування", "Перейти на швидший хостинг"],
    "no_cta_clicks": ["Додати Telegram-кнопку", "Розмістити CTA в середині тексту", "Додати спливаюче вікно"],
    "clicks_no_sessions": ["Перевірити редиректи 301", "Перевірити GA4-тег на сторінці", "Перевірити hreflang"],
    "telegram_preferred": ["Додати Telegram-посилання в header", "Додати sticky Telegram-кнопку"],
    "new_queries": ["Створити окрему сторінку під кластер", "Оновити наявний контент під нові запити"],
    "low_engagement_bounce": ["Переписати вступ (читач має одразу знайти відповідь)", "Скоротити текст вдвічі"],
}


# ─────────────────────────────────────────────
# Типи даних
# ─────────────────────────────────────────────

@dataclass
class ActionItem:
    title: str
    priority_score: float          # 0–100
    urgency: str                   # "today" | "week" | "plan" | "idea"
    cause_type: str
    page: str | None
    effort_minutes: int
    risk: str                      # "low" | "medium" | "high"
    roi_score: float
    expected_clicks_delta: int
    expected_conv_delta: float
    reason: str
    alternatives: list[str]
    rec_id: int | None = None


# ─────────────────────────────────────────────
# Внутрішні розрахунки
# ─────────────────────────────────────────────

def _learning_stats(cause_type: str, learning_log: list[dict], page: str | None = None) -> tuple[int, int]:
    """Повертає (кількість успіхів, загальна кількість) для cause_type.
    Якщо page вказано — спочатку шукає per-page записи, fallback до всіх."""
    if page:
        entries = [e for e in learning_log if e.get("cause_type") == cause_type and e.get("page") == page]
        if not entries:
            entries = [e for e in learning_log if e.get("cause_type") == cause_type]
    else:
        entries = [e for e in learning_log if e.get("cause_type") == cause_type]
    return sum(1 for e in entries if e.get("worked")), len(entries)


def _learning_confidence_factor(cause_type: str, learning_log: list[dict], page: str | None = None) -> float:
    """
    Множник 0.5–1.3 на основі успішності в learning_log.
    Немає даних → 1.0 (нейтрально).
    """
    successes, total = _learning_stats(cause_type, learning_log, page)
    if total == 0:
        return 1.0
    return 0.5 + (successes / total) * 0.8


def _estimate_clicks_delta(cause_type: str, funnel: dict | None) -> int:
    """Очікуваний приріст кліків від усунення причини."""
    impressions = (funnel or {}).get("impressions", 0)
    clicks = (funnel or {}).get("clicks", 0)
    if cause_type == "low_ctr_title":
        # CTR 2% → 4%: +2% від impressions
        return max(int(impressions * 0.02), 5)
    if cause_type == "title_change_ctr":
        return max(int(clicks * 0.3), 5)
    if cause_type in ("position_drop", "organic_traffic_drop"):
        return max(int(clicks * 0.3), 5)
    if cause_type in ("position_improvement", "content_improvement_effect"):
        return max(int(clicks * 0.2), 3)
    if cause_type in ("content_mismatch", "low_engagement_bounce"):
        return max(int(clicks * 0.1), 3)
    if cause_type in ("pagespeed_low", "pagespeed_bounce"):
        return max(int(impressions * 0.01), 5)
    if cause_type == "new_queries":
        return max(int(impressions * 0.015), 5)
    return max(int(clicks * 0.1), 2)


def _estimate_conv_delta(cause_type: str, funnel: dict | None) -> float:
    """Очікуваний приріст заявок від усунення причини."""
    sessions = (funnel or {}).get("sessions", 0)
    if cause_type in ("engagement_no_cta", "cta_too_low", "no_cta_clicks"):
        return round(sessions * 0.015, 1)
    if cause_type == "telegram_preferred":
        return round(sessions * 0.01, 1)
    if cause_type in ("low_ctr_title", "title_change_ctr"):
        return round(sessions * 0.005, 1)
    return 0.0


def _page_business_value(page: str | None, funnel_pages: list[dict]) -> float:
    """Business value (0–30) з існуючих page priority scores."""
    if not page or not funnel_pages:
        return 5.0
    from lib.metrics import score_page_priority
    for f in funnel_pages:
        if f.get("page") == page:
            return min(30.0, score_page_priority(f) * 0.15)
    return 5.0


def _roi(clicks_delta: int, conv_delta: float, effort_minutes: int) -> float:
    """ROI = бізнес-цінність / витрачений час (у годинах)."""
    if effort_minutes == 0:
        return 0.0
    value = clicks_delta * 0.3 + conv_delta * 10
    return round(value / max(effort_minutes / 60, 0.1), 1)


def _is_failed_duplicate(cause_type: str, page: str | None,
                          learning_log: list[dict], backlog: list[dict]) -> bool:
    """
    True якщо аналогічна рекомендація вже пробувалась ≥2 рази і завжди провалювалась.
    Не блокує якщо хоча б один раз спрацювало.
    """
    relevant = [e for e in learning_log if e.get("cause_type") == cause_type]
    if len(relevant) < 2:
        return False
    # Перевіряємо лише записи що прив'язані до тієї ж сторінки (якщо page відома)
    if page:
        page_rec_ids = {r.get("id") for r in backlog if r.get("target_page_path") == page}
        page_entries = [e for e in relevant if e.get("rec_id") in page_rec_ids]
        if page_entries and all(not e.get("worked") for e in page_entries):
            return True
    return False


def _age_boost(page: str | None, backlog: list[dict], today: datetime.date) -> float:
    """Старіші pending рекомендації отримують невеликий буст (max 10 балів)."""
    if not page:
        return 0.0
    for r in backlog:
        if r.get("target_page_path") == page and r.get("status") == "pending":
            created = r.get("created", "")
            if created:
                try:
                    days = (today - datetime.date.fromisoformat(created)).days
                    return min(10.0, days * 0.3)
                except ValueError:
                    pass
    return 0.0


def _classify_urgency(score: float, cause_type: str) -> str:
    """Класифікація терміновості на 4 рівні."""
    fast_wins = {"low_ctr_title", "engagement_no_cta", "telegram_preferred",
                 "cta_too_low", "title_change_ctr"}
    if score >= 70 and cause_type in fast_wins:
        return "today"
    if score >= 55:
        return "week"
    if score >= 30:
        return "plan"
    return "idea"


# ─────────────────────────────────────────────
# Витягування Cause-об'єктів з ExplainResult
# ─────────────────────────────────────────────

def _extract_causes(explain_result) -> list[tuple]:
    """
    Збирає всі Cause-об'єкти з ExplainResult.
    Повертає список: [(cause, page_path_or_None, funnel_dict_or_None)]
    """
    items = []

    # Зміни на рівні сайту
    for chain in explain_result.site_chains:
        for node in chain.nodes:
            for cause in node.causes:
                items.append((cause, None, None))

    # Зміни позицій запитів — тільки топ-причина
    for node in explain_result.query_nodes:
        for cause in node.causes[:1]:
            items.append((cause, None, None))

    # SEO воронка — найспецифічніша: є page + funnel
    for fp in explain_result.funnel_pages:
        for cause in fp.get("causes", []):
            items.append((cause, fp.get("page"), fp))

    return items


# ─────────────────────────────────────────────
# Побудова ActionItem
# ─────────────────────────────────────────────

def _build_action(
    cause,
    page: str | None,
    funnel: dict | None,
    funnel_pages: list[dict],
    learning_log: list[dict],
    backlog: list[dict],
    today: datetime.date,
) -> ActionItem | None:
    cause_type = cause.cause_type

    # Алгоритмічні коливання — не вимагають дій
    if cause_type == "algorithm_fluctuation":
        return None

    effort = EFFORT_MINUTES.get(cause_type, 30)
    risk = RISK_LEVEL.get(cause_type, "medium")
    exp_clicks = _estimate_clicks_delta(cause_type, funnel)
    exp_conv = _estimate_conv_delta(cause_type, funnel)

    # Складові Priority Score
    biz_value = _page_business_value(page, funnel_pages)   # 0–30
    traffic_pot = min(25.0, exp_clicks * 0.5)              # 0–25
    conv_pot = min(25.0, exp_conv * 15)                    # 0–25
    learn_factor = _learning_confidence_factor(cause_type, learning_log, page)
    learn_score = min(20.0, cause.confidence * 0.2 * learn_factor)  # 0–20
    age = _age_boost(page, backlog, today)                 # 0–10

    raw = biz_value + traffic_pot + conv_pot + learn_score + age

    # Штрафи
    difficulty_pen = min(20.0, effort / 10)
    risk_pen = RISK_PENALTY.get(risk, 5.0)
    dup_pen = 30.0 if _is_failed_duplicate(cause_type, page, learning_log, backlog) else 0.0

    priority_score = max(0.0, min(100.0, raw - difficulty_pen - risk_pen - dup_pen))
    roi = _roi(exp_clicks, exp_conv, effort)
    urgency = _classify_urgency(priority_score, cause_type)

    # Рядок причини
    parts = []
    if funnel:
        if funnel.get("impressions", 0) > 0:
            parts.append(f"{funnel['impressions']} показів")
        if funnel.get("sessions", 0) > 0:
            parts.append(f"{funnel['sessions']} сесій")
    successes, total = _learning_stats(cause_type, learning_log, page)
    if total > 0:
        parts.append(f"learning: {successes}/{total}")
    reason = cause.description
    if parts:
        reason += f" ({', '.join(parts)})"

    return ActionItem(
        title=cause.recommendation,
        priority_score=round(priority_score, 1),
        urgency=urgency,
        cause_type=cause_type,
        page=page,
        effort_minutes=effort,
        risk=risk,
        roi_score=roi,
        expected_clicks_delta=exp_clicks,
        expected_conv_delta=exp_conv,
        reason=reason[:150],
        alternatives=ALTERNATIVES.get(cause_type, [])[:2],
    )


# ─────────────────────────────────────────────
# Семантична дедублікація
# ─────────────────────────────────────────────

def _keyword_overlap(a: str, b: str, min_len: int = 4) -> bool:
    """True якщо рядки a і b мають спільне змістовне слово (≥4 символи)."""
    words_a = {w.lower() for w in a.split() if len(w) >= min_len}
    words_b = {w.lower() for w in b.split() if len(w) >= min_len}
    return bool(words_a & words_b)


def _semantic_dedup(actions: list[ActionItem]) -> tuple[list[ActionItem], list[str]]:
    """
    Якщо два різних ActionItem (різні cause_type) мають схожий title
    (є спільні ключові слова), залишаємо тільки той з вищим priority_score
    і генеруємо попередження.
    """
    kept: list[ActionItem] = []
    warnings: list[str] = []
    for action in actions:
        duplicate_of = None
        for existing in kept:
            if existing.cause_type != action.cause_type and _keyword_overlap(existing.title, action.title):
                duplicate_of = existing
                break
        if duplicate_of:
            if action.priority_score > duplicate_of.priority_score:
                kept.remove(duplicate_of)
                kept.append(action)
                warnings.append(
                    f"Схожі дії об'єднано: «{action.title[:40]}» замінює «{duplicate_of.title[:40]}» (вищий пріоритет)"
                )
            else:
                warnings.append(
                    f"Схожі дії пропущено: «{action.title[:40]}» → вже є «{duplicate_of.title[:40]}»"
                )
        else:
            kept.append(action)
    return kept, warnings


# ─────────────────────────────────────────────
# Виявлення конфліктів
# ─────────────────────────────────────────────

def _detect_conflicts(actions: list[ActionItem]) -> list[str]:
    """Знаходить конфлікти: кілька дій на одній сторінці, потенційна канібалізація."""
    conflicts = []
    page_acts: dict[str, list[ActionItem]] = {}
    for a in actions:
        if a.page:
            page_acts.setdefault(a.page, []).append(a)

    for page, acts in page_acts.items():
        if len(acts) > 1:
            first = acts[0].title[:40]
            conflicts.append(
                f"{page}: кілька змін одночасно — спочатку \"{first}\" (вищий пріоритет)"
            )

    # Канібалізація: нова стаття + оптимізація схожої сторінки
    new_content = [a for a in actions if "нову" in a.title.lower() and a.urgency in ("week", "plan")]
    existing_edits = [a for a in actions if a.page and a.urgency == "today"]
    if new_content and existing_edits:
        conflicts.append(
            "Нова стаття може конкурувати з існуючими сторінками — "
            "спочатку оптимізуйте існуючі, потім створюйте нові"
        )

    return conflicts


# ─────────────────────────────────────────────
# 30-денний план
# ─────────────────────────────────────────────

def _build_30day_plan(actions: list[ActionItem]) -> str:
    today_acts = [a for a in actions if a.urgency == "today"]
    week_acts = [a for a in actions if a.urgency == "week"]
    plan_acts = [a for a in actions if a.urgency == "plan"]

    def _titles(lst: list[ActionItem], n: int) -> str:
        items = lst[:n]
        return ", ".join(a.title[:28] for a in items) if items else "моніторинг"

    lines = [
        "\n📅 ПЛАН НА 30 ДНІВ:",
        f"  Тиждень 1: {_titles(today_acts, 3)}",
        f"  Тиждень 2: {_titles(week_acts, 2)}",
        f"  Тиждень 3: {_titles(week_acts[2:] or plan_acts, 2)}",
        f"  Тиждень 4: {_titles(plan_acts[2:], 2) if len(plan_acts) > 2 else 'аналіз ефектів + нові гіпотези'}",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────
# Загальна впевненість
# ─────────────────────────────────────────────

def _overall_confidence(actions: list[ActionItem], learning_log: list[dict]) -> tuple[int, str]:
    """Повертає (відсоток впевненості, пояснення)."""
    if not actions:
        return 40, "мало даних для формування плану"

    avg_score = sum(a.priority_score for a in actions) / len(actions)
    log_size = len(learning_log)
    base = int(avg_score * 0.7)
    log_boost = min(20, log_size * 2)
    pct = min(95, base + log_boost)

    if log_size == 0:
        reason = "немає прецедентів у learning_log — перші оцінки"
    elif log_size < 5:
        reason = f"мало прецедентів ({log_size} записів у learning_log)"
    elif avg_score >= 65:
        reason = f"є {log_size} прецедентів, середній пріоритет рекомендацій {avg_score:.0f}/100"
    else:
        reason = f"є дані ({log_size} записів), але середня впевненість у рекомендаціях {avg_score:.0f}/100"

    return pct, reason


# ─────────────────────────────────────────────
# Форматування
# ─────────────────────────────────────────────

def _stars(score: float) -> str:
    n = max(1, min(5, 1 + int(score // 22)))
    return "★" * n + "☆" * (5 - n)


def _format_action(idx: int, a: ActionItem, detailed: bool = True) -> str:
    risk_uk = {"low": "низький", "medium": "середній", "high": "високий"}.get(a.risk, a.risk)
    effort_str = f"{a.effort_minutes} хв" if a.effort_minutes else "—"

    effect_parts = []
    if a.expected_clicks_delta > 0:
        effect_parts.append(f"+{a.expected_clicks_delta} кліків")
    if a.expected_conv_delta > 0:
        effect_parts.append(f"+{a.expected_conv_delta:.1f} заявок")
    effect_str = ", ".join(effect_parts) or "важко оцінити"

    page_str = f" ({a.page})" if a.page else ""
    lines = [
        f"  {idx}. {a.title}{page_str}",
        f"     {_stars(a.priority_score)} {a.priority_score:.0f}/100 | ⏱ {effort_str} | ⚠️ {risk_uk} | ROI {a.roi_score}",
        f"     Ефект: {effect_str}",
    ]
    if detailed:
        lines.append(f"     Причина: {a.reason}")
        if a.alternatives:
            lines.append(f"     Альтернатива: {a.alternatives[0]}")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# Публічне API
# ─────────────────────────────────────────────

def build_decision_plan(
    explain_result,
    backlog: list[dict],
    learning_log: list[dict],
    today: datetime.date,
    mode: str = "daily",
) -> str:
    """
    Головна функція Decision Engine.

    Приймає ExplainResult (з explain_why.analyze()) + стан системи.
    Повертає відформатований рядок для Claude-промпту.
    Не звертається до Google API, WordPress або бази даних.
    """
    funnel_pages = explain_result.funnel_pages or []

    # 1. Витягуємо всі Cause з аналізу
    raw_causes = _extract_causes(explain_result)

    # 2. Будуємо ActionItem-и, дедублюємо за (cause_type, page)
    seen: set[tuple] = set()
    actions: list[ActionItem] = []
    for (cause, page, funnel) in raw_causes:
        key = (cause.cause_type, page)
        if key in seen:
            continue
        seen.add(key)
        item = _build_action(cause, page, funnel, funnel_pages, learning_log, backlog, today)
        if item:
            actions.append(item)

    # 3. Сортуємо за пріоритетом + семантична дедублікація
    actions.sort(key=lambda a: -a.priority_score)
    actions, sem_warnings = _semantic_dedup(actions)
    actions = actions[:20]  # не більше 20 дій у плані

    # 4. Розбиваємо по категоріях
    today_acts = [a for a in actions if a.urgency == "today"][:3]
    week_acts = [a for a in actions if a.urgency == "week"][:4]
    plan_acts = [a for a in actions if a.urgency == "plan"][:4]
    idea_acts = [a for a in actions if a.urgency == "idea"][:3]

    # 5. Конфлікти серед топ-дій
    conflicts = _detect_conflicts(today_acts + week_acts)

    # 6. 30-денний план
    plan_30 = _build_30day_plan(actions)

    # 7. Впевненість
    conf_pct, conf_reason = _overall_confidence(actions, learning_log)

    # ── Формуємо вивід ──
    lines = ["\n🎯 ПЛАН ДІЙ (Decision Engine):"]

    if not actions:
        lines.append("  (недостатньо даних для формування плану — потрібна більша historія)")
        return "\n".join(lines)

    idx = 1
    if today_acts:
        lines.append("\n🔥 ЗРОБИТИ СЬОГОДНІ:")
        for a in today_acts:
            lines.append(_format_action(idx, a, detailed=True))
            idx += 1

    if week_acts:
        lines.append("\n⚡ ЦЬОГО ТИЖНЯ:")
        for a in week_acts:
            lines.append(_format_action(idx, a, detailed=True))
            idx += 1

    if plan_acts:
        lines.append("\n📅 ЗАПЛАНУВАТИ:")
        for a in plan_acts:
            lines.append(_format_action(idx, a, detailed=False))
            idx += 1

    if idea_acts:
        lines.append("\n💡 ІДЕЇ:")
        for a in idea_acts:
            page_str = f" ({a.page})" if a.page else ""
            lines.append(f"  {idx}. {a.title}{page_str}")
            idx += 1

    if conflicts or sem_warnings:
        lines.append("\n⚠️ КОНФЛІКТИ:")
        for c in conflicts:
            lines.append(f"  — {c}")
        for w in sem_warnings:
            lines.append(f"  — {w}")

    lines.append(plan_30)
    lines.append(f"\n📊 ВПЕВНЕНІСТЬ АГЕНТА: {conf_pct}%")
    lines.append(f"   Причина: {conf_reason}")
    lines.append(
        "\n  ⚠️ Інструкція для Claude: використовуй цей план як основу розділу «🎯 Що робити далі». "
        "Не дублюй пункти з бэклогу. Адаптуй мову для власника бізнесу (без технічного жаргону)."
    )

    return "\n".join(lines)
