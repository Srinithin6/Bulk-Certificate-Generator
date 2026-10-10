"""Retrieving generated certificates."""

import io
import os
import zipfile

from app import config
from tests.conftest import create_job, list_certs


def test_list_pagination_and_filter(client):
    job = create_job(client, *[f"P {i}" for i in range(5)])
    page = client.get(f"/api/v1/jobs/{job['id']}/certificates", params={"limit": 2, "offset": 3}).json()
    assert page["total"] == 5 and [r["index"] for r in page["results"]] == [3, 4]
    assert client.get(f"/api/v1/jobs/{job['id']}/certificates", params={"status": "BOGUS"}).status_code == 422
    assert client.get(f"/api/v1/jobs/{job['id']}/certificates", params={"limit": 0}).status_code == 422


def test_certificate_details(client):
    job = create_job(client, "Asha")
    cert = list_certs(client, job["id"])[0]
    meta = client.get(f"/api/v1/certificates/{cert['id']}").json()
    assert meta["status"] == "COMPLETED" and meta["recipient_name"] == "Asha" and meta["job_id"] == job["id"]


def test_missing_job_and_certificate_return_404(client):
    for path in (
        "/api/v1/jobs/nope",
        "/api/v1/jobs/nope/certificates",
        "/api/v1/jobs/nope/download",
        "/api/v1/certificates/nope",
        "/api/v1/certificates/nope/download",
    ):
        r = client.get(path)
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found", path


def test_zip_contains_only_generated_certificates(client):
    body = {"title": "T", "recipients": [{"name": "Asha Rao"}, {"name": "Ben Carter"}, {"name": ""}]}
    job = client.post("/api/v1/jobs", json=body).json()
    r = client.get(f"/api/v1/jobs/{job['id']}/download")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert zf.namelist() == ["00001_Asha_Rao.pdf", "00002_Ben_Carter.pdf"]
    assert zf.read("00001_Asha_Rao.pdf").startswith(b"%PDF")


def test_zip_entry_names_cannot_traverse_paths(client):
    job = create_job(client, "../../etc/passwd", "a\\b\\c")
    names = zipfile.ZipFile(io.BytesIO(client.get(f"/api/v1/jobs/{job['id']}/download").content)).namelist()
    assert len(names) == 2
    assert all("/" not in n and "\\" not in n and n.endswith(".pdf") for n in names)


def test_temporary_zip_is_deleted_after_download(client):
    job = create_job(client, "A", "B")
    assert client.get(f"/api/v1/jobs/{job['id']}/download").status_code == 200
    assert not [f for f in os.listdir(config.TMP_DIR) if f.endswith(".zip")]


def test_zip_409_when_nothing_generated(client):
    job = client.post("/api/v1/jobs", json={"title": "T", "recipients": [{}]}).json()
    r = client.get(f"/api/v1/jobs/{job['id']}/download")
    assert r.status_code == 409 and r.json()["error"]["code"] == "conflict"
