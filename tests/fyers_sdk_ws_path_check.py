"""
Fyers SDK WebSocket path-check — proves which network path the SDK uses for
each of its two connections. Operational diagnostic; must run inside the
trading-core container (needs the Fyers token, the SDK, and the container's
network namespace).

Findings from runs on 2026-05-14 (validated against Squid access logs):

  - REST call to api-t1.fyers.in/data/symbol-token (SDK bootstrap step
    `access_token_to_hsmtoken`) goes THROUGH Squid. Visible as
    `CONNECT api-t1.fyers.in:443` in /var/log/squid/access.log.
  - WebSocket connection to socket.fyers.in:443 goes DIRECT to Cloudflare
    (104.18.4.135 / 104.18.5.135). NEVER appears in the Squid access log.

  Why: `requests` reads HTTPS_PROXY from env by default. websocket-client's
  WebSocketApp.run_forever path (which is what the Fyers SDK uses) does NOT
  auto-read HTTPS_PROXY — only its sync create_connection path does. The SDK
  doesn't pass proxy params to run_forever either, so the WS bypasses Squid.

Implication for production:
  - The SDK works as-is. No Layer-4 monkey-patch needed for WebSocket.
  - But the SDK's bootstrap still needs the proxy (because of the REST call).
    If Squid is down, the SDK can't start. The WebSocket itself survives a
    Squid outage once already connected.

Usage:
  # Copy into container, then exec:
  docker cp tests/fyers_sdk_ws_path_check.py trading-core:/tmp/
  docker exec trading-core python3 -u /tmp/fyers_sdk_ws_path_check.py envon

  # Pass 'envoff' to strip HTTPS_PROXY env before SDK import — proves the
  # WS still connects direct, but the REST bootstrap will also go direct
  # (and may fail if Fyers ever IP-whitelists the REST API more strictly).

Re-run procedure (to re-verify):
  1. Note current epoch + Squid access.log line count before the run.
  2. Run this script — get `msgs >= 3` in the result line if SDK works.
  3. On the Squid host (`ssh root@187.127.149.104`):
       awk -v s=<epoch_start> '$1+0 >= s' /var/log/squid/access.log
     Look for CONNECT records: only api-t1 should appear; no socket.fyers.in.
"""
import json
import os
import socket
import sys
import threading
import time

MODE = sys.argv[1] if len(sys.argv) > 1 else "envon"  # 'envon' | 'envoff'

if MODE == "envoff":
    # Strip all proxy env vars BEFORE importing anything network-related,
    # to verify the WS connection truly does not need them.
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        os.environ.pop(k, None)

_LOG = open("/tmp/fyers_sdk_ws_path_check.log", "w", buffering=1)
def log(m: str) -> None:
    _LOG.write(m + "\n")
    _LOG.flush()
    sys.stdout.write(m + "\n")
    sys.stdout.flush()

log(f"=== MODE: {MODE} ===")
log(f"[env] HTTPS_PROXY={os.environ.get('HTTPS_PROXY', '<unset>')}")
log(f"[env] http_proxy ={os.environ.get('http_proxy', '<unset>')}")

with open("/app/tokens/access_token.json") as f:
    tok = json.load(f)["access_token"]
appid = os.environ.get("FYERS_CLIENT_ID", "")
full_token = f"{appid}:{tok}"

# DNS resolution snapshot — confirms both v4 and v6 are advertised for the WS host.
try:
    infos = socket.getaddrinfo("socket.fyers.in", 443, type=socket.SOCK_STREAM)
    seen = {
        ("AF_INET6" if fam == socket.AF_INET6 else "AF_INET", sa[0])
        for fam, _t, _p, _c, sa in infos
    }
    for fam, ip in sorted(seen):
        log(f"[dns] socket.fyers.in → {fam} {ip}")
except Exception as e:
    log(f"[dns] FAILED: {e}")


def hex_ip6_to_str(h: str) -> str:
    """/proc/net/tcp6 stores IPv6 as 32 hex chars with byte-reversed 32-bit words."""
    if len(h) != 32:
        return h
    parts = []
    for i in range(0, 32, 8):
        word = h[i:i + 8]
        reversed_word = "".join(word[j - 2:j] for j in range(8, 0, -2))
        parts.append(reversed_word[:4])
        parts.append(reversed_word[4:])
    return ":".join(parts).lower()


def snapshot_sockets(tag: str) -> None:
    """Dump ESTABLISHED TCP connections (v4 + v6) for this PID's netns."""
    log(f"--- TCP snapshot @ {tag} ---")
    try:
        with open("/proc/net/tcp") as f:
            lines = f.readlines()
        for line in lines[1:]:
            parts = line.split()
            if len(parts) >= 4 and parts[3] == "01":  # ESTABLISHED
                ip_hex, port_hex = parts[2].split(":")
                ip = ".".join(str(int(ip_hex[i:i + 2], 16)) for i in range(6, -1, -2))
                port = int(port_hex, 16)
                # Suppress noise from container-internal Redis / scheduler traffic
                if not (ip.startswith("172.") and port == 6379):
                    log(f"  [tcp4] ESTAB {ip}:{port}")
    except Exception as e:
        log(f"  [tcp4] read failed: {e}")
    try:
        with open("/proc/net/tcp6") as f:
            lines = f.readlines()
        for line in lines[1:]:
            parts = line.split()
            if len(parts) >= 4 and parts[3] == "01":
                ip_hex, port_hex = parts[2].split(":")
                port = int(port_hex, 16)
                log(f"  [tcp6] ESTAB {hex_ip6_to_str(ip_hex)}:{port}")
    except FileNotFoundError:
        log("  [tcp6] /proc/net/tcp6 not present (no IPv6 stack)")
    except Exception as e:
        log(f"  [tcp6] read failed: {e}")


from fyers_apiv3.FyersWebsocket import data_ws  # noqa: E402

state = {"opened": False, "msg_count": 0, "error": None}

def on_open():
    state["opened"] = True
    log(f"[+{int((time.monotonic()-t0)*1000):>5}ms] OPEN")
    fyers.subscribe(symbols=["NSE:NIFTY50-INDEX"], data_type="SymbolUpdate")

def on_message(_m):
    state["msg_count"] += 1
    if state["msg_count"] <= 3:
        log(f"[+{int((time.monotonic()-t0)*1000):>5}ms] msg #{state['msg_count']}")

def on_error(e):
    state["error"] = repr(e)
    log(f"[+{int((time.monotonic()-t0)*1000):>5}ms] ERROR: {e!r}")

def on_close(*a):
    log(f"[+{int((time.monotonic()-t0)*1000):>5}ms] CLOSE: {a}")


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

t0 = time.monotonic()
log("[+    0ms] calling fyers.connect()...")
threading.Thread(target=fyers.connect, daemon=True).start()

# The SDK calls on_open from a fixed time.sleep(2) regardless of real WS state.
# So 'opened' alone doesn't prove the connection succeeded — count messages instead.
for _ in range(80):
    if state["opened"]:
        break
    time.sleep(0.1)

time.sleep(0.3)
snapshot_sockets("post-OPEN +0.3s")
time.sleep(2)
snapshot_sockets("post-OPEN +2.3s")

log(
    f"[RESULT] mode={MODE} opened={state['opened']} "
    f"msgs={state['msg_count']} error={state['error']}"
)
log("Healthy session: msgs >= 3 (cn, ful, sub) plus at least 1 tick.")
log("If msgs == 0 the SDK never completed handshake (often Squid down or token expired).")
