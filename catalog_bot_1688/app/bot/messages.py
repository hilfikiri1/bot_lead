"""All user-facing Russian text strings and status mappings."""

from __future__ import annotations

from app.database.models import JobStatus

START_MESSAGE = (
    "Отправьте ссылку на товар с сайта 1688.com. "
    "Я загружу фотографии, переведу информацию и подготовлю PDF-каталог."
)

LINK_RECEIVED = "Ссылка получена. Загружаю информацию о товаре…"

INVALID_URL = (
    "Пожалуйста, отправьте корректную ссылку на карточку товара сайта 1688.com."
)

ALREADY_RUNNING = (
    "Ваш предыдущий каталог ещё формируется. Дождитесь его завершения."
)

RATE_LIMITED = (
    "Слишком много запросов подряд. Подождите немного и отправьте ссылку снова."
)

SERVER_BUSY = (
    "Сейчас все обработчики заняты. Пожалуйста, попробуйте повторить запрос "
    "через минуту."
)

DOCUMENT_CAPTION = (
    "Каталог сформирован автоматически на основании информации поставщика "
    "с 1688.com. Цена и наличие требуют подтверждения перед оформлением заказа."
)

DONE = "Каталог готов."

# Progress texts shown by editing a single status message.
STATUS_TEXT: dict[JobStatus, str] = {
    JobStatus.RECEIVED: LINK_RECEIVED,
    JobStatus.VALIDATING: "Проверяю ссылку…",
    JobStatus.PARSING: "Открываю страницу 1688…",
    JobStatus.DOWNLOADING_IMAGES: "Загружаю фотографии…",
    JobStatus.GENERATING_CONTENT: "Перевожу и подготавливаю описание…",
    JobStatus.RENDERING_PDF: "Формирую PDF-каталог…",
    JobStatus.COMPLETED: DONE,
}


def status_text(status: JobStatus) -> str:
    return STATUS_TEXT.get(status, "Обрабатываю запрос…")
