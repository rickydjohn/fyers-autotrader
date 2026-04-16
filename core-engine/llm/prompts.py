"""
Prompt templates for Ollama LLM inference.
Structured to produce deterministic JSON output.

v2: Extended with multi-timeframe historical context block.
v3: Added per-symbol options OI block (PCR, call/put wall, VIX, basis).
v4: Replaced PDH/PDL breakout override with three-layer framework + 12-session daily candle block.
"""

from typing import Any, Dict, List, Optional


def format_options_oi_block(oi: Optional[Dict[str, Any]]) -> str:
    """Format an options OI snapshot dict into a prompt-ready text block."""
    if not oi:
        return "  No options data available yet — skip this section."

    spot     = oi.get("spot", 0) or 0
    futures  = oi.get("futures", spot) or spot
    basis    = oi.get("basis", 0) or 0
    vix      = oi.get("vix", 0) or 0
    pcr      = oi.get("pcr", 0) or 0
    call_wall     = oi.get("call_wall", "N/A")
    call_wall_oi  = oi.get("call_wall_oi") or 0
    put_wall      = oi.get("put_wall", "N/A")
    put_wall_oi   = oi.get("put_wall_oi") or 0
    max_pain      = oi.get("max_pain", "N/A")
    expiry        = oi.get("expiry", "N/A")

    if basis > spot * 0.001:
        basis_signal = "bullish (contango)"
    elif basis < -(spot * 0.001):
        basis_signal = "bearish (backwardation)"
    else:
        basis_signal = "neutral"

    if vix > 20:
        vix_signal = "HIGH — widen stops to 0.5-0.7%"
    elif vix < 15:
        vix_signal = "LOW — tight range, tighten stops to 0.2-0.3%"
    else:
        vix_signal = "MODERATE"

    if pcr > 1.2:
        pcr_signal = "BULLISH bias (contrarian — panic put buying)"
    elif pcr < 0.8:
        pcr_signal = "BEARISH bias (contrarian — retail call buying)"
    else:
        pcr_signal = "NEUTRAL"

    return (
        f"  Spot: ₹{spot:.2f}  Futures: ₹{futures:.2f}  Basis: {basis:+.2f} ({basis_signal})\n"
        f"  India VIX: {vix:.2f} ({vix_signal})\n"
        f"  PCR: {pcr:.3f} ({pcr_signal})\n"
        f"  Call Wall: {call_wall} (OI: {call_wall_oi:,}) — resistance\n"
        f"  Put Wall:  {put_wall} (OI: {put_wall_oi:,}) — support\n"
        f"  Max Pain:  {max_pain}  |  Expiry: {expiry}"
    )


