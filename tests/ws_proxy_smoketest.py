"""
WebSocket proxy smoke test — validates that the Fyers WebSocket is reachable
through the Squid proxy, end-to-end, BEFORE we wire the Fyers SDK through it.

WS URL note: the public Fyers KB documents wss://api.fyers.in/socket/v2/data/
but the actual SDK uses wss://socket.fyers.in/hsm/v1-5/prod (verified in
fyers_apiv3/FyersWebsocket/data_ws.py line 175). The docs URL returns HTTP 500
on upgrade — always test against the SDK URL.

What this proves:
  - CONNECT to socket.fyers.in:443 succeeds through Squid with basic auth
  - TLS handshake completes through the tunnel
  - WebSocket Upgrade returns 101 (verified 2026-05-14: handshake green at +270ms)

What this does NOT prove:
  - That the Fyers SDK can use the proxy (that needs a Layer-4 patch — Task #3)
  - That symbol subscriptions return ticks (we'd need a real access token AND
    Fyers may IP-whitelist on top of auth)

Usage:
  PROXY_IP=187.127.149.104 PROXY_PORT=3128 \
  PROXY_USER=trading PROXY_PASSWORD=trading \
  FYERS_ACCESS_TOKEN=appid:token \   # optional — without it, auth will fail
  python3 tests/ws_proxy_smoketest.py

Without the token, the test still validates the proxy + TLS + WS-upgrade
path; Fyers will close with an auth error which is logged.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
from typing import Optional

# Always line-buffer stdout so output streams to the caller in real time,
# even when redirected to a file. Without this, hangs look identical to
# silent-but-still-running.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# websocket-client is the library Fyers' SDK uses under the hood
try:
    import websocket  # type: ignore
except ImportError:
    print("ERROR: websocket-client not installed. Run: pip install websocket-client", file=sys.stderr)
    sys.exit(2)


# ── Configuration via env ────────────────────────────────────────────────────
PROXY_HOST = os.environ.get("PROXY_IP", "187.127.149.104")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "3128"))
PROXY_USER = os.environ.get("PROXY_USER", "")
PROXY_PASS = os.environ.get("PROXY_PASSWORD", "")
TOKEN      = os.environ.get("FYERS_ACCESS_TOKEN", "")
WS_URL     = os.environ.get("FYERS_WS_URL", "wss://socket.fyers.in/hsm/v1-5/prod")
TIMEOUT    = float(os.environ.get("WS_SMOKETEST_TIMEOUT", "15"))


# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ws-smoketest")


# Force IPv4 — defensive against AAAA records the proxy can't route.
# api.fyers.in currently has none, but the production patch in
# core-engine/fyers/proxy.py forces AF_INET globally and we want the smoke
# test to mirror that behaviour.
_orig_getaddrinfo = socket.getaddrinfo

def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

socket.getaddrinfo = _ipv4_only_getaddrinfo  # type: ignore[assignment]


# ── Milestone tracking ───────────────────────────────────────────────────────
class Milestones:
    def __init__(self) -> None:
        self.start = time.monotonic()
        self.proxy_connect_ok = False
        self.ws_upgrade_ok    = False
        self.first_frame      = False
        self.close_code: Optional[int] = None
        self.close_msg:  Optional[str] = None
        self.error:      Optional[str] = None

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.start) * 1000)


m = Milestones()


# ── WebSocket callbacks ──────────────────────────────────────────────────────
def on_open(_ws):
    m.ws_upgrade_ok = True
    log.info(f"[+{m.elapsed_ms():>5}ms] WS UPGRADE OK — proxy + TLS + WS handshake all green")
    # If we have an auth token, try a no-op subscribe just to provoke any
    # server-side response. Without it, just wait — Fyers will close us out
    # and that's still a proxy-success signal.
    if TOKEN:
        log.info("Sending no-op auth frame to provoke server response...")
        # Fyers expects a JSON subscribe frame. Send a minimal one.
        import json as _json
        _ws.send(_json.dumps({"T": "SUB_L1", "L1LIST": ["NSE:NIFTY50-INDEX"], "SUB_T": 1}))


def on_message(_ws, message):
    m.first_frame = True
    snippet = message[:120] if isinstance(message, str) else f"<{len(message)} bytes>"
    log.info(f"[+{m.elapsed_ms():>5}ms] MESSAGE: {snippet}")


def on_error(_ws, err):
    m.error = repr(err)
    log.error(f"[+{m.elapsed_ms():>5}ms] ERROR: {err}")


def on_close(_ws, code, msg):
    m.close_code = code
    m.close_msg  = msg
    log.info(f"[+{m.elapsed_ms():>5}ms] CLOSED code={code} msg={msg!r}")


# ── Run ──────────────────────────────────────────────────────────────────────
def main() -> int:
    log.info(f"WS URL:     {WS_URL}")
    log.info(f"Proxy:      {PROXY_HOST}:{PROXY_PORT} (user={PROXY_USER or '<none>'})")
    log.info(f"Auth token: {'present' if TOKEN else 'absent (auth failure after WS upgrade is fine)'}")
    log.info(f"Timeout:    {TIMEOUT}s")

    headers = {}
    if TOKEN:
        headers["Authorization"] = TOKEN

    ws_app = websocket.WebSocketApp(
        WS_URL,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
        header=headers,
    )

    # run_forever runs the loop until the connection closes OR we hit the
    # ping timeout. We rely on Fyers closing us (no token) or our own
    # close-on-first-frame logic to terminate.
    proxy_auth = (PROXY_USER, PROXY_PASS) if PROXY_USER and PROXY_PASS else None

    # Enforce a hard runtime cap by closing the socket from a timer thread.
    # websocket-client's run_forever has no native max-duration option.
    def _deadline():
        log.info(f"[+{m.elapsed_ms():>5}ms] deadline reached, closing socket")
        try:
            ws_app.close()
        except Exception:
            pass

    deadline_t = threading.Timer(TIMEOUT, _deadline)
    deadline_t.daemon = True
    deadline_t.start()

    try:
        ws_app.run_forever(
            http_proxy_host=PROXY_HOST,
            http_proxy_port=PROXY_PORT,
            http_proxy_auth=proxy_auth,
            proxy_type="http",
            ping_interval=30,
            ping_timeout=10,
        )
    except KeyboardInterrupt:
        log.warning("Interrupted")
    except Exception as e:
        m.error = repr(e)
        log.error(f"run_forever raised: {e!r}")
    finally:
        deadline_t.cancel()

    # If we connected but auth-failed (no token), that's still a proxy success.
    if m.ws_upgrade_ok and not TOKEN:
        log.info("Smoke test PROXY PATH OK — WS upgrade succeeded; auth failure expected without token")
        return 0
    if m.ws_upgrade_ok and TOKEN and m.first_frame:
        log.info("Smoke test FULL OK — proxy + auth + data frame received")
        return 0
    if m.ws_upgrade_ok and TOKEN and not m.first_frame:
        log.warning("Smoke test PARTIAL — proxy + WS upgrade OK but no data frame; check token/IP-whitelist")
        return 1

    log.error(f"Smoke test FAILED — error={m.error} close_code={m.close_code}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
