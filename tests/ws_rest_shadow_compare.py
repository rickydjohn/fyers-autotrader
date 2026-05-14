"""
Shadow comparison: WS-populated ltp:{symbol} vs live REST /quotes for the same
symbol — gives us empirical confidence that the WS feed is producing values
consistent with the existing REST quote path, before we lean on it for trading
decisions.

Usage (inside trading-core container, during market hours):
  docker cp tests/ws_rest_shadow_compare.py trading-core:/tmp/
  docker exec -d trading-core python3 -u /tmp/ws_rest_shadow_compare.py \
      --interval 5 --duration 1800 --out /tmp/ws_rest_shadow.csv
  # then inspect:
  docker exec trading-core cat /tmp/ws_rest_shadow.csv | tail
  docker exec trading-core python3 /tmp/ws_rest_shadow_compare.py --summarize /tmp/ws_rest_shadow.csv

What this captures (one row per (symbol, interval) tick):
  timestamp_ist, symbol, ws_ltp, rest_ltp, ws_age_ms, delta_bps, delta_abs,
  ws_status   (ok | missing | stale | malformed)

What "healthy" looks like:
  - ws_status=ok for nearly every row during market hours
  - ws_age_ms typically < 1000ms (WS leads REST poll)
  - delta_bps |x| < 2 (under 0.02% — within bid/ask noise for indices)
  - delta_bps absolute median converges to 0 across the run

Red flags:
  - Persistent same-sign delta (WS reads systematically high or low)
  - ws_age_ms growing over time (WS feed losing freshness)
  - Spikes in delta_bps during volatility — investigate whether one path
    is stale
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime
from statistics import median

import pytz
import redis

# Make core-engine modules importable so we can use the existing get_quote().
sys.path.insert(0, "/app")

IST = pytz.timezone("Asia/Kolkata")


def _make_redis() -> redis.Redis:
    url = os.environ.get("REDIS_URL", "redis://trading-redis:6379")
    return redis.Redis.from_url(url, decode_responses=True)


def _read_ws_ltp(r: redis.Redis, symbol: str) -> dict:
    """Returns dict with status + ws_ltp + ws_age_ms."""
    raw = r.get(f"ltp:{symbol}")
    if not raw:
        return {"status": "missing", "ws_ltp": None, "ws_age_ms": None}
    try:
        data = json.loads(raw)
    except Exception:
        return {"status": "malformed", "ws_ltp": None, "ws_age_ms": None}
    ts_ms = data.get("ts")
    ltp = data.get("ltp")
    if ltp is None:
        return {"status": "malformed", "ws_ltp": None, "ws_age_ms": None}
    if not ts_ms:
        # No ts → can't judge freshness — likely written by a pre-fix path
        return {"status": "no_ts", "ws_ltp": float(ltp), "ws_age_ms": None}
    age_ms = int(time.time() * 1000) - int(ts_ms)
    status = "ok" if age_ms <= 5_000 else "stale"
    return {"status": status, "ws_ltp": float(ltp), "ws_age_ms": age_ms}


def _read_rest_quote(symbol: str) -> dict:
    """Lazy-import get_quote so this script works outside the trading-core
    container too (won't be callable in that case, but Redis-only reads still
    work). Returns {rest_ltp, rest_ms_taken} or {error}."""
    try:
        from fyers.market_data import get_quote  # type: ignore
    except Exception as e:
        return {"rest_ltp": None, "rest_ms_taken": None, "error": f"import_failed: {e}"}
    start = time.monotonic()
    try:
        q = get_quote(symbol)
    except Exception as e:
        return {"rest_ltp": None, "rest_ms_taken": None, "error": f"quote_failed: {e}"}
    elapsed_ms = int((time.monotonic() - start) * 1000)
    if not q or q.get("ltp") is None:
        return {"rest_ltp": None, "rest_ms_taken": elapsed_ms, "error": "no_ltp"}
    return {"rest_ltp": float(q["ltp"]), "rest_ms_taken": elapsed_ms, "error": None}


def run_loop(symbols: list[str], interval_s: float, duration_s: int, out_path: str) -> None:
    r = _make_redis()
    deadline = time.monotonic() + duration_s
    headers = [
        "timestamp_ist",
        "symbol",
        "ws_status",
        "ws_ltp",
        "rest_ltp",
        "ws_age_ms",
        "rest_ms_taken",
        "delta_bps",
        "delta_abs",
        "rest_error",
    ]
    write_header = not os.path.exists(out_path)
    with open(out_path, "a", newline="", buffering=1) as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(headers)
        n_rows = 0
        while time.monotonic() < deadline:
            t_ist = datetime.now(IST).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            for sym in symbols:
                ws = _read_ws_ltp(r, sym)
                rest = _read_rest_quote(sym)
                ws_ltp = ws["ws_ltp"]
                rest_ltp = rest["rest_ltp"]
                if ws_ltp is not None and rest_ltp is not None and rest_ltp > 0:
                    delta_abs = ws_ltp - rest_ltp
                    delta_bps = (delta_abs / rest_ltp) * 10_000
                else:
                    delta_abs = None
                    delta_bps = None
                w.writerow([
                    t_ist, sym, ws["status"],
                    ws_ltp if ws_ltp is not None else "",
                    rest_ltp if rest_ltp is not None else "",
                    ws.get("ws_age_ms") if ws.get("ws_age_ms") is not None else "",
                    rest.get("rest_ms_taken") if rest.get("rest_ms_taken") is not None else "",
                    f"{delta_bps:+.3f}" if delta_bps is not None else "",
                    f"{delta_abs:+.4f}"  if delta_abs is not None else "",
                    rest.get("error") or "",
                ])
                n_rows += 1
            time.sleep(interval_s)
    print(f"wrote {n_rows} rows to {out_path}", flush=True)


def summarize(csv_path: str) -> None:
    """Print headline stats from a captured CSV."""
    if not os.path.exists(csv_path):
        print(f"no file: {csv_path}", file=sys.stderr)
        sys.exit(2)

    per_sym: dict[str, list] = {}
    status_counts: dict[str, dict[str, int]] = {}
    ages: dict[str, list[int]] = {}
    rest_times: dict[str, list[int]] = {}

    with open(csv_path, newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            sym = row["symbol"]
            st = row["ws_status"]
            status_counts.setdefault(sym, {})
            status_counts[sym][st] = status_counts[sym].get(st, 0) + 1
            if row["delta_bps"]:
                per_sym.setdefault(sym, []).append(float(row["delta_bps"]))
            if row["ws_age_ms"]:
                ages.setdefault(sym, []).append(int(row["ws_age_ms"]))
            if row["rest_ms_taken"]:
                rest_times.setdefault(sym, []).append(int(row["rest_ms_taken"]))

    for sym in sorted(per_sym):
        deltas = per_sym[sym]
        med = median(deltas)
        med_abs = median(abs(d) for d in deltas)
        ws_ages = ages.get(sym, [])
        rest_t = rest_times.get(sym, [])
        print(f"\n=== {sym} ===")
        print(f"  samples with both values: {len(deltas)}")
        print(f"  delta_bps median signed: {med:+.3f}    (drift bias if not ~0)")
        print(f"  delta_bps median |abs|:  {med_abs:.3f}     (typical noise)")
        if ws_ages:
            ws_ages_sorted = sorted(ws_ages)
            p50 = ws_ages_sorted[len(ws_ages_sorted) // 2]
            p95 = ws_ages_sorted[int(len(ws_ages_sorted) * 0.95)]
            print(f"  ws_age_ms p50/p95: {p50} / {p95}")
        if rest_t:
            rest_sorted = sorted(rest_t)
            p50r = rest_sorted[len(rest_sorted) // 2]
            p95r = rest_sorted[int(len(rest_sorted) * 0.95)]
            print(f"  rest_ms_taken p50/p95: {p50r} / {p95r}")
        print(f"  status counts: {status_counts.get(sym, {})}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--symbols", default="NSE:NIFTY50-INDEX,NSE:NIFTYBANK-INDEX")
    ap.add_argument("--interval", type=float, default=5.0,
                    help="seconds between sampling rounds")
    ap.add_argument("--duration", type=int, default=1800,
                    help="total run duration in seconds (default 30 min)")
    ap.add_argument("--out", default="/tmp/ws_rest_shadow.csv")
    ap.add_argument("--summarize", default=None,
                    help="if set, skip sampling and just summarize this CSV file")
    args = ap.parse_args()

    if args.summarize:
        summarize(args.summarize)
        return 0

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    print(f"Sampling {symbols} every {args.interval}s for {args.duration}s, "
          f"writing to {args.out}", flush=True)
    run_loop(symbols, args.interval, args.duration, args.out)
    print("done. To analyze:", flush=True)
    print(f"  python3 {os.path.abspath(__file__)} --summarize {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
