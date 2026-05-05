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
    slack_webhook_url: str = Field("", env="SLACK_WEBHOOK_URL")

    # Session close time (IST) — no new positions opened at or after this time
    session_close_hour: int = Field(15, env="SESSION_CLOSE_HOUR")
    session_close_minute: int = Field(15, env="SESSION_CLOSE_MINUTE")

    # Minimum option premium (₹) — below this, a 10% SL is within the bid-ask spread
    min_option_premium: float = Field(30.0, env="MIN_OPTION_PREMIUM")

    # Maximum number of lots per trade — prevents huge qty on cheap/near-expiry options
    max_lots: int = Field(5, env="MAX_LOTS")

    # Minutes to block re-entry on an underlying after a STOP_LOSS or TRAIL_STOP
    sl_cooldown_minutes: int = Field(15, env="SL_COOLDOWN_MINUTES")

    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"


settings = Settings()
