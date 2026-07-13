from __future__ import annotations

from typing import ClassVar

from audit.security import sanitize_error_summary


class ProviderError(RuntimeError):
    retryable: ClassVar[bool] = False

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        safe_message = sanitize_error_summary(message, maximum_length=500)
        super().__init__(safe_message or "Provider operation failed.")
        self.http_status = http_status


class ProviderTemporaryError(ProviderError):
    retryable = True


class ProviderTimeoutError(ProviderTemporaryError):
    pass


class ProviderRateLimitError(ProviderTemporaryError):
    def __init__(
        self,
        message: str = "Provider rate limit reached.",
        *,
        http_status: int = 429,
        retry_after_seconds: int | None = None,
    ) -> None:
        if retry_after_seconds is not None and retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must be non-negative.")
        super().__init__(message, http_status=http_status)
        self.retry_after_seconds = retry_after_seconds


class ProviderPermanentError(ProviderError):
    pass


class ProviderAuthenticationError(ProviderPermanentError):
    pass


class ProviderValidationError(ProviderPermanentError):
    pass


class ProviderResponseTooLargeError(ProviderPermanentError):
    def __init__(
        self,
        *,
        limit_bytes: int,
        observed_bytes: int | None = None,
    ) -> None:
        message = f"Provider response exceeds the configured {limit_bytes}-byte limit."
        super().__init__(message)
        self.limit_bytes = limit_bytes
        self.observed_bytes = observed_bytes
