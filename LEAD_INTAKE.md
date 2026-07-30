# Приём новых лидов Facebook (`app/services/lead_intake`)

Отдельный, надёжный конвейер для входящих/неразобранных сделок Kommo,
созданных формами Facebook Lead Ads. Обрабатывает лиды **по одному**, с
постоянным ID, безопасной нумерацией через PostgreSQL, структурированным
AI-анализом и полным подтверждением в Telegram перед любой записью.

Это отдельная функция от `/status_sync` (см. [`STATUS_SYNC_SETUP.md`](STATUS_SYNC_SETUP.md)),
которая делает сверку всего реестра Sheets ↔ Kommo. `/new_leads` решает
конкретную проблему: `Facebook #...`/`Facebook №...` лиды, которые ещё не
приняты в воронку, поэтому не видны обычному поиску `/api/v4/leads`.

## Почему старый процесс не находил лиды

1. Новые Facebook-лиды попадают в очередь Kommo «Неразобранное»
   (`/api/v4/leads/unsorted`), а не в обычную воронку. Отчёт `/status_sync`
   искал совпадения только через `get_all_leads_for_status_sync()`
   (обычная `/api/v4/leads`, привязанная к одной сконфигурированной
   воронке) — поэтому «Надёжно найдено в Kommo: 0», даже когда лид реально
   существует.
2. Сравнение телефонов не учитывало код страны последовательно в разных
   местах кода: `kommo_service._contact_has_exact_value` сравнивало «сырые»
   цифры без кода страны, поэтому `728387128` (форма Facebook) и
   `+48 728 387 128` (таблица) не совпадали, хотя это один номер.
3. Активный monkeypatch `install_lead_registry_runtime()` присваивал `Y`
   **номер строки таблицы**, а не настоящий последовательный внутренний
   номер, независимо от того, что уже было в названии Kommo — источник
   дублей/несостыковок номеров.
4. Не было ни одной durable записи о прогрессе одного лида: всё
   пересчитывалось заново при каждом запуске отчёта, поэтому частичный сбой
   Kommo/Sheets не имел из чего резюмироваться, кроме повторного дифа.

`/new_leads` фиксирует это: постоянный ID с первого момента, единая
проверенная нормализация телефона, конкурентно-безопасная нумерация через
PostgreSQL, и durable state machine (`lead_processing_jobs`) как источник
истины для каждого шага.

## Рабочий процесс

1. **Обнаружение.** `kommo_service.get_all_unreviewed_leads()` (реально
   читает `/api/v4/leads/unsorted`) отдаёт входящие сделки; отбираются
   только с названием `Facebook #...`/`Facebook №...`. Сразу создаётся
   строка `lead_processing_jobs` с постоянным `kommo_lead_id` и снимком
   всех полей (Facebook Lead ID, телефон, email, товар, бюджет, канал,
   регион, дата создания). Дальше `Facebook #...` название никогда не
   используется для повторного поиска — только `kommo_lead_id`.
2. **Сопоставление со строкой Google Sheets** — приоритет:
   `Facebook Lead ID` → телефон+email → уникальный телефон → уникальный
   email. Товар/имя/регион — только вспомогательные подсказки, никогда не
   ключ сопоставления. Несколько совпадений → Telegram присылает кнопки
   выбора строки (`[167] [181] [Skip]`) и причину (`duplicate_phone`,
   `duplicate_email`, `no_matching_row`, `conflicting_facebook_id`,
   `assigned_number_conflict`, `missing_required_fields`).
3. **Номер.** Если у строки уже есть `Y` — он переиспользуется. Если нет —
   выделяется следующий безопасный номер через
   `app/services/lead_intake/numbering.py`
   (`pg_advisory_xact_lock` + `SELECT ... FOR UPDATE` + `UNIQUE` backstop).
4. **AI-квалификация.** `app/services/lead_intake/ai_service.py` вызывает
   OpenAI со Structured Outputs (JSON Schema, `strict: true`), с фолбэком
   на `json_object`, и всегда валидирует ответ через Pydantic
   (`app/services/lead_intake/schema.py`). Невалидный ответ → одна попытка
   починки → при повторном провале лид помечается `error`, в Kommo ничего
   не пишется.
