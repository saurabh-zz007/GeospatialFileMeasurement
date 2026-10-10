import sys
import asyncio
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from src.utils.settings import settings

if sys.platform == "win32":
    # Required for psycopg async on Windows to avoid InterfaceError
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _get_async_database_url(url: str) -> str:
    url = url.strip()
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


# Async Database Engine and SessionMaker
async_database_url = _get_async_database_url(settings.DATABASE_URL)

# Supabase Transaction Pooler (PgBouncer/Supavisor on port 6543):
# In transaction pooling mode, prepared statements MUST be disabled
# (prepare_threshold=None) to avoid "prepared statement already exists / does not exist" errors.
async_connect_args: dict = {}
if "psycopg" in async_database_url:
    async_connect_args["prepare_threshold"] = None
elif "asyncpg" in async_database_url:
    async_connect_args.update({
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
    })

async_engine = create_async_engine(
    async_database_url,
    pool_size=settings.POOL_SIZE,
    max_overflow=settings.MAX_OVERFLOW,
    pool_timeout=settings.POOL_TIMEOUT,
    pool_pre_ping=settings.POOL_PRE_PING,
    pool_recycle=getattr(settings, "POOL_RECYCLE", 1800),
    connect_args=async_connect_args,
    future=True,
)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)

# Synchronous Database Engine and SessionMaker (maintained for compatibility)
sync_database_url = settings.DATABASE_URL.strip()
sync_connect_args: dict = {}
if "psycopg" in sync_database_url and "asyncpg" not in sync_database_url:
    sync_connect_args["prepare_threshold"] = None

engine = create_engine(
    sync_database_url,
    pool_size=settings.POOL_SIZE,
    max_overflow=settings.MAX_OVERFLOW,
    pool_timeout=settings.POOL_TIMEOUT,
    pool_pre_ping=settings.POOL_PRE_PING,
    pool_recycle=getattr(settings, "POOL_RECYCLE", 1800),
    connect_args=sync_connect_args,
    future=True,
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    expire_on_commit=False,
)

Base = declarative_base()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI request-scoped async database session dependency."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


@asynccontextmanager
async def get_async_db_context() -> AsyncGenerator[AsyncSession, None]:
    """Independent async database session context manager for background workers."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# Alias for backward-compatible background imports
get_db_context = get_async_db_context
