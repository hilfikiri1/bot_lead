from __future__ import annotations


class CatalogBotError(Exception):
    """Base exception for all catalog bot errors."""

    user_message: str = "Произошла ошибка. Попробуйте повторить запрос позже."


class InvalidProductUrlError(CatalogBotError):
    user_message = "Не удалось распознать ссылку. Отправьте ссылку на карточку товара 1688.com."


class UnsupportedDomainError(CatalogBotError):
    user_message = "Пожалуйста, отправьте корректную ссылку на карточку товара сайта 1688.com."


class ProductPageNotFoundError(CatalogBotError):
    user_message = "Страница товара недоступна или товар был удалён."


class AuthenticationRequiredError(CatalogBotError):
    user_message = "1688 запросил повторную авторизацию. Сообщите администратору бота."


class CaptchaDetectedError(CatalogBotError):
    user_message = "1688 запросил повторную авторизацию или проверку CAPTCHA. Администратору необходимо обновить сессию 1688."


class ProductDataNotFoundError(CatalogBotError):
    user_message = "Не удалось получить основные данные товара. Возможно, 1688 ограничил доступ к странице."


class ImageDownloadError(CatalogBotError):
    user_message = "Не удалось загрузить изображения товара."


class OpenAIProcessingError(CatalogBotError):
    user_message = "Не удалось сформировать каталог из-за временной ошибки. Попробуйте повторить запрос позже."


class PdfRenderingError(CatalogBotError):
    user_message = "Не удалось создать PDF-каталог. Попробуйте повторить запрос позже."


class JobAlreadyActiveError(CatalogBotError):
    user_message = "Ваш предыдущий каталог ещё формируется. Дождитесь его завершения."
