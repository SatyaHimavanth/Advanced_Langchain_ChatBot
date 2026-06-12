"""
database.py
───────────
SQLAlchemy engine + session for the application relational DB (users, chats).

This is SEPARATE from LangGraph's agent persistence (store + checkpointer),
which manages its own tables (store, checkpoints, checkpoint_*). To guarantee
the two never clash, every application table here is prefixed with ``app_``
(see models.py).

The connection URL is read from settings.SQLALCHEMY_DATABASE_URL. A bare
``postgresql://`` URL is normalized to the psycopg v3 driver
(``postgresql+psycopg://``) because that is the driver shipped with this
project (psycopg[binary,pool]); psycopg2 is not a dependency.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.settings import settings


def _normalize_url(url: str) -> str:
    """Ensure Postgres URLs use the psycopg v3 driver."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    return url


SQLALCHEMY_DATABASE_URL = _normalize_url(settings.SQLALCHEMY_DATABASE_URL)

# SQLite needs check_same_thread=False for FastAPI's threadpool.
_connect_args = (
    {"check_same_thread": False}
    if SQLALCHEMY_DATABASE_URL.startswith("sqlite")
    else {}
)

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
