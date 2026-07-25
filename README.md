# Babrik Solutions 1688 Catalog Bot (MVP)

Telegram-бот принимает ссылку на товар 1688.com, извлекает данные через Playwright, структурирует русскоязычный контент через OpenAI Structured Outputs и формирует PDF-каталог в фирменном стиле Babrik Solutions.

## Архитектура

- `aiogram 3.x` — Telegram polling bot (готов к расширению под webhook).
- `FastAPI` — `/health` endpoint.
- `Playwright (Chromium)` — динамический парсинг карточек 1688 и HTML→PDF.
- `OpenAI Responses API` + `JSON Schema` — перевод и структурирование текста.
- `PostgreSQL` + `SQLAlchemy async` + `Alembic` — хранение задач `catalog_jobs`.
- `Jinja2 + CSS` — шаблон каталога.
- `Pillow` — нормализация изображений.

## Структура проекта

```text
app/
  main.py
  config.py
  logging_config.py
  exceptions.py
  bot/
    __init__.py
    handlers/
      __init__.py
      start.py
      product_link.py
    keyboards/__init__.py
    middlewares/__init__.py
    messages.py
  parser/
    __init__.py
    models.py
    url_validator.py
    browser.py
    parser_1688.py
    selectors.py
    image_downloader.py
    session_manager.py
  ai/
    __init__.py
    openai_client.py
    schemas.py
    prompts.py
  catalog/
    __init__.py
    formatting.py
    renderer.py
    models.py
    templates/catalog.html
    static/catalog.css
    static/fonts/.gitkeep
  database/
    __init__.py
    base.py
    session.py
    models.py
    repositories.py
    migrations/.gitkeep
  services/
    catalog_service.py
    task_service.py
    cleanup_service.py
  api/health.py
  utils/
    filenames.py
    retry.py
    images.py
scripts/
  login_1688.py
tests/
  fixtures/product_1688_sample.html
  test_url_validator.py
  test_openai_schema.py
  test_catalog_renderer.py
  test_parser_fixtures.py
storage/
  temporary/.gitkeep
  output/.gitkeep
  browser/.gitkeep
migrations/
  env.py
  versions/001_catalog_jobs.py
Dockerfile
docker-compose.yml
pyproject.toml
.env.example
alembic.ini
```

## 1) Создать Telegram-бота через BotFather

1. Откройте `@BotFather`.
2. Выполните `/newbot`.
3. Скопируйте токен в `.env` → `TELEGRAM_BOT_TOKEN`.

## 2) Получить OpenAI API key

1. Создайте ключ в OpenAI Console.
2. Сохраните в `.env` → `OPENAI_API_KEY`.

## 3) Заполнить `.env`

```bash
cp .env.example .env
```

Отредактируйте значения токенов и при необходимости бренд-настройки.

## 4) Куда положить логотип

Путь по умолчанию:

```text
app/catalog/static/logo.png
```

Если файл отсутствует, используется текстовый логотип.

## 5) Запустить PostgreSQL

```bash
docker compose up -d db
```

## 6) Выполнить Alembic migrations

Локально:

```bash
alembic upgrade head
```

В Docker миграции выполняются при старте сервиса `bot`.

## 7) Установить Playwright локально

```bash
pip install -e .
playwright install chromium
```

## 8) Выполнить ручной вход в 1688

```bash
python scripts/login_1688.py
```

Скрипт сохранит сессию в:

```text
storage/browser/1688_storage_state.json
```

## 9) Запуск проекта локально

```bash
python -m app.main
```

## 10) Запуск через Docker Compose

```bash
docker compose up --build
```

## 11) Проверка `/health`

```bash
curl http://localhost:8080/health
```

Ожидается:

```json
{"status":"ok"}
```

## 12) Отправка тестовой ссылки

1. Напишите боту `/start`.
2. Отправьте ссылку вида:
   `https://detail.1688.com/offer/xxxxxxxx.html`

## 13) Ограничения парсинга 1688

- 1688 динамический и может менять структуру.
- Часть полей может отсутствовать.
- При отсутствии цены в PDF выводится: `Цена уточняется у поставщика.`

## 14) Что делать при CAPTCHA

Если бот сообщает про CAPTCHA/авторизацию:

1. Повторите ручной вход `python scripts/login_1688.py`.
2. Проверьте актуальность `PLAYWRIGHT_STORAGE_STATE`.

## 15) Как изменить фирменные цвета

Измените в `.env`:

- `BRAND_PRIMARY_COLOR`
- `BRAND_ACCENT_COLOR`
- `BRAND_TEXT_COLOR`

## 16) Как заменить логотип

1. Положите PNG в `app/catalog/static/logo.png`.
2. Или измените путь `BRAND_LOGO_PATH` в `.env`.

## Команды разработки

```bash
pip install -e ".[dev]"
pytest
```

## Безопасность и SSRF-защита

- Разрешён только `https`.
- Домены только `1688.com` и поддомены.
- Блокируются `localhost` и внутренние IP.
- Повторная проверка домена после redirect.

## Примечания по MVP

- Одна задача = одна ссылка = один товар.
- Не обрабатываются массовые загрузки, CRM, оплата, anti-captcha сервисы.
- OpenAI не создаёт PDF — только структурирует текст, PDF генерируется локально через Jinja2 + Playwright.
