"""
End-to-end test for the LLM decision logic.
Sends real prompts to Ollama and validates responses through _validate_decision.

Run from project root:
    python test_decision_logic.py

Ollama must be running on localhost:11434.
"""

import asyncio
import json
import re
import sys
from dataclasses import dataclass
from typing import Optional

import httpx

sys.path.insert(0, "core-engine")
from llm.prompts import build_decision_prompt

OLLAMA_URL = "http://host.docker.internal:11434/api/generate"
OLLAMA_MODEL = "gpt-oss:120b-cloud"

# ── Inline copies of parse + validate (avoids Redis/Fyers/config imports) ─────

def _parse_llm_response(raw: str) -> Optional[dict]:
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
    return None


def _validate_decision(data: dict, price: float) -> dict:
    decision = data.get("decision", "HOLD").upper()
    if decision not in ("BUY", "SELL", "HOLD"):
        decision = "HOLD"
    confidence = float(data.get("confidence", 0.5))
    confidence = max(0.0, min(1.0, confidence))
    if confidence < 0.5:
        decision = "HOLD"
    stop_loss = float(data.get("stop_loss", 0.0))
    target    = float(data.get("target", 0.0))
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
    risk        = abs(price - stop_loss)
    reward      = abs(target - price)
    risk_reward = round(reward / risk, 2) if risk > 0 else 0.0
    return {
        "decision":    decision,
        "confidence":  confidence,
        "reasoning":   str(data.get("reasoning", ""))[:500],
        "stop_loss":   stop_loss,
        "target":      target,
        "risk_reward": risk_reward,
    }


