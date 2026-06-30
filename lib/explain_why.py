"""
Explain Why Engine v2 — центральний модуль прийняття рішень SEO-агента.

Архітектура:
  DataContext   — єдиний контейнер для всіх джерел даних (розширюваний)
  Evidence      — конкретний факт з джерелом і confidence (0-100)
  Cause         — причина зміни: evidence + recommendation + effect + risk
  CausalNode    — один вузол метрики з переліком причин
  CausalChain   — причинно-наслідковий ланцюг через кілька вузлів
  ExplainResult — повний результат аналізу

Публічне API (зворотно сумісне):
  build_data_context(...)  → DataContext
  build_explain_why(...)   → str  (для Claude-промпту)
  analyze_page(...)        → str  (глибокий аналіз однієї сторінки)
  analyze_for_impact_review(...) → str  (ефект конкретної рекомендації)

Принципи:
  - Жодних Claude API викликів — тільки детерміновані правила
  - Кожна Evidence містить source і конкретні числа
  - Confidence коригується через learning_log
  - Нові джерела даних додаються через DataContext без змін правил
"""

import datetime
from dataclasses import dataclass, field
from urllib.parse import urlparse

from lib.metrics import find_page_metrics


# ─────────────────────────────────────────────
# Типи даних
# ─────────────────────────────────────────────

@dataclass
class Evidence:
    text: str           # "позиція впала 8 → 12"
    source: str         # "GSC" | "GA4" | "keyword_history" | "recommendations" | "PageSpeed"
    confidence: int     # 0–100


@dataclass
class Cause:
    cause_type: str             # ідентифікатор для learning_log: "position_drop", "new_content", …
    description: str            # людський опис причини
    evidence: list[Evidence]
    confidence: int             # 0–100, може бути скориговано learning_log
    recommendation: str         # що рекомендується зробити
    expected_effect: str        # очікуваний результат якщо виконати
    risk: str                   # ризик якщо не діяти або помилились


@dataclass
class CausalNode:
    metric: str             # "clicks", "impressions", "sessions", "ctr", "position", "conversions"
    label: str              # "Кліки"
    direction: str          # "up" | "down" | "stable"
    prev_val: float
    curr_val: float
    change_pct: float | None
    causes: list[Cause]


@dataclass
class CausalChain:
    title: str
    nodes: list[CausalNode]
    summary: str            # 1-2 речення підсумку ланцюга


@dataclass
class ExplainResult:
    site_chains: list[CausalChain]          # зміни на рівні сайту
    page_nodes: list[CausalNode]            # сторінки з проблемами
    query_nodes: list[CausalNode]           # зміни позицій запитів
    conversion_analysis: str               # аналіз конверсій
    recent_changes_analysis: list[str]     # ефект нещодавніх змін
    has_data: bool
    funnel_pages: list[dict] = field(default_factory=list)   # повна воронка по сторінках
    traffic_channels_text: str = ""
    page_rankings_text: str = ""


@dataclass
class DataContext:
    """
    Єдиний контейнер для всіх джерел даних.
    Щоб додати нове джерело (напр. Google Ads) — просто додай поле і передай у build_data_context().
    Правила аналізу читають з DataContext, тому не потребують змін.
    """
    # Поточний і попередній стан
    current_totals: dict
    prev_totals: dict | None
    # Search Console
    gsc_data: list[dict]
    # Google Analytics 4
    ga4_data: list[dict]
    ga4_events: list[dict]          # [{name, count}, …]
    # Keyword history
    keyword_history: dict           # {query: [{date, position, clicks, impressions}, …]}
    keyword_changes: list[dict]     # вже обраховані зміни позицій
    # Recommendations
    recent_published: list[dict]    # опубліковані за останні 30 днів
    backlog: list[dict]
    # Technical
    technical_data: dict | None     # {url: {performance, lcp, …}}
    # Learning log (persistence через data/learning_log.json)
    learning_log: list[dict]        # [{cause_type, worked, date}, …]
    # Meta
    mode: str
    today: datetime.date
    # Розширення: майбутні джерела (None = не підключено)
    google_ads_data: dict | None = None
    clarity_data: dict | None = None
    backlinks_data: dict | None = None
    # Воронка v3: конверсії по сторінках і канали трафіку
    page_conversions: dict = field(default_factory=dict)
    traffic_channels: dict = field(default_factory=dict)


# ─────────────────────────────────────────────
# Допоміжні функції
# ─────────────────────────────────────────────

def _pct(new_val: float, old_val: float) -> float | None:
    if not old_val:
        return None
    return round((new_val - old_val) / old_val * 100, 1)


def _path_from_url(url: str) -> str:
    p = urlparse(url).path
    return p.rstrip("/") or "/"


def _days_ago(date_iso: str, today: datetime.date) -> int:
    try:
        return (today - datetime.date.fromisoformat(date_iso)).days
    except Exception:
        return 9999


def _conf_bar(confidence: int) -> str:
    """0-100 → текстове відображення: 92% | ★★★★★"""
    stars = round(confidence / 20)
    stars = max(1, min(5, stars))
    return f"{confidence}% {'★' * stars}{'☆' * (5 - stars)}"


def _learning_boost(cause_type: str, learning_log: list[dict]) -> int:
    """
    Коригує confidence на основі того, чи спрацювала ця причина раніше.
    +10 за кожен підтверджений випадок, -8 за кожен спростований (max ±30).
    """
    relevant = [e for e in learning_log if e.get("cause_type") == cause_type]
    if not relevant:
        return 0
    delta = sum(10 if e.get("worked") else -8 for e in relevant)
    return max(-30, min(30, delta))


