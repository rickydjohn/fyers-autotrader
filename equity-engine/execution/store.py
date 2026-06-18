"""
Redis-backed state for equity execution: the operating mode and paper positions.

Sync client on purpose — the broker is sync and the API handlers call it via
asyncio.to_thread, so we avoid mixing async/sync. Keys are namespaced under
``equity:`` and are independent of the old options system's keys.
"""

import json
from typing import Optional

import redis

from config import settings

MODE_KEY = "equity:mode"               # "simulation" | "live"
POS_KEY = "equity:paper:positions"     # hash: symbol -> position json
TRADES_KEY = "equity:paper:trades"     # list of executed paper trades
REPORT_KEY = "equity:analysis:report"  # cached LLM analysis report (JSON)

_client: Optional[redis.Redis] = None


def _r() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(settings.redis_url, decode_responses=True)
    return _client


def get_mode() -> str:
    return _r().get(MODE_KEY) or "simulation"


def set_mode(mode: str) -> str:
    mode = "live" if mode == "live" else "simulation"
    _r().set(MODE_KEY, mode)
    return mode


def get_paper_positions() -> list[dict]:
    return [json.loads(v) for v in _r().hgetall(POS_KEY).values()]


def get_paper_position(symbol: str) -> Optional[dict]:
    raw = _r().hget(POS_KEY, symbol)
    return json.loads(raw) if raw else None


def upsert_paper_position(pos: dict) -> None:
    _r().hset(POS_KEY, pos["symbol"], json.dumps(pos))


def remove_paper_position(symbol: str) -> None:
    _r().hdel(POS_KEY, symbol)


def log_paper_trade(trade: dict) -> None:
    _r().rpush(TRADES_KEY, json.dumps(trade))


def get_report() -> Optional[dict]:
    raw = _r().get(REPORT_KEY)
    return json.loads(raw) if raw else None


def set_report(report: dict) -> None:
    _r().set(REPORT_KEY, json.dumps(report))
