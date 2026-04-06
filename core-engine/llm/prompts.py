"""
Prompt templates for Ollama LLM inference.
Structured to produce deterministic JSON output.

v2: Extended with multi-timeframe historical context block.
"""

DECISION_PROMPT_TEMPLATE = """{historical_context_block}

## Current Market Snapshot
Symbol: {symbol}
Current Price: ₹{price:.2f}
Time: {timestamp} IST

## Intraday Technical Indicators
CPR: BC=₹{bc:.2f}, TC=₹{tc:.2f}, Pivot=₹{pivot:.2f}
CPR Width: {cpr_width_pct:.2f}% ({cpr_type})
Price vs CPR: {cpr_signal}
Previous Day: High=₹{prev_day_high:.2f} Low=₹{prev_day_low:.2f}
Nearest Resistance: ₹{nearest_resistance:.2f} ({resistance_label})
Nearest Support: ₹{nearest_support:.2f} ({support_label})
RSI(14): {rsi:.1f}
EMA(9): ₹{ema_9:.2f} | EMA(21): ₹{ema_21:.2f}
MACD Signal: {macd_signal}
VWAP: ₹{vwap:.2f}

## News Sentiment (last 2 hours)
{news_summary}
Overall Sentiment: {sentiment_label} (score: {sentiment_score:.2f})

## Decision Rules
You are a disciplined intraday equity trader analyzing NSE Indian markets.
Use ALL timeframe context above before deciding.
- BUY if: price above CPR + RSI 45-65 + bullish/neutral sentiment + price above VWAP + 1h/daily trend not BEARISH
- SELL if: price below CPR + RSI 35-55 + bearish/neutral sentiment + price below VWAP + 1h/daily trend not BULLISH
- HOLD if: price inside CPR OR RSI extreme (>70 or <30) OR conflicting multi-timeframe signals
- PDH breakout (price crossing above PDH) is a strong BUY signal; rejection at PDH is HOLD/SELL
- PDL breakdown (price crossing below PDL) is a strong SELL signal; bounce at PDL is HOLD/BUY
- Set stop_loss 0.3-0.5% below entry for BUY (above for SELL)
- Set target for minimum 1.5:1 risk/reward ratio

Respond ONLY with a valid JSON object, no explanation outside the JSON:
{{
  "decision": "BUY",
  "confidence": 0.75,
  "reasoning": "Single sentence explaining key factors including multi-timeframe context.",
  "stop_loss": 0.00,
  "target": 0.00,
  "risk_reward": 0.00
}}"""


def build_decision_prompt(
    symbol: str,
    price: float,
    timestamp: str,
    bc: float,
    tc: float,
    pivot: float,
    cpr_width_pct: float,
    cpr_signal: str,
    prev_day_high: float,
    prev_day_low: float,
    nearest_resistance: float,
    resistance_label: str,
    nearest_support: float,
    support_label: str,
    rsi: float,
    ema_9: float,
    ema_21: float,
    macd_signal: str,
    vwap: float,
    news_summary: str,
    sentiment_label: str,
    sentiment_score: float,
    historical_context_block: str = "",
) -> str:
    cpr_type = "NARROW (trending day)" if cpr_width_pct < 0.25 else "WIDE (rangebound day)"
    # Provide a default header when no historical context is available
    if not historical_context_block:
        historical_context_block = (
            "## Historical Context\n"
            "No historical data available — operating on intraday data only."
        )
    return DECISION_PROMPT_TEMPLATE.format(
        historical_context_block=historical_context_block,
        symbol=symbol,
        price=price,
        timestamp=timestamp,
        bc=bc,
        tc=tc,
        pivot=pivot,
        cpr_width_pct=cpr_width_pct,
        cpr_type=cpr_type,
        cpr_signal=cpr_signal,
        prev_day_high=prev_day_high,
        prev_day_low=prev_day_low,
        nearest_resistance=nearest_resistance,
        resistance_label=resistance_label,
        nearest_support=nearest_support,
        support_label=support_label,
        rsi=rsi,
        ema_9=ema_9,
        ema_21=ema_21,
        macd_signal=macd_signal,
        vwap=vwap,
        news_summary=news_summary,
        sentiment_label=sentiment_label,
        sentiment_score=sentiment_score,
    )
