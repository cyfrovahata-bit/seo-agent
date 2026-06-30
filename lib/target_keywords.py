"""
Цільові ключові слова для cyfrovahata.com.ua.
Загальноукраїнські запити — без прив'язки до міст.
"""

TARGET_KEYWORDS = [
    # Розробка сайтів
    {"keyword": "розробка сайту",              "target_position": 20, "cluster": "розробка"},
    {"keyword": "створення сайту",             "target_position": 20, "cluster": "розробка"},
    {"keyword": "розробка сайту під ключ",     "target_position": 20, "cluster": "розробка"},
    {"keyword": "створення сайту під ключ",    "target_position": 20, "cluster": "розробка"},
    {"keyword": "замовити сайт",               "target_position": 20, "cluster": "розробка"},
    {"keyword": "зробити сайт",                "target_position": 20, "cluster": "розробка"},
    {"keyword": "розробка сайту ціна",         "target_position": 15, "cluster": "розробка"},
    {"keyword": "скільки коштує сайт",         "target_position": 15, "cluster": "розробка"},
    {"keyword": "розробка корпоративного сайту","target_position": 25, "cluster": "розробка"},
    {"keyword": "розробка landing page",       "target_position": 25, "cluster": "розробка"},
    {"keyword": "розробка сайту wordpress",    "target_position": 20, "cluster": "розробка"},

    # SEO просування
    {"keyword": "seo просування",              "target_position": 20, "cluster": "seo"},
    {"keyword": "seo оптимізація сайту",       "target_position": 20, "cluster": "seo"},
    {"keyword": "просування сайту",            "target_position": 20, "cluster": "seo"},
    {"keyword": "розкрутка сайту",             "target_position": 20, "cluster": "seo"},
    {"keyword": "seo просування сайту ціна",   "target_position": 15, "cluster": "seo"},
    {"keyword": "скільки коштує seo",          "target_position": 15, "cluster": "seo"},
    {"keyword": "замовити seo просування",     "target_position": 20, "cluster": "seo"},
    {"keyword": "seo аудит сайту",             "target_position": 20, "cluster": "seo"},
    {"keyword": "технічне seo",                "target_position": 25, "cluster": "seo"},

    # Підтримка і обслуговування
    {"keyword": "підтримка сайту",             "target_position": 20, "cluster": "підтримка"},
    {"keyword": "обслуговування сайту",        "target_position": 20, "cluster": "підтримка"},
    {"keyword": "технічна підтримка сайту",    "target_position": 20, "cluster": "підтримка"},
    {"keyword": "адміністрування сайту",       "target_position": 25, "cluster": "підтримка"},
    {"keyword": "підтримка wordpress сайту",   "target_position": 20, "cluster": "підтримка"},

    # Аналітика
    {"keyword": "налаштування google analytics","target_position": 25, "cluster": "аналітика"},
    {"keyword": "веб аналітика",               "target_position": 25, "cluster": "аналітика"},
    {"keyword": "налаштування ga4",            "target_position": 20, "cluster": "аналітика"},
    {"keyword": "google tag manager налаштування","target_position": 20, "cluster": "аналітика"},
    {"keyword": "аналітика сайту",             "target_position": 25, "cluster": "аналітика"},
]

CLUSTERS = {
    "розробка":   "Розробка сайтів",
    "seo":        "SEO просування",
    "підтримка":  "Підтримка сайтів",
    "аналітика":  "Веб-аналітика",
}


def build_target_keyword_report(keyword_history: dict) -> str:
    """
    Порівнює реальні позиції (з keyword_history) з цільовими.
    Повертає текстовий звіт по кластерах.
    """
    lines = ["ЦІЛЬОВІ КЛЮЧОВІ СЛОВА (реальна позиція → ціль):"]
    by_cluster: dict[str, list] = {k: [] for k in CLUSTERS}

    for kw in TARGET_KEYWORDS:
        query = kw["keyword"]
        entries = keyword_history.get(query, [])
        if entries:
            pos = entries[-1]["position"]
            diff = pos - kw["target_position"]
            status = "✅" if pos <= kw["target_position"] else ("🔜" if diff <= 20 else "🔴")
            row = f"  {status} «{query}» — позиція {pos} (ціль ≤{kw['target_position']})"
        else:
            row = f"  ❓ «{query}» — ще не з'явився в пошуку"
        by_cluster[kw["cluster"]].append(row)

    for cluster_key, cluster_name in CLUSTERS.items():
        lines.append(f"\n{cluster_name}:")
        lines.extend(by_cluster.get(cluster_key, []))

    return "\n".join(lines)


