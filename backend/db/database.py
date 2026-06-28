"""Database initialization and session management."""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "data" / "pulse.db"
SCHEMA_PATH = ROOT / "schema" / "schema.sql"


class Base(DeclarativeBase):
    pass


def get_database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{DEFAULT_DB}"
    elif url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        path = url.replace("sqlite:///", "")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    return url


engine = create_engine(
    get_database_url(),
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _connection_record):
    if engine.dialect.name != "sqlite":
        return
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA cache_size=-64000")
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    schema_sql = SCHEMA_PATH.read_text()
    with engine.begin() as conn:
        for stmt in schema_sql.split(";"):
            stmt = stmt.strip()
            if stmt and not stmt.startswith("--"):
                conn.execute(text(stmt))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