def compute_trading_gates(
    rsi: float,
    price: float,
    day_low: float,
    day_high: float,
    macd_signal: str,
    recent_candles: List[Dict[str, Any]],
) -> Dict[str, str]:
    """Pre-compute BUY gate, SELL gate, and volume reversal signal for the prompt.

    Returns a dict with keys: buy_gate, sell_gate, volume_signal.
    Values are human-readable strings injected directly into the prompt template.
    """
    buy_gate_parts: List[str] = []
    sell_gate_parts: List[str] = []

    # RSI hard stops
    if rsi < 45:
        buy_gate_parts.append(f"BLOCKED — RSI {rsi:.1f} < 45")
    if rsi > 55:
        sell_gate_parts.append(f"BLOCKED — RSI {rsi:.1f} > 55")
    if rsi > 78 or rsi < 20:
        buy_gate_parts.append(f"BLOCKED — RSI {rsi:.1f} extreme")
        sell_gate_parts.append(f"BLOCKED — RSI {rsi:.1f} extreme")

    buy_gate  = "; ".join(buy_gate_parts)  if buy_gate_parts  else "OPEN"
    sell_gate = "; ".join(sell_gate_parts) if sell_gate_parts else "OPEN"

    # Day's low proximity — informational only (not a hard block).
    # LLM reads candle structure to decide whether the low is defended (bounce) or breaking.
    day_low_dist_pct = ((price - day_low) / day_low * 100) if day_low > 0 else 99.0

    # Volume reversal signal
    volume_signal = "NONE"
    if len(recent_candles) >= 4:
        candles12 = recent_candles[-12:]

        # --- Bearish reversal near day's high: check full visible window ---
        if "BLOCKED" not in sell_gate:
            vols = [float(c.get("volume", 0) or 0) for c in candles12]
            for idx, c in enumerate(candles12):
                c_vol  = float(c.get("volume", 0) or 0)
                c_open = float(c.get("open",   0) or 0)
                c_close= float(c.get("close",  0) or 0)
                c_high = float(c.get("high",   0) or 0)
                other_vols = [v for i, v in enumerate(vols) if i != idx]
                avg_vol = sum(other_vols) / len(other_vols) if other_vols else 0
                if (avg_vol > 0 and c_vol >= 5 * avg_vol
                        and c_close < c_open
                        and day_high > 0 and (day_high - c_high) / day_high * 100 <= 1.5
                        and 20 <= rsi <= 55):
                    # Check LH+LL structure after this candle
                    after = candles12[idx + 1:] if idx + 1 < len(candles12) else []
                    lhll = len(after) == 0 or all(
                        after[i].get("high", 0) <= candles12[idx + i].get("high", 0)
                        for i in range(len(after))
                    )
                    multiplier = c_vol / avg_vol
                    time_str = str(c.get("time", ""))
                    h, m = 0, 0
                    if "T" in time_str:
                        t = time_str.split("T")[1][:5].split(":")
                        h, m = int(t[0]) + 5, int(t[1]) + 30
                        if m >= 60:
                            h += 1; m -= 60
                    label = f"{h:02d}:{m:02d}" if h else "??"
                    base_conf = 0.68 if macd_signal == "BULLISH" else 0.72
                    volume_signal = (
                        f"BEARISH_AT_HIGH — {label} candle {multiplier:.1f}× avg vol, "
                        f"bearish, near day's high; SELL confidence {base_conf:.2f}"
                    )
                    break  # first qualifying candle wins

        # --- Bullish reversal near day's low: check last 3 candles ---
        if volume_signal == "NONE" and "BLOCKED" not in buy_gate:
            last3  = recent_candles[-3:]
            prior9 = recent_candles[-12:-3] if len(recent_candles) >= 12 else recent_candles[:-3]
            avg9   = (sum(float(c.get("volume", 0) or 0) for c in prior9) / len(prior9)) if prior9 else 0
            for i, c in enumerate(last3):
                c_vol   = float(c.get("volume", 0) or 0)
                c_open  = float(c.get("open",   0) or 0)
                c_close = float(c.get("close",  0) or 0)
                if (avg9 > 0 and c_vol >= 5 * avg9
                        and c_close > c_open
                        and day_low > 0 and day_low_dist_pct <= 1.5
                        and 45 <= rsi <= 75):
                    confirmed = (
                        i + 1 < len(last3)
                        and float(last3[i + 1].get("close", 0)) > float(last3[i + 1].get("open", 0))
                    )
                    multiplier = c_vol / avg9
                    time_str = str(c.get("time", ""))
                    h, m = 0, 0
                    if "T" in time_str:
                        t = time_str.split("T")[1][:5].split(":")
                        h, m = int(t[0]) + 5, int(t[1]) + 30
                        if m >= 60:
                            h += 1; m -= 60
                    label = f"{h:02d}:{m:02d}" if h else "??"
                    base_conf = 0.76 if confirmed else 0.72
                    if macd_signal == "BEARISH":
                        base_conf -= 0.08
                    volume_signal = (
                        f"BULLISH_AT_LOW — {label} candle {multiplier:.1f}× avg vol, "
                        f"bullish, near day's low"
                        + (" + next candle confirmed" if confirmed else "")
                        + f"; BUY confidence {base_conf:.2f}"
                    )
                    break

    return {"buy_gate": buy_gate, "sell_gate": sell_gate, "volume_signal": volume_signal}


