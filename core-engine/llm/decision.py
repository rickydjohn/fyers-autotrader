"""
Parses LLM output into structured trade decisions.
Publishes decisions to Redis stream for simulation engine consumption.
Also persists decisions to data-service.
"""

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Optional

import pytz
import redis.asyncio as aioredis

from config import settings
from llm.client import query_ollama
from llm.prompts import build_decision_prompt, format_options_oi_block
from models.schemas import LLMDecision, MarketSnapshot, TechnicalIndicators
from news.sentiment import format_news_for_prompt
from indicators.technicals import get_macd_signal_label
from indicators.historical_sr import format_sr_for_prompt
import data_client
from context.formatter import format_context_for_prompt, format_magnet_zones
from fyers.options import get_affordable_option
from fyers.market_data import get_quote

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")


def _fix_json_strings(s: str) -> str:
    """Replace literal newlines/tabs inside JSON string values with spaces.

    JSON strings cannot contain raw newlines — they must be escaped as \\n.
    Ollama sometimes generates multi-sentence reasoning with actual line breaks.
    This walks the string character-by-character tracking quote state so it
    only touches content inside string literals, leaving structural JSON intact.
    """
    result = []
    in_string = False
    escape_next = False
    for ch in s:
        if escape_next:
            result.append(ch)
            escape_next = False
        elif ch == "\\":
            result.append(ch)
            escape_next = True
        elif ch == '"':
            result.append(ch)
            in_string = not in_string
        elif in_string and ch in ("\n", "\r"):
            result.append(" ")  # flatten literal newlines inside strings
        elif in_string and ch == "\t":
            result.append(" ")
        else:
            result.append(ch)
    return "".join(result)


