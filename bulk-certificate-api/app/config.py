"""Configuration from environment variables (see .env.example).

Other modules read ``config.X`` at call time (never ``from app.config import X``), so tests
can override values with ``monkeypatch.setattr(config, "X", ...)``.
"""

import os
from pathlib import Path


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"Environment variable {name} must be an integer, got {raw!r}") from None


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None or raw.strip() == "" else raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


# --- storage -------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data/certificates.db")
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
CERT_DIR = Path(os.getenv("CERT_DIR", str(DATA_DIR / "certificates")))
TMP_DIR = Path(os.getenv("TMP_DIR", str(DATA_DIR / "tmp")))  # temporary ZIP files

# --- limits --------------------------------------------------------------------------
MAX_RECIPIENTS = _int("MAX_RECIPIENTS", 5000)  # per request
MAX_REQUEST_BYTES = _int("MAX_REQUEST_BYTES", 5 * 1024 * 1024)  # JSON body size
MAX_ATTEMPTS = _int("MAX_ATTEMPTS", 3)  # generation attempts per certificate

# --- background processing -----------------------------------------------------------
WORKERS = _int("WORKERS", 2)  # jobs processed concurrently
# True = generate inside the request (used by tests); False = background workers
INLINE_PROCESSING = _bool("INLINE_PROCESSING", False)

# --- retention -----------------------------------------------------------------------
RETENTION_DAYS = _int("RETENTION_DAYS", 7)  # generated files are deleted this long after a job finishes
CLEANUP_INTERVAL_MINUTES = _int("CLEANUP_INTERVAL_MINUTES", 60)  # 0 disables the periodic cleanup

# --- certificate defaults ------------------------------------------------------------
DEFAULT_ISSUER = os.getenv("DEFAULT_ISSUER", "Certificate Authority")
DEFAULT_SIGNATORY_NAME = os.getenv("DEFAULT_SIGNATORY_NAME") or None
DEFAULT_SIGNATORY_TITLE = os.getenv("DEFAULT_SIGNATORY_TITLE") or None
FONT_PATH = os.getenv("CERT_FONT_PATH") or None  # TTF font for non-Latin-1 characters

# --- security / ops ------------------------------------------------------------------
API_KEY = os.getenv("API_KEY") or None  # if set, every /api request needs header X-API-Key
CORS_ORIGINS = _csv("CORS_ORIGINS")  # empty = CORS disabled
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = os.getenv("LOG_FORMAT", "text").lower()  # "text" or "json"