def format_daily_candles_for_prompt(candles: List[Dict[str, Any]]) -> str:
    """Format a list of daily OHLCV dicts into a prompt-readable block.

    Each row: '  2026-04-14 | ▲ O:24050 H:24320 L:23980 C:24280 | +1.2% | Vol:1.8B'
    Oldest first so the LLM reads left-to-right as a time series.
    """
    if not candles:
        return "  No daily data available."

    lines = []
    for c in candles[-12:]:   # at most 12 sessions
        date_str = str(c.get("time", c.get("timestamp", "???")))[:10]
        o     = float(c.get("open",   0) or 0)
        h     = float(c.get("high",   0) or 0)
        lo    = float(c.get("low",    0) or 0)
        close = float(c.get("close",  0) or 0)
        vol   = int(c.get("volume",   0) or 0)

        direction  = "▲" if close >= o else "▼"
        change_pct = ((close - o) / o * 100) if o > 0 else 0.0

        if vol >= 1_000_000_000:
            vol_str = f"{vol / 1_000_000_000:.1f}B"
        elif vol >= 1_000_000:
            vol_str = f"{vol / 1_000_000:.0f}M"
        elif vol >= 1_000:
            vol_str = f"{vol / 1_000:.0f}K"
        else:
            vol_str = str(vol)

        lines.append(
            f"  {date_str} | {direction} O:{o:.0f} H:{h:.0f} L:{lo:.0f} C:{close:.0f}"
            f" | {change_pct:+.1f}% | Vol:{vol_str}"
        )
    return "\n".join(lines)


