"""
Proxy configuration for Fyers API traffic.

Three-layer fix to prevent IPv6 bypass:

Layer 1 — urllib3 address family:
  urllib3.util.connection.allowed_gai_family() controls which address family
  urllib3 passes to socket.getaddrinfo. By default it returns AF_UNSPEC when
  the host has IPv6, which causes it to pick the AAAA record and connect
  directly via IPv6, bypassing the IPv4-only proxy.
  Patching this to always return AF_INET forces urllib3 to only consider IPv4.

Layer 2 — requests.Session proxy injection:
  The Fyers SDK creates its own requests.Session instances, some of which
  may set trust_env=False or proxies={}, ignoring HTTPS_PROXY env vars.
  Patching requests.Session.__init__ injects the proxy URL into every session
  regardless of how it is constructed.

Layer 3 — module-level requests.* function patching:
  The Fyers SDK also calls requests.post() / requests.get() etc. as module-level
  functions. These create a temporary session internally, but proxy injection via
  Session.__init__ alone is not reliable in all SDK code paths. Patching the
  module-level functions directly guarantees the proxy and timeout are injected
  at every call site regardless of how the SDK constructs its HTTP calls.
"""

import logging
import os
import socket

import requests
import urllib3

from config import settings

logger = logging.getLogger(__name__)


def _ipv4_only_gai_family():
    """Always return AF_INET so urllib3 never picks an IPv6 address."""
    return socket.AF_INET


def configure_fyers_proxy() -> None:
    """
    Patch urllib3 and requests to force all Fyers traffic through IPv4 proxy.
    Must be called before any Fyers SDK import or session creation.
    """
    # ── Layer 1: Force IPv4 at the urllib3 address-family level ──────────────
    urllib3.util.connection.allowed_gai_family = _ipv4_only_gai_family
    # Also patch the reference inside the already-imported module
    try:
        import urllib3.util.connection as _conn
        _conn.allowed_gai_family = _ipv4_only_gai_family
    except Exception:
        pass
    logger.info("urllib3 patched: AF_INET forced (IPv6 disabled for all HTTP/S connections)")

    # ── Layer 2: Inject proxy into every requests.Session ────────────────────
    proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")

    if not proxy_url:
        logger.warning(
            "HTTPS_PROXY not set — Fyers traffic will be direct. "
            "Ensure PROXY_IP/PORT/USER/PASSWORD are in .env and container was restarted."
        )
        return

    _original_init = requests.Session.__init__
    _original_request = requests.Session.request

    def _patched_session_init(self, *args, **kwargs):
        _original_init(self, *args, **kwargs)
        self.proxies.update({
            "http":  proxy_url,
            "https": proxy_url,
        })
        self.trust_env = True

    def _patched_session_request(self, method, url, **kwargs):
        # Inject a default timeout so hung Fyers API calls never block the event loop
        # indefinitely. Callers can still override by passing timeout= explicitly.
        kwargs.setdefault("timeout", 10)
        return _original_request(self, method, url, **kwargs)

    requests.Session.__init__ = _patched_session_init
    requests.Session.request = _patched_session_request

    # ── Layer 3: Patch module-level requests.* functions directly ────────────
    # The Fyers SDK calls requests.post(URL, ...) as module-level functions,
    # not via a session instance. Even though Session.__init__ is patched,
    # the temporary session created internally by requests.post() may not
    # reliably inherit proxies in all SDK code paths. Patching the functions
    # directly guarantees the proxy and timeout are injected at the call site.
    _orig_get = requests.get
    _orig_post = requests.post
    _orig_put = requests.put
    _orig_patch = requests.patch
    _orig_delete = requests.delete

    def _make_patched(orig_fn):
        def _patched(url, **kwargs):
            kwargs.setdefault("proxies", {"http": proxy_url, "https": proxy_url})
            kwargs.setdefault("timeout", 10)
            return orig_fn(url, **kwargs)
        _patched.__name__ = orig_fn.__name__
        _patched.__doc__  = orig_fn.__doc__
        return _patched

    requests.get    = _make_patched(_orig_get)
    requests.post   = _make_patched(_orig_post)
    requests.put    = _make_patched(_orig_put)
    requests.patch  = _make_patched(_orig_patch)
    requests.delete = _make_patched(_orig_delete)

    logger.info(
        "requests.Session + module-level functions patched: "
        "all HTTP via proxy %s:%s (timeout=10s)",
        settings.proxy_ip,
        settings.proxy_port,
    )