async def query_ollama(prompt: str) -> Optional[str]:
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1, "top_p": 0.9, "num_predict": 2048},
    }
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(OLLAMA_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
            text = data.get("response", "").strip()
            if not text:
                print(f"  [debug] Ollama response keys: {list(data.keys())}")
                print(f"  [debug] Full response: {str(data)[:300]}")
            return text
    except Exception as e:
        print(f"  [debug] Ollama request failed: {e}")
        return None


# ── Test case definitions ──────────────────────────────────────────────────────

@dataclass
class Case:
    name: str
    expected: str          # "BUY", "SELL", or "HOLD"
    # snapshot params
    price: float
    cpr_width_pct: float   # < 0.25 → NARROW, >= 0.25 → WIDE
    cpr_signal: str        # ABOVE_CPR / BELOW_CPR / INSIDE_CPR
    prev_day_high: float
    prev_day_low: float
    rsi: float
    vwap: float
    macd_signal: str       # BULLISH / BEARISH / NEUTRAL
    ema_9: float
    ema_21: float
    nearest_resistance: float
    resistance_label: str
    nearest_support: float
    support_label: str
    bc: float
    tc: float
    pivot: float
    sentiment_label: str = "NEUTRAL"
    sentiment_score: float = 0.0
    news_summary: str = "No significant news."
    historical_context_block: str = ""


NIFTY_BASE = 22500.0

CASES = [
    # ── The exact failing scenario: 256-pt trending day move ──────────────────
    Case(
        name         = "FAIL_SCENARIO: Trending day, RSI 70, above CPR+PDH+VWAP",
        expected     = "BUY",
        price        = NIFTY_BASE + 256,         # 22756 — 256 pts above open
        cpr_width_pct= 0.12,                     # NARROW → trending day
        cpr_signal   = "ABOVE_CPR",
        prev_day_high= NIFTY_BASE + 120,         # 22620 — price has broken above PDH
        prev_day_low = NIFTY_BASE - 180,         # 22320
        rsi          = 70.0,                     # was hitting HOLD with old rule
        vwap         = NIFTY_BASE + 200,         # price above VWAP
        macd_signal  = "BULLISH",
        ema_9        = NIFTY_BASE + 220,
        ema_21       = NIFTY_BASE + 150,
        nearest_resistance = NIFTY_BASE + 300,
        resistance_label   = "R1",
        nearest_support    = NIFTY_BASE + 120,
        support_label      = "PDH",
        bc           = NIFTY_BASE - 30,
        tc           = NIFTY_BASE + 10,
        pivot        = NIFTY_BASE - 10,
        sentiment_label = "BULLISH",
        sentiment_score = 0.6,
    ),
    # ── Trending day, RSI 72 (also previously blocked) ────────────────────────
    Case(
        name         = "Trending day, RSI 72, above CPR+PDH+VWAP",
        expected     = "BUY",
        price        = NIFTY_BASE + 280,
        cpr_width_pct= 0.15,
        cpr_signal   = "ABOVE_CPR",
        prev_day_high= NIFTY_BASE + 100,
        prev_day_low = NIFTY_BASE - 200,
        rsi          = 72.0,
        vwap         = NIFTY_BASE + 210,
        macd_signal  = "BULLISH",
        ema_9        = NIFTY_BASE + 240,
        ema_21       = NIFTY_BASE + 160,
        nearest_resistance = NIFTY_BASE + 350,
        resistance_label   = "R2",
        nearest_support    = NIFTY_BASE + 100,
        support_label      = "PDH",
        bc           = NIFTY_BASE - 20,
        tc           = NIFTY_BASE + 15,
        pivot        = NIFTY_BASE,
    ),
    # ── Trending day, RSI 76 — should HOLD (above new 75 cap) ─────────────────
    Case(
        name         = "Trending day, RSI 76 — expect HOLD (overbought)",
        expected     = "HOLD",
        price        = NIFTY_BASE + 320,
        cpr_width_pct= 0.10,
        cpr_signal   = "ABOVE_CPR",
        prev_day_high= NIFTY_BASE + 100,
        prev_day_low = NIFTY_BASE - 200,
        rsi          = 76.0,
        vwap         = NIFTY_BASE + 230,
        macd_signal  = "BULLISH",
        ema_9        = NIFTY_BASE + 280,
        ema_21       = NIFTY_BASE + 190,
        nearest_resistance = NIFTY_BASE + 400,
        resistance_label   = "R2",
        nearest_support    = NIFTY_BASE + 100,
        support_label      = "PDH",
        bc           = NIFTY_BASE - 20,
        tc           = NIFTY_BASE + 15,
        pivot        = NIFTY_BASE,
    ),
    # ── Rangebound day, RSI 68 — should HOLD (> 65 cap for WIDE CPR) ──────────
    Case(
        name         = "Rangebound day, RSI 68 — expect HOLD (> rangebound cap)",
        expected     = "HOLD",
        price        = NIFTY_BASE + 80,
        cpr_width_pct= 0.60,                     # WIDE → rangebound day
        cpr_signal   = "ABOVE_CPR",
        prev_day_high= NIFTY_BASE + 200,
        prev_day_low = NIFTY_BASE - 200,
        rsi          = 68.0,
        vwap         = NIFTY_BASE + 50,
        macd_signal  = "NEUTRAL",
        ema_9        = NIFTY_BASE + 60,
        ema_21       = NIFTY_BASE + 30,
        nearest_resistance = NIFTY_BASE + 200,
        resistance_label   = "PDH",
        nearest_support    = NIFTY_BASE,
        support_label      = "Pivot",
        bc           = NIFTY_BASE - 80,
        tc           = NIFTY_BASE + 80,
        pivot        = NIFTY_BASE,
    ),
    # ── Rangebound day, RSI 55 — should BUY ───────────────────────────────────
    Case(
        name         = "Rangebound day, RSI 55, above CPR+VWAP — expect BUY",
        expected     = "BUY",
        price        = NIFTY_BASE + 100,
        cpr_width_pct= 0.55,
        cpr_signal   = "ABOVE_CPR",
        prev_day_high= NIFTY_BASE + 300,
        prev_day_low = NIFTY_BASE - 200,
        rsi          = 55.0,
        vwap         = NIFTY_BASE + 70,
        macd_signal  = "BULLISH",
        ema_9        = NIFTY_BASE + 90,
        ema_21       = NIFTY_BASE + 40,
        nearest_resistance = NIFTY_BASE + 300,
        resistance_label   = "PDH",
        nearest_support    = NIFTY_BASE,
        support_label      = "Pivot",
        bc           = NIFTY_BASE - 80,
        tc           = NIFTY_BASE + 80,
        pivot        = NIFTY_BASE,
        sentiment_label = "BULLISH",
        sentiment_score = 0.4,
    ),
    # ── Price inside CPR — should HOLD regardless ─────────────────────────────
    Case(
        name         = "Price inside CPR — expect HOLD",
        expected     = "HOLD",
        price        = NIFTY_BASE + 5,           # inside BC/TC band
        cpr_width_pct= 0.30,
        cpr_signal   = "INSIDE_CPR",
        prev_day_high= NIFTY_BASE + 200,
        prev_day_low = NIFTY_BASE - 200,
        rsi          = 52.0,
        vwap         = NIFTY_BASE + 10,
        macd_signal  = "NEUTRAL",
        ema_9        = NIFTY_BASE + 8,
        ema_21       = NIFTY_BASE - 5,
        nearest_resistance = NIFTY_BASE + 200,
        resistance_label   = "PDH",
        nearest_support    = NIFTY_BASE - 200,
        support_label      = "PDL",
        bc           = NIFTY_BASE - 20,
        tc           = NIFTY_BASE + 20,
        pivot        = NIFTY_BASE,
    ),
    # ── TODAY'S ACTUAL SCENARIO: Wide CPR + PDH breakout + RSI 72 ────────────
    Case(
        name         = "TODAY: Wide CPR (0.68%), price 207pts above PDH, RSI 72 — expect BUY",
        expected     = "BUY",
        price        = 22989.5,
        cpr_width_pct= 0.6817,                   # WIDE → rangebound classification
        cpr_signal   = "ABOVE_CPR",
        prev_day_high= 22782.30,                 # price is 207 pts above PDH
        prev_day_low = 22182.55,
        rsi          = 71.94,                    # was blocked by rangebound 65 cap
        vwap         = 22702.16,                 # price well above VWAP
        macd_signal  = "BULLISH",
        ema_9        = 22942.23,
        ema_21       = 22901.02,
        nearest_resistance = 23159.07,
        resistance_label   = "R2",
        nearest_support    = 22782.30,
        support_label      = "PDH",
        bc           = 22482.42,
        tc           = 22636.21,
        pivot        = 22559.32,
        sentiment_label = "BULLISH",
        sentiment_score = 0.2,
    ),
    # ── Trending day PDL breakdown — expect SELL ──────────────────────────────
    Case(
        name         = "Trending day PDL breakdown, RSI 40, below VWAP — expect SELL",
        expected     = "SELL",
        price        = NIFTY_BASE - 250,         # 22250 — broken below PDL
        cpr_width_pct= 0.13,
        cpr_signal   = "BELOW_CPR",
        prev_day_high= NIFTY_BASE + 150,
        prev_day_low = NIFTY_BASE - 120,         # 22380 — price has broken below PDL
        rsi          = 40.0,
        vwap         = NIFTY_BASE - 180,
        macd_signal  = "BEARISH",
        ema_9        = NIFTY_BASE - 200,
        ema_21       = NIFTY_BASE - 130,
        nearest_resistance = NIFTY_BASE - 120,
        resistance_label   = "PDL",
        nearest_support    = NIFTY_BASE - 350,
        support_label      = "S1",
        bc           = NIFTY_BASE + 10,
        tc           = NIFTY_BASE + 40,
        pivot        = NIFTY_BASE + 20,
        sentiment_label = "BEARISH",
        sentiment_score = -0.5,
    ),
]


# ── Runner ─────────────────────────────────────────────────────────────────────

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
WARN = "\033[93mWARN\033[0m"


async def run_case(case: Case, idx: int, total: int) -> bool:
    print(f"\n[{idx}/{total}] {case.name}")
    print(f"  Expected : {case.expected}")

    prompt = build_decision_prompt(
        symbol             = "NSE:NIFTY50-INDEX",
        price              = case.price,
        timestamp          = "2026-04-06 11:30",
        bc                 = case.bc,
        tc                 = case.tc,
        pivot              = case.pivot,
        cpr_width_pct      = case.cpr_width_pct,
        cpr_signal         = case.cpr_signal,
        prev_day_high      = case.prev_day_high,
        prev_day_low       = case.prev_day_low,
        nearest_resistance = case.nearest_resistance,
        resistance_label   = case.resistance_label,
        nearest_support    = case.nearest_support,
        support_label      = case.support_label,
        rsi                = case.rsi,
        ema_9              = case.ema_9,
        ema_21             = case.ema_21,
        macd_signal        = case.macd_signal,
        vwap               = case.vwap,
        news_summary       = case.news_summary,
        sentiment_label    = case.sentiment_label,
        sentiment_score    = case.sentiment_score,
        historical_context_block = case.historical_context_block,
    )

    raw = await query_ollama(prompt)
    if not raw:
        print(f"  Result   : {FAIL} — Ollama returned no response")
        return False

    parsed = _parse_llm_response(raw)
    if not parsed:
        print(f"  Result   : {FAIL} — could not parse JSON from: {raw[:120]}")
        return False

    validated = _validate_decision(parsed, case.price)
    decision   = validated["decision"]
    confidence = validated["confidence"]
    reasoning  = validated["reasoning"]

    passed = decision == case.expected
    status = PASS if passed else FAIL

    # Warn if confidence is near the 0.5 threshold (could be fragile)
    if passed and decision in ("BUY", "SELL") and confidence < 0.65:
        status = f"{PASS} ({WARN} low confidence={confidence:.2f})"

    print(f"  Got      : {decision}  confidence={confidence:.2f}  rr={validated['risk_reward']}")
    print(f"  Reasoning: {reasoning}")
    print(f"  Result   : {status}")
    return passed


async def main():
    print("=" * 70)
    print(f"Decision Logic Test  |  Model: {OLLAMA_MODEL}")
    print("=" * 70)

    results = []
    for i, case in enumerate(CASES, 1):
        passed = await run_case(case, i, len(CASES))
        results.append((case.name, passed))

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    passed_n = sum(1 for _, p in results if p)
    for name, passed in results:
        icon = "✓" if passed else "✗"
        print(f"  {icon}  {name}")
    print(f"\n{passed_n}/{len(results)} passed")
    print("=" * 70)

    sys.exit(0 if passed_n == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
