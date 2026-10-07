import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Establish clean path resolution for workspace modules
BASE_DIR = Path(__file__).resolve().parent
SRC_DIR = BASE_DIR / "src"
LOGS_DIR = SRC_DIR / "logs"

for path in (BASE_DIR, SRC_DIR, LOGS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

try:
    from src.db import Base, engine
    from src.logs.router import router as files_router
    from src.settings import settings
except ImportError:
    from db import Base, engine
    from logs.router import router as files_router
    from settings import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize database tables on application startup
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Wire up routers
app.include_router(files_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