def _adjusted(base_confidence: int, cause_type: str, learning_log: list[dict]) -> int:
    return max(5, min(98, base_confidence + _learning_boost(cause_type, learning_log)))


# ─────────────────────────────────────────────
# Правила — кожна функція повертає list[Cause]
# ─────────────────────────────────────────────

def _rules_for_impressions_drop(ctx: DataContext) -> list[Cause]:
    causes = []
    drops = [k for k in ctx.keyword_changes if k["direction"] == "down" and k["clicks"] > 0]
    if drops:
        ev = [Evidence(
            f"позиція «{k['query']}»: {k['prev_pos']} → {k['curr_pos']} ({k['diff']:+.1f})",
            "keyword_history", 90
        ) for k in drops[:3]]
        causes.append(Cause(
            cause_type="position_drop",
            description="Позиції ключових запитів погіршились — менше показів",
            evidence=ev,
            confidence=_adjusted(90, "position_drop", ctx.learning_log),
            recommendation="Перевір контент сторінок з падінням позицій, оновити мета-теги або контент",
            expected_effect="Повернення позицій через 2-4 тижні після правки",
            risk="Без дій позиції продовжать падати",
        ))
    slow = _get_slow_pages(ctx)
    if slow:
        causes.append(Cause(
            cause_type="pagespeed_low",
            description="Повільне завантаження — Google знижує ранжування",
            evidence=[Evidence(f"{p['page']}: PageSpeed {p['performance']}/100", "PageSpeed", 65) for p in slow[:2]],
            confidence=_adjusted(65, "pagespeed_low", ctx.learning_log),
            recommendation="Оптимізувати зображення (WebP), мінімізувати JS/CSS",
            expected_effect="PageSpeed +20-40 балів → позитивний сигнал для ранжування",
            risk="Повільні сторінки також збільшують відмови",
        ))
    return causes


def _rules_for_impressions_growth(ctx: DataContext) -> list[Cause]:
    causes = []
    new_q = _get_new_queries(ctx)
    if new_q:
        q_list = ", ".join(f"«{q['query']}»" for q in new_q[:3])
        causes.append(Cause(
            cause_type="new_queries",
            description=f"Google почав показувати сайт за новими запитами: {q_list}",
            evidence=[Evidence(f"«{q['query']}»: {q['impressions']} показів (новий)", "GSC", 92) for q in new_q[:3]],
            confidence=_adjusted(92, "new_queries", ctx.learning_log),
            recommendation="Перевір ці запити — можливо варто розширити контент під них",
            expected_effect="Якщо клікають — зростання трафіку без додаткових дій",
            risk="Якщо запити нерелевантні — трафік буде з відмовами",
        ))
    for pub in ctx.recent_published[:2]:
        if pub.get("type") == "content" and pub.get("action") == "create_new":
            days = pub["_days_ago"]
            base_conf = 85 if 14 <= days <= 60 else (50 if days < 14 else 70)
            causes.append(Cause(
                cause_type="new_content_indexed",
                description=f"Стаття «{pub['title']}» ({days} днів тому) почала збирати покази",
                evidence=[Evidence(f"Опубліковано {pub.get('published_date', '?')}", "recommendations", base_conf)],
                confidence=_adjusted(base_conf, "new_content_indexed", ctx.learning_log),
                recommendation="Стежити за запитами з цієї статті, можливо додати внутрішні посилання",
                expected_effect="Поступове зростання протягом 1-3 місяців",
                risk="Якщо покази є але кліків немає — переробити title/description",
            ))
    gains = [k for k in ctx.keyword_changes if k["direction"] == "up"]
    if gains:
        ev = [Evidence(f"«{k['query']}»: {k['prev_pos']} → {k['curr_pos']} ({k['diff']:+.1f})", "keyword_history", 88) for k in gains[:3]]
        causes.append(Cause(
            cause_type="position_improvement",
            description="Позиції ключових запитів покращились",
            evidence=ev,
            confidence=_adjusted(88, "position_improvement", ctx.learning_log),
            recommendation="Зафіксувати що саме змінилось — можливо повторити підхід для інших сторінок",
            expected_effect="Подальше зростання кліків при стабільному CTR",
            risk="Позиції можуть коливатися, не рахувати зростання постійним до 30 днів стабільності",
        ))
    return causes


def _rules_for_clicks_drop(ctx: DataContext) -> list[Cause]:
    causes = _rules_for_impressions_drop(ctx)
    # CTR міг впасти при стабільних позиціях
    curr_clicks = ctx.current_totals.get("clicks", 0)
    curr_impr = ctx.current_totals.get("impressions", 0)
    if ctx.prev_totals:
        prev_clicks = ctx.prev_totals.get("clicks", 0)
        prev_impr = ctx.prev_totals.get("impressions", 0)
        curr_ctr = curr_clicks / curr_impr if curr_impr else 0
        prev_ctr = prev_clicks / prev_impr if prev_impr else 0
        if prev_ctr and (curr_ctr - prev_ctr) / prev_ctr < -0.15:
            # CTR впав — перевіряємо чи були правки title
            title_edits = [p for p in ctx.recent_published if "заголовок" in p.get("title", "").lower() or "title" in p.get("title", "").lower()]
            if title_edits:
                causes.append(Cause(
                    cause_type="title_change_ctr",
                    description=f"CTR впав після зміни заголовку «{title_edits[0]['title']}»",
                    evidence=[
                        Evidence(f"CTR: {prev_ctr:.1%} → {curr_ctr:.1%}", "GSC", 78),
                        Evidence(f"Правка {title_edits[0].get('published_date', '?')}", "recommendations", 70),
                    ],
                    confidence=_adjusted(75, "title_change_ctr", ctx.learning_log),
                    recommendation="Порівняй новий title з конкурентами. Якщо програє — повернути або переформулювати",
                    expected_effect="CTR +1-3% після покращення title",
                    risk="Якщо не реагувати — кліки продовжать падати навіть при хороших позиціях",
                ))
            else:
                causes.append(Cause(
                    cause_type="ctr_organic_drop",
                    description=f"CTR впав без видимих змін на сайті ({prev_ctr:.1%} → {curr_ctr:.1%})",
                    evidence=[Evidence(f"CTR: {prev_ctr:.1%} → {curr_ctr:.1%}", "GSC", 72)],
                    confidence=_adjusted(55, "ctr_organic_drop", ctx.learning_log),
                    recommendation="Перевір конкурентів у SERP — можливо з'явились нові або Google змінив формат видачі",
                    expected_effect="Оновлення title/description може повернути CTR",
                    risk="Низький CTR означає витрачений потенціал навіть при хороших позиціях",
                ))
    return causes


