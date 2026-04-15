"""
Ollama HTTP client for local LLM inference.
Communicates with Ollama running on the host machine.
"""

import json
import logging
import re
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)


async def query_ollama(prompt: str) -> Optional[str]:
    """Send a prompt to Ollama and return raw response text."""
    url = f"{settings.ollama_base_url}/api/generate"
    payload = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.1,   # low temperature for deterministic JSON output
            "top_p": 0.9,
            "num_predict": 16384,  # thinking model burns CoT tokens before JSON output; 4096 was insufficient
        },
    }
    try:
        async with httpx.AsyncClient(timeout=settings.ollama_timeout) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            return data.get("response", "").strip()
    except httpx.ConnectError:
        logger.error(f"Cannot connect to Ollama at {settings.ollama_base_url}. Is it running?")
        return None
    except Exception as e:
        logger.exception(f"Ollama query failed: {e}")
        return None


async def check_ollama_health() -> bool:
    """Check if Ollama is reachable."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{settings.ollama_base_url}/api/tags")
            return resp.status_code == 200
    except Exception:
        return False
