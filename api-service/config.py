from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    redis_url: str = Field("redis://localhost:6379", env="REDIS_URL")
    core_engine_url: str = Field("http://localhost:8001", env="CORE_ENGINE_URL")
    sim_engine_url: str = Field("http://localhost:8002", env="SIM_ENGINE_URL")
    data_service_url: str = Field("http://data-service:8003", env="DATA_SERVICE_URL")
    log_level: str = Field("INFO", env="LOG_LEVEL")

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
