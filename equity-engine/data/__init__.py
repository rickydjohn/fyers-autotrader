"""Market-data access for equity-engine.

core-engine owns Fyers auth + the SDK; this layer is a thin HTTP consumer with a
once-per-day on-disk cache for daily bars. Everything downstream depends only on
the ``CandleProvider`` protocol, so tests/backtests can inject synthetic bars.
"""

from data.candles import CandleProvider, CoreEngineProvider, get_provider

__all__ = ["CandleProvider", "CoreEngineProvider", "get_provider"]