def _rules_for_sessions_drop(ctx: DataContext) -> list[Cause]:
    causes = []
    drops = [k for k in ctx.keyword_changes if k["direction"] == "down"]
    if drops:
        causes.append(Cause(
            cause_type="organic_traffic_drop",
            description=f"Позиції {len(drops)} запитів погіршились → менше органічного трафіку",
            evidence=[Evidence(f"«{k['query']}»: {k['prev_pos']} → {k['curr_pos']}", "keyword_history", 85) for k in drops[:3]],
            confidence=_adjusted(85, "organic_traffic_drop", ctx.learning_log),
            recommendation="Перевір сторінки цих запитів, оновити контент",
            expected_effect="Повернення трафіку через 2-6 тижнів після виправлення",
            risk="Втрата органічних відвідувачів знижує шанс на конверсії",
        ))
    slow = _get_slow_pages(ctx)
    if slow:
        causes.append(Cause(
            cause_type="pagespeed_bounce",
            description="Повільне завантаження — відвідувачі йдуть не дочекавшись",
            evidence=[Evidence(f"{p['page']}: {p['performance']}/100", "PageSpeed", 70) for p in slow[:2]],
            confidence=_adjusted(70, "pagespeed_bounce", ctx.learning_log),
            recommendation="Оптимізувати зображення та час до першого відображення (LCP)",
            expected_effect="Відмови -10-30% після оптимізації",
            risk="Кожна секунда затримки = -7% конверсій (середня статистика)",
        ))
    return causes


def _rules_for_zero_conversions(ctx: DataContext) -> list[Cause]:
    causes = []
    conv_events = {e["name"]: e["count"] for e in ctx.ga4_events if e["name"] in ("phone_click", "telegram_click", "form_submit")}
    total = sum(conv_events.values())
    high_traffic_pages = [p for p in ctx.ga4_data if p.get("sessions", 0) >= 10]
    if total == 0 and high_traffic_pages:
        causes.append(Cause(
            cause_type="no_cta_clicks",
            description="Жодного кліку по CTA за весь період — кнопки не працюють або погано видні",
            evidence=[
                Evidence(f"form_submit: {conv_events.get('form_submit', 0)}", "GA4", 95),
                Evidence(f"telegram_click: {conv_events.get('telegram_click', 0)}", "GA4", 95),
                Evidence(f"phone_click: {conv_events.get('phone_click', 0)}", "GA4", 95),
            ],
            confidence=_adjusted(92, "no_cta_clicks", ctx.learning_log),
            recommendation="Перевір: 1) чи видно кнопки на мобільному, 2) чи GTM коректно відстежує кліки, 3) розмістити CTA вище на сторінці",
            expected_effect="Навіть 1-2 конверсії на місяць = реальні клієнти",
            risk="Без конверсій весь SEO-трафік не дає бізнес-результату",
        ))
    for page in high_traffic_pages[:3]:
        avg_dur = page.get("avg_session_duration", 0)
        if avg_dur < 30:
            causes.append(Cause(
                cause_type="content_mismatch",
                description=f"{page.get('page', '')} — люди йдуть за {avg_dur:.0f}с, контент не відповідає запиту",
                evidence=[Evidence(f"{page['sessions']} сесій, {avg_dur:.0f}с середня тривалість", "GA4", 80)],
                confidence=_adjusted(78, "content_mismatch", ctx.learning_log),
                recommendation="Перевір з яких запитів приходять на цю сторінку, переконайся що вступний абзац відповідає їм",
                expected_effect="Час на сторінці +30-60с → більше шансів на конверсію",
                risk="Контент-мисматч також погіршує поведінкові сигнали і ранжування",
            ))
        elif avg_dur > 120 and total == 0:
            causes.append(Cause(
                cause_type="cta_too_low",
                description=f"{page.get('page', '')} — читають {avg_dur:.0f}с, але CTA не клікають",
                evidence=[Evidence(f"{page['sessions']} сесій, {avg_dur:.0f}с, 0 конверсій", "GA4", 75)],
                confidence=_adjusted(72, "cta_too_low", ctx.learning_log),
                recommendation="Додай CTA в середину сторінки, не тільки в кінці. Перевір чи CTA кнопка помітна (колір, розмір)",
                expected_effect="Конверсія +0.5-2% після покращення розміщення CTA",
                risk="Читачі, які дочитали, вже зацікавлені — їх відхід = пряма втрата клієнта",
            ))
    return causes