DECISION_PROMPT_TEMPLATE = """{historical_context_block}

## Daily Chart Context (last 12 sessions)
Each row: date | direction O:open H:high L:low C:close | day-change% | volume
{daily_candle_block}

## Current Market Snapshot
Symbol: {symbol}
Current Price: ₹{price:.2f}
Time: {timestamp} IST

## Recent Price Action (last 12 candles)
{candle_block}

## Intraday Technical Indicators
CPR: BC=₹{bc:.2f}, TC=₹{tc:.2f}, Pivot=₹{pivot:.2f}
CPR Width: {cpr_width_pct:.2f}% ({cpr_type})
Price vs CPR: {cpr_signal}
Previous Day: High=₹{prev_day_high:.2f} Low=₹{prev_day_low:.2f}
Today's Range: High=₹{day_high:.2f} Low=₹{day_low:.2f}
Intraday Position: {day_low_dist_pct:.2f}% above day's low | {day_high_dist_pct:.2f}% below day's high
Consolidation: {consolidation_pct:.2f}% range over last 8 candles ({consolidation_status})
Range Breakout: {range_breakout}
Nearest Resistance: ₹{nearest_resistance:.2f} ({resistance_label})
Nearest Support: ₹{nearest_support:.2f} ({support_label})
RSI(14): {rsi:.1f}
EMA(9): ₹{ema_9:.2f} | EMA(21): ₹{ema_21:.2f}
MACD Signal: {macd_signal}
VWAP: ₹{vwap:.2f}

## Pre-Computed Trading Gates (Python-derived — treat as facts, not hints)
BUY Gate:  {buy_gate}
SELL Gate: {sell_gate}
Volume Signal: {volume_signal}

## Historical Support/Resistance (multi-year daily chart)
Zones where price has historically reversed — derived from {years_of_data} of daily swing data.
{sr_levels_block}

## Price Magnet Zones (unfilled gaps & unbreached CPRs)
{magnet_zones_block}

## Options Market Structure
{options_oi_block}

## News Sentiment (last 2 hours)
{news_summary}
Overall Sentiment: {sentiment_label} (score: {sentiment_score:.2f})

## Decision Rules
You are a disciplined intraday equity trader analyzing NSE Indian markets.

### STEP 0 — TRADING GATES (mandatory — check before any other rule)
Read the ## Pre-Computed Trading Gates block above. These are facts computed by Python, not suggestions.
- If BUY Gate says BLOCKED: you MUST NOT output BUY regardless of any indicator alignment.
- If SELL Gate says BLOCKED: you MUST NOT output SELL regardless of any indicator alignment.
- If Volume Signal is not NONE: use that direction and confidence as your starting point. Adjust by ±0.08 max for Layer 1 or Layer 3 factors. Do not override to HOLD unless a hard RSI stop applies.
- If all gates are OPEN and Volume Signal is NONE: proceed to Steps 1–2 and the three-layer framework.

### STEP 1 — PRICE ACTION READ (mandatory — complete before any rule)
Read the ## Recent Price Action candle block and answer all four questions. Capture answers in candle_summary. Your decision MUST be consistent with candle_summary — if they contradict, candle_summary overrides indicators.
1. BODIES: Are the last 3 candle bodies large (strong momentum) or small with large wicks (indecision/exhaustion)?
2. WICKS: Are rejection wicks forming at the nearest resistance or support? Long upper wicks at resistance = sellers active. Long lower wicks at support = buyers defending.
3. STRUCTURE: Are recent candles making higher highs + higher lows (bullish) or lower highs + lower lows (bearish)?
4. PATTERN: Is a candle pattern forming at a key level (CPR BC/TC, VWAP, a daily swing high/low, nearest S/R)? Name it or say "none".

### STEP 2 — DAILY CONTEXT READ (mandatory — complete before any rule)
Read the ## Daily Chart Context block and establish macro bias before applying any intraday rule.
1. TREND: Is the daily trend rising (HH+HL over last 5-10 sessions), falling (LH+LL), or sideways (range-bound)?
2. POSITION: Is today's price near the top, middle, or bottom of the 12-session range?
3. KEY LEVELS: Which daily swing highs and lows in the last 12 sessions are acting as natural resistance or support today? These are the levels that actually matter — not just yesterday's single high/low.
4. MOMENTUM: Are recent daily candle bodies growing (trend accelerating) or shrinking (exhaustion near a level)?

Note: Previous Day High/Low are the two most recent reference points, not the only ones. Use all 12 sessions of structure — swing highs, swing lows, gap zones, and multi-day consolidation ranges — to assess where meaningful supply and demand exist.

### CANDLE PATTERN SIGNALS (at CPR, VWAP, key daily levels, nearest S/R — evaluated BEFORE indicator rules)
BULLISH patterns (add +0.08 to BUY confidence, or flip HOLD → BUY if 2+ indicators already align):
- Hammer / Bullish Pin Bar: small body, lower wick ≥ 2× body size, at support/CPR BC/VWAP — buyers absorbing
- Bullish Engulfing: large green body fully covers prior red candle at support — momentum reversal
BEARISH patterns (add +0.08 to SELL confidence, or flip HOLD → SELL if 2+ indicators already align):
- Shooting Star / Bearish Pin Bar: small body, upper wick ≥ 2× body size, at resistance/CPR TC/key daily high — sellers active
- Bearish Engulfing: large red body fully covers prior green candle at resistance — momentum reversal
REJECTION at resistance (hard rule — overrides BUY signals):
- If the last 1–2 candles at/near nearest resistance or a key daily swing high show upper wicks larger than the candle body: output HOLD — price is being sold at that level, not accepted above it
- If upper wick ≥ 60% of total candle range at resistance: reduce BUY confidence by 0.10
EXHAUSTION (momentum dying — reduce confidence):
- Last 3 candle bodies progressively shrinking in the trend direction: momentum exhausting → reduce confidence by 0.05
- Last 3 candle bodies growing in the opposite direction of your signal: reversal building → reduce confidence by 0.08

### THREE-LAYER DECISION FRAMEWORK
Work through all three layers before assigning a decision. Each layer can confirm, weaken, or veto.

#### Layer 1 — Daily Context (from ## Daily Chart Context)
Sets the macro bias for the session.
- Rising trend (HH+HL structure over 5-10 sessions): BULLISH bias — favor BUY setups; SELL needs strong intraday confirmation
- Falling trend (LH+LL structure): BEARISH bias — favor SELL setups; BUY needs strong intraday confirmation
- Sideways range: NEUTRAL — both directions valid; require intraday and price action confirmation
- Price near the TOP of the 12-session range (within 0.3% of the 12-session high): reduce BUY confidence by 0.08; SELL setups more favorable
- Price near the BOTTOM of the 12-session range (within 0.3% of the 12-session low): reduce SELL confidence by 0.08; BUY setups more favorable
- Prior session was a large bearish candle (body > 1% range): BUY signal requires +1 additional intraday confirmation condition
- Prior session was a large bullish candle (body > 1% range): SELL signal requires +1 additional intraday confirmation condition

#### Layer 2 — Intraday Structure
Confirms or contradicts the daily bias.

⚡ VOLUME REVERSAL TRIGGERS — already evaluated in STEP 0 via the Volume Signal gate. This section explains the logic behind that pre-computation.
- BEARISH_AT_HIGH: a high-volume bearish candle occurred near the day's high with LH+LL follow-through → SELL.
- BULLISH_AT_LOW: a high-volume bullish candle occurred near the day's low, reversal confirmed → BUY.
- If Volume Signal is NONE, these triggers did not fire; continue with standard conditions below.

RSI HARD STOPS — apply before all other checks:
- RSI < 45: BUY is BLOCKED — output HOLD regardless of CPR/VWAP/EMA alignment
- RSI > 55: SELL is BLOCKED — output HOLD regardless of CPR/VWAP/EMA alignment
- RSI > 78 or RSI < 20: HOLD regardless of all signals
- Valid BUY range: RSI 45–75 (must be satisfied; not just "nice to have")
- Valid SELL range: RSI 20–55 (must be satisfied; not just "nice to have")

CPR relevance qualifier (apply before using ABOVE/BELOW_CPR as a confirmation):
- ABOVE_CPR only counts as a Layer 2 BUY confirmation when price is within 1.0% of CPR TC. If price is more than 1% above TC, the market has been above CPR for hours — it carries no fresh intraday information and must NOT be counted as a confirmation signal.
- BELOW_CPR only counts as a Layer 2 SELL confirmation when price is within 1.0% of CPR BC.
- When CPR is irrelevant (price too far away), replace it with: is price above or below VWAP by >0.5%? That becomes the structural anchor instead.

Intraday range position (apply before directional conditions):
- Intraday Position shows < 0.5% below day's high: reduce BUY confidence by 0.10; risk/reward is poor this close to the high.
- Day's low proximity (from Intraday Position line): if price is < 0.25% above the day's low AND the candle block shows lower wicks / buyers defending (hammer, bullish engulfing, BULLISH_AT_LOW), treat as support — reduce SELL confidence by 0.10. If candle block shows LH+LL with no lower-wick defense (clean bearish closes, no bounces), this is a breakdown — proceed with SELL normally.

Volume spike awareness (general — applies when reversal triggers above did not fire):
- Bearish spike (close < open) ≥ 5× avg in the last 3 candles: strong distribution — reduce BUY confidence by 0.10
- Bullish spike (close > open) ≥ 5× avg in the last 3 candles near day's low: reduce SELL confidence by 0.10 (potential reversal — check candle block for confirmation before committing to SELL)

Directional conditions:
- ABOVE_CPR (when within 1%) + price above VWAP + EMA9 > EMA21: intraday structure BULLISH — aligns with BUY, contradicts SELL
- BELOW_CPR (when within 1%) + price below VWAP + EMA9 < EMA21: intraday structure BEARISH — aligns with SELL, contradicts BUY
- INSIDE_CPR: no directional edge — HOLD unless Layer 1 and Layer 3 both strongly agree on direction
- MACD divergence: MACD BULLISH while signaling SELL → reduce SELL confidence by 0.08; MACD BEARISH while signaling BUY → reduce BUY confidence by 0.08. MACD lags price by 2–5 candles — do not let a lagging MACD override a fresh high-volume price signal; it is one of the 4 directional conditions, not a veto.
- Range Breakout = BREAKOUT_HIGH (consolidation_pct < 0.40%): high-probability BUY if above VWAP + RSI 45-75 + MACD not BEARISH; confidence 0.75-0.85
- Range Breakout = BREAKOUT_LOW: high-probability SELL if below VWAP + RSI 20-55 + MACD not BULLISH; confidence 0.75-0.85
- Range Breakout = NONE: no breakout setup — apply standard conditions above

#### Layer 3 — Price Action (from candle_summary)
Final confirmation or veto.
- HH+HL structure + large bodies + clean closes: +0.05 to BUY confidence
- LH+LL structure: BEARISH — this is a hard veto on BUY if RSI is also below 55; otherwise reduce BUY confidence by 0.08
- Rejection wicks at resistance (upper wicks > body at resistance): HOLD regardless of Layer 1/2 BUY signal
- Rejection wicks at support (lower wicks > body at support): HOLD regardless of Layer 1/2 SELL signal
- Candle pattern at key level: apply CANDLE PATTERN SIGNALS adjustments above

#### Minimum Conditions for BUY/SELL
- BUY: RSI 45–75 (hard requirement) + Layer 1 neutral/bullish + at least 3 of (ABOVE_CPR within 1%, above VWAP, EMA9>EMA21, MACD not BEARISH) + Layer 3 no rejection; confidence 0.70-0.85
  Volume reversal exception: if the bullish volume spike trigger above fires, the 3-condition gate is waived — confidence is set by that rule directly
- SELL: RSI 20–55 (hard requirement) + Layer 1 neutral/bearish + at least 3 of (BELOW_CPR within 1%, below VWAP, EMA9<EMA21, MACD not BULLISH) + Layer 3 no rejection; confidence 0.70-0.85
  Volume reversal exception: if the bearish volume spike near day's high trigger above fires, the 3-condition gate is waived — confidence is set by that rule directly
- HOLD: RSI outside valid range, or fewer than 3 Layer 2 conditions align (unless a volume reversal exception applies), or Layer 3 shows rejection/exhaustion, or Layer 1 directly contradicts
- Confidence 0.55–0.69 when exactly 2 conditions align; always output HOLD when fewer than 2 align

### HISTORICAL S/R CONFLUENCE (multi-year daily zones)
Use the multi-year S/R zones to adjust confidence — do not change the decision direction, only the conviction level.
- BUY signal near a SUPPORT or BOTH zone (within 0.5%): confidence +0.05 to +0.10 — zone has held before
- SELL signal near a RESISTANCE or BOTH zone (within 0.5%): confidence +0.05 to +0.10
- BUY signal approaching a RESISTANCE zone (within 0.5% above): confidence -0.05 — price may stall there
- SELL signal near a strong SUPPORT zone (within 0.5% below): confidence -0.05 — price may bounce
- "BOTH" zones (acted as both S and R historically): strongest confluences; add +0.10 when aligned, -0.05 when opposing
- The more tests (strength) a zone has, the more weight it carries — a 10-test zone outweighs a 2-test zone
- If no historical S/R data is available, ignore this section

### MONTHLY CPR/PIVOT CONFLUENCE (⚡ flagged in context)
Monthly pivots act as macro S/R — price often stalls or reverses at monthly levels.
When the context block shows ⚡ Confluence (daily and monthly level within 0.5%):
- These are the strongest S/R zones in the session — weight them above single-timeframe levels
- Confluence resistance above current price: confidence -0.10 for BUY (strong ceiling); use as primary target
- Confluence support below current price: confidence +0.10 for BUY at that zone; confidence -0.10 for SELL near it
- Monthly R3/S3 zones frequently act as the session high/low on extreme trend days — if price is at/near a monthly R3-R5 or S3-S5, treat it as a potential reversal zone and reduce position confidence by 0.05
- Even without ⚡ Confluence, monthly R1-R3 resistance above and S1-S3 support below should factor into stop-loss and target placement

### PRICE MAGNET ZONES (unfilled gaps and unbreached CPR zones)
Use the magnet zones block above to adjust confidence — do not change the decision direction.
Zones are classified BULLISH or BEARISH magnets based on which direction price needs to move to fill them.
- Zone labeled [BULLISH magnet]: near a DOWN gap or CPR below current price → price tends to RISE toward it
  - BUY signal aligned with a BULLISH magnet (HIGH pull): confidence +0.05 to +0.08
  - SELL signal opposing a BULLISH magnet (HIGH pull): confidence -0.03 to -0.05
- Zone labeled [BEARISH magnet]: near an UP gap or CPR above current price → price tends to DROP toward it
  - SELL signal aligned with a BEARISH magnet (HIGH pull): confidence +0.05 to +0.08
  - BUY signal opposing a BEARISH magnet (HIGH pull): confidence -0.03 to -0.05
- MODERATE pull zones: half the adjustment of HIGH pull zones
- LOW/NEGLIGIBLE pull zones: no adjustment
- Multiple aligned magnets (same direction): stack adjustments up to a cap of +0.12 total
- If no magnet zones listed above: skip this section entirely

### OPTIONS OI SIGNALS
Use the Options Market Structure block above to adjust confidence — do not change the decision direction, only conviction.
- Call Wall within 0.3% above price: BUY confidence -0.05 (strong ceiling ahead — calls are being written there)
- Put Wall within 0.3% below price: SELL confidence -0.05 (strong floor below — puts are being written there)
- PCR < 0.80: retail is aggressively buying calls → market makers are net short calls above; reduce BUY confidence -0.03
- PCR > 1.20: panic put buying → market makers defending below; reduce SELL confidence -0.03
- PCR 0.80–1.20: neutral — no adjustment
- VIX > 20: elevated volatility; widen stop_loss to 0.5–0.7% of entry regardless of day type
- VIX < 15: low volatility; tighten stop_loss to 0.2–0.3% of entry
- Basis positive (futures > spot by > 0.1%): bullish institutional positioning → +0.02 for BUY signals
- Basis negative (futures < spot by > 0.1%): bearish institutional positioning → +0.02 for SELL signals
- If no options data listed above: skip this section entirely

### ALL DAYS
- Set stop_loss 0.3-0.5% from entry (below entry for BUY, above entry for SELL)
- Target must give minimum 1.5:1 risk/reward ratio
- Confidence for BUY/SELL: 0.70-0.85 when 3+ conditions align; 0.55-0.69 when 2 conditions align; output HOLD instead of BUY/SELL when fewer than 2 conditions align
- Confidence for HOLD: always between 0.55-0.80 — reflects certainty in the hold call, never output 0.0
- Volume: if last 3 candles show declining volume on a directional move, momentum is weakening — reduce confidence by 0.05

Respond ONLY with a valid JSON object, no explanation outside the JSON:
{{
  "candle_summary": "Bodies:[large/small/mixed] Wicks:[rejection at resistance/support or clean closes] Structure:[HH+HL/LH+LL/sideways] Pattern:[name at level or none]",
  "decision": "BUY",
  "confidence": 0.80,
  "reasoning": "Single sentence citing candle_summary first, then daily trend/position (from 12-session chart), then intraday structure (CPR position, VWAP, RSI, MACD), and any key OI or level factor that influenced the decision.",
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
    day_type: str = "",
    magnet_zones_block: str = "",
    options_oi_block: str = "",
    candle_block: str = "",
    daily_candle_block: str = "",
    buy_gate: str = "OPEN",
    sell_gate: str = "OPEN",
    volume_signal: str = "NONE",
) -> str:
    if day_type == "NARROW":
        cpr_type = "NARROW (trending day)"
    elif day_type == "MODERATE":
        cpr_type = "MODERATE (mixed/weak trend)"
    elif day_type == "WIDE":
        cpr_type = "WIDE (rangebound day)"
    else:
        cpr_type = "NARROW (trending day)" if cpr_width_pct < 0.25 else "WIDE (rangebound day)"
    consolidation_status = "SIDEWAYS" if consolidation_pct < 0.40 else "ACTIVE"

    # Pre-compute intraday range position so LLM doesn't need to do the arithmetic
    day_low_dist_pct  = ((price - day_low)  / day_low  * 100) if day_low  > 0 else 0.0
    day_high_dist_pct = ((day_high - price) / day_high * 100) if day_high > 0 else 0.0

    if not historical_context_block:
        historical_context_block = (
            "## Historical Context\n"
            "No historical data available — operating on intraday data only."
        )
    if not sr_levels_block:
        sr_levels_block = "  No historical S/R data available yet — will populate after first bootstrap."
    if not magnet_zones_block:
        magnet_zones_block = "  No magnet zones identified — skip this section."
    if not options_oi_block:
        options_oi_block = "  No options data available yet — skip this section."
    if not candle_block:
        candle_block = "  No candle data available yet."
    if not daily_candle_block:
        daily_candle_block = "  No daily data available."

    # Build gate strings (already validated by caller; defaults are safe fallbacks)
    if not buy_gate:
        buy_gate = "OPEN"
    if not sell_gate:
        sell_gate = "OPEN"
    if not volume_signal:
        volume_signal = "NONE"

    return DECISION_PROMPT_TEMPLATE.format(
        historical_context_block=historical_context_block,
        daily_candle_block=daily_candle_block,
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
        day_low_dist_pct=day_low_dist_pct,
        day_high_dist_pct=day_high_dist_pct,
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
        magnet_zones_block=magnet_zones_block,
        options_oi_block=options_oi_block,
        candle_block=candle_block,
        buy_gate=buy_gate,
        sell_gate=sell_gate,
        volume_signal=volume_signal,
    )
