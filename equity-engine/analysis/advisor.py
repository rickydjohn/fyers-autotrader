"""
Holdings-aware, LLM-assisted entry/exit advisor (ADVISORY ONLY).

For each holding and each momentum candidate:
  1. Deterministic layer computes indicators + explicit resistance/support levels.
  2. The LLM is handed those concrete numbers and asked for a structured, grounded
     call — judged on TECHNICALS ONLY (it never sees the cost basis).
  3. For holdings we attach the P&L context + a loss-cut flag SEPARATELY (deterministic),
     so the card shows the technical call and the money view side by side.

The LLM explains the case from real levels; it is not trusted to find edge. The user
decides — nothing here places an order.
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

import httpx
import pytz

from config import settings
from data import CandleProvider
from features import build_features
from features.levels import support_resistance_levels
from models import Bar, EquitySymbol
from analysis.llm import complete, parse_json

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

LOOKBACK, SKIP = 252, 21
LIQUID_FILE = "/app/cache/liquid180.txt"   # persisted tradeable universe for candidates


def fetch_holdings() -> list[dict]:
    """Existing stock holdings from the Fyers account, via core-engine."""
    try:
        r = httpx.get(f"{settings.core_engine_url}/fyers/holdings", timeout=20.0)
        r.raise_for_status()
        return r.json().get("holdings", []) or []
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("holdings fetch failed: %s", e)
        return []


def _context(symbol: str, bars: list[Bar]):
    """Deterministic indicators + levels block shared by both prompt types."""
    f = build_features(symbol, bars)
    if f is None:
        return None
    supports, resistances = support_resistance_levels(bars, f.ltp)
    hi52 = max(b.high for b in bars[-250:])
    mom = (bars[-1 - SKIP].close / bars[-1 - SKIP - LOOKBACK].close - 1.0) * 100 \
        if len(bars) > LOOKBACK + SKIP else 0.0
    return f, supports, resistances, hi52, mom


def _levels_text(f, supports, resistances, hi52):
    cpr = f.monthly_cpr
    return (
        f"Trend {f.regime.value} | RSI {f.rsi:.0f} | MACD hist {f.macd_histogram:+.2f} | "
        f"ATR {f.atr_pct:.1f}%\n"
        f"{f.pct_from_52w_high:+.0f}% from 52w high (₹{hi52:.1f}) | "
        f"Monthly CPR {cpr.position} (pivot ₹{cpr.pivot}, top ₹{cpr.tc}, bottom ₹{cpr.bc})\n"
        f"Resistance above: {resistances or 'none (at/near highs)'}\n"
        f"Support below: {supports or 'none'}"
    )


def analyze_symbol(symbol: str, bars: list[Bar], kind: str, holding: Optional[dict] = None) -> Optional[dict]:
    ctx = _context(symbol, bars)
    if ctx is None:
        return None
    f, supports, resistances, hi52, mom = ctx
    levels = _levels_text(f, supports, resistances, hi52)

    if kind == "holding":
        prompt = (
            "You are a disciplined equity technical analyst. Judge ONLY on the technicals "
            "and levels below. Do NOT consider any cost basis — the market doesn't know it.\n\n"
            f"STOCK {symbol} — spot ₹{f.ltp} | 12-month momentum {mom:+.0f}%\n{levels}\n\n"
            'Respond with ONLY a JSON object, no prose:\n'
            '{"action":"HOLD|ADD|TRIM|EXIT","conviction":"high|medium|low",'
            '"key_resistance":<number>,"stop":<number>,"target":<number>,'
            '"reasons":"<2-3 sentences citing the specific levels/indicators above>"}'
        )
    else:
        prompt = (
            "You are a disciplined equity technical analyst evaluating a NEW long entry. "
            "Judge ONLY on the technicals and levels below.\n\n"
            f"STOCK {symbol} — spot ₹{f.ltp} | 12-month momentum {mom:+.0f}%\n{levels}\n\n"
            'Respond with ONLY a JSON object, no prose:\n'
            '{"action":"ENTER|WATCH|AVOID","conviction":"high|medium|low",'
            '"entry_zone":[<low>,<high>],"stop":<number>,"target":<number>,'
            '"key_resistance":<number>,"reasons":"<2-3 sentences citing the specific levels above>"}'
        )

    rec = parse_json(complete(prompt)) or {"action": "N/A", "reasons": "LLM unavailable"}

    card = {
        "symbol": symbol,
        "name": symbol.split(":")[-1].replace("-EQ", ""),
        "kind": kind,
        "ltp": f.ltp,
        "regime": f.regime.value,
        "rsi": f.rsi,
        "momentum_12m_pct": round(mom, 0),
        "pct_from_52w_high": f.pct_from_52w_high,
        "monthly_cpr": f.monthly_cpr.position,
        "resistances": resistances,
        "supports": supports,
        "recommendation": rec,
    }

    if kind == "holding" and holding:
        cost = float(holding.get("costPrice") or 0)
        qty = int(holding.get("quantity") or 0)
        pl = float(holding.get("pl") or 0)
        pl_pct = round((f.ltp - cost) / cost * 100, 1) if cost else 0.0
        below_support = bool(supports) and f.ltp < supports[0]
        card["position"] = {
            "qty": qty, "cost": round(cost, 2), "pl": round(pl, 0), "pl_pct": pl_pct,
            "loss_note": (
                f"Down {pl_pct:.0f}% and below nearest support ₹{supports[0]} — loss-cut candidate"
                if pl_pct <= -10 and below_support else
                f"Down {pl_pct:.0f}%" if pl_pct <= -10 else
                f"Up {pl_pct:.0f}%" if pl_pct >= 10 else "Near breakeven"
            ),
        }
    return card


def run_analysis(
    provider: CandleProvider,
    candidate_symbols: list[EquitySymbol],
    history: int = 320,
    holdings_limit: int = 0,
    workers: int = 5,
) -> dict:
    """Analyse every holding + the candidate shortlist. The per-stock LLM calls run
    in parallel (workers) so the batch is ~minutes, not tens of minutes."""
    holdings = fetch_holdings()
    if holdings_limit > 0:
        holdings = holdings[:holdings_limit]

    # Prepare tasks (fetch bars — mostly cache hits); then analyse (LLM) in parallel.
    tasks = []  # (symbol, bars, kind, holding)
    for h in holdings:
        sym = h.get("symbol", "")
        if sym:
            bars = provider.daily_bars(sym, limit=history)
            if len(bars) >= LOOKBACK + SKIP + 5:
                tasks.append((sym, bars, "holding", h))
    for s in candidate_symbols:
        bars = provider.daily_bars(s.symbol, limit=history)
        if len(bars) >= LOOKBACK + SKIP + 5:
            tasks.append((s.symbol, bars, "candidate", None))

    cards = [None] * len(tasks)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(analyze_symbol, t[0], t[1], t[2], t[3]): i for i, t in enumerate(tasks)}
        for fut in futures:
            cards[futures[fut]] = fut.result()

    holdings_cards = [c for c, t in zip(cards, tasks) if c and t[2] == "holding"]
    candidate_cards = [c for c, t in zip(cards, tasks) if c and t[2] == "candidate"]
    logger.info("Analysis: %d holdings, %d candidates", len(holdings_cards), len(candidate_cards))
    return {"holdings": holdings_cards, "candidates": candidate_cards}


def _candidate_universe() -> list[EquitySymbol]:
    """The tradeable universe to screen for candidates — the persisted liquid set if
    present (fast, cached bars), else a slice of the full universe (slower)."""
    if os.path.exists(LIQUID_FILE):
        with open(LIQUID_FILE) as f:
            toks = f.read().replace("\n", ",").split(",")
        return [EquitySymbol(symbol=t.strip(), short_symbol=t.strip().split(":")[-1].replace("-EQ", ""),
                             name=t.strip()) for t in toks if t.strip()]
    from universe import load_universe
    return load_universe()[:200]


def build_report(provider: CandleProvider, candidate_count: int = 8) -> dict:
    """Full report: screen the liquid universe for top momentum candidates, then run
    the LLM analysis on holdings + those candidates. Cached by the API for instant load."""
    from screener import momentum_watchlist

    universe = _candidate_universe()
    rows = momentum_watchlist(universe, provider, top_n=candidate_count, clean_only=True) if candidate_count else []
    cands = [EquitySymbol(symbol=r["symbol"], short_symbol=r["name"], name=r["name"]) for r in rows]
    report = run_analysis(provider, cands)
    report["generated_at"] = datetime.now(IST).isoformat()
    return report