def _rules_for_position_change(kw: dict, ctx: DataContext) -> list[Cause]:
    causes = []
    if kw["direction"] == "down":
        # Перевіряємо чи були правки на цій сторінці
        related_edits = [
            p for p in ctx.recent_published
            if p.get("target_page_path") and kw.get("query", "").split()[0] in (p.get("target_page_path") or "")
        ]
        if related_edits:
            causes.append(Cause(
                cause_type="edit_caused_position_drop",
                description=f"Можлива реакція Google на нещодавню правку сторінки",
                evidence=[Evidence(f"Правка «{related_edits[0]['title']}» ({related_edits[0].get('published_date', '?')})", "recommendations", 55)],
                confidence=_adjusted(50, "edit_caused_position_drop", ctx.learning_log),
                recommendation="Дочекайтись 2-3 тижні — Google часто тимчасово знижує позиції після змін",
                expected_effect="Позиції стабілізуються або повернуться за 14-21 день",
                risk="Якщо не повернуться — можливо правка погіршила relevance сторінки",
            ))
        else:
            causes.append(Cause(
                cause_type="algorithm_fluctuation",
                description="Коливання алгоритму Google — нормальне для молодого сайту",
                evidence=[Evidence(f"{kw['prev_pos']} → {kw['curr_pos']} ({kw['diff']:+.1f})", "keyword_history", 45)],
                confidence=_adjusted(45, "algorithm_fluctuation", ctx.learning_log),
                recommendation="Якщо падіння триватиме >2 тижні — перевірити контент і технічний стан сторінки",
                expected_effect="Позиції повернуться самостійно якщо причина — флуктуація",
                risk="Якщо ігнорувати тривале падіння — можна пропустити реальну проблему",
            ))
    else:
        for pub in ctx.recent_published:
            if pub.get("type") in ("content", "onpage"):
                causes.append(Cause(
                    cause_type="content_improvement_effect",
                    description=f"Покращення позиції після зміни «{pub['title']}»",
                    evidence=[Evidence(f"Правка {pub.get('published_date', '?')}, {pub['_days_ago']} днів тому", "recommendations", 60)],
                    confidence=_adjusted(58, "content_improvement_effect", ctx.learning_log),
                    recommendation="Зафіксувати цей підхід, застосувати до схожих сторінок",
                    expected_effect="Зростання позицій може тривати ще 2-4 тижні",
                    risk="Не змінювати те що почало працювати",
                ))
    return causes


# ─────────────────────────────────────────────
# Внутрішні допоміжні
# ─────────────────────────────────────────────

def _get_slow_pages(ctx: DataContext) -> list[dict]:
    if not ctx.technical_data or "error" in ctx.technical_data:
        return []
    result = []
    for url, data in ctx.technical_data.items():
        if isinstance(data, dict) and data.get("performance", 100) < 50:
            result.append({"page": _path_from_url(url), "performance": data["performance"], "lcp": data.get("lcp")})
    return result


def _get_new_queries(ctx: DataContext) -> list[dict]:
    result = []
    for row in ctx.gsc_data:
        q = row.get("query", "")
        entries = ctx.keyword_history.get(q, [])
        if len(entries) <= 1 and row.get("impressions", 0) >= 3:
            result.append({"query": q, "impressions": row["impressions"], "clicks": row.get("clicks", 0)})
    result.sort(key=lambda x: -x["impressions"])
    return result[:8]


def _prev_totals_from_history(history: list[dict], mode: str) -> dict | None:
    relevant = [e for e in history if e.get("mode") == mode]
    return relevant[-1]["site_totals"] if relevant else None


# ─────────────────────────────────────────────
# Правила воронки (per-page breakpoints)
# ─────────────────────────────────────────────

