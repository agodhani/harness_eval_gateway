"""Shared FastAPI dependencies — currently just the DB session."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session, sessionmaker

from api.config import get_settings
from db.session import make_engine

_engine = None
_session_factory: sessionmaker[Session] | None = None


def get_session_factory() -> sessionmaker[Session]:
    """Lazily build the engine + session factory from settings.

    Lazy so tests can point DATABASE_URL at sqlite via env var before first use.
    """
    global _engine, _session_factory
    if _session_factory is None:
        _engine = make_engine(get_settings().DATABASE_URL)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _session_factory


def get_db() -> Iterator[Session]:
    """Yield a SQLAlchemy session, closing it afterwards."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
