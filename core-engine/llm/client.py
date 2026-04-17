"""
LLM client factory.

Usage:
    from llm.client import get_provider

    provider = get_provider()
    raw = await provider.query(prompt)
    ok  = await provider.health_check()
    print(provider.name)      # "Ollama (gemma4:latest)" or "Claude (claude-...)"
    print(provider.timeout)   # seconds

Provider is selected via LLM_PROVIDER env var ("ollama" or "claude").
The instance is a module-level singleton — constructed once on first call.
"""

import logging
from typing import Optional

from config import settings
from llm.providers.base import LLMProvider

logger = logging.getLogger(__name__)

_provider: Optional[LLMProvider] = None


def get_provider() -> LLMProvider:
    """Return the active LLM provider singleton."""
    global _provider
    if _provider is None:
        _provider = _build_provider(settings.llm_provider)
        logger.info(f"LLM provider initialised: {_provider.name}")
    return _provider


def _build_provider(name: str) -> LLMProvider:
    name = name.lower().strip()
    if name == "ollama":
        from llm.providers.ollama import OllamaProvider
        return OllamaProvider()
    if name == "claude":
        from llm.providers.claude import ClaudeProvider
        return ClaudeProvider()
    raise ValueError(
        f"Unknown LLM_PROVIDER '{name}'. Supported values: 'ollama', 'claude'"
    )


# ── Convenience shims (keep backtest_candles.py and any other callers working) ─

async def query_llm(prompt: str):
    """Query the active provider. Replaces query_ollama()."""
    return await get_provider().query(prompt)


async def check_llm_health() -> bool:
    """Health check for the active provider. Replaces check_ollama_health()."""
    return await get_provider().health_check()


# Legacy aliases — avoids breaking any code that still imports the old names
async def query_ollama(prompt: str):
    return await query_llm(prompt)


async def check_ollama_health() -> bool:
    return await check_llm_health()
