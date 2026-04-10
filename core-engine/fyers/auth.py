"""
Fyers authentication manager.

Two modes:
  TOTP (automated, preferred):
    Requires FYERS_USER_ID, FYERS_PIN, FYERS_TOTP_KEY in .env.
    Called on container startup and refreshed daily at 07:30 IST.
    No browser interaction needed.

  Manual OAuth2 (fallback):
    1. GET /fyers/auth  → redirects to Fyers login page
    2. User logs in → Fyers redirects to /fyers/callback?auth_code=...
    3. We exchange auth_code for access_token and persist it
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import pyotp
import requests
from fyers_apiv3 import fyersModel

from config import settings

logger = logging.getLogger(__name__)

TOKEN_FILE = Path(settings.token_path)

# Fyers internal auth endpoints (not part of the trading API)
_VAGATOR_BASE = "https://api-t2.fyers.in/vagator/v2"
_TOKEN_URL    = "https://api-t1.fyers.in/api/v3/token"


# ── Token cache ───────────────────────────────────────────────────────────────

def _load_cached_token() -> Optional[str]:
    if not TOKEN_FILE.exists():
        return None
    try:
        data = json.loads(TOKEN_FILE.read_text())
        expires_at = datetime.fromisoformat(data["expires_at"])
        if datetime.utcnow() < expires_at - timedelta(minutes=5):
            return data["access_token"]
        logger.info("Cached Fyers token expired")
    except Exception as e:
        logger.warning(f"Could not load cached token: {e}")
    return None


def _save_token(access_token: str) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "access_token": access_token,
        "expires_at": (datetime.utcnow() + timedelta(hours=23)).isoformat(),
    }
    TOKEN_FILE.write_text(json.dumps(data))
    logger.info("Fyers access token saved")


# ── TOTP automated login ──────────────────────────────────────────────────────

def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def totp_login() -> str:
    """
    Fully automated Fyers login using TOTP + PIN.

    Requires FYERS_USER_ID, FYERS_PIN, FYERS_TOTP_KEY to be set.
    Raises RuntimeError with a descriptive message on any failure so the
    caller can decide whether to retry or fall back to manual auth.
    """
    if not all([settings.fyers_user_id, settings.fyers_pin, settings.fyers_totp_key]):
        raise RuntimeError(
            "TOTP auth requires FYERS_USER_ID, FYERS_PIN, and FYERS_TOTP_KEY in .env"
        )

    # Step 1: Initiate login — Fyers sends nothing to the user; this just opens a session
    r = requests.post(
        f"{_VAGATOR_BASE}/send_login_otp_v2",
        json={"fy_id": settings.fyers_user_id, "app_id": "0"},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 200:
        raise RuntimeError(f"send_login_otp_v2 failed: {data}")
    request_key = data["request_key"]
    logger.debug("TOTP step 1/4: login session opened")

    # Step 2: Submit TOTP code as the OTP
    totp_code = pyotp.TOTP(settings.fyers_totp_key).now()
    r = requests.post(
        f"{_VAGATOR_BASE}/verify_otp",
        json={"request_key": request_key, "otp": totp_code},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 200:
        raise RuntimeError(f"verify_otp (TOTP) failed: {data}")
    request_key = data["request_key"]
    logger.debug("TOTP step 2/4: TOTP verified")

    # Step 3: Submit PIN (SHA256-hashed)
    r = requests.post(
        f"{_VAGATOR_BASE}/verify_pin_v2",
        json={
            "request_key": request_key,
            "identity_type": "pin",
            "identifier": _sha256(settings.fyers_pin),
        },
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 200:
        raise RuntimeError(f"verify_pin_v2 failed: {data}")
    session_token = data["data"]["token"]
    logger.debug("TOTP step 3/4: PIN verified")

    # Step 4: Obtain auth_code from Fyers token endpoint
    app_id = settings.fyers_client_id.split("-")[0]
    r = requests.post(
        _TOKEN_URL,
        json={
            "fyers_id":      settings.fyers_user_id,
            "app_id":        app_id,
            "redirect_uri":  settings.fyers_redirect_uri,
            "appType":       "100",
            "code_challenge": "",
            "state":         "None",
            "scope":         "",
            "nonce":         "",
            "response_type": "code",
            "create_cookie": True,
        },
        headers={"Authorization": f"Bearer {session_token}"},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    auth_url = data.get("Url", "")
    qs = parse_qs(urlparse(auth_url).query)
    auth_code = (qs.get("auth_code") or qs.get("code") or [None])[0]
    if not auth_code:
        raise RuntimeError(f"Could not extract auth_code from redirect URL: {auth_url!r}")
    logger.debug("TOTP step 4/4: auth_code obtained")

    # Exchange auth_code for access_token (same as manual flow)
    access_token = exchange_auth_code(auth_code)
    logger.info("Fyers TOTP login successful — token valid for 23h")
    return access_token


# ── Manual OAuth2 (fallback) ──────────────────────────────────────────────────

def get_auth_url() -> str:
    """Generate Fyers OAuth2 login URL (manual fallback)."""
    session = fyersModel.SessionModel(
        client_id=settings.fyers_client_id,
        secret_key=settings.fyers_secret_key,
        redirect_uri=settings.fyers_redirect_uri,
        response_type="code",
        grant_type="authorization_code",
    )
    return session.generate_authcode()


def exchange_auth_code(auth_code: str) -> str:
    """Exchange authorization code for access token and persist it."""
    session = fyersModel.SessionModel(
        client_id=settings.fyers_client_id,
        secret_key=settings.fyers_secret_key,
        redirect_uri=settings.fyers_redirect_uri,
        response_type="code",
        grant_type="authorization_code",
    )
    session.set_token(auth_code)
    response = session.generate_token()
    if response.get("s") != "ok":
        raise RuntimeError(f"Token exchange failed: {response}")
    access_token = response["access_token"]
    _save_token(access_token)
    return access_token


# ── Client factory ────────────────────────────────────────────────────────────

def get_valid_token() -> str:
    """Return a valid access token, raising if not yet authenticated."""
    token = _load_cached_token()
    if token:
        return token
    raise RuntimeError(
        "No valid Fyers token. "
        "Set FYERS_USER_ID / FYERS_PIN / FYERS_TOTP_KEY for automated login, "
        "or visit http://localhost:8001/fyers/auth to authenticate manually."
    )


def get_fyers_client() -> fyersModel.FyersModel:
    """Return an authenticated Fyers API client."""
    token = get_valid_token()
    return fyersModel.FyersModel(
        client_id=settings.fyers_client_id,
        token=token,
        is_async=False,
        log_path="",
    )