def _funnel_breakpoint_rules(page: str, funnel: dict, ctx: DataContext) -> list[Cause]:
    """Знаходить розриви воронки для конкретної сторінки."""
    causes = []
    impressions = funnel.get("impressions", 0)
    ctr = funnel.get("ctr", 0.0)
    clicks = funnel.get("clicks", 0)
    sessions = funnel.get("sessions", 0)
    avg_dur = funnel.get("avg_duration_sec", 0.0)
    total_conv = funnel.get("total_conversions", 0)
    tg = funnel.get("telegram_click", 0)
    form = funnel.get("form_submit", 0)

    # Багато показів → мало CTR → проблема title/description
    if impressions >= 50 and ctr < 2.0:
        causes.append(Cause(
            cause_type="low_ctr_title",
            description=f"{page}: {impressions} показів але CTR {ctr:.1f}% — title або description слабкий",
            evidence=[Evidence(f"{impressions} показів, {clicks} кліків, CTR {ctr:.1f}%", "GSC", 88)],
            confidence=_adjusted(85, "low_ctr_title", ctx.learning_log),
            recommendation="Переписати title/description: додати цифри, вигоди, CTA у snippet",
            expected_effect="CTR +1-3% → десятки додаткових відвідувачів без нових позицій",
            risk="Низький CTR = витрачений потенціал навіть при хороших позиціях",
        ))

    # Кліки є → сесій немає → проблема з аналітикою або редирект
    if clicks >= 5 and sessions == 0:
        causes.append(Cause(
            cause_type="clicks_no_sessions",
            description=f"{page}: {clicks} кліків у GSC але 0 сесій в GA4 — GA4 тег або редирект",
            evidence=[
                Evidence(f"GSC: {clicks} кліків", "GSC", 90),
                Evidence(f"GA4: 0 сесій", "GA4", 90),
            ],
            confidence=_adjusted(80, "clicks_no_sessions", ctx.learning_log),
            recommendation="Перевірити: 1) GA4 тег на сторінці, 2) редирект що скидає UTM, 3) realtime GA4",
            expected_effect="Після виправлення аналітика показуватиме реальний трафік",
            risk="Без GA4 даних неможливо оцінити ефект будь-яких змін",
        ))

    # Сесії є → дуже мало часу → контент не відповідає наміру
    if sessions >= 10 and avg_dur < 20:
        causes.append(Cause(
            cause_type="low_engagement_bounce",
            description=f"{page}: {sessions} сесій, але час {avg_dur:.0f}с — контент не відповідає наміру",
            evidence=[Evidence(f"{sessions} сесій, {avg_dur:.0f}с середній час", "GA4", 82)],
            confidence=_adjusted(80, "low_engagement_bounce", ctx.learning_log),
            recommendation="Вступний абзац повинен одразу відповідати запиту з якого приходять",
            expected_effect="Час на сторінці +30-60с → кращі поведінкові сигнали для Google",
            risk="Низький engagement погіршує позиції через поведінкові фактори",
        ))

    # Хороший engagement → нуль конверсій → CTA проблема
    if sessions >= 10 and avg_dur > 90 and total_conv == 0:
        causes.append(Cause(
            cause_type="engagement_no_cta",
            description=f"{page}: читають {avg_dur:.0f}с але 0 конверсій — CTA слабкий або розміщений погано",
            evidence=[Evidence(f"{sessions} сесій, {avg_dur:.0f}с, 0 конверсій", "GA4", 78)],
            confidence=_adjusted(75, "engagement_no_cta", ctx.learning_log),
            recommendation="Додати CTA в середину сторінки (не лише знизу), контрастний колір кнопки",
            expected_effect="Конверсія +0.5-2% після покращення розміщення CTA",
            risk="Зацікавлені читачі йдуть — пряма втрата потенційних клієнтів",
        ))

    # Telegram є → форми немає → аудиторія надає перевагу месенджерам
    if tg > 0 and form == 0 and sessions >= 5:
        causes.append(Cause(
            cause_type="telegram_preferred",
            description=f"{page}: Telegram {tg}x але форма 0 — аудиторія обирає месенджери",
            evidence=[Evidence(f"telegram_click: {tg}, form_submit: {form}", "GA4", 75)],
            confidence=_adjusted(70, "telegram_preferred", ctx.learning_log),
            recommendation="Додати більше Telegram-кнопок, спростити форму або зробити її менш формальною",
            expected_effect="Більше звернень через Telegram → загальна конверсія зросте",
            risk="Ігнорування вподобань аудиторії = втрата звернень",
        ))

    return causes


def _normalize_page_path(raw: str) -> str:
    """Нормалізує шлях: /seo/ і /seo → /seo, кореневий / лишається /."""
    p = raw.rstrip("/")
    return p or "/"


def _build_funnel_analysis(ctx: DataContext) -> list[dict]:
    """Будує повну воронку для всіх сторінок з достатнім трафіком."""
    from lib.metrics import build_page_funnel
    pages_seen: set = set()
    all_page_paths: set = set()
    for row in ctx.gsc_data:
        p = _normalize_page_path(_path_from_url(row.get("page", "")))
        if p:
            all_page_paths.add(p)
    for row in ctx.ga4_data:
        p = _normalize_page_path(row.get("page", ""))
        if p:
            all_page_paths.add(p)

    results = []
    for page_path in all_page_paths:
        if page_path in pages_seen:
            continue
        pages_seen.add(page_path)
        funnel = build_page_funnel(page_path, ctx.gsc_data, ctx.ga4_data, ctx.page_conversions, {})
        if funnel["sessions"] < 3 and funnel["impressions"] < 10:
            continue
        causes = _funnel_breakpoint_rules(page_path, funnel, ctx)
        results.append({**funnel, "causes": causes})

    results.sort(key=lambda x: x["sessions"] * 1 + x["total_conversions"] * 10, reverse=True)
    return results


def _format_funnel_block(funnel_pages: list[dict]) -> str:
    if not funnel_pages:
        return ""
    lines = ["\n📊 SEO ВОРОНКА ПО СТОРІНКАХ:"]
    for f in funnel_pages[:10]:
        page = f["page"]
        cr_str = f"{f['conversion_rate']:.1f}%" if f["sessions"] > 0 else "—"
        ctr_str = f"{f['ctr']:.1f}%" if f["impressions"] > 0 else "—"
        lines.append(
            f"\n  {page}\n"
            f"    Покази: {f['impressions']} | CTR: {ctr_str} | Кліки: {f['clicks']}\n"
            f"    Сесії: {f['sessions']} | Час: {f['avg_duration_sec']:.0f}с | Відмови: {f['bounce_rate']:.0f}%\n"
            f"    📞 {f['phone_click']} Тел | 💬 {f['telegram_click']} TG | 📝 {f['form_submit']} Форм | CR: {cr_str}"
        )
        for c in f.get("causes", [])[:2]:
            lines.append(f"    ⚡ [{_conf_bar(c.confidence)}] {c.description}")
            lines.append(f"       → {c.recommendation}")
    return "\n".join(lines)


