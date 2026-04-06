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
The CPR Width label above tells you the day type — use the matching ruleset.

### BREAKOUT OVERRIDE (applies on ANY day type — check this FIRST)
A confirmed PDH/PDL breakout overrides the CPR day-type classification entirely.
- BUY  if: price > PDH + price above VWAP + RSI between 45 and 75 (inclusive) + MACD not BEARISH — assign confidence >= 0.80; valid even on a WIDE CPR day
- SELL if: price < PDL + price below VWAP + RSI between 25 and 55 (inclusive) + MACD not BULLISH — assign confidence >= 0.80; valid even on a WIDE CPR day
- HOLD — hard stop — if RSI > 75 OR RSI < 25: output HOLD regardless of breakout; do not output BUY or SELL under any circumstances when RSI exceeds these limits
- HOLD if: price is at PDH but has NOT closed above it (rejection)

### RANGEBOUND DAY (CPR is WIDE) — only apply if no PDH/PDL breakout
- BUY  if: price ABOVE_CPR + RSI 45-65 + price above VWAP + sentiment not BEARISH + 1h/daily trend not BEARISH
- SELL if: price BELOW_CPR + RSI 35-55 + price below VWAP + sentiment not BULLISH + 1h/daily trend not BULLISH
- HOLD if: price INSIDE_CPR OR RSI > 65 OR RSI < 35 OR conflicting multi-timeframe signals

### TRENDING DAY (CPR is NARROW) — only apply if no PDH/PDL breakout
- BUY  if: price ABOVE_CPR + RSI 45-75 + price above VWAP + MACD not BEARISH
- SELL if: price BELOW_CPR + RSI 25-55 + price below VWAP + MACD not BULLISH
- HOLD if: price INSIDE_CPR OR RSI > 75 OR RSI < 25

### ALL DAYS
- Set stop_loss 0.3-0.5% from entry (below entry for BUY, above entry for SELL)
- Target must give minimum 1.5:1 risk/reward ratio
- Confidence for BUY/SELL: 0.70-0.85 when 3+ conditions align; 0.55-0.69 when 2 conditions align; output HOLD instead of BUY/SELL when fewer than 2 conditions align
- Confidence for HOLD: always between 0.55-0.80 — reflects certainty in the hold call, never output 0.0

Respond ONLY with a valid JSON object, no explanation outside the JSON:
{{
  "decision": "BUY",
  "confidence": 0.80,
  "reasoning": "Single sentence citing day type, RSI, CPR position, VWAP, and PDH/PDL context.",
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