5. **Telegram preview.** До нажатия `Apply` ни Kommo, ни Sheets не
   меняются — создаются/обновляются только записи
   `lead_processing_jobs`. Кнопки: `✅ Apply`, `✏️ Edit`, `⏭ Skip`, а также
   `📲 Send WhatsApp` или `📞 Prepare call` в зависимости от рекомендованного
   действия.
6. **Apply — чек-пойнтед saga.** `app/services/lead_intake/service.py:apply_job`
   выполняет по порядку: подтвердить существование лида → подтвердить
   номер свободен → записать `Y` → перечитать и проверить → переименовать
   Kommo → перевести на «Первый контакт» → добавить примечание → создать
   задачу → `completed`. Каждый шаг помечается в `current_checkpoint`;
   повтор (ретрай, повторный клик, второй webhook от Telegram) продолжает с
   первого незавершённого шага и никогда не повторяет уже выполненные
   внешние операции.

## Статусы `lead_processing_jobs`

```
detected → matching → matched → number_assigned → ai_generated →
waiting_approval → applying → completed
                                  ↘ skipped
                                  ↘ manual_match_required
                                  ↘ error (Retry возобновляет с checkpoint)
```

## Защита от дублей

- **Google Sheets** — пишется только колонка `GOOGLE_SHEETS_LEAD_NUMBER_COLUMN`
  (`Y` по умолчанию) через `google_sheets_service.write_internal_lead_number`,
  которая физически не знает о `W`/`X`. Перед записью строка перечитывается
  и сверяется по «отпечатку» (телефон/email/имя/товар); после записи —
  повторное чтение и проверка совпадения значения.
- **Kommo note** — маркер `[AUTO_LEAD_ANALYSIS_V{version}:{kommo_lead_id}]`
  в конце примечания; перед созданием сканируются последние примечания.
- **Kommo task** — маркер `[AUTO_LEAD_TASK_V{version}:{kommo_lead_id}:primary]`
  внутри текста задачи; перед созданием сканируются открытые задачи.
  (Kommo API v4 не даёт скрытое техническое поле для задач, поэтому маркер
  виден в тексте — компромисс, зафиксированный явно.)
- **Внутренний номер** — `UNIQUE` на `lead_processing_jobs.assigned_number`
  плюс конкурентно-безопасный аллокатор.
- **Telegram callback** — каждый `lp:*` callback идемпотентен: повторный
  `lp:apply:<id>` после `completed` возвращает «уже обработан», не повторяя
  внешние вызовы.

## WhatsApp / звонок

- Если рекомендованное действие — WhatsApp: `Apply` выполняет
  переименование/этап/примечание, но задачу-фоллоу-ап откладывает до
  подтверждения `✅ Message sent` (кнопка показывает готовый текст и
  `wa.me`-ссылку с URL-encoded сообщением).
- Если рекомендованное действие — звонок: `Apply` сразу создаёт задачу
  «позвонить», кнопка `📞 Prepare call` показывает сценарий (вопросы,
  возражения, ответы), а `✅ Поговорили` / `📵 Не ответил` / `📅 Перенести`
  / `❌ Номер неверный` сохраняют результат отдельным примечанием в Kommo.
  Автоматическое сообщение «в конце рабочего дня» из спецификации реализовано
  как **ручной, а не запланированный** триггер (кнопки доступны сразу после
  Apply) — полноценный cron/celery-шедулер для этого не входил в объём
  данного изменения и отмечен как последующая работа.

## Dry-run

```env
LEAD_PROCESSING_DRY_RUN=true
```

В dry-run режиме: чтение Kommo/Sheets, сопоставление, AI-анализ и Telegram
preview работают как обычно; номер только «подсматривается»
(`numbering.peek_next_number`, счётчик не расходуется); кнопка `Apply`
подписана `🧪 Apply (dry-run)` и не выполняет ни одной внешней записи
(`apply_job` возвращает `status="dry_run"` до первого внешнего вызова).

## Переменные окружения

