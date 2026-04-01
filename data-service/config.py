from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    database_url: str = Field(
        "postgresql+asyncpg://trading:trading@timescaledb:5432/trading",
        env="DATABASE_URL",
    )
    redis_url: str = Field("redis://redis:6379", env="REDIS_URL")
    log_level: str = Field("INFO", env="LOG_LEVEL")

    # How many days of candles to include in context snapshot
    context_lookback_days: int = Field(5, env="CONTEXT_LOOKBACK_DAYS")

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
