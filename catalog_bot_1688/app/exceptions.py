"""Domain-specific exceptions for the catalog pipeline.

Each exception carries a ``user_message`` (short, safe, Russian text shown to the
Telegram user) and an ``error_code`` (stored in the database and logs). Tracebacks
are never exposed to the user.
"""

from __future__ import annotations


class CatalogError(Exception):
    """Base class for all catalog pipeline errors."""

    error_code: str = "internal_error"
    user_message: str = (
        "Не удалось сформировать каталог из-за временной ошибки. "
        "Попробуйте повторить запрос позже."
    )

    def __init__(self, message: str | None = None, *, user_message: str | None = None):
        super().__init__(message or self.__class__.__name__)
        if user_message is not None:
            self.user_message = user_message


class InvalidProductUrlError(CatalogError):
    error_code = "invalid_url"
    user_message = (
        "Не удалось распознать ссылку. Отправьте ссылку на карточку товара 1688.com."
    )


class UnsupportedDomainError(CatalogError):
    error_code = "unsupported_domain"
    user_message = (
        "Пожалуйста, отправьте корректную ссылку на карточку товара сайта 1688.com."
    )


class ProductPageNotFoundError(CatalogError):
    error_code = "page_not_found"
    user_message = "Страница товара недоступна или товар был удалён."


class AuthenticationRequiredError(CatalogError):
    error_code = "auth_required"
    user_message = "1688 запросил повторную авторизацию. Сообщите администратору бота."


class CaptchaDetectedError(CatalogError):
    error_code = "captcha_detected"
    user_message = (
        "1688 запросил повторную авторизацию или проверку CAPTCHA. "
        "Администратору необходимо обновить сессию 1688."
    )


class ProductDataNotFoundError(CatalogError):
    error_code = "no_product_data"
    user_message = (
        "Не удалось получить основные данные товара. "
        "Возможно, 1688 ограничил доступ к странице."
    )


class ImageDownloadError(CatalogError):
    error_code = "image_download_failed"
    user_message = (
        "Не удалось загрузить фотографии товара. Попробуйте повторить запрос позже."
    )


class OpenAIProcessingError(CatalogError):
    error_code = "openai_failed"
    user_message = (
        "Не удалось подготовить описание товара из-за временной ошибки. "
        "Попробуйте повторить запрос позже."
    )


class PdfRenderingError(CatalogError):
    error_code = "pdf_render_failed"
    user_message = (
        "Не удалось сформировать PDF-каталог из-за временной ошибки. "
        "Попробуйте повторить запрос позже."
    )
