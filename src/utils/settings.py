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
    MAX_OVERFLOW: int = 10
    POOL_TIMEOUT: float = 10.0
    POOL_PRE_PING: bool = True

    # Bounded background worker concurrency limit
    MAX_CONCURRENT_WORKERS: int = 4


settings = Settings()
