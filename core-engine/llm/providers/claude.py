"""
Claude provider — Anthropic Messages API.

Handles concurrent calls natively (no semaphore needed).
Auth via x-api-key header.
Response extracted from content[0].text.
"""

import logging
from typing import Optional

import httpx

from config import settings
from llm.providers.base import LLMProvider

logger = logging.getLogger(__name__)

# Cumulative token counters — reset per process, read via get_token_stats()
_total_input_tokens: int = 0
_total_output_tokens: int = 0
_total_calls: int = 0


def get_token_stats() -> dict:
    return {
        "calls":         _total_calls,
        "input_tokens":  _total_input_tokens,
        "output_tokens": _total_output_tokens,
        "total_tokens":  _total_input_tokens + _total_output_tokens,
    }


def reset_token_stats() -> None:
    global _total_input_tokens, _total_output_tokens, _total_calls
    _total_input_tokens = _total_output_tokens = _total_calls = 0


class ClaudeProvider(LLMProvider):

    @property
    def name(self) -> str:
        return f"Claude ({settings.claude_model})"

    @property
    def timeout(self) -> int:
        return settings.claude_timeout

    async def query(self, prompt: str) -> Optional[str]:
        if not settings.claude_api_key:
            logger.error("CLAUDE_API_KEY is not set — cannot query Claude")
            return None

        url = f"{settings.claude_endpoint}/v1/messages"
        headers = {
            "x-api-key":         settings.claude_api_key,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        }
        payload = {
            "model":      settings.claude_model,
            "max_tokens": 16384,
            "messages": [
                {"role": "user", "content": prompt}
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=settings.claude_timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
                # Track token usage
                global _total_input_tokens, _total_output_tokens, _total_calls
                usage = data.get("usage", {})
                _total_input_tokens  += usage.get("input_tokens", 0)
                _total_output_tokens += usage.get("output_tokens", 0)
                _total_calls         += 1
                logger.debug(
                    f"Claude tokens — in:{usage.get('input_tokens',0)} "
                    f"out:{usage.get('output_tokens',0)} "
                    f"total_so_far:{_total_input_tokens+_total_output_tokens}"
                )
                return data["content"][0]["text"].strip()
        except httpx.ConnectError:
            logger.error(
                f"Cannot connect to Claude endpoint at {settings.claude_endpoint}"
            )
            return None
        except httpx.HTTPStatusError as e:
            logger.error(
                f"Claude API error {e.response.status_code}: {e.response.text[:200]}"
            )
            return None
        except Exception as e:
            logger.error(f"Claude query failed: {e}")
            return None

    async def health_check(self) -> bool:
        """Verify endpoint is reachable and API key is accepted."""
        if not settings.claude_api_key:
            return False
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    f"{settings.claude_endpoint}/v1/models",
                    headers={"x-api-key": settings.claude_api_key,
                             "anthropic-version": "2023-06-01"},
                )
                return resp.status_code == 200
        except Exception:
            return False
