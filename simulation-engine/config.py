from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    redis_url: str = Field("redis://localhost:6379", env="REDIS_URL")
    initial_budget: float = Field(100000.0, env="INITIAL_BUDGET")
    max_position_size_pct: float = Field(10.0, env="MAX_POSITION_SIZE_PCT")
    slippage_pct: float = Field(0.05, env="SLIPPAGE_PCT")
    commission_flat: float = Field(20.0, env="COMMISSION_FLAT")
    commission_pct: float = 0.03
    data_service_url: str = Field("http://data-service:8003", env="DATA_SERVICE_URL")
    core_engine_url: str = Field("http://core-engine:8001", env="CORE_ENGINE_URL")
    log_level: str = Field("INFO", env="LOG_LEVEL")

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
