from collections.abc import Generator
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from src.utils.settings import settings


# NOTE ON POSTGRES MAX_CONNECTIONS:
# Ensure that: total_app_instances * (POOL_SIZE + MAX_OVERFLOW) < Postgres max_connections.
# Background worker concurrency is bounded by settings.MAX_CONCURRENT_WORKERS (default 2-4),
# and background jobs release DB sessions during parsing/measuring, so connections
# are only held briefly during status updates and chunked bulk inserts.
engine = create_engine(
    settings.DATABASE_URL,
    pool_size=settings.POOL_SIZE,
    max_overflow=settings.MAX_OVERFLOW,
    pool_timeout=settings.POOL_TIMEOUT,
    pool_pre_ping=settings.POOL_PRE_PING,
    future=True,
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    expire_on_commit=False
)

Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    """FastAPI request-scoped database session dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def get_db_context() -> Generator[Session, None, None]:
    """Independent database session context manager for background workers."""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
