"""SQLAlchemy engine/session wiring and the ``get_db`` FastAPI dependency."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

CONNECT_TIMEOUT_SECONDS = 5


def connect_args(database_url: str) -> dict:
    """Bound the TCP connect so a black-holed PostgreSQL host fails fast (e.g. in /health)."""
    if make_url(database_url).get_backend_name() == "postgresql":
        return {"connect_timeout": CONNECT_TIMEOUT_SECONDS}
    return {}


engine = create_engine(
    settings.database_url, pool_pre_ping=True, connect_args=connect_args(settings.database_url)
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