def auto_cluster_queries(gsc_data: list[dict], min_queries: int = 200) -> dict[str, list[str]]:
    """
    Якщо в GSC більше min_queries запитів — автоматично кластеризує їх за спільними словами.
    Повертає словник {cluster_name: [query, ...]} або порожній dict якщо запитів замало.
    Не замінює TARGET_KEYWORDS — доповнює ручну кластеризацію новими темами.
    """
    queries = [r.get("query", "").lower() for r in gsc_data if r.get("query")]
    if len(queries) < min_queries:
        return {}

    from collections import Counter

    # Збираємо всі слова довжиною ≥5 символів
    word_counter: Counter = Counter()
    for q in queries:
        for word in q.split():
            if len(word) >= 5:
                word_counter[word] += 1

    # Топ-слова (мінімум у 3 запитах) — кандидати в назви кластерів
    cluster_seeds = [w for w, cnt in word_counter.most_common(50) if cnt >= 3]

    # Ігноруємо стоп-слова
    STOP = {
        "через", "своє", "своїх", "якість", "послуги", "замовити",
        "після", "перед", "більше", "менше", "такий", "також",
    }
    cluster_seeds = [w for w in cluster_seeds if w not in STOP]

    # Призначаємо кожен запит до першого підходящого кластера
    clusters: dict[str, list[str]] = {}
    assigned: set[str] = set()
    for seed in cluster_seeds[:15]:
        members = [q for q in queries if seed in q and q not in assigned]
        if len(members) >= 3:
            clusters[seed] = members[:20]
            assigned.update(members)

    return clusters


def build_auto_cluster_report(gsc_data: list[dict]) -> str:
    """Повертає текстовий звіт з автоматичних кластерів для Claude-промпту."""
    clusters = auto_cluster_queries(gsc_data)
    if not clusters:
        return ""
    lines = [f"\nАВТОКЛАСТЕРИ GSC (знайдено {len(clusters)} нових тем):"]
    for cluster_name, members in list(clusters.items())[:10]:
        lines.append(f"  {cluster_name}: {len(members)} запитів → напр. «{members[0]}»")
    return "\n".join(lines)


def build_cluster_summary(gsc_data: list[dict]) -> str:
    """Групує реальні GSC-запити по кластерах і показує силу кожного."""
    cluster_stats: dict[str, dict] = {k: {"clicks": 0, "impressions": 0, "queries": []} for k in CLUSTERS}

    target_map = {kw["keyword"]: kw["cluster"] for kw in TARGET_KEYWORDS}

    for row in gsc_data:
        query = row.get("query", "").lower()
        matched_cluster = None
        for kw, cluster in target_map.items():
            if kw in query or query in kw:
                matched_cluster = cluster
                break
        if not matched_cluster:
            # Просте keyword-matching по кластерних словах
            if any(w in query for w in ["сайт", "розробк", "створен", "landing"]):
                matched_cluster = "розробка"
            elif any(w in query for w in ["seo", "просуванн", "оптиміз", "розкрутк"]):
                matched_cluster = "seo"
            elif any(w in query for w in ["підтримк", "обслугов", "адмініструванн"]):
                matched_cluster = "підтримка"
            elif any(w in query for w in ["аналітик", "analytics", "ga4", "gtm", "tag manager"]):
                matched_cluster = "аналітика"
        if matched_cluster:
            cluster_stats[matched_cluster]["clicks"] += row.get("clicks", 0)
            cluster_stats[matched_cluster]["impressions"] += row.get("impressions", 0)
            cluster_stats[matched_cluster]["queries"].append(query)

    lines = ["СЕМАНТИЧНІ КЛАСТЕРИ (які теми вже приносять трафік):"]
    for cluster_key, cluster_name in CLUSTERS.items():
        s = cluster_stats[cluster_key]
        q_count = len(set(s["queries"]))
        lines.append(
            f"  {cluster_name}: {s['clicks']} кліків, {s['impressions']} показів, {q_count} запитів"
            + (" ⚠️ слабкий кластер" if s["impressions"] < 10 else "")
        )
    return "\n".join(lines)
