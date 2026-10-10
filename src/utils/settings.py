from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config=SettingsConfigDict(env_file=".env", extra="ignore")

    PROJECT_NAME: str
    VERSION: str
    API_PREFIX: str

    DATABASE_URL: str
    UPLOAD_DIR: str
    TARGET_PROJECTED_CRS: str
    LOG_LEVEL: str

    # Database connection pool configuration
    POOL_SIZE: int = 5
    MAX_OVERFLOW: int = 5
    POOL_TIMEOUT: float = 10.0
    POOL_PRE_PING: bool = False
    POOL_RECYCLE: int = 1800

    # Bounded background worker concurrency limit
    MAX_CONCURRENT_WORKERS: int = 4
    MAX_CONCURRENT_JOBS: int = 2

    # Rate limiting configuration (slowapi)
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_POST_FILES: str = "30/minute"
    RATE_LIMIT_GET_FILES: str = "300/minute"

    # In-memory caching configuration (cachetools TTLCache)
    CACHE_ENABLED: bool = True
    CACHE_MAX_ENTRIES: int = 1000
    CACHE_TTL_SECONDS: int = 300


settings = Settings()