def _format_page_rankings(funnel_pages: list[dict]) -> str:
    if not funnel_pages:
        return ""
    from lib.metrics import score_page_priority
    lines = ["\n🏆 РЕЙТИНГ СТОРІНОК:"]

    by_conv = sorted(funnel_pages, key=lambda x: -x["total_conversions"])
    if any(f["total_conversions"] > 0 for f in by_conv):
        lines.append("  За конверсіями:")
        for f in by_conv[:5]:
            if f["total_conversions"] > 0:
                lines.append(f"    {f['page']} — {f['total_conversions']} конв. (CR {f['conversion_rate']:.1f}%)")

    by_sessions = sorted(funnel_pages, key=lambda x: -x["sessions"])
    lines.append("  За трафіком:")
    for f in by_sessions[:5]:
        if f["sessions"] > 0:
            lines.append(f"    {f['page']} — {f['sessions']} сесій")

    by_ctr = sorted([f for f in funnel_pages if f["impressions"] >= 20], key=lambda x: -x["ctr"])
    if by_ctr:
        lines.append("  За CTR (мін. 20 показів):")
        for f in by_ctr[:3]:
            lines.append(f"    {f['page']} — CTR {f['ctr']:.1f}% ({f['impressions']} показів)")

    scored = sorted(funnel_pages, key=lambda x: -score_page_priority(x))
    if scored:
        lines.append("  Пріоритет (★ = бізнес-цінність):")
        max_score = score_page_priority(scored[0]) or 1
        for f in scored[:6]:
            score = score_page_priority(f)
            stars = min(5, max(1, round(score / max_score * 5)))
            star_str = "★" * stars + "☆" * (5 - stars)
            lines.append(f"    {star_str} {f['page']}")

    return "\n".join(lines)


def _format_traffic_channels(traffic_channels: dict) -> str:
    if not traffic_channels:
        return ""
    total_sessions = sum(v.get("sessions", 0) for v in traffic_channels.values())
    if total_sessions == 0:
        return ""
    lines = ["\n📡 КАНАЛИ ТРАФІКУ:"]
    for channel, data in sorted(traffic_channels.items(), key=lambda x: -x[1].get("sessions", 0)):
        sessions = data.get("sessions", 0)
        pct = round(sessions / total_sessions * 100, 1) if total_sessions else 0
        lines.append(f"  {channel}: {sessions} сесій ({pct}%)")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# Побудова CausalChain
# ─────────────────────────────────────────────

def _build_site_chains(ctx: DataContext) -> list[CausalChain]:
    chains = []
    metrics_config = [
        ("clicks",      "Кліки",        "clicks"),
        ("impressions", "Покази",        "impressions"),
        ("sessions",    "Сесії",         "sessions"),
        ("users",       "Користувачі",   "users"),
    ]

    for metric, label, key in metrics_config:
        curr = ctx.current_totals.get(key, 0)
        prev = ctx.prev_totals.get(key, 0) if ctx.prev_totals else None
        if prev is None:
            continue
        pct = _pct(curr, prev)
        if pct is None or abs(pct) < 15:
            continue

        direction = "up" if pct > 0 else "down"
        if metric == "clicks":
            causes = _rules_for_clicks_drop(ctx) if direction == "down" else _rules_for_impressions_growth(ctx)
        elif metric == "impressions":
            causes = _rules_for_impressions_drop(ctx) if direction == "down" else _rules_for_impressions_growth(ctx)
        elif metric in ("sessions", "users"):
            causes = _rules_for_sessions_drop(ctx) if direction == "down" else _rules_for_impressions_growth(ctx)
        else:
            causes = []

        causes.sort(key=lambda c: -c.confidence)
        node = CausalNode(
            metric=metric, label=label, direction=direction,
            prev_val=prev, curr_val=curr, change_pct=pct, causes=causes,
        )
        arrow = "↑" if pct > 0 else "↓"
        summary_parts = []
        if causes:
            top = causes[0]
            summary_parts.append(f"Найімовірніша причина ({top.confidence}%): {top.description}")
        chains.append(CausalChain(
            title=f"{arrow} {label} {pct:+.0f}% ({prev} → {curr})",
            nodes=[node],
            summary=" | ".join(summary_parts) if summary_parts else "Причина невизначена",
        ))

    return chains


def _build_conversion_chain(ctx: DataContext) -> str:
    """Аналіз воронки конверсії — повертає текст."""
    conv_events = {e["name"]: e["count"] for e in ctx.ga4_events if e["name"] in ("phone_click", "telegram_click", "form_submit")}
    total = sum(conv_events.values())
    lines = [
        "🎯 ВОРОНКА КОНВЕРСІЙ:",
        f"  Заявки (form_submit): {conv_events.get('form_submit', 0)}",
        f"  Telegram-кліки: {conv_events.get('telegram_click', 0)}",
        f"  Телефон-кліки: {conv_events.get('phone_click', 0)}",
        f"  Всього конверсій: {total}",
    ]
    if ctx.current_totals.get("sessions", 0) > 0:
        conv_rate = total / ctx.current_totals["sessions"] * 100
        lines.append(f"  Конверсія: {conv_rate:.2f}%")

    zero_causes = _rules_for_zero_conversions(ctx)
    if zero_causes:
        lines.append("  Виявлені причини низьких конверсій:")
        for c in zero_causes[:3]:
            lines.append(f"    [{_conf_bar(c.confidence)}] {c.description}")
            lines.append(f"      → {c.recommendation}")
    return "\n".join(lines)


def _build_recent_changes_analysis(ctx: DataContext) -> list[str]:
    lines = []
    for pub in ctx.recent_published:
        days = pub["_days_ago"]
        page = pub.get("target_page_path") or "новий контент"
        action = pub.get("action", "")

        # Знаходимо поточні метрики сторінки для оцінки ефекту
        current_page_metrics = None
        if page and page != "новий контент":
            current_page_metrics = find_page_metrics(page, ctx.gsc_data, ctx.ga4_data)

        if days < 7:
            status = "⏳ надто рано (Google ще індексує)"
            conf = 20
        elif days < 14:
            status = "🔄 рання фаза (ефект ще формується)"
            conf = 40
        elif days < 30:
            status = "🔍 оцінка ефекту (оптимальний час)"
            conf = 75
        else:
            status = "✅ ефект сформувався"
            conf = 90

        summary = f"  «{pub['title']}» ({page}) — {days} дн. тому | {status}"
        if current_page_metrics and page != "новий контент":
            summary += f"\n    Кліки: {current_page_metrics.get('clicks', 0)}, Покази: {current_page_metrics.get('impressions', 0)}, Сесії: {current_page_metrics.get('sessions', 0)}"

        # Зв'яжи з learning_log
        boost = _learning_boost(
            "new_content_indexed" if action == "create_new" else "content_improvement_effect",
            ctx.learning_log,
        )
        if boost > 0:
            summary += f"\n    📚 Цей тип змін раніше спрацьовував (+{boost}% до confidence)"
        elif boost < 0:
            summary += f"\n    ⚠️ Цей тип змін раніше давав слабкий результат ({boost}% до confidence)"

        lines.append(summary)
    return lines


