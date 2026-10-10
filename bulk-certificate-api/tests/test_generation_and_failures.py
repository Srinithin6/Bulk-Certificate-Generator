"""PDF content, templates, progress, partial failures, error hygiene."""

import io
from datetime import date

from pypdf import PdfReader
from sqlalchemy import select

from app.database import SessionLocal
from app.models import Job
from app.services import jobs as jobs_service
from app.services import processor
from app.services.pdf import generate_certificate_pdf
from tests.conftest import create_job, list_certs


def pdf_text(content: bytes) -> str:
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(content)).pages)


# ---------------------------------------------------------------- generation
def test_generated_pdf_contains_recipient_and_org_data(client):
    job = create_job(client, "Zoë Müller", signatory_name="Dr. Priya Nair", signatory_title="Program Director")
    cert = list_certs(client, job["id"])[0]
    r = client.get(cert["download_url"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")
    text = pdf_text(r.content)
    for expected in (
        "Zoë Müller",
        "Python Bootcamp 2026",
        "AEREO ACADEMY",
        "01 October 2026",
        "Dr. Priya Nair",
        "Program Director",
        cert["certificate_number"],
    ):
        assert expected in text


def test_both_templates_render(client):
    for template in ("classic", "modern"):
        job = create_job(client, "Asha Rao", template=template)
        assert job["template"] == template and job["completed"] == 1
        cert = list_certs(client, job["id"])[0]
        assert client.get(cert["download_url"]).content.startswith(b"%PDF")


def test_certificate_ids_and_numbers_are_unique(client):
    job = create_job(client, *[f"Person {i}" for i in range(25)])
    certs = list_certs(client, job["id"])
    assert len({c["id"] for c in certs}) == 25 and len({c["certificate_number"] for c in certs}) == 25


def test_pdf_generator_directly(tmp_path):
    dest = tmp_path / "x" / "c.pdf"
    generate_certificate_pdf(
        dest,
        recipient_name="Asha Rao",
        title="T",
        issued_by="Org",
        issue_date=date(2026, 1, 2),
        certificate_number="N-1",
        remarks="Grade: A",
    )
    assert dest.exists() and "Grade: A" in pdf_text(dest.read_bytes())
    assert not list(dest.parent.glob("*.tmp"))  # atomic write leaves no temp file


# ---------------------------------------------------------------- progress
def test_progress_is_visible_while_job_runs(client, monkeypatch):
    snapshots = []
    real = processor.generate_certificate_pdf

    def spy(dest, **kw):
        with SessionLocal() as db:
            job = db.scalars(select(Job)).one()
            snapshots.append(jobs_service.job_status(db, job))
        return real(dest, **kw)

    monkeypatch.setattr(processor, "generate_certificate_pdf", spy)
    final = create_job(client, "A One", "B Two", "C Three")

    assert [s["status"] for s in snapshots] == ["PROCESSING"] * 3
    assert [s["completed"] for s in snapshots] == [0, 1, 2]
    assert [s["progress"] for s in snapshots] == [0.0, 33.3, 66.7]
    assert snapshots[1]["pending"] == 2
    assert final["status"] == "COMPLETED" and final["pending"] == 0


def test_stored_tallies_match_live_counts(client):
    body = {"title": "T", "recipients": [{"name": "A"}, {"name": "B"}, {"name": ""}, {"name": "இளங்கோ"}]}
    job = client.post("/api/v1/jobs", json=body).json()
    with SessionLocal() as db:
        stored = db.scalars(select(Job)).one()
        live = jobs_service.count_by_status(db, stored.id)
        assert (
            (stored.succeeded_count, stored.failed_count, stored.invalid_count)
            == (live.get("COMPLETED", 0), live.get("FAILED", 0), live.get("INVALID", 0))
            == (2, 1, 1)
        )
    assert job["completed"] == 2 and job["failed"] == 1 and job["invalid"] == 1


# ---------------------------------------------------------------- failures
def test_one_failure_does_not_stop_the_others(client, monkeypatch):
    real = processor.generate_certificate_pdf

    def flaky(dest, **kw):
        if kw["recipient_name"] == "Boom":
            raise RuntimeError("renderer exploded")
        return real(dest, **kw)

    monkeypatch.setattr(processor, "generate_certificate_pdf", flaky)
    job = create_job(client, "Alpha", "Boom", "Gamma")

    assert job["status"] == "COMPLETED_WITH_ERRORS"
    assert (job["completed"], job["failed"], job["invalid"]) == (2, 1, 0)
    failed = list_certs(client, job["id"], status="FAILED")
    assert len(failed) == 1 and failed[0]["recipient_name"] == "Boom"
    assert failed[0]["download_url"] is None and failed[0]["attempts"] == 1
    ok = list_certs(client, job["id"], status="COMPLETED")
    assert [c["recipient_name"] for c in ok] == ["Alpha", "Gamma"]
    assert client.get(f"/api/v1/certificates/{failed[0]['id']}/download").status_code == 409


def test_unexpected_errors_do_not_leak_internal_details(client, monkeypatch):
    def boom(dest, **kw):
        raise OSError("[Errno 28] No space left on device: '/srv/secret/path/file.pdf'")

    monkeypatch.setattr(processor, "generate_certificate_pdf", boom)
    job = create_job(client, "A")
    cert = list_certs(client, job["id"])[0]
    assert cert["error"] == "Unexpected error while generating the certificate"
    assert "/srv" not in str(client.get(f"/api/v1/jobs/{job['id']}/certificates").json())


def test_unrenderable_characters_fail_only_that_certificate(client):
    job = create_job(client, "Asha Rao", "இளங்கோ", "Ben")
    assert (job["completed"], job["failed"]) == (2, 1)
    failed = list_certs(client, job["id"], status="FAILED")[0]
    assert "cannot render" in failed["error"]


def test_all_generation_failures_mark_job_failed(client, monkeypatch):
    def always_fail(dest, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(processor, "generate_certificate_pdf", always_fail)
    job = create_job(client, "A", "B")
    assert job["status"] == "FAILED" and job["failed"] == 2 and job["files_available"] is False
