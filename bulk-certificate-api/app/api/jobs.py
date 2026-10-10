import os
import re
import tempfile
import zipfile
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app import config
from app.database import get_db
from app.errors import ConflictError, GoneError, PayloadTooLargeError
from app.models import Certificate, CertStatus, JobStatus
from app.schemas import CertificateListOut, CertificateOut, CertStatusName, ErrorResponse, JobCreate, JobOut, RetryOut
from app.security import require_api_key
from app.services import jobs as jobs_service
from app.services.processor import dispatch
from app.storage import storage

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"], dependencies=[Depends(require_api_key)])

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid API key"},
    404: {"model": ErrorResponse, "description": "Job not found"},
}


def cert_out(c: Certificate) -> CertificateOut:
    """DB row -> API model (adds the download link only when the file can be downloaded)."""
    return CertificateOut(
        id=c.id,
        job_id=c.job_id,
        index=c.seq,
        status=c.status,
        attempts=c.attempts,
        recipient_name=c.recipient_name,
        email=c.email,
        certificate_number=c.certificate_number,
        error=c.error,
        completed_at=c.completed_at,
        download_url=(
            f"/api/v1/certificates/{c.id}/download" if c.status == CertStatus.COMPLETED and c.storage_key else None
        ),
    )


@router.post(
    "",
    response_model=JobOut,
    status_code=202,
    summary="Create a bulk certificate-generation job",
    description=(
        "Validates the request and returns immediately with a job id; certificates are generated in the "
        "background. Invalid recipients are reported per recipient (status INVALID) and do not stop the "
        "others. Send an `Idempotency-Key` header to make retries of this call safe: the same key with "
        "the same body returns the existing job (200) instead of creating a duplicate."
    ),
    responses={
        **_ERRORS,
        200: {"model": JobOut, "description": "Idempotent replay: the job already existed"},
        409: {"model": ErrorResponse, "description": "Idempotency-Key reused with a different body"},
        413: {"model": ErrorResponse, "description": "Request body too large"},
        422: {"model": ErrorResponse, "description": "Invalid request"},
    },
)
def create_job(
    payload: JobCreate,
    response: Response,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", pattern=r"^[A-Za-z0-9_.-]{1,100}$"),
) -> dict[str, Any]:
    if len(payload.recipients) > config.MAX_RECIPIENTS:
        raise PayloadTooLargeError(
            f"Too many recipients ({len(payload.recipients)}); the limit is {config.MAX_RECIPIENTS} per request"
        )
    job, created = jobs_service.create_job(db, payload, idempotency_key)
    if created:
        if job.status == JobStatus.PENDING:
            dispatch(job.id)  # returns immediately in background mode
            db.refresh(job)  # inline mode: the worker used its own session; pick up its changes
    else:
        response.status_code = 200
        response.headers["Idempotent-Replayed"] = "true"
    return jobs_service.job_status(db, job)


@router.get("/{job_id}", response_model=JobOut, summary="Job status and progress", responses=_ERRORS)
def get_job(job_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    return jobs_service.job_status(db, jobs_service.get_job(db, job_id))


@router.get(
    "/{job_id}/certificates",
    response_model=CertificateListOut,
    summary="List the certificates (per-recipient results) of a job",
    responses=_ERRORS,
)
def list_certificates(
    job_id: str,
    status: CertStatusName | None = Query(None, description="Only certificates with this status"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> CertificateListOut:
    jobs_service.get_job(db, job_id)
    where = [Certificate.job_id == job_id]
    if status:
        where.append(Certificate.status == status)
    total = db.scalar(select(func.count()).select_from(Certificate).where(*where)) or 0
    rows = db.scalars(select(Certificate).where(*where).order_by(Certificate.seq).limit(limit).offset(offset)).all()
    return CertificateListOut(
        job_id=job_id, total=total, limit=limit, offset=offset, results=[cert_out(c) for c in rows]
    )


@router.post(
    "/{job_id}/retry",
    response_model=RetryOut,
    status_code=202,
    summary="Retry the FAILED certificates of a finished job",
    description=(
        "Re-queues only FAILED certificates that have attempts left (MAX_ATTEMPTS). Completed and "
        "INVALID certificates are never touched, so a retry cannot create duplicates."
    ),
    responses={
        **_ERRORS,
        409: {"model": ErrorResponse, "description": "Nothing to retry / job still running"},
        410: {"model": ErrorResponse, "description": "Files expired"},
    },
)
def retry_job(job_id: str, db: Session = Depends(get_db)) -> RetryOut:
    requeued = jobs_service.requeue_failed(db, job_id)
    dispatch(job_id)
    job = jobs_service.get_job(db, job_id)
    db.refresh(job)
    return RetryOut(job_id=job_id, requeued=requeued, status=job.status)


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:50] or "certificate"


@router.get(
    "/{job_id}/download",
    summary="Download all generated certificates of a job as a ZIP",
    response_class=FileResponse,
    responses={
        **_ERRORS,
        200: {"content": {"application/zip": {}}},
        409: {"model": ErrorResponse, "description": "Nothing generated yet"},
        410: {"model": ErrorResponse, "description": "Files expired"},
    },
)
def download_job_zip(job_id: str, db: Session = Depends(get_db)) -> FileResponse:
    job = jobs_service.get_job(db, job_id)
    if job.purged_at is not None:
        raise GoneError("Job files have expired and were removed")
    certs = db.scalars(
        select(Certificate)
        .where(Certificate.job_id == job_id, Certificate.status == CertStatus.COMPLETED)
        .order_by(Certificate.seq)
    ).all()
    if not certs:
        raise ConflictError("No certificates have been generated for this job yet")

    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(suffix=".zip", dir=config.TMP_DIR)
    os.close(fd)
    with zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_DEFLATED) as zf:
        for c in certs:
            if c.storage_key and storage.exists(c.storage_key):
                # The entry name is built from the sequence number + a sanitised name: never a stored path.
                entry = f"{c.seq + 1:05d}_{_safe_name(c.recipient_name or '')}.pdf"
                zf.write(storage.resolve(c.storage_key), entry)
    return FileResponse(
        tmp_name,
        media_type="application/zip",
        filename=f"certificates-{job_id[:8]}.zip",
        background=BackgroundTask(os.unlink, tmp_name),
    )