```env
KOMMO_POLAND_PIPELINE_ID=
KOMMO_FIRST_CONTACT_STATUS_ID=
KOMMO_FIRST_CONTACT_STATUS_NAME=Первый контакт
GOOGLE_SHEETS_FACEBOOK_LEAD_ID_COLUMN=
TELEGRAM_APPROVAL_CHAT_ID=
LEAD_PROCESSING_DRY_RUN=false
LEAD_PROCESSING_TIMEZONE=Europe/Warsaw
LEAD_PROCESSING_BUSINESS_START=09:00
LEAD_PROCESSING_BUSINESS_END=18:00
LEAD_PROCESSING_VERSION=1
```

Уже существующие переменные переиспользуются без дублирования:
`GOOGLE_SHEETS_LEAD_NUMBER_COLUMN`, `GOOGLE_SHEETS_PHONE_COLUMN`,
`GOOGLE_SHEETS_EMAIL_COLUMN`, `GOOGLE_SHEETS_STATUS_COLUMN` (`W`, не
меняется), `GOOGLE_SHEETS_COMMENT_COLUMN` (`X`, не меняется),
`GOOGLE_SHEETS_WRITE_ENABLED`, `KOMMO_UNREVIEWED_*`,
`MANAGER_TIMEZONE` (используется, если `LEAD_PROCESSING_TIMEZONE` пуст),
`OPENAI_API_KEY`, `OPENAI_MODEL`.

## Значения, требующие ручной настройки

- `KOMMO_POLAND_PIPELINE_ID` — technical ID воронки «Польша»;
- `KOMMO_FIRST_CONTACT_STATUS_ID` — technical ID этапа «Первый контакт»
  внутри этой воронки (если не задан, бот ищет этап по имени);
- `GOOGLE_SHEETS_FACEBOOK_LEAD_ID_COLUMN` — буква колонки, если таблица
  хранит Facebook Lead ID (иначе сопоставление идёт по телефону/email);
- `TELEGRAM_APPROVAL_CHAT_ID` — чат для preview, если он должен отличаться
  от чата, из которого запущена команда;
- `LEAD_PROCESSING_BUSINESS_START` / `_END`, `LEAD_PROCESSING_TIMEZONE`.

## Тесты

```bash
pytest -q tests/test_lead_intake_phone_utils.py
pytest -q tests/test_lead_intake_matching.py
pytest -q tests/test_lead_intake_numbering.py
pytest -q tests/test_lead_intake_ai_schema.py
pytest -q tests/test_lead_intake_detection.py
pytest -q tests/test_lead_intake_sheets_write.py
pytest -q tests/test_lead_intake_service.py
pytest -q tests/test_lead_intake_telegram.py
pytest -q tests/test_lead_intake_product_translation.py
```

или всё сразу: `pytest -q -k lead_intake`.

## Проверить сценарий Andrzej Janka вручную

1. Создайте (или используйте существующую тестовую) сделку Kommo в
   Неразобранном с названием `Facebook #12312412`, телефоном `728387128`,
   email `jan_ovo@wp.pl`, регионом/товаром/бюджетом как в задаче.
2. В тестовой строке Google Sheets убедитесь, что телефон/email совпадают и
   `Y` уже равен `167` (или оставьте `Y` пустым, чтобы проверить выделение
   нового номера).
3. Установите `LEAD_PROCESSING_DRY_RUN=true` для первого прогона и
   выполните `/new_leads` в Telegram — проверьте, что preview показывает
   `167 - Инструменты`, приоритет `C — квалификация`, рекомендацию
   WhatsApp и готовое польское сообщение, и явный баннер
   `🧪 DRY RUN — изменения не применяются`.
4. Отключите dry-run (`LEAD_PROCESSING_DRY_RUN=false`), включите
   `GOOGLE_SHEETS_WRITE_ENABLED=true`, повторите `/new_leads`, нажмите
   `✅ Apply` — проверьте `Y=167` в таблице, новое название и этап сделки в
   Kommo, одно примечание, одну задачу.
5. Повторно нажмите `Apply` (или повторите `/new_leads`) — бот должен
   ответить, что лид уже обработан, без повторной записи.
