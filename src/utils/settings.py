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


settings = Settings()
