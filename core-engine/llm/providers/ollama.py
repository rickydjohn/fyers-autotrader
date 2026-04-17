"""
Ollama provider — local LLM inference via Ollama running on the host machine.

Concurrency: Ollama processes one request at a time; a second concurrent
request gets a 429. A module-level asyncio.Semaphore(1) serialises calls
so the scheduler's asyncio.gather() across symbols works without errors.
"""

import asyncio
import logging
from typing import Optional

import httpx

from config import settings
from llm.providers.base import LLMProvider

logger = logging.getLogger(__name__)

_semaphore = asyncio.Semaphore(1)   # Ollama is single-threaded


class OllamaProvider(LLMProvider):

    @property
    def name(self) -> str:
        return f"Ollama ({settings.ollama_model})"

    @property
    def timeout(self) -> int:
        return settings.ollama_timeout

    async def query(self, prompt: str) -> Optional[str]:
        url     = f"{settings.ollama_endpoint}/api/generate"
        payload = {
            "model":  settings.ollama_model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.1,    # deterministic JSON output
                "top_p":       0.9,
                "num_predict": 16384,  # thinking models burn CoT tokens before JSON
            },
        }
        async with _semaphore:          # serialise — no concurrent Ollama calls
            try:
                async with httpx.AsyncClient(timeout=settings.ollama_timeout) as client:
                    response = await client.post(url, json=payload)
                    response.raise_for_status()
                    return response.json().get("response", "").strip()
            except httpx.ConnectError:
                logger.error(
                    f"Cannot connect to Ollama at {settings.ollama_endpoint}. Is it running?"
                )
                return None
            except Exception as e:
                logger.error(f"Ollama query failed: {e}")
                return None

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{settings.ollama_endpoint}/api/tags")
                return resp.status_code == 200
        except Exception:
            return False
