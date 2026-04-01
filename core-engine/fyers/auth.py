"""
Fyers OAuth2 authentication manager.

Flow:
  1. GET /fyers/auth  → redirects user to Fyers login page
  2. User logs in → Fyers redirects to /fyers/callback?auth_code=...
  3. We exchange auth_code for access_token and persist it
  4. APScheduler refreshes the token every 23 hours
"""

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
from fyers_apiv3 import fyersModel
from config import settings

logger = logging.getLogger(__name__)

TOKEN_FILE = Path(settings.token_path)


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


def get_auth_url() -> str:
    """Generate Fyers OAuth2 login URL."""
    session = fyersModel.SessionModel(
        client_id=settings.fyers_client_id,
        secret_key=settings.fyers_secret_key,
        redirect_uri=settings.fyers_redirect_uri,
        response_type="code",
        grant_type="authorization_code",
    )
    return session.generate_authcode()


def exchange_auth_code(auth_code: str) -> str:
    """Exchange authorization code for access token."""
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


def get_valid_token() -> str:
    """Return a valid access token, raising if not yet authenticated."""
    token = _load_cached_token()
    if token:
        return token
    raise RuntimeError(
        "No valid Fyers token. Visit http://localhost:8001/fyers/auth to authenticate."
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
