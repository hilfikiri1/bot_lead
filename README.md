# Babrik Solutions 1688 Catalog Bot

Полностью рабочий MVP Telegram-бота: пользователь отправляет ссылку на карточку товара 1688.com, бот открывает страницу через Playwright Chromium, извлекает данные и изображения, получает структурированное русскоязычное содержание через OpenAI Responses API и локально формирует PDF-каталог Babrik Solutions через Jinja2 HTML + Playwright `page.pdf()`.

## Итоговая архитектура

```text
app/
  main.py                 # FastAPI health API
  config.py               # Pydantic Settings
  logging_config.py       # structlog JSON logs
  bot/                    # aiogram polling handlers
  parser/                 # URL validation, Playwright browser, 1688 parser, image downloader
  ai/                     # OpenAI prompts, JSON schema, Responses API client
  catalog/                # Jinja2 template, CSS, PDF renderer
  database/               # SQLAlchemy async models, repositories, Alembic migrations
  services/               # orchestration, task limiter, cleanup
  api/                    # health router
  utils/                  # filenames, retry, image helpers
scripts/login_1688.py     # manual 1688 login and storage_state save
tests/                    # offline tests and fixtures
storage/                  # temporary files, browser state, PDFs
```

## 1. Создать Telegram-бота через BotFather

1. Откройте Telegram и найдите `@BotFather`.
2. Выполните команду:

```text
/newbot
```

3. Задайте имя и username бота.
4. Скопируйте токен в `.env`:

```env
TELEGRAM_BOT_TOKEN=123456:telegram-token
```

## 2. Получить OpenAI API key

1. Откройте https://platform.openai.com/api-keys
2. Создайте ключ.
3. Добавьте в `.env`:

```env
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-5-mini
```

## 3. Заполнить `.env`

```bash
cp .env.example .env
nano .env
```

Минимально заполните `TELEGRAM_BOT_TOKEN` и `OPENAI_API_KEY`.

## 4. Куда положить логотип

По умолчанию приложение ищет логотип здесь:

```bash
app/catalog/static/logo.png
```

Если файла нет, PDF создаётся с текстовым логотипом `Babrik Solutions`.

## 5. Запустить PostgreSQL локально

```bash
docker compose up -d db
```

## 6. Выполнить Alembic migrations

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
```

Для локального запуска без Docker укажите локальный URL БД:

```env
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/catalog_bot
```

## 7. Установить Playwright локально

```bash
source .venv/bin/activate
playwright install --with-deps chromium
```

## 8. Выполнить ручной вход в 1688

1688 может требовать авторизацию или CAPTCHA. Автоматический обход CAPTCHA не реализуется.

```bash
source .venv/bin/activate
python scripts/login_1688.py
```

Войдите вручную в открытом Chromium и нажмите Enter в терминале. Сессия сохранится в:

```text
storage/browser/1688_storage_state.json
```

## 9. Запустить проект локально

В одном терминале API:

```bash
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Во втором терминале бот polling:

```bash
source .venv/bin/activate
python -m app.bot.runner
```

## 10. Запустить через Docker Compose

```bash
cp .env.example .env
# заполните .env
docker compose up --build
```

Сервисы:

- `db` — PostgreSQL;
- `api` — FastAPI health-check;
- `bot` — Telegram bot polling.

## 11. Проверить `/health`

```bash
curl http://localhost:8000/health
```

Ожидаемый ответ:

```json
{"status":"ok","service":"babrik-1688-catalog-bot"}
```

## 12. Отправить тестовую ссылку

1. Напишите боту `/start`.
2. Отправьте ссылку вида:

```text
https://detail.1688.com/offer/123456789.html
```

Бот будет редактировать одно статусное сообщение и затем отправит PDF документ.

## 13. Ограничения парсинга 1688

- 1688 динамический и меняет структуру страниц.
- Парсер использует стратегии JSON-first и DOM fallback selectors.
- Новые селекторы добавляются в `app/parser/selectors.py`.
- Минимальный успешный результат: название, хотя бы одно изображение, исходная ссылка.
- Если цена не найдена, PDF показывает: `Цена уточняется у поставщика.`

## 14. Что делать при CAPTCHA

Если бот пишет, что нужна повторная авторизация:

```bash
python scripts/login_1688.py
```

Не используйте антикапча-сервисы и не пытайтесь обходить проверку автоматически.

## 15. Изменить фирменные цвета

В `.env`:

```env
BRAND_PRIMARY_COLOR=#0B1F3A
BRAND_ACCENT_COLOR=#D8A34A
BRAND_TEXT_COLOR=#20242A
```

## 16. Заменить логотип

Положите PNG-файл:

```bash
cp /path/to/logo.png app/catalog/static/logo.png
```

Или задайте другой путь:

```env
BRAND_LOGO_PATH=/app/storage/brand/logo.png
```

## Переменные окружения

См. `.env.example`: Telegram token, OpenAI key/model, DATABASE_URL, брендовые цвета, Playwright session, лимиты изображений, semaphore, retention и logging.

## Тесты

Тесты не обращаются к живому 1688:

```bash
source .venv/bin/activate
pytest
```

Для PDF-теста нужен установленный Chromium Playwright.

## Сценарий ручного тестирования

1. Заполните `.env`.
2. Запустите `docker compose up --build`.
3. Проверьте `curl http://localhost:8000/health`.
4. Выполните `python scripts/login_1688.py`, если 1688 просит вход.
5. Отправьте боту `/start`.
6. Отправьте ссылку на карточку 1688.
7. Проверьте, что бот последовательно показывает статусы и отправляет PDF.
8. Повторно отправьте ссылку во время обработки и убедитесь, что бот отвечает: `Ваш предыдущий каталог ещё формируется. Дождитесь его завершения.`

## Безопасность

- Принимаются только HTTPS URL.
- Домен финальной страницы должен быть `1688.com` или поддоменом.
- Localhost, private/link-local/reserved/multicast IP блокируются.
- После redirect домен проверяется повторно.
- Секреты и cookies не пишутся в БД.

## Известные ограничения

- MVP обрабатывает один товар за одну задачу.
- Качество извлечения зависит от текущей верстки 1688 и доступности сессии.
- CAPTCHA и повторная авторизация решаются только ручным обновлением `storage_state`.
- OpenAI не получает все изображения; фотографии используются главным образом локальным PDF-рендерером.
- PDF не содержит QR-код, если не добавлена отдельная библиотека генерации QR.
