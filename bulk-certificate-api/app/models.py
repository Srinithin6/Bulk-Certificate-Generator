import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite drops tzinfo anyway; the API serialises it with a 'Z')."""
    return datetime.now(UTC).replace(tzinfo=None)


class JobStatus:
    PENDING = "PENDING"  # accepted, queued
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"  # every certificate generated
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"  # some failed / invalid (partially completed)
    FAILED = "FAILED"  # nothing could be generated
    FINAL = {COMPLETED, COMPLETED_WITH_ERRORS, FAILED}


class CertStatus:
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"  # valid input, generation raised an error (can be retried)
    INVALID = "INVALID"  # recipient data rejected by validation; never attempted
    ALL = {PENDING, PROCESSING, COMPLETED, FAILED, INVALID}


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(150))
    issued_by: Mapped[str] = mapped_column(String(150))
    issue_date: Mapped[date] = mapped_column(Date)
    template: Mapped[str] = mapped_column(String(20), default="classic")
    signatory_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    signatory_title: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default=JobStatus.PENDING)

    total: Mapped[int] = mapped_column(Integer)
    # Final tallies, written when the job finishes (live numbers come from the certificates table)
    succeeded_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    invalid_count: Mapped[int] = mapped_column(Integer, default=0)

    # Idempotency: the same key + same body returns the existing job instead of a duplicate
    idempotency_key: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, index=True, nullable=True)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # files deleted


class Certificate(Base):
    __tablename__ = "certificates"
    __table_args__ = (
        UniqueConstraint("job_id", "seq"),
        Index("ix_certificates_job_status", "job_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)  # 0-based position in the request
    status: Mapped[str] = mapped_column(String(16), default=CertStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    recipient_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    remarks: Mapped[str | None] = mapped_column(String(120), nullable=True)
    payload: Mapped[Any] = mapped_column(JSON)  # the recipient exactly as submitted
    certificate_number: Mapped[str | None] = mapped_column(String(40), unique=True, nullable=True)
    storage_key: Mapped[str | None] = mapped_column(String(300), nullable=True)  # see app/storage.py
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
