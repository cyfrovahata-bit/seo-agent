"""
Звіт про можливості для зовнішніх посилань.
Повертає список майданчиків де можна безкоштовно розмістити посилання на сайт.
Запускається раз на тиждень.
"""

# Список актуальних безкоштовних майданчиків для українського бізнесу
BACKLINK_OPPORTUNITIES = [
    {
        "type": "Каталог компаній",
        "sites": [
            {"name": "prom.ua", "url": "https://prom.ua", "instructions": "Зареєструй компанію, вкажи сайт у профілі"},
            {"name": "ukrbiznes.com", "url": "https://ukrbiznes.com", "instructions": "Додай компанію в каталог"},
            {"name": "ua.kompass.com", "url": "https://ua.kompass.com", "instructions": "Безкоштовний базовий профіль"},
            {"name": "youcontrol.com.ua", "url": "https://youcontrol.com.ua", "instructions": "Актуалізуй дані компанії"},
            {"name": "all.biz", "url": "https://ua.all.biz", "instructions": "Додай компанію і послуги"},
        ]
    },
    {
        "type": "Google та карти",
        "sites": [
            {"name": "Google Business Profile", "url": "https://business.google.com", "instructions": "Створи профіль компанії — дає посилання і показує сайт в Google Maps"},
            {"name": "2gis.ua", "url": "https://2gis.ua", "instructions": "Додай компанію на карту"},
        ]
    },
    {
        "type": "Фріланс і IT платформи",
        "sites": [
            {"name": "freelancehunt.com", "url": "https://freelancehunt.com", "instructions": "Профіль студії з посиланням на сайт"},
            {"name": "kabanchik.ua", "url": "https://kabanchik.ua", "instructions": "Профіль виконавця послуг"},
            {"name": "clutch.co", "url": "https://clutch.co", "instructions": "Профіль агенції — авторитетний сайт для SEO"},
        ]
    },
    {
        "type": "Соціальні мережі і спільноти",
        "sites": [
            {"name": "dou.ua", "url": "https://dou.ua", "instructions": "Профіль компанії як роботодавця"},
            {"name": "jobs.ua", "url": "https://jobs.ua", "instructions": "Сторінка компанії"},
            {"name": "linkedin.com", "url": "https://linkedin.com", "instructions": "Сторінка компанії з посиланням на сайт"},
        ]
    },
    {
        "type": "Статті і гостьовий блог",
        "sites": [
            {"name": "ain.ua", "url": "https://ain.ua", "instructions": "Можна запропонувати статтю-колонку про SEO або кейс"},
            {"name": "vc.ru (Ukrainian)", "url": "https://vc.ru", "instructions": "Публікація статті з посиланням на сайт"},
            {"name": "habrahabr.io (Ukrainian IT)", "url": "https://habrahabr.io", "instructions": "Технічна стаття про веб-розробку або SEO"},
        ]
    },
]


def get_backlink_report(completed_sites: list[str] | None = None) -> dict:
    """
    Повертає список можливостей для посилань.
    completed_sites — список майданчиків які вже виконані (щоб не повторювати).
    """
    done = set(completed_sites or [])
    result = []
    for category in BACKLINK_OPPORTUNITIES:
        remaining = [s for s in category["sites"] if s["name"] not in done]
        if remaining:
            result.append({
                "type": category["type"],
                "sites": remaining,
            })
    total_remaining = sum(len(c["sites"]) for c in result)
    total_done = len(done)
    return {
        "opportunities": result,
        "total_remaining": total_remaining,
        "total_done": total_done,
    }
