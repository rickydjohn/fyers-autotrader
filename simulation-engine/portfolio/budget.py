"""
Virtual budget management for simulation.
State is persisted in Redis so it survives restarts.
"""

import json
import logging

import redis.asyncio as aioredis

from config import settings
from models.schemas import BudgetState

logger = logging.getLogger(__name__)

BUDGET_KEY = "budget:state"


async def initialize_budget(redis_client: aioredis.Redis) -> BudgetState:
    """Initialize budget if not already set."""
    existing = await redis_client.get(BUDGET_KEY)
    if existing:
        data = json.loads(existing)
        return BudgetState(**data)
    state = BudgetState(
        initial=settings.initial_budget,
        cash=settings.initial_budget,
        invested=0.0,
    )
    await save_budget(redis_client, state)
    logger.info(f"Budget initialized: ₹{settings.initial_budget:,.0f}")
    return state


async def load_budget(redis_client: aioredis.Redis) -> BudgetState:
    data = await redis_client.get(BUDGET_KEY)
    if not data:
        return await initialize_budget(redis_client)
    return BudgetState(**json.loads(data))


async def save_budget(redis_client: aioredis.Redis, state: BudgetState) -> None:
    await redis_client.set(BUDGET_KEY, json.dumps(state.model_dump()))


async def allocate(redis_client: aioredis.Redis, invest_amount: float, fee: float = 0.0) -> bool:
    """
    Deduct invest_amount + fee from cash; add only invest_amount to invested.
    Fee (commission) is a sunk cost — it leaves cash but never enters invested,
    so it doesn't inflate the invested balance after the position closes.
    Returns False if insufficient funds.
    """
    state = await load_budget(redis_client)
    total_needed = invest_amount + fee
    if state.cash < total_needed:
        logger.warning(f"Insufficient cash: need ₹{total_needed:.0f}, have ₹{state.cash:.0f}")
        return False
    state.cash -= total_needed
    state.invested += invest_amount
    await save_budget(redis_client, state)
    return True


async def release(redis_client: aioredis.Redis, invested_amount: float, pnl: float) -> None:
    """Return invested amount + P&L to cash."""
    state = await load_budget(redis_client)
    state.invested = max(0.0, state.invested - invested_amount)
    state.cash += invested_amount + pnl
    await save_budget(redis_client, state)


async def get_max_position_value(redis_client: aioredis.Redis) -> float:
    """Maximum value for a single position based on config."""
    state = await load_budget(redis_client)
    return state.initial * (settings.max_position_size_pct / 100)
