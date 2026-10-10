"""End-to-end example using only the standard library:
submit a job -> poll progress -> list failures -> download the ZIP.

    python examples/demo_client.py                      # server on http://localhost:8000
    set API_KEY=...  (Windows: $env:API_KEY="...")      # if the server requires a key
"""

import json
import os
import time
import urllib.request

BASE = os.getenv("BASE_URL", "http://localhost:8000")
HEADERS = {"Content-Type": "application/json"}
if os.getenv("API_KEY"):
    HEADERS["X-API-Key"] = os.environ["API_KEY"]


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=HEADERS, method=method)
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


with open(os.path.join(os.path.dirname(__file__), "request.json")) as f:
    job = call("POST", "/api/v1/jobs", json.load(f))
print("accepted:", job["id"], job["status"])

while job["status"] in ("PENDING", "PROCESSING"):
    time.sleep(0.5)
    job = call("GET", f"/api/v1/jobs/{job['id']}")
    print(f"  {job['status']}  {job['progress']}%")

print("done:", {k: job[k] for k in ("status", "completed", "failed", "invalid")})
for cert in call("GET", f"/api/v1/jobs/{job['id']}/certificates")["results"]:
    print(" ", cert["index"], cert["status"], cert["recipient_name"], cert["error"] or cert["download_url"])

req = urllib.request.Request(f"{BASE}/api/v1/jobs/{job['id']}/download", headers=HEADERS)
with urllib.request.urlopen(req) as resp, open("certificates.zip", "wb") as out:
    out.write(resp.read())
print("saved certificates.zip")
