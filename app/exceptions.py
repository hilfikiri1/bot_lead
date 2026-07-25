class InvalidProductUrlError(ValueError):
    pass


class UnsupportedDomainError(InvalidProductUrlError):
    pass


class ProductPageNotFoundError(RuntimeError):
    pass


class AuthenticationRequiredError(RuntimeError):
    pass


class CaptchaDetectedError(RuntimeError):
    pass


class ProductDataNotFoundError(RuntimeError):
    pass


class ImageDownloadError(RuntimeError):
    pass


class OpenAIProcessingError(RuntimeError):
    pass


class PdfRenderingError(RuntimeError):
    pass
