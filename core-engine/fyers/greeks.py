"""
Fetches live option quote and computes Greeks via Black-Scholes.

Fyers v3 quotes endpoint does not return Greeks (delta, theta, vega, gamma, iv)
for option symbols — the fields are absent from the response regardless of plan.
This module:
  1. Fetches the option LTP and underlying spot from Fyers.
  2. Derives Implied Volatility (IV) by inverting Black-Scholes using
     Newton-Raphson iteration.
  3. Computes delta, gamma, theta, vega from the IV.

All calculations use pure Python (math module only — no scipy dependency).

Risk-free rate: India 10yr G-Sec yield approximation (6.5% p.a.).
"""

import logging
import math
from datetime import datetime
from typing import Optional

import pytz

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# India 10yr G-Sec yield (used as risk-free rate for Black-Scholes)
_RISK_FREE_RATE = 0.065


# ── Normal distribution helpers ──────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    """Standard normal CDF via math.erfc."""
    return 0.5 * math.erfc(-x / math.sqrt(2))


def _norm_pdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


# ── Black-Scholes price and vega ─────────────────────────────────────────────

def _bs_price(S: float, K: float, T: float, r: float, sigma: float, opt_type: str) -> float:
    """
    Black-Scholes European option price.
      S        — spot price
      K        — strike
      T        — time to expiry in years
      r        — risk-free rate
      sigma    — volatility (annualised, decimal)
      opt_type — 'CE' or 'PE'
    """
    if T <= 0 or sigma <= 0:
        # At/past expiry: intrinsic value only
        intrinsic = max(S - K, 0) if opt_type == "CE" else max(K - S, 0)
        return float(intrinsic)

    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    if opt_type == "CE":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    else:
        return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def _bs_vega(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """BS vega = S * N'(d1) * sqrt(T). Same for calls and puts."""
    if T <= 0 or sigma <= 0:
        return 0.0
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
    return S * _norm_pdf(d1) * sqrt_T


# ── Implied Volatility via Newton-Raphson ────────────────────────────────────

def _implied_vol(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    opt_type: str,
    max_iter: int = 100,
    tol: float = 1e-6,
) -> Optional[float]:
    """
    Compute implied volatility by inverting Black-Scholes using Newton-Raphson.
    Returns None if convergence fails (deep ITM/OTM, near-zero premium).
    """
    if market_price <= 0 or T <= 0:
        return None

    # Initial guess: simplified Brenner-Subrahmanyam approximation
    sigma = math.sqrt(2 * math.pi / T) * market_price / S

    # Clamp to sane range
    sigma = max(0.01, min(sigma, 10.0))

    for _ in range(max_iter):
        price = _bs_price(S, K, T, r, sigma, opt_type)
        vega  = _bs_vega(S, K, T, r, sigma)

        diff = market_price - price
        if abs(diff) < tol:
            return sigma

        if abs(vega) < 1e-10:
            # Vega too small — can't converge (deep ITM/OTM or near-expiry)
            return None

        sigma += diff / vega
        sigma = max(0.001, min(sigma, 20.0))  # keep in valid range

    # Final check: accept if close enough after max_iter
    price = _bs_price(S, K, T, r, sigma, opt_type)
    if abs(market_price - price) < 0.50:  # within ₹0.50
        return sigma

    return None


# ── Full Greeks ───────────────────────────────────────────────────────────────

def _compute_greeks(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    opt_type: str,
) -> dict:
    """
    Compute delta, gamma, theta, vega given IV.
    Theta is returned as daily decay (divided by 365).
    """
    if T <= 0 or sigma <= 0:
        delta = 1.0 if opt_type == "CE" else -1.0
        return {"delta": delta, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    nd1 = _norm_pdf(d1)
    discount = math.exp(-r * T)

    if opt_type == "CE":
        delta = _norm_cdf(d1)
        theta_annual = (
            -(S * nd1 * sigma) / (2 * sqrt_T)
            - r * K * discount * _norm_cdf(d2)
        )
    else:
        delta = _norm_cdf(d1) - 1.0
        theta_annual = (
            -(S * nd1 * sigma) / (2 * sqrt_T)
            + r * K * discount * _norm_cdf(-d2)
        )

    gamma = nd1 / (S * sigma * sqrt_T)
    vega  = S * nd1 * sqrt_T / 100   # per 1% move in IV (conventional)
    theta = theta_annual / 365        # per calendar day

    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta, 4),
        "vega":  round(vega, 4),
    }


# ── Time to expiry helper ─────────────────────────────────────────────────────

def _years_to_expiry(expiry_str: str) -> float:
    """
    Compute time to expiry in years from expiry date string.
    Expects format: 'YYYY-MM-DD' (ISO) or '21-04-2026' (Fyers DD-MM-YYYY).
    Expiry is treated as market close (15:30 IST) of that date.
    """
    try:
        try:
            expiry = datetime.strptime(expiry_str, "%Y-%m-%d")
        except ValueError:
            expiry = datetime.strptime(expiry_str, "%d-%m-%Y")
        # Treat expiry as 15:30 IST
        expiry = IST.localize(expiry.replace(hour=15, minute=30))
        now = datetime.now(IST)
        diff = (expiry - now).total_seconds()
        return max(diff / (365 * 24 * 3600), 0.0)
    except Exception:
        return 0.0