def _build_query_nodes(ctx: DataContext) -> list[CausalNode]:
    nodes = []
    for kw in ctx.keyword_changes[:8]:
        causes = _rules_for_position_change(kw, ctx)
        causes.sort(key=lambda c: -c.confidence)
        nodes.append(CausalNode(
            metric="position", label=f"Позиція «{kw['query']}»",
            direction=kw["direction"],
            prev_val=kw["prev_pos"], curr_val=kw["curr_pos"],
            change_pct=None,
            causes=causes,
        ))
    return nodes


# ─────────────────────────────────────────────
# Форматування виводу
# ─────────────────────────────────────────────

def _format_cause(c: Cause) -> list[str]:
    lines = [f"    [{_conf_bar(c.confidence)}] {c.description}"]
    for ev in c.evidence[:2]:
        lines.append(f"      📊 {ev.text} (джерело: {ev.source})")
    lines.append(f"      → Дія: {c.recommendation}")
    lines.append(f"      → Ефект: {c.expected_effect}")
    lines.append(f"      ⚠ Ризик: {c.risk}")
    return lines


def _format_result(result: ExplainResult) -> str:
    sections = ["\n🧠 АНАЛІЗ ПРИЧИН ЗМІН:\n"]

    # 1. Зміни на рівні сайту
    for chain in result.site_chains:
        sections.append(f"\n  {chain.title}")
        for node in chain.nodes:
            if not node.causes:
                sections.append("    (недостатньо даних для визначення причини)")
                continue
            for c in node.causes[:3]:
                sections.extend(_format_cause(c))

    # 2. Зміни позицій запитів
    if result.query_nodes:
        sections.append("\n  📍 Зміни позицій запитів:")
        for node in result.query_nodes:
            arrow = "↓" if node.direction == "down" else "↑"
            sections.append(f"    {arrow} {node.label}: {node.prev_val} → {node.curr_val}")
            for c in node.causes[:1]:
                sections.append(f"      [{_conf_bar(c.confidence)}] {c.description}")
                sections.append(f"      → {c.recommendation}")

    # 3. Воронка конверсій
    if result.conversion_analysis:
        sections.append(f"\n  {result.conversion_analysis}")

    # 4. Нещодавні зміни
    if result.recent_changes_analysis:
        sections.append("\n  📌 Ефект нещодавніх змін:")
        sections.extend(result.recent_changes_analysis)

    # 5. Канали трафіку
    if result.traffic_channels_text:
        sections.append(result.traffic_channels_text)

    # 6. SEO воронка по сторінках
    funnel_text = _format_funnel_block(result.funnel_pages)
    if funnel_text:
        sections.append(funnel_text)

    # 7. Рейтинг сторінок
    if result.page_rankings_text:
        sections.append(result.page_rankings_text)

    if not result.has_data:
        sections.append("  (недостатньо даних для аналізу — потрібна більша historія)")

    sections.append(
        "\n  ⚠️ Інструкція для Claude: використовуй ці дані щоб пояснювати ЧОМУ відбулись зміни. "
        "НІКОЛИ не вигадуй причини — тільки підкріплені даними вище. "
        "Після кожної суттєвої зміни додавай блок «🧠 Чому це сталося» з причинами і confidence."
    )
    return "\n".join(sections)


# ─────────────────────────────────────────────
# Публічне API
# ─────────────────────────────────────────────

def build_data_context(
    current_totals: dict,
    current_gsc: list[dict],
    current_ga4: list[dict],
    history: list[dict],
    keyword_history: dict,
    backlog: list[dict],
    ga4_events: list[dict],
    technical_data: dict | None,
    learning_log: list[dict],
    mode: str,
    today: datetime.date,
    page_conversions: dict | None = None,
    traffic_channels: dict | None = None,
) -> DataContext:
    """Збирає DataContext з усіх доступних джерел."""
    prev_totals = _prev_totals_from_history(history, mode)
    keyword_changes = []
    for query, entries in keyword_history.items():
        if len(entries) < 2:
            continue
        diff = round(entries[-1]["position"] - entries[-2]["position"], 1)
        if abs(diff) < 3:
            continue
        keyword_changes.append({
            "query": query,
            "prev_pos": entries[-2]["position"],
            "curr_pos": entries[-1]["position"],
            "diff": diff,
            "clicks": entries[-1]["clicks"],
            "direction": "down" if diff > 0 else "up",
        })
    keyword_changes.sort(key=lambda x: abs(x["diff"]), reverse=True)

    recent_published = []
    for rec in backlog:
        if rec.get("status") == "published" and rec.get("published_date"):
            days = _days_ago(rec["published_date"], today)
            if days <= 30:
                recent_published.append({**rec, "_days_ago": days})

    return DataContext(
        current_totals=current_totals,
        prev_totals=prev_totals,
        gsc_data=current_gsc,
        ga4_data=current_ga4,
        ga4_events=ga4_events,
        keyword_history=keyword_history,
        keyword_changes=keyword_changes,
        recent_published=recent_published,
        backlog=backlog,
        technical_data=technical_data,
        learning_log=learning_log,
        mode=mode,
        today=today,
        page_conversions=page_conversions or {},
        traffic_channels=traffic_channels or {},
    )


