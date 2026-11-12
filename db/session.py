"""Engine / sessionmaker factory.

Kept separate from `api/deps.py` so non-API components (orchestrator, gateway,
evaluator, alembic env, tests) can build their own sessions from a URL.
"""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


def make_engine(database_url: str, **kwargs) -> Engine:
    """Create a SQLAlchemy engine for `database_url`.

    SQLite ``:memory:`` databases get a single shared in-process connection
    (``StaticPool``) — a pooled second connection would see an empty DB.
    Because that is ONE sqlite3 connection, access must be serialized:
    all orchestrator DB work runs on the event-loop thread and API
    handlers use per-request sessions; do NOT wrap DB calls in
    ``asyncio.to_thread`` against a ``:memory:`` engine.

    File-backed sqlite uses the default pool so each session checks out
    its OWN connection — concurrent callers (FastAPI threadpool endpoints,
    ``asyncio.to_thread`` workers) can never share one sqlite3 connection,
    which is what segfaulted under ``StaticPool``. ``check_same_thread``
    stays off because pooled connections migrate between threads, and a
    busy ``timeout`` keeps concurrent writers from failing on SQLITE_BUSY.
    """
    if database_url.startswith("sqlite"):
        connect_args = kwargs.setdefault("connect_args", {})
        connect_args["check_same_thread"] = False
        if ":memory:" in database_url:
            kwargs.setdefault("poolclass", StaticPool)
        else:
            connect_args.setdefault("timeout", 30)
    return create_engine(database_url, pool_pre_ping=True, **kwargs)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
