"""
Abstract base class for LLM providers.

Each provider owns its own:
  - HTTP call shape and auth
  - Response extraction
  - Timeout / retry / concurrency behaviour
  - Health check

decision.py and backtest_candles.py only ever call provider.query(prompt)
and provider.health_check() — they never touch provider-specific details.
"""

from abc import ABC, abstractmethod
from typing import Optional


class LLMProvider(ABC):

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable provider name used in log lines."""
        ...

    @property
    @abstractmethod
    def timeout(self) -> int:
        """Seconds before a query is considered timed out."""
        ...

    @abstractmethod
    async def query(self, prompt: str) -> Optional[str]:
        """
        Send prompt to the LLM and return raw response text.
        Returns None on any failure — callers fall back to HOLD.
        """
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True if the provider endpoint is reachable."""
        ...
