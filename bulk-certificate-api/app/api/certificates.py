from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.jobs import cert_out
from app.database import get_db
from app.errors import ConflictError, GoneError, NotFoundError
from app.models import Certificate, CertStatus, Job
from app.schemas import CertificateOut, ErrorResponse
from app.security import require_api_key
from app.storage import storage

router = APIRouter(prefix="/api/v1/certificates", tags=["certificates"], dependencies=[Depends(require_api_key)])

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid API key"},
    404: {"model": ErrorResponse, "description": "Certificate not found"},
}


def _get(db: Session, cert_id: str) -> Certificate:
    cert = db.get(Certificate, cert_id)
    if cert is None:
        raise NotFoundError(f"Certificate '{cert_id}' not found")
    return cert


@router.get("/{cert_id}", response_model=CertificateOut, summary="Certificate details", responses=_ERRORS)
def get_certificate(cert_id: str, db: Session = Depends(get_db)) -> CertificateOut:
    return cert_out(_get(db, cert_id))


@router.get(
    "/{cert_id}/download",
    summary="Download one certificate (PDF)",
    response_class=FileResponse,
    responses={
        **_ERRORS,
        200: {"content": {"application/pdf": {}}},
        409: {"model": ErrorResponse, "description": "Certificate not generated"},
        410: {"model": ErrorResponse, "description": "File expired"},
    },
)
def download_certificate(cert_id: str, db: Session = Depends(get_db)) -> FileResponse:
    cert = _get(db, cert_id)
    job = db.get(Job, cert.job_id)
    if job is not None and job.purged_at is not None:
        raise GoneError("This certificate has expired and was removed")
    if cert.status != CertStatus.COMPLETED or not cert.storage_key:
        raise ConflictError(f"Certificate is not available (status: {cert.status})")
    if not storage.exists(cert.storage_key):
        raise GoneError("The certificate file is no longer available")
    return FileResponse(
        storage.resolve(cert.storage_key), media_type="application/pdf", filename=f"{cert.certificate_number}.pdf"
    )
