from __future__ import annotations


class CatalogBotError(Exception):
    user_message = "Не удалось сформировать каталог из-за временной ошибки. Попробуйте повторить запрос позже."
    error_code = "catalog_error"


class InvalidProductUrlError(CatalogBotError):
    user_message = "Не удалось распознать ссылку. Отправьте ссылку на карточку товара 1688.com."
    error_code = "invalid_product_url"


class UnsupportedDomainError(InvalidProductUrlError):
    error_code = "unsupported_domain"


class ProductPageNotFoundError(CatalogBotError):
    user_message = "Страница товара недоступна или товар был удалён."
    error_code = "product_page_not_found"


class AuthenticationRequiredError(CatalogBotError):
    user_message = "1688 запросил повторную авторизацию. Сообщите администратору бота."
    error_code = "authentication_required"


class CaptchaDetectedError(AuthenticationRequiredError):
    error_code = "captcha_detected"


class ProductDataNotFoundError(CatalogBotError):
    user_message = "Не удалось получить основные данные товара. Возможно, 1688 ограничил доступ к странице."
    error_code = "product_data_not_found"


class ImageDownloadError(CatalogBotError):
    error_code = "image_download_error"


class OpenAIProcessingError(CatalogBotError):
    error_code = "openai_processing_error"


class PdfRenderingError(CatalogBotError):
    error_code = "pdf_rendering_error"
