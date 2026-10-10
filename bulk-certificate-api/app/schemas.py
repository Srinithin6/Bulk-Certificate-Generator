import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TemplateName = Literal["classic", "modern"]
CertStatusName = Literal["PENDING", "PROCESSING", "COMPLETED", "FAILED", "INVALID"]


def _strip_or_none(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    return v or None


# ------------------------------------------------------------------ requests
class RecipientIn(BaseModel):
    """Validation rules for ONE recipient (applied per item, see services/jobs.py)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    email: str | None = Field(default=None, max_length=254)
    remarks: str | None = Field(default=None, max_length=120)  # e.g. "Grade: A"

    @field_validator("name", "remarks")
    @classmethod
    def _printable(cls, v: str | None) -> str | None:
        if v is not None and not v.isprintable():
            raise ValueError("must not contain control characters or line breaks")
        return v

    @field_validator("email", "remarks")
    @classmethod
    def _blank_to_none(cls, v: str | None) -> str | None:
        return v or None

    @field_validator("email")
    @classmethod
    def _email_format(cls, v: str | None) -> str | None:
        if v is not None and not _EMAIL_RE.match(v):
            raise ValueError("is not a valid email address")
        return v


class JobCreate(BaseModel):
    """The request envelope. Recipients stay untyped here so that one bad recipient
    is reported per-item instead of rejecting the whole request."""

    title: str = Field(min_length=1, max_length=150, description="Course/event name printed on the certificate")
    issued_by: str | None = Field(default=None, max_length=150, description="Organization name")
    issue_date: date | None = None
    template: TemplateName = "classic"
    signatory_name: str | None = Field(default=None, max_length=100)
    signatory_title: str | None = Field(default=None, max_length=100)
    recipients: list[Any] = Field(min_length=1)

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        if not v.isprintable():
            raise ValueError("must not contain control characters or line breaks")
        return v

    @field_validator("issued_by", "signatory_name", "signatory_title")
    @classmethod
    def _optional_text(cls, v: str | None) -> str | None:
        v = _strip_or_none(v)
        if v is not None and not v.isprintable():
            raise ValueError("must not contain control characters or line breaks")
        return v


# ------------------------------------------------------------------ responses
class _UtcModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @staticmethod
    def _z(dt: datetime | None) -> str | None:
        return None if dt is None else dt.isoformat() + "Z"


class JobOut(_UtcModel):
    id: str
    title: str
    issued_by: str
    issue_date: date
    template: str
    signatory_name: str | None
    signatory_title: str | None
    status: str
    total: int
    completed: int
    failed: int
    invalid: int
    pending: int  # queued or being processed
    progress: float  # % of certificates that reached a final state
    files_available: bool  # False once the retention period has passed
    created_at: datetime
    finished_at: datetime | None = None
    expires_at: datetime | None = None

    @field_serializer("created_at", "finished_at", "expires_at")
    def _ser(self, dt: datetime | None) -> str | None:
        return self._z(dt)


class CertificateOut(_UtcModel):
    id: str
    job_id: str
    index: int
    status: str
    attempts: int
    recipient_name: str | None
    email: str | None
    certificate_number: str | None
    error: str | None
    download_url: str | None
    completed_at: datetime | None = None

    @field_serializer("completed_at")
    def _ser(self, dt: datetime | None) -> str | None:
        return self._z(dt)


class CertificateListOut(BaseModel):
    job_id: str
    total: int  # matching the status filter
    limit: int
    offset: int
    results: list[CertificateOut]


class RetryOut(BaseModel):
    job_id: str
    requeued: int
    status: str


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: list[dict[str, Any]] | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class HealthOut(BaseModel):
    status: str
    database: str
