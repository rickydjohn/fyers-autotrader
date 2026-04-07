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
Today's Range: High=₹{day_high:.2f} Low=₹{day_low:.2f}
Consolidation: {consolidation_pct:.2f}% range over last 8 candles ({consolidation_status})
Range Breakout: {range_breakout}
Nearest Resistance: ₹{nearest_resistance:.2f} ({resistance_label})
Nearest Support: ₹{nearest_support:.2f} ({support_label})
RSI(14): {rsi:.1f}
EMA(9): ₹{ema_9:.2f} | EMA(21): ₹{ema_21:.2f}
MACD Signal: {macd_signal}
VWAP: ₹{vwap:.2f}

## Historical Support/Resistance (multi-year daily chart)
Zones where price has historically reversed — derived from {years_of_data} of daily swing data.
{sr_levels_block}

## News Sentiment (last 2 hours)
{news_summary}
Overall Sentiment: {sentiment_label} (score: {sentiment_score:.2f})

## Decision Rules
You are a disciplined intraday equity trader analyzing NSE Indian markets.
The CPR Width label above tells you the day type — use the matching ruleset.

### BREAKOUT OVERRIDE (applies on ANY day type — check this FIRST)
A confirmed PDH/PDL breakout overrides the CPR day-type classification entirely.
- BUY  if: price > PDH + price above VWAP + RSI between 45 and 75 (inclusive) + MACD not BEARISH — assign confidence >= 0.80; valid even on a WIDE CPR day
- BUY  if: price > PDH * 1.005 (more than 0.5% above PDH — strong breakout) + price above VWAP + RSI between 45 and 78 + MACD not BEARISH — RSI cap extends to 78 on a confirmed strong breakout; assign confidence >= 0.80
- Once price has closed above PDH in any scan, treat the breakout as confirmed for the rest of the session — do not revert to HOLD on subsequent scans unless RSI exceeds the cap or price falls back below PDH
- SELL if: price < PDL + price below VWAP + RSI between 25 and 55 (inclusive) + MACD not BULLISH — assign confidence >= 0.80; valid even on a WIDE CPR day
- HOLD — hard stop — if RSI > 78 OR RSI < 25: output HOLD regardless of breakout; do not output BUY or SELL under any circumstances when RSI exceeds these limits
- HOLD if: price is at PDH but has NOT closed above it (rejection)

### INTRADAY RANGE BREAKOUT (check after PDH/PDL breakout, before intraday trend)
The market consolidates in a tight band, then breaks out — this is a high-probability momentum entry.
Fires ONLY when Range Breakout field shows BREAKOUT_HIGH or BREAKOUT_LOW (consolidation_pct < 0.40%).
- BUY  if: Range Breakout = BREAKOUT_HIGH + price above VWAP + RSI 45-75 + EMA9 > EMA21 + MACD not BEARISH → assign confidence 0.75-0.85; this is a CALL trade
- SELL if: Range Breakout = BREAKOUT_LOW  + price below VWAP + RSI 25-55 + EMA9 < EMA21 + MACD not BULLISH → assign confidence 0.75-0.85; this is a PUT trade
- HOLD if: Range Breakout = NONE (no consolidation detected — pattern not confirmed)
- HOLD if: RSI > 78 or RSI < 25 (hard RSI stops apply here too)
- HOLD if: fewer than 3 of the confirmation conditions align (VWAP position, EMA cross, MACD) — breakout alone is not enough

### INTRADAY TREND OVERRIDE (check after breakout, before day-type rules)
When the intraday structure is unambiguously bullish or bearish, override a conflicting daily/1h trend bias.
- BUY  if: EMA9 > EMA21 AND price ABOVE_CPR AND price above VWAP AND RSI 45-75 — intraday trend is BULLISH; daily bearish context is a caution, not a veto; assign confidence 0.65-0.75
- SELL if: EMA9 < EMA21 AND price BELOW_CPR AND price below VWAP AND RSI 25-55 — intraday trend is BEARISH; daily bullish context is a caution, not a veto; assign confidence 0.65-0.75
- This rule fires even on a WIDE CPR day when the above conditions are met

### RANGEBOUND DAY (CPR is WIDE) — only apply if no PDH/PDL breakout and no intraday override
- BUY  if: price ABOVE_CPR + RSI 45-65 + price above VWAP + sentiment not BEARISH + 1h/daily trend not BEARISH
- SELL if: price BELOW_CPR + RSI 35-55 + price below VWAP + sentiment not BULLISH + 1h/daily trend not BULLISH
- HOLD if: price INSIDE_CPR OR RSI > 65 OR RSI < 35 OR conflicting multi-timeframe signals

### TRENDING DAY (CPR is NARROW) — only apply if no PDH/PDL breakout and no intraday override
- BUY  if: price ABOVE_CPR + RSI 45-75 + price above VWAP + MACD not BEARISH
- SELL if: price BELOW_CPR + RSI 25-55 + price below VWAP + MACD not BULLISH
- HOLD if: price INSIDE_CPR OR RSI > 75 OR RSI < 25

### HISTORICAL S/R CONFLUENCE
Use the multi-year S/R zones above to adjust confidence — do not change the decision direction, only the conviction level.
- BUY signal near a SUPPORT or BOTH zone (within 0.5%): confidence +0.05 to +0.10 — zone has held before
- SELL signal near a RESISTANCE or BOTH zone (within 0.5%): confidence +0.05 to +0.10
- BUY signal approaching a RESISTANCE zone (within 0.5% above): confidence -0.05 — price may stall there
- SELL signal near a strong SUPPORT zone (within 0.5% below): confidence -0.05 — price may bounce
- "BOTH" zones (acted as both S and R historically): strongest confluences; add +0.10 when aligned, -0.05 when opposing
- The more tests (strength) a zone has, the more weight it carries — a 10-test zone outweighs a 2-test zone
- If no historical S/R data is available, ignore this section

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
    day_high: float,
    day_low: float,
    consolidation_pct: float,
    range_breakout: str,
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
    sr_levels_block: str = "",
    years_of_data: int = 5,
) -> str:
    cpr_type = "NARROW (trending day)" if cpr_width_pct < 0.25 else "WIDE (rangebound day)"
    consolidation_status = "SIDEWAYS" if consolidation_pct < 0.40 else "ACTIVE"
    if not historical_context_block:
        historical_context_block = (
            "## Historical Context\n"
            "No historical data available — operating on intraday data only."
        )
    if not sr_levels_block:
        sr_levels_block = "  No historical S/R data available yet — will populate after first bootstrap."
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
        day_high=day_high,
        day_low=day_low,
        consolidation_pct=consolidation_pct,
        consolidation_status=consolidation_status,
        range_breakout=range_breakout,
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
        sr_levels_block=sr_levels_block,
        years_of_data=years_of_data,
    )