def analyze(ctx: DataContext) -> ExplainResult:
    """Повний аналіз на основі DataContext."""
    site_chains = _build_site_chains(ctx)
    query_nodes = _build_query_nodes(ctx)
    conversion_analysis = _build_conversion_chain(ctx)
    recent_changes = _build_recent_changes_analysis(ctx)
    funnel_pages = _build_funnel_analysis(ctx)
    has_data = bool(site_chains or query_nodes or ctx.recent_published)
    return ExplainResult(
        site_chains=site_chains,
        page_nodes=[],
        query_nodes=query_nodes,
        conversion_analysis=conversion_analysis,
        recent_changes_analysis=recent_changes,
        has_data=has_data,
        funnel_pages=funnel_pages,
        traffic_channels_text=_format_traffic_channels(ctx.traffic_channels),
        page_rankings_text=_format_page_rankings(funnel_pages),
    )


def analyze_page(page_path: str, ctx: DataContext) -> str:
    """Глибокий аналіз однієї конкретної сторінки — для impact review і аналізу рекомендацій."""
    metrics = find_page_metrics(page_path, ctx.gsc_data, ctx.ga4_data)
    lines = [f"📄 АНАЛІЗ СТОРІНКИ {page_path}:"]
    lines.append(f"  Кліки: {metrics.get('clicks', 0)} | Покази: {metrics.get('impressions', 0)} | Сесії: {metrics.get('sessions', 0)}")

    # Пов'язані зміни з backlog
    related = [r for r in ctx.backlog if r.get("target_page_path") == page_path and r.get("status") == "published"]
    if related:
        lines.append("  Застосовані зміни:")
        for r in related:
            lines.append(f"    • «{r['title']}» ({r.get('published_date', '?')})")

    # Конверсії по сайту (поки немає розбивки per-page)
    conv_events = {e["name"]: e["count"] for e in ctx.ga4_events if e["name"] in ("phone_click", "telegram_click", "form_submit")}
    total_conv = sum(conv_events.values())
    page_sessions = metrics.get("sessions", 0)
    if page_sessions >= 10 and total_conv == 0:
        lines.append(f"  ⚠️ {page_sessions} сесій на сайті, але 0 конверсій — CTA потребує уваги")

    # Повільна сторінка?
    slow = _get_slow_pages(ctx)
    page_slow = [s for s in slow if s["page"] == page_path]
    if page_slow:
        lines.append(f"  ⚡ PageSpeed: {page_slow[0]['performance']}/100 — критично повільна")

    return "\n".join(lines)


def analyze_for_impact_review(rec: dict, ctx: DataContext) -> str:
    """Пояснення ефекту конкретної рекомендації для impact review."""
    page = rec.get("target_page_path")
    lines = [f"📊 IMPACT REVIEW: «{rec['title']}»"]
    if page:
        lines.append(analyze_page(page, ctx))
    baseline = rec.get("baseline_metrics", {})
    current = find_page_metrics(page, ctx.gsc_data, ctx.ga4_data) if page else {}
    if baseline and current:
        for key, label in [("clicks", "кліки"), ("impressions", "покази"), ("sessions", "сесії")]:
            b = baseline.get(key, 0)
            c = current.get(key, 0)
            pct = _pct(c, b)
            if pct is not None:
                arrow = "↑" if pct > 0 else "↓"
                lines.append(f"  {arrow} {label}: {b} → {c} ({pct:+.0f}%)")
    days = _days_ago(rec.get("published_date", ""), ctx.today)
    boost = _learning_boost(
        "new_content_indexed" if rec.get("action") == "create_new" else "content_improvement_effect",
        ctx.learning_log,
    )
    lines.append(f"  Опубліковано {days} днів тому | learning_log поправка: {boost:+d}%")
    return "\n".join(lines)


def record_outcome(
    rec_id: int,
    cause_type: str,
    worked: bool,
    learning_log: list[dict],
    today: datetime.date,
    funnel_before: dict | None = None,
    funnel_after: dict | None = None,
) -> list[dict]:
    """Фіксує результат у learning_log — викликається з analyst.py після impact review."""
    if any(e.get("rec_id") == rec_id for e in learning_log):
        return learning_log
    entry: dict = {
        "rec_id": rec_id,
        "cause_type": cause_type,
        "worked": worked,
        "date": today.isoformat(),
    }
    if funnel_before:
        entry["funnel_before"] = {
            k: funnel_before.get(k)
            for k in ("clicks", "impressions", "sessions", "conversion_rate", "ctr")
        }
    if funnel_after:
        entry["funnel_after"] = {
            k: funnel_after.get(k)
            for k in ("clicks", "impressions", "sessions", "conversion_rate", "ctr")
        }
    learning_log.append(entry)
    return learning_log[-500:]


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
    learning_log: list[dict] | None = None,
    page_conversions: dict | None = None,
    traffic_channels: dict | None = None,
) -> str:
    """
    Зворотно сумісний публічний метод.
    Будує DataContext → аналізує → форматує рядок для Claude-промпту.
    """
    ctx = build_data_context(
        current_totals=current_totals,
        current_gsc=current_gsc,
        current_ga4=current_ga4,
        history=history,
        keyword_history=keyword_history,
        backlog=backlog,
        ga4_events=ga4_events,
        technical_data=technical_data,
        learning_log=learning_log or [],
        mode=mode,
        today=today,
        page_conversions=page_conversions,
        traffic_channels=traffic_channels,
    )
    result = analyze(ctx)
    return _format_result(result)
