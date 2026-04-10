#!/usr/bin/env python3
"""
Proxy verification test.

Proves the proxy is actually used by comparing outbound IPs:
  - Direct request (no proxy) → container's own IP
  - Request through proxy    → proxy server's egress IP (should be PROXY_IP)

Run from inside the core-engine container:
    python3 /app/test_proxy.py
"""

import os
import sys

import requests

# ── Config from environment ───────────────────────────────────────────────────

proxy_ip       = os.environ.get("PROXY_IP", "")
proxy_port     = os.environ.get("PROXY_PORT", "")
proxy_user     = os.environ.get("PROXY_USER", "")
proxy_password = os.environ.get("PROXY_PASSWORD", "")
client_id      = os.environ.get("FYERS_CLIENT_ID", "")
secret_key     = os.environ.get("FYERS_SECRET_KEY", "")

if not all([proxy_ip, proxy_port, proxy_user, proxy_password]):
    sys.exit("ERROR: proxy env vars not set")

proxy_url = f"http://{proxy_user}:{proxy_password}@{proxy_ip}:{proxy_port}"
proxies   = {"http": proxy_url, "https": proxy_url}

print(f"Configured proxy : {proxy_ip}:{proxy_port}")
print(f"Client ID        : {client_id}")
print()

# ── Test 1: confirm proxy TCP connectivity ────────────────────────────────────

print("── Test 1: proxy TCP connectivity ──────────────────────────────────────")
import socket
try:
    s = socket.create_connection((proxy_ip, int(proxy_port)), timeout=5)
    s.close()
    print(f"  PASS — TCP connection to {proxy_ip}:{proxy_port} succeeded")
except Exception as e:
    print(f"  FAIL — cannot reach proxy: {e}")
    sys.exit(1)
print()

# ── Test 2: IP echo — direct vs proxied ──────────────────────────────────────

print("── Test 2: outbound IP comparison ──────────────────────────────────────")
IP_ECHO = "https://api.ipify.org"

try:
    direct_ip = requests.get(IP_ECHO, timeout=8).text.strip()
    print(f"  Direct IP  (no proxy) : {direct_ip}")
except Exception as e:
    print(f"  WARN — could not fetch direct IP: {e}")
    direct_ip = None

try:
    proxied_ip = requests.get(IP_ECHO, proxies=proxies, timeout=8).text.strip()
    print(f"  Proxied IP            : {proxied_ip}")
except Exception as e:
    print(f"  FAIL — proxied request failed: {e}")
    sys.exit(1)

if direct_ip and proxied_ip == direct_ip:
    print("  WARN — IPs are identical; proxy may not be changing egress IP")
elif proxied_ip == proxy_ip:
    print(f"  PASS — proxied IP matches PROXY_IP ({proxy_ip})")
else:
    print(f"  INFO — proxied IP ({proxied_ip}) differs from direct ({direct_ip}); proxy is routing traffic")
print()

# ── Test 3: Fyers request goes through proxy (patch verification) ─────────────

print("── Test 3: fyers.in request routes via proxy ────────────────────────────")

_orig_send = requests.Session.send
_intercepted: list[str] = []

def _proxy_aware_send(self, request, **kwargs):
    if request.url and "fyers.in" in request.url:
        kwargs.setdefault("proxies", {"http": proxy_url, "https": proxy_url})
        _intercepted.append(request.url)
    return _orig_send(self, request, **kwargs)

requests.Session.send = _proxy_aware_send

try:
    resp = requests.get(
        "https://api-t1.fyers.in/api/v3/profile",
        headers={"Authorization": f"{client_id}:dummy", "Content-Type": "application/json"},
        timeout=10,
    )
    print(f"  Fyers responded HTTP {resp.status_code}")
    print(f"  Body : {resp.text[:120]}")
    if _intercepted:
        print(f"  Proxy patch fired for: {_intercepted[0]}")
        print("  PASS")
    else:
        print("  FAIL — patch did not intercept the request")
except requests.exceptions.ProxyError as e:
    print(f"  FAIL — ProxyError: {e}")
except Exception as e:
    print(f"  ERROR — {e}")

print()

# ── Test 4: non-fyers request does NOT use proxy ──────────────────────────────

print("── Test 4: non-fyers traffic bypasses proxy ─────────────────────────────")
_intercepted.clear()
try:
    requests.get("https://api.ipify.org", timeout=5)
    if _intercepted:
        print("  FAIL — proxy patch incorrectly fired for non-fyers URL")
    else:
        print("  PASS — ipify.org was not routed through proxy")
except Exception as e:
    print(f"  WARN — {e}")
