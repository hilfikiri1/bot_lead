# Синхронизация лидов Kommo ↔ Google Sheets

## Роли систем

Google Sheets используется для оценки рекламы и качества лидов. Kommo
используется для текущей работы отдела продаж. Поэтому статусы этих систем не
должны совпадать:

- `W` — независимая маркетинговая оценка: SQL, MQL, Нецелевой, Игнор,
  Недозвон, Первый контакт или Сделка / Продажа;
- этап Kommo — фактическое состояние работы менеджера.

Синхронизация никогда не сравнивает и не изменяет колонку `W`.

## Что делает `/status_sync`

В режиме предпросмотра бот:

1. читает лист `FB` и сделки в польской воронке Kommo;
2. сопоставляет существующие записи по номеру из `Y` и префиксу названия
   Kommo;
3. сопоставляет новые записи по точному телефону, email или однозначному имени;
4. предлагает следующий номер как `MAX(Y и префиксов Kommo) + 1`;
5. предлагает название Kommo в формате `166 - Чай`;
6. пересобирает краткий маркетинговый комментарий в `X`: клиент, товар,
   бюджет, канал, регион, независимая оценка `W`, основание, этап Kommo и
   краткая история заметок;
7. отдельно показывает дубли и неоднозначные записи.

Автоматически ничего не записывается. После подтверждения бот повторно читает
оба источника. Если строка, номер, комментарий или название Kommo изменились,
соответствующее действие пропускается.

## Подтверждённая структура листа `FB`

- бюджет: `M`;
- канал связи: `N`;
- телефон: `O`;
- товар: `P`;
- регион: `Q`;
- имя: `R`;
- email: `T`;
- маркетинговый статус: `W` — только чтение;
- краткий комментарий: `X`;
- внутренний номер: `Y`.

## Railway

```env
GOOGLE_SHEETS_SPREADSHEET_ID=1s2ni7z6O73Drx-8zrbWu0ZzbL9hcBYl8MWsWDboKmJo
GOOGLE_SHEETS_WORKSHEET_NAME=FB
GOOGLE_SHEETS_BUDGET_COLUMN=M
GOOGLE_SHEETS_CHANNEL_COLUMN=N
GOOGLE_SHEETS_PHONE_COLUMN=O
GOOGLE_SHEETS_PRODUCT_COLUMN=P
GOOGLE_SHEETS_REGION_COLUMN=Q
GOOGLE_SHEETS_CLIENT_NAME_COLUMN=R
GOOGLE_SHEETS_EMAIL_COLUMN=T
GOOGLE_SHEETS_STATUS_COLUMN=W
GOOGLE_SHEETS_COMMENT_COLUMN=X
GOOGLE_SHEETS_LEAD_NUMBER_COLUMN=Y

GOOGLE_SHEETS_WRITE_ENABLED=false
LEAD_STATUS_SYNC_ENABLED=true
LEAD_STATUS_SYNC_INTERVAL_MINUTES=180
LEAD_STATUS_SYNC_INITIAL_DELAY_SECONDS=90
LEAD_STATUS_SYNC_NOTIFY_ONLY_ON_DIFFERENCES=true
```

Для ограничения одной воронкой:

```env
LEAD_STATUS_SYNC_PIPELINE_ID=
```

Если ID пустой, используется настроенная воронка меню/default.

Service account должен иметь Viewer для предпросмотра и Editor только после
того, как владелец разрешит подтверждаемые записи в `X/Y`.

## Безопасный rollout

1. Оставить `GOOGLE_SHEETS_WRITE_ENABLED=false`.
2. Запустить `/status_sync` и проверить пары, номера, комментарии и названия.
3. Убедиться, что в отчёте явно написано: `Статус W не изменяется`.
4. Дать service account роль Editor.
5. Установить `GOOGLE_SHEETS_WRITE_ENABLED=true`.
6. Повторить `/status_sync`, нажать `Подготовить обновление`, проверить
   предпросмотр и подтвердить.
7. Проверить одну новую строку: номер появился в `Y`, комментарий — в `X`,
   название Kommo начинается с того же номера.

Планировщик остаётся только уведомляющим и никогда не подтверждает запись.
