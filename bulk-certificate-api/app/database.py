from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app import config


class Base(DeclarativeBase):
    pass


def normalize_url(url: str) -> str:
    """Accept the common 'postgres://' / 'postgresql://' forms and use the psycopg 3 driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


DATABASE_URL = normalize_url(config.DATABASE_URL)
_sqlite = DATABASE_URL.startswith("sqlite")
if _sqlite:
    _path = DATABASE_URL.replace("sqlite:///", "", 1)
    if _path and _path != ":memory:":
        Path(_path).parent.mkdir(parents=True, exist_ok=True)

_engine_args: dict[str, Any] = {"pool_pre_ping": True}
if _sqlite:
    _engine_args["connect_args"] = {"check_same_thread": False, "timeout": 30}

engine = create_engine(DATABASE_URL, **_engine_args)

if _sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn: Any, _: Any) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")  # readers don't block the background writer
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed."""
    with SessionLocal() as db:
        yield db


def init_db() -> None:
    from app import models  # noqa: F401  (registers the tables on Base.metadata)

    Base.metadata.create_all(engine)
