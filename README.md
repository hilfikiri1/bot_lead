# Babrik Solutions — 1688 Catalog Bot

Репозиторий: https://github.com/hilfikiri1/1688_parser

Telegram-бот для автоматического формирования PDF-каталогов товаров с сайта [1688.com](https://www.1688.com).

Пользователь отправляет ссылку на товар → бот парсит страницу, переводит описание через OpenAI и возвращает фирменный PDF-каталог Babrik Solutions.

---

## Архитектура

```
Telegram (aiogram 3)
        │
        ▼
  TaskService ──► CatalogJob (PostgreSQL)
        │
        ├── URL Validator (SSRF-safe)
        ├── Playwright Parser (1688)
        ├── Image Downloader (Pillow)
        ├── OpenAI Responses API (Structured Output)
        ├── Jinja2 HTML Template
        └── Playwright page.pdf() → PDF
        │
        ▼
  Telegram Document + Cleanup

FastAPI /health  (параллельно с ботом)
```

**Уровни парсинга 1688:**
1. JSON из script-тегов, XHR-ответов, JSON-LD
2. DOM fallback через `selectors.py`
3. Частичный результат (минимум: название + 1 фото + ссылка)

---

## Быстрый старт

### 1. Создание Telegram-бота

1. Откройте [@BotFather](https://t.me/BotFather) в Telegram
2. Отправьте `/newbot`
3. Укажите имя и username бота
4. Скопируйте токен в `.env` → `TELEGRAM_BOT_TOKEN`

### 2. Получение OpenAI API key

1. Зарегистрируйтесь на [platform.openai.com](https://platform.openai.com)
2. Перейдите в **API keys** → **Create new secret key**
3. Скопируйте ключ в `.env` → `OPENAI_API_KEY`

### 3. Настройка `.env`

```bash
cp .env.example .env
# Отредактируйте .env — заполните TELEGRAM_BOT_TOKEN и OPENAI_API_KEY
```

### 4. Логотип

Поместите файл логотипа по пути:

```
app/catalog/static/logo.png
```

Если логотип отсутствует, PDF создаётся с текстовым логотипом «Babrik Solutions».

### 5. Запуск PostgreSQL

```bash
docker compose up -d db
```

### 6. Миграции Alembic

```bash
pip install -r requirements.txt
alembic upgrade head
```

### 7. Установка Playwright (локально)

```bash
pip install playwright
playwright install chromium
```

### 8. Ручной вход в 1688

```bash
python scripts/login_1688.py
```

1. Откроется окно Chromium
2. Войдите в аккаунт 1688 вручную
3. Нажмите Enter в терминале
4. Сессия сохранится в `storage/browser/1688_storage_state.json`

### 9. Локальный запуск

```bash
# Терминал 1 — API health-check
uvicorn app.main:app --host 0.0.0.0 --port 8000

# Терминал 2 — Telegram-бот (polling)
python -m app.main
```

Или только бот (API запускается вместе):

```bash
python -m app.main
```

### 10. Запуск через Docker Compose

```bash
docker compose up --build
```

Сервисы:
- `bot` — Telegram-бот + миграции
- `api` — FastAPI health-check на порту 8000
- `db` — PostgreSQL 16

### 11. Проверка health-check

```bash
curl http://localhost:8000/health
# {"status":"ok","service":"babrik-catalog-bot"}
```

### 12. Тестовая ссылка

1. Откройте бота в Telegram
2. Отправьте `/start`
3. Отправьте ссылку вида:
   ```
   https://detail.1688.com/offer/1234567890.html
   ```
4. Дождитесь PDF-документа

### 13. Ограничения парсинга 1688

- Страницы динамические — структура может меняться
- Требуется авторизованная сессия для большинства товаров
- CAPTCHA не обходится автоматически
- Цены и характеристики зависят от доступности данных на странице
- Максимум 12 изображений в PDF

### 14. CAPTCHA / авторизация

Если 1688 показывает CAPTCHA или страницу входа:

```bash
python scripts/login_1688.py
```

Бот ответит: «1688 запросил повторную авторизацию. Сообщите администратору бота.»

### 15. Изменение фирменных цветов

В `.env`:

```env
BRAND_PRIMARY_COLOR=#0B1F3A
BRAND_ACCENT_COLOR=#D8A34A
BRAND_TEXT_COLOR=#20242A
```

### 16. Замена логотипа

Замените файл `app/catalog/static/logo.png` (рекомендуемый размер: до 200×48 px, PNG с прозрачностью).

---

## Тесты

```bash
pip install -r requirements.txt
pytest -v
```

---

## Переменные окружения

| Переменная | Описание | По умолчанию |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота | — |
| `OPENAI_API_KEY` | Ключ OpenAI API | — |
| `OPENAI_MODEL` | Модель OpenAI | `gpt-5-mini` |
| `DATABASE_URL` | PostgreSQL async URL | `postgresql+asyncpg://postgres:postgres@db:5432/catalog_bot` |
| `BRAND_NAME` | Название бренда | `Babrik Solutions` |
| `BRAND_PRIMARY_COLOR` | Основной цвет | `#0B1F3A` |
| `BRAND_ACCENT_COLOR` | Акцентный цвет | `#D8A34A` |
| `BRAND_TEXT_COLOR` | Цвет текста | `#20242A` |
| `BRAND_LOGO_PATH` | Путь к логотипу | `app/catalog/static/logo.png` |
| `BRAND_WEBSITE` | Сайт компании | — |
| `BRAND_EMAIL` | Email | — |
| `BRAND_PHONE` | Телефон | — |
| `PLAYWRIGHT_HEADLESS` | Headless режим | `true` |
| `PLAYWRIGHT_TIMEOUT_SECONDS` | Таймаут страницы | `45` |
| `PLAYWRIGHT_STORAGE_STATE` | Путь к сессии 1688 | `storage/browser/1688_storage_state.json` |
| `MAX_CONCURRENT_JOBS` | Параллельные задачи | `2` |
| `MAX_IMAGES` | Макс. изображений | `12` |
| `MAX_IMAGE_SIZE_MB` | Макс. размер файла | `10` |
| `MAX_TOTAL_DOWNLOAD_MB` | Общий лимит загрузки | `100` |
| `PDF_RETENTION_HOURS` | Хранение PDF | `24` |
| `LOG_LEVEL` | Уровень логов | `INFO` |
| `DEBUG_SAVE_PAGE` | Сохранять HTML при ошибке | `false` |
| `RATE_LIMIT_SECONDS` | Лимит между запросами | `10` |

---

## Структура проекта

```
app/
  main.py                  # Точка входа: бот + API
  config.py                # Pydantic Settings
  exceptions.py            # Пользовательские исключения
  bot/                     # aiogram handlers
  parser/                  # Playwright парсер 1688
  ai/                      # OpenAI Structured Outputs
  catalog/                 # Jinja2 + PDF renderer
  database/                # SQLAlchemy + Alembic
  services/                # Бизнес-логика
  api/                     # FastAPI health
  utils/                   # Утилиты
scripts/
  login_1688.py            # Ручной вход в 1688
tests/                     # pytest
storage/
  temporary/               # Временные файлы задач
  output/                  # Готовые PDF
  browser/                 # Сессия Playwright
```

---

## Известные ограничения MVP

- Одна ссылка = одна задача
- Нет webhook-режима (только polling, архитектура готова к webhook)
- Парсинг зависит от структуры страниц 1688
- OpenAI не создаёт PDF — только структурированный текст
- Без настоящего логотипа используется текстовый fallback

---

## Места для настройки компании

1. `app/catalog/static/logo.png` — логотип
2. `.env` → `BRAND_NAME`, `BRAND_WEBSITE`, `BRAND_EMAIL`, `BRAND_PHONE`
3. `.env` → цвета бренда
4. `app/catalog/static/catalog.css` — стили PDF
5. `app/catalog/templates/catalog.html` — шаблон каталога
