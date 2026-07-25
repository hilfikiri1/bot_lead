# Babrik 1688 Catalog — Chrome Extension

Расширение для сбора списка товаров с **1688.com** и формирования PDF-каталога в стиле **Babrik Solutions** через backend `buybring`.

## Возможности

- Парсинг товаров со страниц поиска, магазина и категорий 1688
- Извлечение: название, цена, фото, название фабрики/поставщика, ссылка
- Отправка выбранных товаров на API `/api/catalog/batch`
- AI-перевод и формирование PDF на сервере
- Скачивание готового PDF из popup

## Установка (режим разработчика)

1. Запустите backend `buybring` и задайте в `.env`:
   ```env
   CATALOG_ENABLED=true
   CATALOG_EXTENSION_API_KEY=your-secret-key
   ADMIN_API_KEY=your-secret-key
   ```
2. Откройте Chrome → `chrome://extensions`
3. Включите **Режим разработчика**
4. **Загрузить распакованное расширение** → выберите папку `extension/`
5. Откройте **Настройки расширения** и укажите:
   - URL API (например `http://localhost:8000`)
   - API ключ (`CATALOG_EXTENSION_API_KEY`)

## Использование

1. Войдите в 1688.com в том же профиле Chrome
2. Откройте страницу с товарами (поиск, витрина магазина)
3. Нажмите иконку расширения → **Обновить список**
4. Выберите товары → **Сформировать PDF**
5. После готовности нажмите **Скачать PDF**

## API

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/api/catalog/batch` | Создать batch job |
| GET | `/api/catalog/jobs/{id}` | Статус job |
| GET | `/api/catalog/jobs/{id}/download` | Скачать PDF |

Авторизация: заголовок `Authorization: Bearer <API_KEY>`.

## Структура

```
extension/
├── manifest.json
├── icons/
├── src/
│   ├── background.js
│   ├── content/
│   │   ├── selectors.js
│   │   ├── parser.js
│   │   └── index.js
│   ├── popup/
│   └── options/
└── README.md
```

## Примечания

- Расширение использует сессию пользователя в браузере; cookies на сервер не передаются.
- Лимит товаров задаётся `CATALOG_MAX_PRODUCTS_PER_BATCH` (по умолчанию 20).
- Для отправки PDF в Telegram укажите Telegram user ID в настройках расширения.
