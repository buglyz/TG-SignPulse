from __future__ import annotations

from typing import Optional

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker

from backend.core.config import get_settings

Base = declarative_base()

_engine: Optional[Engine] = None
_SessionLocal: Optional[sessionmaker] = None


def ensure_schema_compat() -> None:
    engine = get_engine()
    inspector = inspect(engine)
    try:
        if "accounts" not in inspector.get_table_names():
            return
        column_names = {column["name"] for column in inspector.get_columns("accounts")}
        with engine.begin() as conn:
            missing_columns = []
            if "session_string" not in column_names:
                missing_columns.append("session_string")
            if "remark" not in column_names:
                missing_columns.append("remark")
            for column_name in missing_columns:
                conn.execute(
                    text(f"ALTER TABLE accounts ADD COLUMN {column_name} TEXT")
                )
    except Exception:
        # Keep startup resilient; create_all still handles fresh databases.
        pass


def init_engine() -> None:
    global _engine, _SessionLocal
    if _engine is not None and _SessionLocal is not None:
        return

    settings = get_settings()
    create_engine_kwargs = {"echo": False}
    if settings.is_sqlite:
        create_engine_kwargs["connect_args"] = {
            "check_same_thread": False,
            "timeout": 30,
        }
    else:
        create_engine_kwargs["pool_pre_ping"] = True

    engine = create_engine(settings.database_url, **create_engine_kwargs)

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        if not settings.is_sqlite:
            return
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    _engine = engine
    _SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_engine() -> Engine:
    if _engine is None:
        init_engine()
    return _engine  # type: ignore[return-value]


def get_session_local() -> sessionmaker:
    if _SessionLocal is None:
        init_engine()
    return _SessionLocal  # type: ignore[return-value]


def get_db():
    session_local = get_session_local()
    db = session_local()
    try:
        yield db
    finally:
        db.close()
