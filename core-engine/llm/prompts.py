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


def format_option_greeks_block(
    dte: int,
    delta: float = 0.0,
    gamma: float = 0.0,
    theta: float = 0.0,
    vega: float = 0.0,
    iv: float = 0.0,
) -> str:
    """Format DTE and option Greeks for prompt injection (used post-decision for context in logs)."""
    if dte == 0:
        expiry_label = "SAME-DAY (0DTE) — extreme gamma, any noise move = large P&L swing"
    elif dte <= 2:
        expiry_label = f"{dte}DTE — elevated gamma, tighter than normal P&L sensitivity"
    else:
        expiry_label = f"{dte} days to expiry"

    return (
        f"  Expiry:  {expiry_label}\n"
        f"  Delta:   {delta:+.3f}  |  Gamma: {gamma:.5f}  |  Theta: {theta:+.2f}/day\n"
        f"  Vega:    {vega:.3f}   |  IV:    {iv:.1f}%"
    )


def _aggregate_to_5m(candles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Aggregate 1m candle dicts to 5m bars aligned to 09:15 IST (03:45 UTC)."""
    SESSION_START_UTC_MIN = 3 * 60 + 45   # 03:45 UTC = 09:15 IST
    bars: dict = {}
    for c in candles:
        ts = str(c.get("time", ""))
        if len(ts) < 16 or "T" not in ts:
            continue
        h_utc = int(ts[11:13])
        m_utc = int(ts[14:16])
        total  = h_utc * 60 + m_utc
        offset = total - SESSION_START_UTC_MIN
        if offset < 0:
            continue
        bar_min = SESSION_START_UTC_MIN + (offset // 5) * 5
        o  = float(c.get("open",   0) or 0)
        h  = float(c.get("high",   0) or 0)
        l  = float(c.get("low",    0) or 0)
        cl = float(c.get("close",  0) or 0)
        v  = float(c.get("volume", 0) or 0)
        bh = bar_min // 60; bm = bar_min % 60
        key = f"{ts[:10]}T{bh:02d}:{bm:02d}:00"
        if key not in bars:
            bars[key] = {"time": key, "open": o, "high": h, "low": l, "close": cl, "volume": v}
        else:
            b = bars[key]
            b["high"]    = max(b["high"], h)
            b["low"]     = min(b["low"],  l)
            b["close"]   = cl
            b["volume"] += v
    return [bars[k] for k in sorted(bars.keys())]


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

    # Volume reversal signal — use 5m aggregated candles to avoid mid-bar noise
    # (minutes 2-3 of a 5m bar flip direction ~38% of the time)
    candles_5m = _aggregate_to_5m(recent_candles) if len(recent_candles) > 10 else recent_candles
    volume_signal = "NONE"
    if len(candles_5m) >= 4:
        candles12 = candles_5m[-12:]

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
                day_midpoint = (day_high + day_low) / 2 if day_high and day_low else day_high
                if (avg_vol > 0 and c_vol >= 5 * avg_vol
                        and c_close < c_open
                        and day_high > 0 and (day_high - c_high) / day_high * 100 <= 1.5
                        and price >= day_midpoint
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

        # --- Bullish reversal near day's low: check last 3 5m bars ---
        if volume_signal == "NONE" and "BLOCKED" not in buy_gate:
            last3  = candles_5m[-3:]
            prior9 = candles_5m[-12:-3] if len(candles_5m) >= 12 else candles_5m[:-3]
            avg9   = (sum(float(c.get("volume", 0) or 0) for c in prior9) / len(prior9)) if prior9 else 0
            for i, c in enumerate(last3):
                c_vol   = float(c.get("volume", 0) or 0)
                c_open  = float(c.get("open",   0) or 0)
                c_close = float(c.get("close",  0) or 0)
                c_body    = c_close - c_open
                c_wick_up = float(c.get("high", c_close) or c_close) - c_close
                if (avg9 > 0 and c_vol >= 5 * avg9
                        and c_close > c_open
                        and c_wick_up <= c_body          # reject shooting stars
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


def format_sector_breadth_block(breadth: Dict[str, Any]) -> str:
    """Format sector sub-index quotes into a prompt-ready breadth block.

    Args:
        breadth: {sector: {change_pct, ltp, weight, symbol}} from get_sector_breadth()

    Returns a multi-line string summarising sector direction, weighted
    contribution, and a net breadth signal for the LLM to use.
    """
    if not breadth:
        return "  No sector data available — skip this section."

    lines = []
    net_contribution = 0.0
    declining = 0
    advancing = 0
    total_weight = 0

    for sector, data in breadth.items():
        chp    = data.get("change_pct", 0.0)
        weight = data.get("weight", 0)
        contrib = chp * weight / 100          # contribution to index in %
        net_contribution += contrib
        total_weight += weight

        arrow = "▲" if chp > 0.05 else ("▼" if chp < -0.05 else "—")
        if chp < -0.05:
            declining += 1
        elif chp > 0.05:
            advancing += 1

        lines.append(
            f"  {arrow} {sector:<7} {chp:+.2f}%  "
            f"(wt ~{weight}%,  contribution {contrib:+.3f}%)"
        )

    total = advancing + declining
    if total_weight > 0:
        breadth_pct = advancing / (advancing + declining) * 100 if (advancing + declining) > 0 else 50

    # Net signal label
    if net_contribution <= -0.30:
        signal = f"STRONGLY BEARISH ({declining}/{len(breadth)} sectors declining)"
    elif net_contribution <= -0.10:
        signal = f"BEARISH ({declining}/{len(breadth)} sectors declining)"
    elif net_contribution >= 0.30:
        signal = f"STRONGLY BULLISH ({advancing}/{len(breadth)} sectors advancing)"
    elif net_contribution >= 0.10:
        signal = f"BULLISH ({advancing}/{len(breadth)} sectors advancing)"
    else:
        signal = f"MIXED/NEUTRAL ({advancing} up, {declining} down)"

    header = f"  ~{total_weight}% of NIFTY weight covered\n"
    footer = f"  Net weighted contribution: {net_contribution:+.3f}%  →  {signal}"
    return header + "\n".join(lines) + "\n" + footer


def compute_forming_bar_signal(
    forming_1m_candles: List[Dict[str, Any]],
    bar_position: int,
    volume_profile: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Analyse the current incomplete 5m bar and return a confidence delta + prompt block.

    Args:
        forming_1m_candles: 1m candles that belong to the current (unfinished) 5m bar.
        bar_position: Which minute we are in — 0 = first minute, 4 = fifth/last minute.
        volume_profile: List of {time_slot: "HH:MM:SS", avg_volume: int, sample_count: int}
                        from the volume_profile table (one entry per 5m slot per symbol).

    Returns:
        {
          "confidence_delta": float,   # -0.22 to +0.22
          "forming_bar_block": str,    # ready-to-inject LLM text
          "skip_llm": bool,            # True when bar_position < 2 (38% noise window)
        }
    """
    skip_llm = bar_position < 2

    if not forming_1m_candles:
        return {"confidence_delta": 0.0, "forming_bar_block": "", "skip_llm": skip_llm}

    # Aggregate the forming candles into one bar
    o  = float(forming_1m_candles[0].get("open",  0) or 0)
    hi = max(float(c.get("high",  0) or 0) for c in forming_1m_candles)
    lo = min(float(c.get("low",   0) or 0) for c in forming_1m_candles)
    cl = float(forming_1m_candles[-1].get("close", 0) or 0)
    current_vol = sum(float(c.get("volume", 0) or 0) for c in forming_1m_candles)

    body       = abs(cl - o)
    candle_range = hi - lo
    is_bull    = cl >= o
    wick_up    = hi - max(o, cl)
    wick_dn    = min(o, cl) - lo
    body_ratio = body / candle_range if candle_range > 0 else 0.0

    # Bar start time from first candle's timestamp
    ts = str(forming_1m_candles[0].get("time", ""))
    bar_time_label = "??"
    bar_time_slot  = None
    if "T" in ts:
        t_part = ts.split("T")[1][:5].split(":")
        try:
            h_utc, m_utc = int(t_part[0]), int(t_part[1])
            h_ist = (h_utc + 5) % 24
            m_ist = m_utc + 30
            if m_ist >= 60:
                h_ist += 1; m_ist -= 60
            bar_time_label = f"{h_ist:02d}:{m_ist:02d}"
            bar_time_slot  = f"{h_ist:02d}:{m_ist:02d}:00"
        except (ValueError, IndexError):
            pass

    # Volume context: pro-rate expected volume for minutes elapsed
    vp_entry     = next((s for s in volume_profile if s.get("time_slot") == bar_time_slot), None)
    expected_vol = 0.0
    volume_ratio = 0.0
    if vp_entry and vp_entry.get("avg_volume", 0) > 0:
        minutes_elapsed = bar_position + 1  # 1 through 5
        expected_vol    = vp_entry["avg_volume"] * minutes_elapsed / 5
        volume_ratio    = current_vol / expected_vol if expected_vol > 0 else 0.0

    # Volume note
    if expected_vol == 0:
        vol_note = "no historical norm available"
    elif volume_ratio < 0.30:
        vol_note = f"ultra-low volume ({volume_ratio:.1f}× norm) — no signal"
    elif volume_ratio < 0.50:
        vol_note = f"below-avg volume ({volume_ratio:.1f}× norm)"
    elif volume_ratio > 2.5:
        vol_note = f"surge volume ({volume_ratio:.1f}× norm)"
    elif volume_ratio > 1.5:
        vol_note = f"strong volume ({volume_ratio:.1f}× norm)"
    else:
        vol_note = f"avg volume ({volume_ratio:.1f}× norm)"

    # Body classification
    if body_ratio < 0.15:
        body_label = "Doji (indecision)"
    elif body_ratio < 0.40:
        body_label = "Weak bullish" if is_bull else "Weak bearish"
    elif body_ratio < 0.65:
        body_label = "Bullish" if is_bull else "Bearish"
    else:
        body_label = "Strong bullish" if is_bull else "Strong bearish"

    # Base confidence delta from body strength + volume
    if body_ratio < 0.15:                              # doji
        confidence_delta = -0.05
        reason = "indecision (doji)"
    elif body_ratio < 0.40:                            # weak
        confidence_delta = +0.03 if volume_ratio >= 1.0 else -0.05
        reason = "weak body with avg/low volume"
    elif body_ratio < 0.65:                            # moderate
        confidence_delta = +0.07 if volume_ratio >= 1.0 else +0.02
        reason = "moderate body"
    else:                                              # strong
        confidence_delta = +0.10 if volume_ratio >= 1.0 else +0.05
        reason = "strong body"

    # Volume extremes adjust delta
    if volume_ratio >= 2.5 and body_ratio >= 0.40:
        confidence_delta += 0.06
        reason += " + volume surge"
    elif volume_ratio < 0.30 and expected_vol > 0:
        confidence_delta = 0.0
        reason = "ultra-low volume — signal suppressed"

    # Late reversal penalty at minutes 4-5 (bar_position 3-4).
    # Only penalise heavily if the reversal candle has real body (>20% of bar range).
    # A 2-point tick against a 20-point bar is noise, not a reversal — don't kill the signal.
    if bar_position >= 3 and len(forming_1m_candles) >= 2:
        last_1m      = forming_1m_candles[-1]
        last_1m_cl   = float(last_1m.get("close", 0))
        last_1m_op   = float(last_1m.get("open",  0))
        last_1m_bull = last_1m_cl >= last_1m_op
        if last_1m_bull != is_bull:
            reversal_ratio = abs(last_1m_cl - last_1m_op) / candle_range if candle_range > 0 else 0.0
            if reversal_ratio >= 0.20:
                confidence_delta -= 0.15
                reason += f" | significant late reversal ({reversal_ratio:.0%}, -0.15)"
            else:
                confidence_delta -= 0.05
                reason += f" | minor late reversal ({reversal_ratio:.0%}, -0.05)"

    # Confirmation boost at minutes 4-5 if body + volume both confirm
    elif bar_position >= 3 and volume_ratio >= 1.5 and body_ratio >= 0.40:
        confidence_delta += 0.08
        reason += " | late confirmation (+0.08)"

    # Clamp
    confidence_delta = max(-0.22, min(0.22, round(confidence_delta, 3)))

    direction_arrow = "▲" if is_bull else "▼"
    forming_bar_block = (
        f"## Forming 5m Bar — {bar_time_label} IST ({bar_position + 1}/5 minutes elapsed)\n"
        f"  {direction_arrow} O:{o:.0f} H:{hi:.0f} L:{lo:.0f} C:{cl:.0f}\n"
        f"  Body: {body_label} ({body_ratio:.0%})  "
        f"Upper wick: {wick_up:.0f} pt  Lower wick: {wick_dn:.0f} pt\n"
        f"  Volume so far: {int(current_vol / 1000)}K"
        + (f"  (expected ~{int(expected_vol / 1000)}K at this point — {vol_note})" if expected_vol > 0 else f"  ({vol_note})")
        + f"\n  Confidence delta: {confidence_delta:+.3f} — {reason}\n\n"
        "  Apply this delta to your base confidence before outputting. "
        "This bar reflects the freshest price action — if it contradicts your Step 1 "
        "candle analysis, weight the forming bar higher."
    )

    return {
        "confidence_delta": confidence_delta,
        "forming_bar_block": forming_bar_block,
        "skip_llm": skip_llm,
        "forming_bar_is_bull": is_bull,
    }


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

{forming_bar_block}

## Historical Support/Resistance (multi-year daily chart)
Zones where price has historically reversed — derived from {years_of_data} of daily swing data.
{sr_levels_block}

## Price Magnet Zones (unfilled gaps & unbreached CPRs)
{magnet_zones_block}

## Options Market Structure
{options_oi_block}

## Sector Momentum (NSE Sub-Indices)
{sector_breadth_block}

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
- If a ## Forming 5m Bar block is present: read the confidence_delta value and apply it to your final confidence. A negative delta means the forming bar is working against your direction — treat it seriously.
  **Exception — bullish bar arriving at resistance:** If the forming bar delta is positive (bullish) AND the completed candle structure (Step 1) shows LH+LL AND price is within 0.3% of a known resistance (CPR TC, VWAP, R1, PDH, daily swing high): the bullish bar is delivering price to the resistance level, not breaking through it. This is setup completion for a SELL, not a contradiction. In this case treat the positive delta as 0.00 for SELL purposes — do not let it reduce SELL confidence. A bullish forming bar that terminates at resistance with a bearish macro structure is the trigger arriving, not a reversal signal.

### LEVEL BREAKTHROUGH CONTINUATION — HIGH PRIORITY (evaluate after STEP 0, before Steps 1–2)

Three high-probability setups that bypass the standard 3-condition Layer 2 gate. Check Step 1 candle structure to confirm, then output the decision directly — you do not need to complete all of Steps 1–2.

#### Setup A — Broken Support → SELL continuation
**Trigger** — all four must be true:
1. `nearest_resistance` label is an S-level (S1, S2, etc.) — a pivot support that price has broken below and now acts as overhead resistance
2. RSI in valid SELL range (20–55) — hard requirement
3. Step 1 candle structure shows LH+LL — lower highs, lower lows, bearish momentum continuing
4. `prev_day_low` (PDL) is below current price — a named target exists below

**Decision**: SELL. Confidence 0.72–0.80. Target = `prev_day_low`. Stop = `nearest_resistance` + 0.15%.

The broken S-level is now overhead resistance and your invalidation level. PDL is the natural floor target. You are in a momentum continuation trade — the breakdown happened; enter it.

**⚠ Do NOT output HOLD because price is "between levels."** Being between a broken support (now resistance) and the next support below IS the continuation trade zone for a breakdown. HOLD between levels = missed trade.

**Day's low proximity exception during breakdowns**: `day_low` tracks the session low and falls with price during a downtrend — it is always close to current price in a decline. When LH+LL structure is present with clean bearish closes (no significant lower-wick defense), do NOT apply the -0.10 day's-low proximity penalty. A falling `day_low` trailing the breakdown is not a static support floor.

#### Setup B — Level Bounce → BUY reversal
**Trigger** — all three must be true:
1. Current price is within 0.5% above `prev_day_low` or `nearest_support` — the level is being tested from above
2. Step 1 candle structure shows active defense: lower wick ≥ 2× body (hammer / pin bar), bullish engulfing, or a BULLISH_AT_LOW volume signal
3. RSI ≥ 30 — price at a major support after a sustained decline will often be oversold; the normal RSI ≥ 45 floor is relaxed here to RSI ≥ 30

**Decision**: BUY. Confidence 0.70–0.78. Target = first named resistance above (the previously broken S-level now acts as target). Stop = `prev_day_low` − 0.2%.

A bullish candle at PDL after a sustained intraday decline is one of the highest-probability reversal signals of the session. If the candle structure shows lower-wick defense at this level, output BUY — do not output HOLD at PDL.

#### Setup C — Broken Resistance → BUY continuation
**Trigger** — mirror of Setup A for breakouts:
1. `nearest_support` label is an R-level (R1, R2, etc.) — a pivot resistance that price has broken above and now acts as floor support
2. RSI in valid BUY range (45–75)
3. Step 1 candle structure shows HH+HL — bullish continuation

**Decision**: BUY. Confidence 0.72–0.80. Target = next named resistance above. Stop = `nearest_support` − 0.15%.

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
- Strong bearish close at a key level (CPR TC, VWAP, daily swing high, R1): body ≥ 60% of candle range, closing near the low of the bar, at a recognised resistance level — sellers committing, not just probing. This is a momentum confirmation candle; treat it as a high-conviction SELL signal even if no prior candle is engulfed.
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
- TC and BC are not symmetric walls — they are the two edges of a decision zone. TC is the resistance ceiling (sellers defend above it); BC is the support floor (buyers defend below it). Price inside the band is testing one of those edges, not sitting in neutral space.

Intraday range position (apply before directional conditions):
- Intraday Position shows < 0.5% below day's high: reduce BUY confidence by 0.10; risk/reward is poor this close to the high.
- Day's low proximity (from Intraday Position line): if price is < 0.25% above the day's low AND the candle block shows lower wicks / buyers defending (hammer, bullish engulfing, BULLISH_AT_LOW), treat as support — reduce SELL confidence by 0.10. If candle block shows LH+LL with no lower-wick defense (clean bearish closes, no bounces), this is a breakdown — proceed with SELL normally.

Volume spike awareness (general — applies when reversal triggers above did not fire):
- Bearish spike (close < open) ≥ 5× avg in the last 3 candles: strong distribution — reduce BUY confidence by 0.10
- Bullish spike (close > open) ≥ 5× avg in the last 3 candles near day's low: reduce SELL confidence by 0.10 (potential reversal — check candle block for confirmation before committing to SELL)

Directional conditions:
- ABOVE_CPR (when within 1%) + price above VWAP + EMA9 > EMA21: intraday structure BULLISH — aligns with BUY, contradicts SELL
- BELOW_CPR (when within 1%) + price below VWAP + EMA9 < EMA21: intraday structure BEARISH — aligns with SELL, contradicts BUY
- INSIDE_CPR — read position within the band, not just "inside":
  - Price near TC (within 0.2% of TC) with a strong bearish close or LH+LL structure: TC is acting as resistance. Count this as a SELL confirmation equivalent to BELOW_CPR. The candle is telling you sellers are defending TC — that is the signal.
  - Price near BC (within 0.2% of BC) with a strong bullish close or HH+HL structure: BC is acting as support. Count this as a BUY confirmation equivalent to ABOVE_CPR.
  - Price mid-band (not near TC or BC): genuinely no directional edge — require Layer 1 and Layer 3 to strongly agree before committing.
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

### SECTOR BREADTH (from ## Sector Momentum block)
Use the net weighted contribution and sector breakdown to adjust confidence. Do not change decision direction — only conviction.
- Net contribution ≤ −0.20% (strongly bearish): SELL confidence +0.05; BUY confidence −0.05
- Net contribution ≥ +0.20% (strongly bullish): BUY confidence +0.05; SELL confidence −0.05
- Net contribution between −0.10% and +0.10%: no adjustment
- BANK sector alone: if BANK change ≤ −0.5% and you are trading BANKNIFTY → additional SELL confidence +0.03; if BANK ≥ +0.5% → additional BUY confidence +0.03
- All 5+ sectors in same direction (breadth sweep): double the net contribution adjustment (cap total sector adjustment at +0.10)
- If no sector data listed: skip this section entirely
- In your reasoning, name the specific sectors driving the signal (e.g. "IT −3.9%, AUTO −1.2% dragging NIFTY"), not just "sector breadth". Only mention sectors with |change| > 0.5%.

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
    forming_bar_block: str = "",
    sector_breadth_block: str = "",
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
    if not forming_bar_block:
        forming_bar_block = ""
    if not sector_breadth_block:
        sector_breadth_block = "  No sector data available — skip this section."

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
        forming_bar_block=forming_bar_block,
        sector_breadth_block=sector_breadth_block,
    )
