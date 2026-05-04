"""
Unit tests for _apply_cross_symbol_gate in core-engine/llm/decision.py.
Pure Python — no Redis, Fyers, Ollama, or DB.
No test data is written anywhere.
"""

import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

# ── Stub out every heavy dependency that decision.py imports ──────────────────

def _stub(name, **attrs):
    m = ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m

# redis — Redis class must exist as an attribute
_redis_async = _stub("redis.asyncio", Redis=MagicMock)
_stub("redis", asyncio=_redis_async)

# config
_cfg = _stub("config")
_cfg.settings = MagicMock(log_level="INFO")

# llm sub-modules
_stub("llm.client", query_ollama=MagicMock())
_stub("llm.prompts", build_decision_prompt=MagicMock())

# models.schemas — only LLMDecision and co. are needed at import time
_stub("models.schemas", LLMDecision=MagicMock, MarketSnapshot=MagicMock, TechnicalIndicators=MagicMock)

# news
_stub("news.sentiment", format_news_for_prompt=MagicMock())

# indicators
_stub("indicators.technicals", get_macd_signal_label=MagicMock(return_value="NEUTRAL"))
_stub("indicators.historical_sr", format_sr_for_prompt=MagicMock(return_value=""))

# data_client
_stub("data_client", persist_decision=MagicMock())

# context
_stub("context.formatter", format_context_for_prompt=MagicMock(return_value=""), format_magnet_zones=MagicMock(return_value=""))

# fyers
_stub("fyers.options", get_affordable_option=MagicMock(return_value=None))
_stub("fyers.market_data", get_quote=MagicMock(return_value=None))
_stub("fyers.orders", get_funds=MagicMock(return_value=None))

# Add core-engine to path so the real module can be imported
import os as _os
_REPO = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_CORE = _os.path.join(_REPO, "core-engine")
if not _os.path.isdir(_CORE):
    _CORE = _REPO
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)

from llm.decision import _apply_cross_symbol_gate  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _validated(decision="BUY", confidence=0.70):
    return {
        "decision":   decision,
        "confidence": confidence,
        "reasoning":  "original reasoning",
        "stop_loss":  22000.0,
        "target":     22500.0,
        "risk_reward": 2.0,
    }


def _peer(decision="BUY", confidence=0.72):
    return {"decision": decision, "confidence": confidence, "timestamp": 1.0}


# ── No-op cases ───────────────────────────────────────────────────────────────

def test_no_peer_signal_is_noop():
    v = _validated("BUY")
    result = _apply_cross_symbol_gate(v, None, symbol="NSE:NIFTYBANK-INDEX")
    assert result["decision"] == "BUY"
    assert result["confidence"] == pytest.approx(0.70)
    assert "Cross-symbol gate" not in result["reasoning"]


def test_peer_hold_is_noop():
    v = _validated("BUY")
    result = _apply_cross_symbol_gate(v, _peer("HOLD"), symbol="NSE:NIFTYBANK-INDEX")
    assert result["decision"] == "BUY"
    assert result["confidence"] == pytest.approx(0.70)


def test_current_hold_is_noop():
    v = _validated("HOLD")
    result = _apply_cross_symbol_gate(v, _peer("BUY"), symbol="NSE:NIFTYBANK-INDEX")
    assert result["decision"] == "HOLD"
    assert "Cross-symbol gate" not in result["reasoning"]


def test_no_peer_signal_field_is_noop():
    v = _validated("BUY")
    result = _apply_cross_symbol_gate(v, {}, symbol="NSE:NIFTYBANK-INDEX")
    assert result["decision"] == "BUY"


# ── Conflict: override to HOLD ────────────────────────────────────────────────

def test_conflict_buy_vs_sell_overrides_to_hold():
    v = _validated("SELL", confidence=0.75)
    result = _apply_cross_symbol_gate(v, _peer("BUY"), symbol="NSE:NIFTYBANK-INDEX")
    assert result["decision"] == "HOLD"
    assert "conflicts" in result["reasoning"]
    assert "NIFTY=BUY" in result["reasoning"]


def test_conflict_sell_vs_buy_overrides_to_hold():
    v = _validated("BUY", confidence=0.72)
    result = _apply_cross_symbol_gate(v, _peer("SELL"), symbol="NSE:NIFTYBANK-INDEX")
    assert result["decision"] == "HOLD"
    assert "conflicts" in result["reasoning"]
    assert "NIFTY=SELL" in result["reasoning"]


def test_conflict_reduces_confidence_by_10pct():
    v = _validated("BUY", confidence=0.80)
    result = _apply_cross_symbol_gate(v, _peer("SELL"), symbol="NSE:NIFTYBANK-INDEX")
    assert result["confidence"] == pytest.approx(0.70)


def test_conflict_confidence_floor_at_055():
    v = _validated("BUY", confidence=0.52)  # 0.52 - 0.10 = 0.42, but floor is 0.55
    result = _apply_cross_symbol_gate(v, _peer("SELL"), symbol="NSE:NIFTYBANK-INDEX")
    assert result["confidence"] == pytest.approx(0.55)


# ── Alignment: confidence boost ───────────────────────────────────────────────

def test_alignment_buy_buy_boosts_confidence():
    v = _validated("BUY", confidence=0.70)
    result = _apply_cross_symbol_gate(v, _peer("BUY"), symbol="NSE:NIFTYBANK-INDEX")
    assert result["decision"] == "BUY"
    assert result["confidence"] == pytest.approx(0.78)
    assert "aligns" in result["reasoning"]
    assert "+0.08 confidence" in result["reasoning"]


def test_alignment_sell_sell_boosts_confidence():
    v = _validated("SELL", confidence=0.65)
    result = _apply_cross_symbol_gate(v, _peer("SELL"), symbol="NSE:NIFTYBANK-INDEX")
    assert result["decision"] == "SELL"
    assert result["confidence"] == pytest.approx(0.73)


def test_alignment_confidence_cap_at_1():
    v = _validated("BUY", confidence=0.96)
    result = _apply_cross_symbol_gate(v, _peer("BUY"), symbol="NSE:NIFTYBANK-INDEX")
    assert result["confidence"] == pytest.approx(1.0)


def test_alignment_confidence_exactly_1_stays_at_1():
    v = _validated("BUY", confidence=1.0)
    result = _apply_cross_symbol_gate(v, _peer("BUY"), symbol="NSE:NIFTYBANK-INDEX")
    assert result["confidence"] == pytest.approx(1.0)


# ── Return value ──────────────────────────────────────────────────────────────

def test_returns_same_dict_object():
    """Gate mutates and returns the same dict — no copy created."""
    v = _validated("BUY")
    result = _apply_cross_symbol_gate(v, _peer("BUY"), symbol="NSE:NIFTYBANK-INDEX")
    assert result is v


# ── Reasoning annotation ──────────────────────────────────────────────────────

def test_conflict_reasoning_prepended_to_original():
    v = _validated("BUY", confidence=0.70)
    result = _apply_cross_symbol_gate(v, _peer("SELL"), symbol="NSE:NIFTYBANK-INDEX")
    assert result["reasoning"].endswith("original reasoning")


def test_alignment_reasoning_prepended_to_original():
    v = _validated("BUY", confidence=0.70)
    result = _apply_cross_symbol_gate(v, _peer("BUY"), symbol="NSE:NIFTYBANK-INDEX")
    assert result["reasoning"].endswith("original reasoning")
