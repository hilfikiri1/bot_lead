# Babrik Solutions — Product Catalog Bot

Telegram-бот для автоматической генерации PDF-каталогов компании **Babrik Solutions** на основании страниц товаров с сайта [1688.com](https://www.1688.com).

Пользователь отправляет боту ссылку на товар — бот извлекает данные, переводит их через OpenAI и возвращает готовый PDF-каталог в фирменном стиле.

---

## Содержание

1. [Быстрый старт через Docker Compose](#быстрый-старт)
2. [Создание Telegram-бота](#1-создание-telegram-бота)
3. [Получение OpenAI API Key](#2-получение-openai-api-key)
4. [Заполнение .env](#3-заполнение-env)
5. [Логотип компании](#4-логотип-компании)
6. [Запуск PostgreSQL](#5-запуск-postgresql)
7. [Alembic миграции](#6-alembic-миграции)
8. [Установка Playwright локально](#7-установка-playwright-локально)
9. [Ручной вход в 1688](#8-ручной-вход-в-1688)
10. [Запуск локально](#9-запуск-локально)
11. [Запуск через Docker Compose](#10-запуск-через-docker-compose)
12. [Health check](#11-проверка-health)
13. [Тестовая ссылка](#12-тестовая-ссылка)
14. [Ограничения парсинга 1688](#13-ограничения-парсинга-1688)
15. [Что делать при CAPTCHA](#14-что-делать-при-captcha)
16. [Фирменные цвета](#15-фирменные-цвета)
17. [Замена логотипа](#16-замена-логотипа)
18. [Архитектура проекта](#архитектура)

---

## Быстрый старт

```bash
git clone <repo-url>
cd <repo>

cp .env.example .env
# Отредактируйте .env — добавьте TELEGRAM_BOT_TOKEN и OPENAI_API_KEY

docker compose up --build
```

---

## 1. Создание Telegram-бота

1. Откройте Telegram и найдите [@BotFather](https://t.me/BotFather).
2. Отправьте `/newbot`.
3. Введите имя бота, например: `Babrik Solutions Catalog`.
4. Введите username бота, например: `babrik_catalog_bot`.
5. BotFather пришлёт **токен** вида `123456789:AAF...`.
6. Скопируйте токен в `.env`:
   ```env
   TELEGRAM_BOT_TOKEN=123456789:AAF...
   ```

---

## 2. Получение OpenAI API Key

1. Зайдите на [platform.openai.com](https://platform.openai.com).
2. Перейдите в **API keys** → **Create new secret key**.
3. Скопируйте ключ в `.env`:
   ```env
   OPENAI_API_KEY=sk-...
   ```
4. Убедитесь, что у аккаунта есть баланс или активная подписка.

---

## 3. Заполнение .env

Скопируйте `.env.example` в `.env` и заполните обязательные поля:

```bash
cp .env.example .env
```

Обязательные переменные:

| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен бота от BotFather |
| `OPENAI_API_KEY` | Ключ OpenAI API |
| `DATABASE_URL` | URL PostgreSQL (в Docker настроен автоматически) |

Опциональные (брендинг):

| Переменная | По умолчанию |
|---|---|
| `BRAND_NAME` | `Babrik Solutions` |
| `BRAND_PRIMARY_COLOR` | `#0B1F3A` |
| `BRAND_ACCENT_COLOR` | `#D8A34A` |
| `BRAND_TEXT_COLOR` | `#20242A` |
| `BRAND_WEBSITE` | пусто |
| `BRAND_EMAIL` | пусто |
| `BRAND_PHONE` | пусто |

---

## 4. Логотип компании

Поместите файл логотипа по пути:

```
app/catalog/static/logo.png
```

Рекомендуемый формат: PNG с прозрачным фоном, минимум 200×60 px.

**Если логотип отсутствует** — бот продолжит работу: вместо изображения будет отображаться текстовое название компании. Ошибки не будет.

---

## 5. Запуск PostgreSQL

### Локально через Docker:

```bash
docker run -d \
  --name catalog_bot_db \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=catalog_bot \
  -p 5432:5432 \
  postgres:16-alpine
```

### Через docker-compose:

PostgreSQL запускается автоматически (см. секцию `db` в `docker-compose.yml`).

---

## 6. Alembic миграции

### Первый запуск (создание таблиц):

```bash
alembic upgrade head
```

### Создание новой миграции (при изменении моделей):

```bash
alembic revision --autogenerate -m "description"
alembic upgrade head
```

### Откат:

```bash
alembic downgrade -1
```

---

## 7. Установка Playwright локально

```bash
pip install playwright==1.49.0
playwright install chromium --with-deps
```

На Linux дополнительно установите системные зависимости:

```bash
playwright install-deps chromium
```

---

## 8. Ручной вход в 1688

1688 может потребовать авторизацию. Для сохранения сессии:

```bash
python scripts/login_1688.py
```

Скрипт:
1. Откроет браузер Chromium в видимом режиме.
2. Дождётся вашего входа в аккаунт 1688.
3. После нажатия ENTER сохранит сессию в `storage/browser/1688_storage_state.json`.

Бот будет автоматически использовать эту сессию.

> ⚠️ Сессия периодически истекает. Если бот сообщает об ошибке авторизации — повторите вход.

---

## 9. Запуск локально

### Установка зависимостей:

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -e ".[dev]"
```

### Запуск бота:

```bash
cp .env.example .env
# Заполните .env

alembic upgrade head

python -m app.main
```

Бот доступен по адресу: `http://localhost:8000`

---

## 10. Запуск через Docker Compose

```bash
cp .env.example .env
# Заполните TELEGRAM_BOT_TOKEN и OPENAI_API_KEY

docker compose up --build
```

Для фоновой работы:

```bash
docker compose up -d --build
```

Просмотр логов:

```bash
docker compose logs -f bot
```

Остановка:

```bash
docker compose down
```

---

## 11. Проверка Health

```bash
curl http://localhost:8000/api/health
```

Ожидаемый ответ:

```json
{
  "status": "ok",
  "timestamp": "2025-01-15T10:00:00.000000+00:00",
  "database": "ok"
}
```

---

## 12. Тестовая ссылка

После запуска бота:

1. Откройте Telegram и найдите своего бота.
2. Отправьте `/start`.
3. Отправьте ссылку на товар 1688, например:
   ```
   https://detail.1688.com/offer/123456789.html
   ```
4. Дождитесь PDF-каталога (обычно 30–120 секунд).

---

## 13. Ограничения парсинга 1688

- 1688 — динамический сайт с защитой от ботов. Парсинг не гарантируется на 100%.
- Структура страниц может меняться без предупреждения. При сбоях добавляйте новые CSS-селекторы в `app/parser/selectors.py`.
- Боты обнаруживаются через поведенческий анализ. Сохранённая сессия снижает, но не исключает вероятность блокировки.
- При отсутствии цены в PDF будет написано: «Цена уточняется у поставщика».
- Максимум 12 изображений в одном PDF (8 из галереи + 4 из описания).
- Запросы выполняются последовательно: не более `MAX_CONCURRENT_JOBS` одновременно.

---

## 14. Что делать при CAPTCHA

Если бот сообщает об ошибке авторизации или CAPTCHA:

1. Запустите скрипт входа:
   ```bash
   python scripts/login_1688.py
   ```
2. В открывшемся браузере пройдите CAPTCHA и войдите в аккаунт.
3. Нажмите ENTER в терминале.
4. Перезапустите бота или docker compose:
   ```bash
   docker compose restart bot
   ```

> Автоматический обход CAPTCHA не реализован намеренно — это противоречит Terms of Service 1688.

---

## 15. Фирменные цвета

Измените в `.env`:

```env
BRAND_PRIMARY_COLOR=#0B1F3A   # Тёмно-синий (заголовки)
BRAND_ACCENT_COLOR=#D8A34A    # Золотой (акценты)
BRAND_TEXT_COLOR=#20242A      # Основной текст
```

После изменения перезапустите бота. Изменения вступят в силу немедленно.

---

## 16. Замена логотипа

1. Подготовьте PNG-файл с прозрачным фоном (рекомендуется ≥ 200×60 px).
2. Сохраните как `app/catalog/static/logo.png`.
3. Путь настраивается через `.env`:
   ```env
   BRAND_LOGO_PATH=app/catalog/static/logo.png
   ```
4. Перезапустите бота.

---

## Архитектура

```
app/
  main.py                  # FastAPI + aiogram startup
  config.py                # Pydantic Settings
  logging_config.py        # structlog configuration
  exceptions.py            # Custom exceptions

  bot/
    handlers/
      start.py             # /start command
      product_link.py      # Main URL handler + pipeline orchestration
    middlewares/
      rate_limit.py        # Per-user rate limiting
    messages.py            # User-facing text strings

  parser/
    url_validator.py       # SSRF-safe URL validation with allowlist
    session_manager.py     # Shared Playwright browser lifecycle
    browser.py             # Page open + captcha detection + scroll
    parser_1688.py         # Multi-level parser (JSON → DOM fallback)
    selectors.py           # All CSS selectors in one place
    image_downloader.py    # Async image download, dedup, resize
    models.py              # ParsedProduct Pydantic models

  ai/
    openai_client.py       # OpenAI Responses API with retries
    schemas.py             # CatalogContent model + JSON Schema
    prompts.py             # System prompt + user message builder

  catalog/
    renderer.py            # Jinja2 → HTML → PDF via Playwright
    models.py              # RenderContext dataclass
    templates/catalog.html # Jinja2 HTML template
    static/catalog.css     # Brand stylesheet
    static/logo.png        # ← Place your logo here

  database/
    base.py                # SQLAlchemy DeclarativeBase
    models.py              # CatalogJob ORM model
    session.py             # AsyncEngine + session factory
    repositories.py        # CatalogJobRepository
    migrations/            # Alembic migration files

  services/
    task_service.py        # Job CRUD operations
    catalog_service.py     # Full pipeline orchestration
    cleanup_service.py     # Periodic temp file cleanup

  api/
    health.py              # GET /api/health

  utils/
    filenames.py           # Safe PDF filename generation
    retry.py               # Async retry decorator
    images.py              # Image processing utilities

scripts/
  login_1688.py            # Manual 1688 session save

tests/
  test_url_validator.py
  test_openai_schema.py
  test_filenames.py
  test_parser_models.py
  test_image_dedup.py
  test_catalog_renderer.py
  test_parser_fixtures.py
```

### Поток данных

```
User → Telegram → Bot Handler
         ↓
   URLValidator (SSRF protection)
         ↓
   TaskService.create_job() → PostgreSQL
         ↓
   BrowserSessionManager → Playwright Chromium
         ↓
   Parser1688 (JSON embedded → DOM fallback)
         ↓
   ImageDownloader (async, dedup, resize)
         ↓
   OpenAIClient (Structured Outputs)
         ↓
   PDFRenderer (Jinja2 → HTML → Playwright PDF)
         ↓
   Bot → Telegram (send_document)
         ↓
   CleanupService (delete temp files)
```

---

## Запуск тестов

```bash
pip install -e ".[dev]"
pytest
```

Тесты не обращаются к реальному 1688 или OpenAI. PDF-тест запускается только если установлен Playwright.

---

## Известные ограничения

1. Одна ссылка = один каталог (MVP). Множественный выбор товаров не реализован.
2. Сессия 1688 периодически истекает — требуется ручное обновление.
3. При агрессивной защите 1688 от ботов — данные могут быть неполными.
4. OpenAI Structured Outputs требует поддерживающую модель (`gpt-4o`, `gpt-4o-mini` и выше).
5. Webhook-режим не настроен в MVP — бот работает через polling.

---

## Места для добавления данных компании

| Что изменить | Где |
|---|---|
| Логотип | `app/catalog/static/logo.png` |
| Название компании | `.env` → `BRAND_NAME` |
| Цвета | `.env` → `BRAND_PRIMARY_COLOR`, `BRAND_ACCENT_COLOR` |
| Сайт / email / телефон | `.env` → `BRAND_WEBSITE`, `BRAND_EMAIL`, `BRAND_PHONE` |
| Текст дисклеймера | `app/ai/prompts.py` → `CATALOG_DISCLAIMER` |
| HTML-шаблон | `app/catalog/templates/catalog.html` |
| CSS стили | `app/catalog/static/catalog.css` |
