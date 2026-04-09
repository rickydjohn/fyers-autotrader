"""
Slack trade notifications — fire-and-forget async webhooks.

Sends a formatted message to a configured Slack incoming webhook when a
trade is opened or closed.  All calls are wrapped in asyncio.create_task()
so slow network calls never block the trade execution path.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

import httpx
import pytz

from config import settings

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# ── helpers ───────────────────────────────────────────────────────────────────

def _fmt_inr(value: float) -> str:
    return f"₹{value:,.2f}"


def _fmt_duration(entry: datetime, exit_dt: datetime) -> str:
    secs = int((exit_dt - entry).total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s"
    h = secs // 3600
    m = (secs % 3600) // 60
    return f"{h}h {m}m"


def _truncate(text: str, limit: int = 180) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _mode_label(mode: str) -> str:
    return "🔴 *LIVE*" if mode == "live" else "🔵 *SIMULATION*"


def _direction_label(side: str, option_type: Optional[str]) -> str:
    arrow = "🔼" if side == "BUY" else "🔽"
    suffix = f" {option_type}" if option_type else ""
    return f"{arrow} {side}{suffix}"


# ── payload builders ─────────────────────────────────────────────────────────

def _open_payload(
    mode: str,
    symbol: str,
    side: str,
    entry_price: float,
    quantity: int,
    stop_loss: float,
    target: float,
    option_symbol: Optional[str],
    option_strike: Optional[int],
    option_type: Optional[str],
    option_expiry: Optional[str],
    reasoning: str,
    entry_time: datetime,
    day_type: Optional[str],
) -> dict:
    index_name = symbol.replace("NSE:", "").replace("-INDEX", "")
    opt_name = option_symbol.replace("NSE:", "") if option_symbol else "—"
    total_cost = entry_price * quantity
    direction = _direction_label(side, option_type)
    time_str = entry_time.strftime("%H:%M IST · %d %b %Y")
    strike_str = f"₹{option_strike:,} {option_type}" if option_strike else "—"
    expiry_str = option_expiry or "—"
    mode_str = "🔴 LIVE" if mode == "live" else "🔵 SIM"

    lines = [
        f"*Mode:* {mode_str}   *Direction:* {direction}",
        f"*Index:* {index_name}   *Option:* {opt_name}",
        f"*Strike:* {strike_str}   *Expiry:* {expiry_str}",
        f"*Entry:* {_fmt_inr(entry_price)}   *Qty:* {quantity} lots   *Total:* {_fmt_inr(total_cost)}",
        f"*Stop Loss:* {_fmt_inr(stop_loss)}   *Target:* {_fmt_inr(target)}   *Day:* {day_type or 'TRENDING'}",
    ]

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "🟢 Trade Opened"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
    ]
    if reasoning:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"💡 _{_truncate(reasoning)}_"}],
        })
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"🕐 {time_str}"}],
    })

    return {"attachments": [{"color": "#36a64f", "blocks": blocks}]}


def _close_payload(
    mode: str,
    symbol: str,
    side: str,
    entry_price: float,
    exit_price: float,
    quantity: int,
    pnl: float,
    pnl_pct: float,
    commission: float,
    exit_reason: str,
    entry_time: datetime,
    exit_time: datetime,
    option_symbol: Optional[str],
    option_strike: Optional[int],
    option_type: Optional[str],
) -> dict:
    index_name = symbol.replace("NSE:", "").replace("-INDEX", "")
    opt_name = option_symbol.replace("NSE:", "") if option_symbol else "—"
    is_profit = pnl >= 0
    sign = "+" if is_profit else ""
    color = "#36a64f" if is_profit else "#e01e5a"
    emoji = "✅" if is_profit else "❌"
    direction = _direction_label(side, option_type)
    duration = _fmt_duration(entry_time, exit_time)
    time_str = exit_time.strftime("%H:%M IST · %d %b %Y")
    strike_str = f"₹{option_strike:,} {option_type}" if option_strike else "—"
    mode_str = "🔴 LIVE" if mode == "live" else "🔵 SIM"

    lines = [
        f"*Mode:* {mode_str}   *Exit Reason:* `{exit_reason or 'CLOSED'}`",
        f"*Index:* {index_name}   *Option:* {opt_name}",
        f"*Direction:* {direction}   *Strike:* {strike_str}",
        f"*Entry:* {_fmt_inr(entry_price)}   *Exit:* {_fmt_inr(exit_price)}   *Qty:* {quantity} lots",
        f"*Net P&L:* *{sign}{_fmt_inr(pnl)}*   *P&L %:* *{sign}{pnl_pct:.2f}%*",
        f"*Duration:* {duration}   *Commission:* {_fmt_inr(commission)}",
    ]

    header_text = f"{emoji} Trade Closed  {sign}{_fmt_inr(pnl)}  ({sign}{pnl_pct:.2f}%)"

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": header_text}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"🕐 {time_str}"}]},
    ]

    return {"attachments": [{"color": color, "blocks": blocks}]}


# ── send helper ───────────────────────────────────────────────────────────────

async def _post(payload: dict) -> None:
    url = settings.slack_webhook_url
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(url, json=payload)
            if r.status_code != 200:
                logger.debug(f"Slack webhook returned {r.status_code}: {r.text[:120]}")
    except Exception as e:
        logger.debug(f"Slack notification failed: {e}")


def _fire(coro) -> None:
    """Schedule coroutine as a background task — never blocks the caller."""
    try:
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        pass  # no running loop (unit tests) — skip


# ── public API ────────────────────────────────────────────────────────────────

def notify_trade_opened(
    mode: str,
    symbol: str,
    side: str,
    entry_price: float,
    quantity: int,
    stop_loss: float,
    target: float,
    entry_time: datetime,
    option_symbol: Optional[str] = None,
    option_strike: Optional[int] = None,
    option_type: Optional[str] = None,
    option_expiry: Optional[str] = None,
    reasoning: str = "",
    day_type: Optional[str] = None,
) -> None:
    payload = _open_payload(
        mode=mode, symbol=symbol, side=side,
        entry_price=entry_price, quantity=quantity,
        stop_loss=stop_loss, target=target,
        option_symbol=option_symbol, option_strike=option_strike,
        option_type=option_type, option_expiry=option_expiry,
        reasoning=reasoning, entry_time=entry_time, day_type=day_type,
    )
    _fire(_post(payload))


def notify_trade_closed(
    mode: str,
    symbol: str,
    side: str,
    entry_price: float,
    exit_price: float,
    quantity: int,
    pnl: float,
    pnl_pct: float,
    commission: float,
    exit_reason: str,
    entry_time: datetime,
    exit_time: datetime,
    option_symbol: Optional[str] = None,
    option_strike: Optional[int] = None,
    option_type: Optional[str] = None,
) -> None:
    payload = _close_payload(
        mode=mode, symbol=symbol, side=side,
        entry_price=entry_price, exit_price=exit_price,
        quantity=quantity, pnl=pnl, pnl_pct=pnl_pct,
        commission=commission, exit_reason=exit_reason,
        entry_time=entry_time, exit_time=exit_time,
        option_symbol=option_symbol, option_strike=option_strike,
        option_type=option_type,
    )
    _fire(_post(payload))
