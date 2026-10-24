from __future__ import annotations


class DomainError(Exception):
    """Base class for errors safe to map to an API response."""


class NotFoundError(DomainError):
    pass


class ValidationError(DomainError):
    pass


class CapabilityUnavailableError(DomainError):
    pass


class ConfirmationRequiredError(DomainError):
    pass


class UploadTooLargeError(ValidationError):
    pass


class JobCancelledError(DomainError):
    pass


class CrmError(DomainError):
    """Base class for outbound CRM integration failures."""


class CrmContractError(CrmError):
    """The CRM response did not match the agreed contract."""


class CrmConnectionError(CrmError):
    """The CRM could not be reached (DNS, connection refused, reset)."""


class CrmTimeoutError(CrmConnectionError):
    """The CRM request timed out; delivery state is unknown."""


class CrmHTTPError(CrmError):
    """The CRM answered with an HTTP error status."""

    def __init__(self, status_code: int, detail: str | None = None) -> None:
        self.status_code = status_code
        # Only 408/425/429 and 5xx are considered transient; everything else
        # requires human intervention instead of blind retries.
        self.retryable = status_code in {408, 425, 429} or status_code >= 500
        super().__init__(f"CRM returned HTTP {status_code}" + (f": {detail}" if detail else ""))
