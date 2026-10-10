import sys
import asyncio

if sys.platform == "win32":
    # Required for psycopg async on Windows when imported by ASGI servers
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import TimeoutError as SATimeoutError

from slowapi.errors import RateLimitExceeded

from src.logs.router import router as files_router
from src.utils.db import Base, async_engine
from src.utils.limiter import limiter, rate_limit_exceeded_handler
from src.utils.settings import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    # Initialize database tables asynchronously on application startup
    try:
        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception:
        pass
    yield
    try:
        await async_engine.dispose()
    except Exception:
        pass


async def db_timeout_exception_handler(request: Request, exc: SATimeoutError):
    """Maps database connection pool timeout to HTTP 503 Service Unavailable."""
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "5"},
        content={
            "detail": "Database connection pool timeout. Service temporarily unavailable.",
            "error": "Database connection pool timeout. Service temporarily unavailable.",
        },
    )


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
app.add_exception_handler(SATimeoutError, db_timeout_exception_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["Health"], summary="Health check endpoint")
async def health():
    return {"status": "ok"}


# Wire up routers
app.include_router(files_router)
