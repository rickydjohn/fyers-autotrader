"""
equity-engine configuration.

A clean, single settings object for the equity swing/positional system. Pydantic
BaseSettings so every value is overridable via env var or .env — same pattern as
core-engine/simulation-engine, but scoped to this module only.

Design notes:
  * Reuses the SAME Fyers access token written by core-engine (token_path), so we
    do not run a second OAuth flow. The equity engine is a pure data consumer +
    order placer; auth stays owned by core-engine.
  * The universe is the full NSE cash-equity list (the Fyers symbol master). The
    liquidity/price floors here are applied DOWNSTREAM (in the screener, which has
    daily-candle volume) — the universe module itself just enumerates tradable EQ.
  * trading_mode (paper|real) is the single switch that selects the broker, exactly
    like the options system's simulation|live mode.
"""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Note: equity-engine does NOT touch Fyers auth/SDK directly — all market data
    # and order placement go through core-engine over HTTP. So no Fyers creds here.

    # ── Infra ─────────────────────────────────────────────────────────────────
    redis_url: str = Field("redis://localhost:6379", env="REDIS_URL")
    data_service_url: str = Field("http://data-service:8003", env="DATA_SERVICE_URL")
    # core-engine owns Fyers auth/SDK; we fetch candles + quotes over HTTP from it.
    core_engine_url: str = Field("http://core-engine:8001", env="CORE_ENGINE_URL")
    # Daily bars change once/day → cache them on disk, keyed by IST date.
    candle_cache_dir: str = Field("/app/cache/candles", env="CANDLE_CACHE_DIR")
    # Throttle full-universe fetches so we stay under Fyers rate limits.
    fetch_delay_ms: int = Field(120, env="FETCH_DELAY_MS")
    fetch_timeout_s: float = Field(20.0, env="FETCH_TIMEOUT_S")

    # ── Universe ────────────────────────────────────────────────────────────--
    # Fyers publishes a public, headerless symbol-master CSV per exchange segment.
    # NSE_CM = NSE Capital Market (cash equities, indices, ETFs). We keep only the
    # `-EQ` series (regular rolling-settlement equities).
    symbol_master_url: str = Field(
        "https://public.fyers.in/sym_details/NSE_CM.csv",
        env="SYMBOL_MASTER_URL",
    )
    # Where the parsed universe is cached on disk; refreshed once per trading day.
    universe_cache_path: str = Field("/app/cache/nse_equity_universe.json", env="UNIVERSE_CACHE_PATH")
    universe_max_age_hours: int = Field(20, env="UNIVERSE_MAX_AGE_HOURS")
    # Optional hard cap for dev/testing (0 = no cap, use full universe).
    universe_limit: int = Field(0, env="UNIVERSE_LIMIT")

    # ── Liquidity / price floors (applied in the screener with daily data) ─────
    min_price: float = Field(50.0, env="MIN_PRICE")            # skip penny stocks
    max_price: float = Field(100000.0, env="MAX_PRICE")
    min_avg_turnover_cr: float = Field(5.0, env="MIN_AVG_TURNOVER_CR")  # ₹ crore/day, 20d avg

    # ── Levels / CPR timeframes ────────────────────────────────────────────────
    daily_lookback_bars: int = Field(250, env="DAILY_LOOKBACK_BARS")   # ~1 trading year of daily bars
    monthly_cpr_lookback_months: int = Field(2, env="MONTHLY_CPR_LOOKBACK_MONTHS")  # prior month for CPR

    # ── Scan cadence ────────────────────────────────────────────────────────--
    # Swing/positional on daily levels: a heavy EOD scan that ranks the universe,
    # plus a light intraday monitor that only watches the shortlist + open positions.
    eod_scan_time: str = Field("15:45", env="EOD_SCAN_TIME")            # IST, after close
    intraday_monitor_interval_seconds: int = Field(300, env="INTRADAY_MONITOR_INTERVAL_SECONDS")

    # ── Risk ────────────────────────────────────────────────────────────────--
    initial_capital: float = Field(1_000_000.0, env="INITIAL_CAPITAL")
    risk_per_trade_pct: float = Field(1.0, env="RISK_PER_TRADE_PCT")    # % of capital risked per trade
    max_open_positions: int = Field(10, env="MAX_OPEN_POSITIONS")
    max_position_pct: float = Field(20.0, env="MAX_POSITION_PCT")       # cap any single name's notional
    atr_period: int = Field(14, env="ATR_PERIOD")
    atr_stop_mult_swing: float = Field(2.0, env="ATR_STOP_MULT_SWING")
    atr_stop_mult_positional: float = Field(3.0, env="ATR_STOP_MULT_POSITIONAL")
    min_risk_reward: float = Field(1.5, env="MIN_RISK_REWARD")

    # ── Execution ────────────────────────────────────────────────────────────-
    trading_mode: Literal["paper", "real"] = Field("paper", env="TRADING_MODE")
    slippage_pct: float = Field(0.05, env="SLIPPAGE_PCT")
    commission_pct: float = Field(0.03, env="COMMISSION_PCT")           # round-trip brokerage+charges ~%
    commission_flat: float = Field(20.0, env="COMMISSION_FLAT")

    # ── Optional advisory LLM (OFF by default — deterministic core is v1) ──────
    use_llm_advisor: bool = Field(False, env="USE_LLM_ADVISOR")

    # ── App ─────────────────────────────────────────────────────────────────--
    log_level: str = Field("INFO", env="LOG_LEVEL")
    timezone: str = "Asia/Kolkata"

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
