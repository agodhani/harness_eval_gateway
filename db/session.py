"""Engine / sessionmaker factory.

Kept separate from `api/deps.py` so non-API components (orchestrator, gateway,
evaluator, alembic env, tests) can build their own sessions from a URL.
"""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


def make_engine(database_url: str, **kwargs) -> Engine:
    """Create a SQLAlchemy engine for `database_url`.

    SQLite gets a single shared in-process connection (StaticPool +
    check_same_thread=False) so `:memory:` databases and threaded test
    clients see the same data.
    """
    if database_url.startswith("sqlite"):
        kwargs.setdefault("connect_args", {})["check_same_thread"] = False
        kwargs.setdefault("poolclass", StaticPool)
    return create_engine(database_url, pool_pre_ping=True, **kwargs)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
