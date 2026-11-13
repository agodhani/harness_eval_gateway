"""Engine / sessionmaker factory.

Kept separate from `api/deps.py` so non-API components (orchestrator, gateway,
evaluator, alembic env, tests) can build their own sessions from a URL.
"""

from sqlalchemy import Engine, create_engine, event
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

    engine = create_engine(database_url, pool_pre_ping=True, **kwargs)
    if database_url.startswith("sqlite"):
        # sqlite ships with FK enforcement OFF — without this pragma, tests
        # pass locally while violating constraints (e.g. events.run_id) that
        # Postgres enforces in CI. The pragma is per-connection and a no-op
        # inside a transaction, so it must run on each fresh connect.
        @event.listens_for(engine, "connect")
        def _sqlite_fk_on(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