# ── Public API ────────────────────────────────────────────────────────────────

def get_option_quote_with_greeks(option_symbol: str) -> Optional[dict]:
    """
    Fetch live option LTP from Fyers and compute Greeks via Black-Scholes.

    Returns a dict with keys:
        symbol, ltp, iv, delta, theta, vega, gamma

    iv is annualised volatility as a percentage (e.g. 18.5 means 18.5%).
    Returns None on API error or if LTP is zero/missing.
    """
    from fyers.auth import get_fyers_client
    fyers = get_fyers_client()
    try:
        # ── 1. Fetch option LTP ───────────────────────────────────────────────
        response = fyers.quotes(data={"symbols": option_symbol})
        if response.get("s") != "ok":
            logger.debug(f"Fyers quote error for {option_symbol}: {response}")
            return None

        v = response.get("d", [{}])[0].get("v", {})
        ltp = float(v.get("lp", 0) or 0)
        if not ltp:
            return None

        # ── 2. Parse option symbol for strike, type, expiry ──────────────────
        # Fyers format: NSE:NIFTY2642124250CE
        # strip exchange prefix
        sym = option_symbol.split(":")[-1]  # e.g. NIFTY2642124250CE
        opt_type = "CE" if sym.endswith("CE") else "PE"
        # Extract strike: last 5 digits before CE/PE suffix
        try:
            K = float(sym[-7:-2])  # e.g. '24250' from '...24250CE'
        except ValueError:
            logger.debug(f"Cannot parse strike from {option_symbol}")
            return _fallback_result(option_symbol, ltp)

        # ── 3. Fetch underlying spot ──────────────────────────────────────────
        # Derive underlying from option symbol (NIFTY → NSE:NIFTY50-INDEX)
        if "NIFTY" in sym and "BANK" not in sym:
            underlying = "NSE:NIFTY50-INDEX"
        elif "BANKNIFTY" in sym or "NIFTYBANK" in sym:
            underlying = "NSE:NIFTYBANK-INDEX"
        else:
            logger.debug(f"Unknown underlying for {option_symbol}")
            return _fallback_result(option_symbol, ltp)

        spot_resp = fyers.quotes(data={"symbols": underlying})
        if spot_resp.get("s") != "ok":
            return _fallback_result(option_symbol, ltp)
        S = float(spot_resp["d"][0]["v"].get("lp", 0) or 0)
        if not S:
            return _fallback_result(option_symbol, ltp)

        # ── 4. Time to expiry from option chain (weekly expiry) ───────────────
        # Parse expiry from symbol: NIFTY26421 → year=2026, week=26421
        # Fyers weekly option format: <name><YY><MM><DD><strike><type>
        # e.g. NIFTY2642124250CE → YY=26, MM=04, DD=21 → 2026-04-21
        try:
            # Find where the numeric run starts
            name_end = 0
            for i, ch in enumerate(sym):
                if ch.isdigit():
                    name_end = i
                    break
            date_part = sym[name_end:name_end + 6]  # '264212' → YY=26 MM=42 DD=12?
            # Actually Fyers uses: NIFTY + YY + MM(2digit) + DD(2digit)
            yy = int(date_part[0:2])
            mm = int(date_part[2:4])
            dd = int(date_part[4:6])
            expiry_str = f"20{yy:02d}-{mm:02d}-{dd:02d}"
            T = _years_to_expiry(expiry_str)
        except Exception:
            # Fallback: assume 7 days to expiry
            T = 7 / 365

        if T <= 0:
            T = 1 / 365  # treat as expiry-day: 1 day

        # ── 5. Implied Volatility ─────────────────────────────────────────────
        iv_decimal = _implied_vol(ltp, S, K, T, _RISK_FREE_RATE, opt_type)

        if iv_decimal is None or iv_decimal <= 0:
            logger.debug(
                f"IV computation failed for {option_symbol} "
                f"(ltp={ltp}, S={S}, K={K}, T={T:.4f}) — returning 0 Greeks"
            )
            return _fallback_result(option_symbol, ltp)

        iv_pct = round(iv_decimal * 100, 2)   # e.g. 0.185 → 18.5

        # ── 6. Greeks ─────────────────────────────────────────────────────────
        greeks = _compute_greeks(S, K, T, _RISK_FREE_RATE, iv_decimal, opt_type)

        return {
            "symbol": option_symbol,
            "ltp":    ltp,
            "iv":     iv_pct,
            **greeks,
        }

    except Exception as e:
        logger.debug(f"Greeks fetch failed for {option_symbol}: {e}")
        return None


def _fallback_result(option_symbol: str, ltp: float) -> dict:
    """Return LTP with zeroed Greeks when BS computation is not possible."""
    return {
        "symbol": option_symbol,
        "ltp":    ltp,
        "iv":     0.0,
        "delta":  0.0,
        "gamma":  0.0,
        "theta":  0.0,
        "vega":   0.0,
    }
