"""Single Telegram command catalog for the installed B&BS feature set."""
from __future__ import annotations

from app.services import telegram_service


COMMANDS = [
    {"command": "agent", "description": "Что умеет AI-агент"},
    {"command": "menu", "description": "Главное меню"},
    {"command": "plan", "description": "План на сегодня"},
    {"command": "digest", "description": "Приоритеты по сделкам"},
    {"command": "inbox", "description": "Операционный inbox"},
    {"command": "overdue", "description": "Просроченные действия"},
    {"command": "without_next", "description": "Сделки без следующего шага"},
    {"command": "waiting_us", "description": "Клиенты ждут нас"},
    {"command": "waiting_client", "description": "Мы ждём клиента"},
    {"command": "stale", "description": "Проекты без свежей активности"},
    {"command": "evening", "description": "Подвести итоги дня"},
    {"command": "week", "description": "Итоги и улучшения недели"},
    {"command": "jobs", "description": "Статус обработки аудио"},
    {"command": "status_sync", "description": "Обработать новые лиды Sheets"},
    {"command": "comment_sync", "description": "Сверить комментарии X с Kommo"},
    {"command": "drive_status", "description": "Диагностика Google Drive"},
    {"command": "diag", "description": "Полная диагностика системы"},
    {"command": "integration_status", "description": "Состояние интеграций"},
    {"command": "errors", "description": "Последние ошибки интеграций"},
    {"command": "notion_test", "description": "Проверить базы Notion"},
    {"command": "kommo_test", "description": "Проверить Kommo"},
    {"command": "calendar_test", "description": "Проверить Google Calendar"},
    {"command": "invite", "description": "Пригласить сотрудника"},
    {"command": "team", "description": "Пользователи и роли"},
    {"command": "bind_kommo", "description": "Привязать менеджера к Kommo"},
    {"command": "reset_memory", "description": "Очистить активный контекст"},
]


async def set_bot_commands() -> dict:
    """Register the current command list through the existing Telegram transport."""
    async with telegram_service.httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{telegram_service.TELEGRAM_API}/setMyCommands",
            json={"commands": COMMANDS},
        )
        telegram_service._ensure_success(response, "setMyCommands")
        return response.json()
