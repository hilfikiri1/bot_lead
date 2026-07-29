# Сверка статусов Kommo ↔ Google Sheets

## Что добавлено

Это дополнительная функция существующего Telegram-бота, а не отдельная
программа. Бот:

1. читает сделки и этапы Kommo;
2. читает реестр лидов Google Sheets;
3. сопоставляет сделки по внутреннему номеру из названия `110 - Товар` и
   колонки `Y`;
4. сравнивает этап Kommo со статусом в колонке `W`;
5. сообщает о несовпадениях, дублях и отсутствующих строках;
6. может изменить только колонку `W` после двойного подтверждения в Telegram.

Kommo эта функция никогда не изменяет.

## Подтверждённая структура таблицы

- Spreadsheet ID: `1s2ni7z6O73Drx-8zrbWu0ZzbL9hcBYl8MWsWDboKmJo`
- Лист: `FB`
- Телефон: `O`
- Товар: `P`
- Имя клиента: `R`
- Email: `T`
- Статус: `W`
- Внутренний номер лида: `Y`

## Railway: безопасный режим только чтения

Добавьте или проверьте переменные Web service:

```env
GOOGLE_SHEETS_SPREADSHEET_ID=1s2ni7z6O73Drx-8zrbWu0ZzbL9hcBYl8MWsWDboKmJo
GOOGLE_SHEETS_WORKSHEET_NAME=FB
GOOGLE_SHEETS_PHONE_COLUMN=O
GOOGLE_SHEETS_PRODUCT_COLUMN=P
GOOGLE_SHEETS_CLIENT_NAME_COLUMN=R
GOOGLE_SHEETS_EMAIL_COLUMN=T
GOOGLE_SHEETS_STATUS_COLUMN=W
GOOGLE_SHEETS_LEAD_NUMBER_COLUMN=Y

GOOGLE_SHEETS_WRITE_ENABLED=false
LEAD_STATUS_SYNC_ENABLED=true
LEAD_STATUS_SYNC_INTERVAL_MINUTES=180
LEAD_STATUS_SYNC_INITIAL_DELAY_SECONDS=90
LEAD_STATUS_SYNC_NOTIFY_ONLY_ON_DIFFERENCES=true
```

Для ограничения одной воронкой укажите её ID:

```env
LEAD_STATUS_SYNC_PIPELINE_ID=
```

Если значение пустое, используется `KOMMO_MENU_PIPELINE_ID`, затем
`KOMMO_DEFAULT_PIPELINE_ID`. Если все три значения пустые, проверяются все
воронки.

Для чтения нужен Google service account. Можно использовать уже настроенный
`GOOGLE_SERVICE_ACCOUNT_JSON_BASE64`; таблица должна быть расшарена на его
`client_email` с правом Viewer.

Периодические отчёты отправляются пользователям из
`ALLOWED_TELEGRAM_USER_IDS`. Отдельный Telegram API-ключ или ссылка на чат не
нужны: используется существующий бот.

## Как проверить после deployment

1. Откройте существующего бота.
2. Выполните `/menu`.
3. Нажмите `🔄 Сверка статусов` или выполните `/status_sync`.
4. Проверьте предложенные изменения и списки:
   - только в таблице;
   - только в Kommo;
   - дубли внутренних номеров;
   - сделки без внутреннего номера.

При `GOOGLE_SHEETS_WRITE_ENABLED=false` бот не сможет изменить таблицу.

## Как отдельно разрешить подтверждаемые обновления

Только после проверки отчёта:

1. дайте Google service account право Editor для этой таблицы;
2. установите в Railway:

```env
GOOGLE_SHEETS_WRITE_ENABLED=true
```

3. снова запустите `/status_sync`;
4. нажмите `Подготовить обновление`;
5. проверьте предупреждение;
6. нажмите `Да, обновить`.

Перед записью бот повторно читает Kommo и таблицу. Если этап, строка или старое
значение изменились после отчёта, запись отменяется и требуется новое
подтверждение.

Планировщик всегда работает только в режиме отчёта и никогда не записывает
статусы автоматически.
