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
from llm.prompts import build_decision_prompt
from models.schemas import LLMDecision, MarketSnapshot, TechnicalIndicators
from news.sentiment import format_news_for_prompt
from indicators.technicals import get_macd_signal_label
import data_client
from context.formatter import format_context_for_prompt

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")


def _parse_llm_response(raw: str, price: float) -> Optional[dict]:
    """Extract JSON from LLM response with fallback regex parsing."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[^{}]+\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    logger.warning(f"Could not parse LLM response as JSON: {raw[:200]}")
    return None


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

    prompt = build_decision_prompt(
        symbol=snapshot.symbol,
        price=snapshot.ltp,
        timestamp=datetime.now(IST).strftime("%Y-%m-%d %H:%M"),
        bc=ind.cpr.bc,
        tc=ind.cpr.tc,
        pivot=ind.cpr.pivot,
        cpr_width_pct=ind.cpr.width_pct,
        cpr_signal=ind.cpr_signal,
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
        indicators_snapshot={
            "price": snapshot.ltp,
            "cpr_signal": ind.cpr_signal,
            "rsi": ind.rsi,
            "vwap": ind.vwap,
            "ema_9": ind.ema_9,
            "ema_21": ind.ema_21,
            "macd_signal": macd_label,
            "sentiment_score": snapshot.news.aggregate_score if snapshot.news else 0.0,
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
