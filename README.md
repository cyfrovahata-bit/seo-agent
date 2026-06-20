# SEO-агент

Автоматична система SEO-просування сайтів на базі Claude AI. Аналізує трафік, знаходить можливості для росту, готує правки на сайті і відстежує ефект — все автоматично через GitHub Actions, без власного сервера.

---

## Як додати новий сайт

Щоб агент просував ще один сайт — потрібно повторити налаштування для нього окремо. Найпростіше — форкнути цей репозиторій або скопіювати його в новий приватний репо.

### Крок 1. WordPress: доступ для агента

1. У wp-admin створи **окремого користувача** з роллю **Editor** (не Admin), наприклад `seo-agent`
2. Зайди під ним у **Users → Profile → Application Passwords**
3. Введи назву → **Add New Application Password** → скопіюй пароль (формат `xxxx xxxx xxxx xxxx xxxx xxxx`)
4. Переконайся що REST API відкритий: відкрий `https://твій-сайт.ua/wp-json/wp/v2/posts` — має повернутись JSON

### Крок 2. Google Cloud: Search Console і GA4

1. Створи проєкт у [Google Cloud Console](https://console.cloud.google.com/)
2. Увімкни два API: **Google Search Console API** і **Google Analytics Data API**
3. Створи **Service Account** (IAM & Admin → Service Accounts) → Keys → Add Key → JSON — збережи файл
4. Додай email сервісного акаунту (вигляд: `xxx@yyy.iam.gserviceaccount.com`):
   - у **Search Console** → Settings → Users and permissions → Add user (роль Restricted)
   - у **GA4** → Admin → Property Access Management → Add (роль Viewer)
5. Знайди потрібні ідентифікатори:
   - `GSC_SITE_URL` — точна URL з Search Console (напр. `https://твій-сайт.ua/`)
   - `GA4_PROPERTY_ID` — числовий ID (GA4 → Admin → Property Settings)

### Крок 3. Telegram-бот

1. Напиши **@BotFather** → `/newbot` → отримаєш `TELEGRAM_BOT_TOKEN`
2. Напиши своєму боту будь-яке повідомлення
3. Відкрий `https://api.telegram.org/bot<TOKEN>/getUpdates` — знайди `"chat":{"id": ...}` — це `TELEGRAM_CHAT_ID`

### Крок 4. GitHub: секрети репозиторію

Settings → Secrets and variables → Actions → **New repository secret**, додай:

| Секрет | Що це |
|---|---|
| `ANTHROPIC_API_KEY` | ключ з [console.anthropic.com](https://console.anthropic.com) |
| `TELEGRAM_BOT_TOKEN` | з кроку 3 |
| `TELEGRAM_CHAT_ID` | з кроку 3 |
| `WP_BASE_URL` | `https://твій-сайт.ua` |
| `WP_USERNAME` | логін з кроку 1 |
| `WP_APP_PASSWORD` | пароль з кроку 1 |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | повний вміст JSON-файлу з кроку 2 |
| `GSC_SITE_URL` | з кроку 2 |
| `GA4_PROPERTY_ID` | з кроку 2 |
| `PAGESPEED_API_KEY` | (необов'язково) API key з Google Cloud для аудиту швидкості |

### Крок 5. Запуск

1. Перейди в **Actions** → переконайся, що workflows активні
2. Запусти **SEO Daily Report** вручну (кнопка **Run workflow**) — перевір чи прийшов звіт у Telegram
3. Якщо помилка — дивись логи в Actions, зазвичай причина: неправильний секрет або закритий REST API

---

## Що робить агент

Автоматично, без участі людини:

- Щодня о **19:00** — короткий звіт про зміни за день: кліки, покази, які сторінки відвідують
- Щонеділі о **19:00** — детальний тижневий звіт: тренди, аналіз конкурентів, технічний аудит сайту, список майданчиків для зовнішніх посилань
- Після кожного звіту — список рекомендацій з кнопками **▶️ Виконати / ❌ Відхилити** прямо в Telegram
- Через 14 днів після застосованої зміни — автоматична оцінка: чи допомогло, чи ні (з конкретними цифрами)
- Якщо зміна погіршила результат — пропонує повернути як було

Що робить людина:

- Натискає кнопку **▶️ Виконати** для потрібної рекомендації
- Переглядає підготовлену чернетку в WordPress і публікує
- Пише `/published <номер>` щоб агент почав відстежувати ефект

**Агент ніколи не публікує самостійно** — тільки готує чернетки і ревізії.

---

## Як це влаштовано

```
GitHub Actions (розклад)
│
├── щодня 19:00 ──► analyst.py --mode daily
│                       │
│                       ├── Google Search Console API  (кліки, позиції, запити)
│                       ├── Google Analytics 4 API     (сесії, користувачі, сторінки)
│                       ├── WordPress REST API         (вміст сторінок для аналізу)
│                       ├── Claude API                 (генерує звіт людською мовою)
│                       └── Telegram Bot API           (надсилає звіт + кнопки рекомендацій)
│
├── щонеділі 19:00 ──► analyst.py --mode weekly
│                       │
│                       ├── (все з щоденного +)
│                       ├── Конкуренти: sitemap-аналіз 5 конкурентних сайтів
│                       ├── Технічний аудит: title/H1/meta/canonical кожної сторінки
│                       ├── Динаміка позицій: порівняння з минулим тижнем по кожному запиту
│                       └── Backlinks: список майданчиків для зовнішніх посилань
│
└── кожні 4 години ──► executor.py
                        │
                        ├── Читає нові команди з Telegram (кнопки і текст)
                        │
                        ├── /do <id> або кнопка ▶️ Виконати
                        │       ├── create_new:     копіює структуру зразка, Claude пише нові тексти
                        │       │                   → чернетка (draft) в WordPress
                        │       └── edit_existing:  Claude вносить точкову правку в поточний контент
                        │                           → autosave-ревізія (живий контент не змінюється)
                        │
                        └── /published <id>
                                └── фіксує baseline-метрики сторінки
                                    → через 14 днів analyst.py оцінить ефект

Файли стану (зберігаються в data/, комітяться GitHub Actions):
  metrics_history.json   — щоденна/тижнева статистика (до 90 записів)
  keyword_history.json   — позиції кожного запиту з часом
  recommendations.json   — бэклог: pending → draft_ready → published → impact_checked
  backlinks_done.json    — список виконаних backlink-майданчиків
  telegram_offset.json   — offset для Telegram getUpdates (щоб не обробляти старі команди)
```

---

## Команди в Telegram

| Команда | Що робить |
|---|---|
| Кнопка **▶️ Виконати** | підготувати чернетку/ревізію для рекомендації |
| Кнопка **❌ Відхилити** | відхилити рекомендацію |
| `/do <номер>` | те саме що кнопка Виконати |
| `/published <номер>` | зафіксувати що зміну опубліковано (запускає відлік 14 днів) |
| `/reject <номер>` | відхилити рекомендацію текстом |

---

## Безпека

- WordPress-користувач з роллю **Editor** (не Admin) — обмежений доступ на випадок витоку ключа
- Google-доступи тільки **read-only** (Viewer)
- Агент **ніколи не публікує** без ручного підтвердження — тільки чернетки і ревізії
- Усі ключі зберігаються в GitHub Secrets, не в коді
