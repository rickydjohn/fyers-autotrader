"""Universe filters shared across the screener and CLI."""

# ETFs/ETNs trade in NSE's -EQ series too; exclude them from a stock screener
# (e.g. LIQUIDBEES has ~0 volatility and is not a stock).
ETF_MARKERS = ("BEES", "ETF", "LIQUIDCASE", "LIQUIDADD", "MAFANG", "MON100", "MOM")


def is_etf(short_symbol: str) -> bool:
    s = short_symbol.upper()
    return any(m in s for m in ETF_MARKERS)