def _parse_llm_response(raw: str, price: float) -> Optional[dict]:
    """Extract JSON from LLM response with fallback parsing.

    Handles common Ollama output patterns:
    - Clean JSON
    - JSON wrapped in markdown code blocks (```json ... ```)
    - JSON preceded/followed by explanation text
    - Multi-line reasoning strings with literal newlines (invalid JSON)
    """
    if not raw:
        return None

    # Strip markdown code fences if present
    clean = re.sub(r"```(?:json)?\s*", "", raw).strip()

    # Attempt 1: direct parse
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass

    # Attempt 2: fix literal newlines inside strings, then parse
    try:
        return json.loads(_fix_json_strings(clean))
    except json.JSONDecodeError:
        pass

    # Attempt 3: extract outermost { ... } by balanced brace scanning
    start = clean.find("{")
    if start != -1:
        depth = 0
        for i, ch in enumerate(clean[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = clean[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        try:
                            return json.loads(_fix_json_strings(candidate))
                        except json.JSONDecodeError:
                            break

    logger.warning(f"Could not parse LLM response as JSON: {raw[:200]}")
    return None


def _apply_cross_symbol_gate(
    validated: dict,
    peer_signal: Optional[dict],
    symbol: str = "",
) -> dict:
    """
    Layer 2 cross-symbol confidence gate.

    Rules:
    - No peer or peer=HOLD → no-op
    - Conflict (BUY vs SELL) → override to HOLD, -0.10 confidence
    - Alignment (same direction) → +0.08 confidence (cap 1.0)

    Mutates and returns `validated` in-place for convenience.
    """
    if (
        not peer_signal
        or peer_signal.get("decision") not in ("BUY", "SELL")
        or validated["decision"] not in ("BUY", "SELL")
    ):
        return validated

    peer_dir = peer_signal["decision"]
    if peer_dir != validated["decision"]:
        old_decision = validated["decision"]
        validated["decision"] = "HOLD"
        validated["confidence"] = max(0.55, validated["confidence"] - 0.10)
        validated["reasoning"] = (
            f"[Cross-symbol gate: NIFTY={peer_dir} conflicts with {symbol}={old_decision}, holding] "
            + validated["reasoning"]
        )
        logger.info(
            f"Cross-symbol gate: {symbol} overridden to HOLD "
            f"(NIFTY={peer_dir} conflicts with {old_decision})"
        )
    else:
        validated["confidence"] = min(1.0, validated["confidence"] + 0.08)
        validated["reasoning"] = (
            f"[Cross-symbol gate: NIFTY={peer_dir} aligns, +0.08 confidence] "
            + validated["reasoning"]
        )
        logger.info(
            f"Cross-symbol gate: {symbol} confidence boosted "
            f"(NIFTY={peer_dir} aligns with {validated['decision']})"
        )
    return validated


def _validate_decision(data: dict, price: float) -> dict:
    """Validate and sanitize parsed LLM decision."""
    decision = data.get("decision", "HOLD").upper()
    if decision not in ("BUY", "SELL", "HOLD"):
        decision = "HOLD"

    confidence = float(data.get("confidence", 0.5))
    confidence = max(0.0, min(1.0, confidence))

    if confidence < 0.5:
        decision = "HOLD"

    stop_loss = float(data.get("stop_loss", 0.0))
    target = float(data.get("target", 0.0))

    if decision == "BUY":
        if stop_loss >= price or stop_loss <= 0:
            stop_loss = round(price * 0.997, 2)
        if target <= price or target <= 0:
            target = round(price * 1.006, 2)
    elif decision == "SELL":
        if stop_loss <= price or stop_loss <= 0:
            stop_loss = round(price * 1.003, 2)
        if target >= price or target <= 0:
            target = round(price * 0.994, 2)

    risk = abs(price - stop_loss)
    reward = abs(target - price)
    risk_reward = round(reward / risk, 2) if risk > 0 else 0.0

    return {
        "decision": decision,
        "confidence": confidence,
        "reasoning": str(data.get("reasoning", "No reasoning provided."))[:500],
        "stop_loss": stop_loss,
        "target": target,
        "risk_reward": risk_reward,
    }


async def make_decision(
    snapshot: MarketSnapshot,
    redis_client: aioredis.Redis,
    historical_context: Optional[dict] = None,
    sr_levels: Optional[list] = None,
    magnet_zones: Optional[dict] = None,
    peer_signal: Optional[dict] = None,
    options_oi: Optional[dict] = None,
) -> Optional[LLMDecision]:
    """Build prompt (with historical context), call LLM, parse, publish to Redis and DB."""
    ind: TechnicalIndicators = snapshot.indicators
    news_summary = (
        format_news_for_prompt(snapshot.news) if snapshot.news else "No news available."
    )
    macd_label = get_macd_signal_label(ind.macd, ind.macd_signal)

    # Format historical context block for prompt
    hist_block = ""
    if historical_context:
        hist_block = format_context_for_prompt(historical_context)

    # Format historical S/R levels block
    sr_block = format_sr_for_prompt(sr_levels or [], snapshot.ltp)

    # Format price magnet zones block
    magnet_block = ""
    if magnet_zones:
        magnet_block = format_magnet_zones(
            snapshot.ltp,
            magnet_zones.get("gaps", []),
            magnet_zones.get("cprs", []),
        )

    # Format options OI block
    oi_block = format_options_oi_block(options_oi)

    prompt = build_decision_prompt(
        symbol=snapshot.symbol,
        price=snapshot.ltp,
        timestamp=datetime.now(IST).strftime("%Y-%m-%d %H:%M"),
        bc=ind.cpr.bc,
        tc=ind.cpr.tc,
        pivot=ind.cpr.pivot,
        cpr_width_pct=ind.cpr.width_pct,
        cpr_signal=ind.cpr_signal,
        prev_day_high=ind.prev_day_high,
        prev_day_low=ind.prev_day_low,
        day_high=ind.day_high,
        day_low=ind.day_low,
        consolidation_pct=ind.consolidation_pct,
        range_breakout=ind.range_breakout,
        sr_levels_block=sr_block,
        nearest_resistance=ind.nearest_resistance,
        resistance_label=ind.nearest_resistance_label,
        nearest_support=ind.nearest_support,
        support_label=ind.nearest_support_label,
        rsi=ind.rsi,
        ema_9=ind.ema_9,
        ema_21=ind.ema_21,
        macd_signal=macd_label,
        vwap=ind.vwap,
        news_summary=news_summary,
        sentiment_label=snapshot.news.label if snapshot.news else "NEUTRAL",
        sentiment_score=snapshot.news.aggregate_score if snapshot.news else 0.0,
        historical_context_block=hist_block,
        day_type=ind.cpr.day_type,
        pdh_pivot_confluence=ind.pdh_pivot_confluence,
        magnet_zones_block=magnet_block,
        options_oi_block=oi_block,
    )

    logger.info(f"Querying Ollama for {snapshot.symbol}...")
    raw_response = await query_ollama(prompt)
    if not raw_response:
        logger.warning(f"No LLM response for {snapshot.symbol}, defaulting to HOLD")
        raw_response = '{"decision":"HOLD","confidence":0.5,"reasoning":"LLM unavailable."}'

    parsed = _parse_llm_response(raw_response, snapshot.ltp)
    if not parsed:
        return None

    validated = _validate_decision(parsed, snapshot.ltp)

    # Hard MACD filter — LLM cannot override momentum direction.
    # SELL+BULLISH or BUY+BEARISH is always contradictory — force HOLD unconditionally.
    # Previously this was a confidence penalty (−0.15) with threshold at 0.5, but since
    # LLM confidence for SELL averaged 0.78 the penalty never triggered — all contradictory
    # SELL decisions survived and were published but never traded (PE option resolved to
    # HOLD implicitly when the sim engine saw no valid option). Hard block prevents noise.
    if validated["decision"] == "SELL" and macd_label == "BULLISH":
        validated["decision"] = "HOLD"
        validated["confidence"] = max(0.55, validated["confidence"] - 0.15)
        validated["reasoning"] = f"[MACD override: BULLISH MACD contradicts SELL] {validated['reasoning']}"
        logger.info(f"SELL overridden to HOLD for {snapshot.symbol} — MACD is BULLISH")
    elif validated["decision"] == "BUY" and macd_label == "BEARISH":
        validated["decision"] = "HOLD"
        validated["confidence"] = max(0.55, validated["confidence"] - 0.15)
        validated["reasoning"] = f"[MACD override: BEARISH MACD contradicts BUY] {validated['reasoning']}"
        logger.info(f"BUY overridden to HOLD for {snapshot.symbol} — MACD is BEARISH")

    # Layer 2 cross-symbol confidence gate
    _apply_cross_symbol_gate(validated, peer_signal, symbol=snapshot.symbol)

    # Determine available budget for option selection
    available_cash: Optional[float] = None
    try:
        mode_raw = await redis_client.get("trading:mode")
        trading_mode = (
            mode_raw.decode() if isinstance(mode_raw, bytes) else (mode_raw or "simulation")
        )
        if trading_mode == "live":
            from fyers.orders import get_funds
            funds_data = get_funds()
            if funds_data:
                for _key in ("available_balance", "net_available", "available_margin"):
                    _val = funds_data.get(_key)
                    if _val is not None:
                        available_cash = float(_val)
                        break
        else:
            budget_raw = await redis_client.get("budget:state")
            if budget_raw:
                b = json.loads(budget_raw)
                available_cash = float(b.get("cash", 0))
    except Exception as _e:
        logger.warning(f"Could not determine budget for option sizing: {_e}")

    # Resolve affordable option for actionable decisions
    option_symbol = option_type = option_expiry = None
    option_strike = None
    option_price = None
    option_lot_size = None
    if validated["decision"] in ("BUY", "SELL"):
        opt = get_affordable_option(
            snapshot.symbol, snapshot.ltp, validated["decision"],
            max_spend=available_cash,
        )
        if opt:
            option_symbol, option_strike, option_type, option_expiry, option_lot_size = opt
            try:
                q = get_quote(option_symbol)
                option_price = q["ltp"] if q else None
            except Exception as e:
                logger.warning(f"Could not fetch option quote for {option_symbol}: {e}")
            if option_price:
                await redis_client.setex(
                    f"market:{option_symbol}",
                    600,
                    json.dumps({"ltp": option_price, "symbol": option_symbol}),
                )
                logger.info(f"Option selected: {option_symbol} @ ₹{option_price:.2f}")
            else:
                logger.warning(f"No price for {option_symbol}, will trade without option price")

    decision = LLMDecision(
        decision_id=str(uuid.uuid4()),
        symbol=snapshot.symbol,
        timestamp=datetime.now(IST),
        decision=validated["decision"],
        confidence=validated["confidence"],
        reasoning=validated["reasoning"],
        stop_loss=validated["stop_loss"],
        target=validated["target"],
        risk_reward=validated["risk_reward"],
        option_symbol=option_symbol,
        option_strike=option_strike,
        option_type=option_type,
        option_expiry=option_expiry,
        option_price=option_price,
        option_lot_size=option_lot_size,
        indicators_snapshot={
            "price": snapshot.ltp,
            "cpr_signal": ind.cpr_signal,
            "cpr_width_pct": ind.cpr.width_pct,
            "day_type": ind.cpr.day_type,
            "rsi": ind.rsi,
            "vwap": ind.vwap,
            "ema_9": ind.ema_9,
            "ema_21": ind.ema_21,
            "macd_signal": macd_label,
            "sentiment_score": snapshot.news.aggregate_score if snapshot.news else 0.0,
            "day_high": ind.day_high,
            "day_low": ind.day_low,
            "consolidation_pct": ind.consolidation_pct,
            "range_breakout": ind.range_breakout,
            "pdh_pivot_confluence": ind.pdh_pivot_confluence,
        },
    )

    # Publish to Redis stream (simulation engine consumes this)
    await redis_client.xadd(
        "decisions",
        {
            "decision_id": decision.decision_id,
            "symbol": decision.symbol,
            "timestamp": decision.timestamp.isoformat(),
            "decision": decision.decision,
            "confidence": str(decision.confidence),
            "reasoning": decision.reasoning,
            "stop_loss": str(decision.stop_loss),
            "target": str(decision.target),
            "risk_reward": str(decision.risk_reward),
            "indicators": json.dumps(decision.indicators_snapshot),
            "option_symbol": decision.option_symbol or "",
            "option_strike": str(decision.option_strike or 0),
            "option_type": decision.option_type or "",
            "option_expiry": decision.option_expiry or "",
            "option_price": str(decision.option_price or 0),
            "option_lot_size": str(decision.option_lot_size or 0),
        },
        maxlen=1000,
    )

    # Save to decision log sorted set (Redis — short-term cache)
    await redis_client.zadd(
        "decision:log",
        {decision.model_dump_json(): decision.timestamp.timestamp()},
    )

    # Persist to TimescaleDB via data-service (durable storage)
    await data_client.persist_decision({
        "decision_id":         decision.decision_id,
        "time":                decision.timestamp.isoformat(),
        "symbol":              decision.symbol,
        "decision":            decision.decision,
        "confidence":          decision.confidence,
        "reasoning":           decision.reasoning,
        "stop_loss":           decision.stop_loss,
        "target":              decision.target,
        "risk_reward":         decision.risk_reward,
        "indicators_snapshot": decision.indicators_snapshot,
        "acted_upon":          False,
        "historical_context":  historical_context,
    })

    logger.info(
        f"Decision for {snapshot.symbol}: {decision.decision} "
        f"(confidence={decision.confidence:.2f})"
    )
    return decision
