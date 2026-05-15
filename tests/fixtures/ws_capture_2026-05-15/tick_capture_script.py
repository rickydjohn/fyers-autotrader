"""5-minute live tick capture from Fyers WS. Writes JSONL to /tmp/tick_capture.jsonl
for offline replay/analysis. Each line is one WS message with _recv_ts added."""
from __future__ import annotations

import json
import os
import sys
import threading
import time

sys.path.insert(0, "/app")

DURATION_S = int(os.environ.get("CAPTURE_SECONDS", "300"))
OUT_PATH   = os.environ.get("CAPTURE_OUT",     "/tmp/tick_capture.jsonl")
SYMBOLS    = os.environ.get("CAPTURE_SYMBOLS", "NSE:NIFTY50-INDEX,NSE:NIFTYBANK-INDEX").split(",")

with open("/app/tokens/access_token.json") as f:
    tok = json.load(f)["access_token"]
appid = os.environ.get("FYERS_CLIENT_ID", "")
full_token = f"{appid}:{tok}"

from fyers_apiv3.FyersWebsocket import data_ws  # noqa: E402

out_f = open(OUT_PATH, "w", buffering=1)
counts = {s: 0 for s in SYMBOLS}


def stamp_iso() -> str:
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + f".{int(t*1000)%1000:03d}"


def write_event(payload: dict) -> None:
    payload["_recv_ts"] = stamp_iso()
    out_f.write(json.dumps(payload) + "\n")


def on_open():
    fyers.subscribe(symbols=SYMBOLS, data_type="SymbolUpdate")
    write_event({"_event": "subscribed", "symbols": SYMBOLS})


def on_message(m):
    if not isinstance(m, dict):
        return
    payload = dict(m)  # copy so we don't mutate
    write_event(payload)
    sym = payload.get("symbol")
    if sym in counts:
        counts[sym] += 1


def on_close(*a):
    write_event({"_event": "close", "args": [str(x) for x in a]})


def on_error(e):
    write_event({"_event": "error", "err": repr(e)})


fyers = data_ws.FyersDataSocket(
    access_token=full_token,
    log_path="/tmp",
    litemode=False,
    write_to_file=False,
    reconnect=False,
    on_connect=on_open,
    on_close=on_close,
    on_error=on_error,
    on_message=on_message,
)

threading.Thread(target=fyers.connect, daemon=True).start()
start = time.monotonic()
time.sleep(DURATION_S)
write_event({"_event": "done", "duration_s": int(time.monotonic() - start), "counts": counts})
out_f.close()
print(f"capture done → {OUT_PATH}; counts={counts}", flush=True)
