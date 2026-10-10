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
    POOL_SIZE: int = 20
    MAX_OVERFLOW: int = 20
    POOL_TIMEOUT: float = 30.0
    POOL_PRE_PING: bool = False
    POOL_RECYCLE: int = 1800

    # Bounded background worker concurrency limit
    MAX_CONCURRENT_WORKERS: int = 4


settings = Settings()
