"""
NSE equity universe from the Fyers public symbol master.

Fyers publishes a headerless CSV per exchange segment at
``https://public.fyers.in/sym_details/NSE_CM.csv`` (no auth needed). NSE_CM is the
NSE Capital Market segment — it contains cash equities, indices and ETFs mixed
together. We keep only the regular rolling-settlement equity series, identified by
the ``-EQ`` suffix on the Fyers ticker (e.g. ``NSE:SBIN-EQ``).

The file has ~20 comma-separated columns. The ones we need (0-indexed):

    col 1  → company / instrument name
    col 3  → lot size (1 for cash equities)
    col 5  → ISIN
    col 9  → Fyers ticker  (NSE:SYMBOL-EQ)

These indices are pinned as constants below so they are trivial to correct if Fyers
reorders the file — run ``python -m universe.symbol_master`` to dump a sample and
verify. Parsing is deliberately defensive: any malformed/short row is skipped, never
fatal, because this list gates the entire scan.

Result is cached to disk (JSON) and refreshed once per trading day.
"""

import csv
import io
import json
import logging
import os
from datetime import datetime, timezone

import httpx

from config import settings
from models import EquitySymbol

logger = logging.getLogger(__name__)

# Column positions in the Fyers NSE_CM.csv (0-indexed). See module docstring.
COL_NAME = 1
COL_LOT = 3
COL_ISIN = 5
COL_TICKER = 9
_MIN_COLS = COL_TICKER + 1

EQ_SUFFIX = "-EQ"
NSE_PREFIX = "NSE:"


def _parse_equities(csv_text: str) -> list[EquitySymbol]:
    """Parse raw NSE_CM.csv text into the EQ-only universe."""
    equities: list[EquitySymbol] = []
    seen: set[str] = set()
    reader = csv.reader(io.StringIO(csv_text))

    total = 0
    for row in reader:
        total += 1
        if len(row) < _MIN_COLS:
            continue
        ticker = row[COL_TICKER].strip()
        if not ticker.startswith(NSE_PREFIX) or not ticker.endswith(EQ_SUFFIX):
            continue
        if ticker in seen:
            continue
        seen.add(ticker)

        short = ticker[len(NSE_PREFIX):-len(EQ_SUFFIX)]
        try:
            lot = int(float(row[COL_LOT])) if row[COL_LOT].strip() else 1
        except ValueError:
            lot = 1

        equities.append(
            EquitySymbol(
                symbol=ticker,
                short_symbol=short,
                name=row[COL_NAME].strip(),
                isin=row[COL_ISIN].strip(),
                lot_size=lot,
            )
        )

    logger.info("Parsed %d EQ equities from %d symbol-master rows", len(equities), total)
    return equities


def refresh_universe() -> list[EquitySymbol]:
    """Fetch the symbol master, parse the EQ universe, and write the cache."""
    logger.info("Fetching Fyers symbol master: %s", settings.symbol_master_url)
    resp = httpx.get(settings.symbol_master_url, timeout=30.0, follow_redirects=True)
    resp.raise_for_status()

    equities = _parse_equities(resp.text)
    if not equities:
        raise RuntimeError(
            "Symbol master parsed to 0 equities — column layout likely changed; "
            "verify COL_* constants in universe/symbol_master.py"
        )

    _write_cache(equities)
    return equities


def load_universe(force_refresh: bool = False) -> list[EquitySymbol]:
    """Return the NSE equity universe, refreshing from Fyers if the cache is stale.

    Honours ``universe_limit`` (a dev cap; 0 = full universe). Sorted by ticker for
    stable, deterministic scan ordering.
    """
    equities = None
    if not force_refresh:
        equities = _read_cache_if_fresh()

    if equities is None:
        try:
            equities = refresh_universe()
        except Exception:
            logger.exception("Universe refresh failed; falling back to cache if any")
            equities = _read_cache_if_fresh(ignore_age=True)
            if equities is None:
                raise

    equities.sort(key=lambda e: e.symbol)
    if settings.universe_limit > 0:
        equities = equities[: settings.universe_limit]
    return equities


# ── Cache I/O ─────────────────────────────────────────────────────────────────
def _write_cache(equities: list[EquitySymbol]) -> None:
    path = settings.universe_cache_path
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "count": len(equities),
        "equities": [e.model_dump() for e in equities],
    }
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)
    logger.info("Cached %d equities → %s", len(equities), path)


def _read_cache_if_fresh(ignore_age: bool = False) -> list[EquitySymbol] | None:
    path = settings.universe_cache_path
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("Universe cache unreadable: %s", path)
        return None

    if not ignore_age:
        fetched = datetime.fromisoformat(payload["fetched_at"])
        age_h = (datetime.now(timezone.utc) - fetched).total_seconds() / 3600.0
        if age_h > settings.universe_max_age_hours:
            logger.info("Universe cache stale (%.1fh old) — will refresh", age_h)
            return None

    return [EquitySymbol(**e) for e in payload["equities"]]


if __name__ == "__main__":
    # Manual verification: dump a sample so the COL_* indices can be eyeballed
    # against the live file.
    logging.basicConfig(level=logging.INFO)
    universe = load_universe(force_refresh=True)
    print(f"\nNSE EQ universe: {len(universe)} symbols\n")
    for e in universe[:10]:
        print(f"  {e.symbol:<24} {e.short_symbol:<12} lot={e.lot_size:<4} {e.name}")
