"""
LLM consult — thin client over core-engine's /llm/complete.

equity-engine builds the equity-specific prompt; core-engine owns the model
transport/config. Includes a robust JSON extractor since the model may wrap its
answer in prose or markdown fences.
"""

import json
import logging
import re
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)


def complete(prompt: str, timeout: float = 180.0) -> str:
    """Return the model's raw text completion ("" on failure)."""
    try:
        r = httpx.post(
            f"{settings.core_engine_url}/llm/complete",
            json={"prompt": prompt},
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json().get("text", "") or ""
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("LLM complete failed: %s", e)
        return ""


def parse_json(text: str) -> Optional[dict]:
    """Extract the first JSON object from an LLM response (handles ```json fences)."""
    if not text:
        return None
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None
