from pydantic_settings import BaseSettings
from pydantic import Field
from typing import List, Optional


class Settings(BaseSettings):
    # Fyers
    fyers_client_id: str = Field(..., env="FYERS_CLIENT_ID")
    fyers_secret_key: str = Field(..., env="FYERS_SECRET_KEY")
    fyers_redirect_uri: str = Field("http://100.78.91.15:8001/fyers/callback", env="FYERS_REDIRECT_URI")
    token_path: str = "/app/tokens/access_token.json"

    # LLM provider selection — "ollama" or "claude"
    llm_provider: str = Field("ollama", env="LLM_PROVIDER")

    # Ollama
    ollama_endpoint: str = Field("http://localhost:11434", env="OLLAMA_ENDPOINT")
    ollama_model: str    = Field("gemma4:latest",          env="OLLAMA_MODEL")
    ollama_timeout: int  = Field(45,                       env="OLLAMA_TIMEOUT")

    # Claude (Anthropic)
    claude_endpoint: str       = Field("https://api.anthropic.com", env="CLAUDE_ENDPOINT")
    claude_model: str          = Field("claude-haiku-4-5-20251001", env="CLAUDE_MODEL")
    claude_api_key: str        = Field("",                          env="CLAUDE_API_KEY")
    claude_timeout: int        = Field(30,                          env="CLAUDE_TIMEOUT")

    # Redis
    redis_url: str = Field("redis://localhost:6379", env="REDIS_URL")

    # Market
    market_open: str = "09:15"
    market_close: str = "15:30"
    session_close_hour: int = Field(15, env="SESSION_CLOSE_HOUR")
    session_close_minute: int = Field(15, env="SESSION_CLOSE_MINUTE")
    timezone: str = "Asia/Kolkata"
    scan_interval_seconds: int = Field(300, env="SCAN_INTERVAL_SECONDS")
    position_watcher_interval_seconds: int = Field(5, env="POSITION_WATCHER_INTERVAL_SECONDS")
    candle_interval: str = Field("1", env="CANDLE_INTERVAL")

    # Watchlist
    symbols: List[str] = [
        "NSE:NIFTY50-INDEX",
        "NSE:NIFTYBANK-INDEX",
    ]

    # Proxy (optional — routes fyers.in traffic through proxy when all four are set)
    proxy_ip: Optional[str] = Field(None, env="PROXY_IP")
    proxy_user: Optional[str] = Field(None, env="PROXY_USER")
    proxy_password: Optional[str] = Field(None, env="PROXY_PASSWORD")
    proxy_port: Optional[int] = Field(None, env="PROXY_PORT")

    # Data Service
    data_service_url: str = Field("http://data-service:8003", env="DATA_SERVICE_URL")

    # App
    log_level: str = Field("INFO", env="LOG_LEVEL")
    environment: str = Field("simulation", env="ENVIRONMENT")

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
