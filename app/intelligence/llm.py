# intelligence/llm.py
# Thin async wrapper around the OpenRouter /chat/completions endpoint.
# Used by all three intelligence modules so the HTTP boilerplate lives here.
#
# Two entry points:
#   llm_call()      → plain text completion
#   llm_tool_call() → structured output via OpenAI-style function/tool calling
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from config import OPENROUTER_BASE_URL, OPENROUTER_KEY, QA_MODEL

logger = logging.getLogger(__name__)

# Slightly higher cap for intelligence tasks which need more reasoning space
_DEFAULT_MAX_TOKENS = 2000
_TOOL_MAX_TOKENS    = 3000
_TIMEOUT            = 90   # seconds


async def llm_call(
    messages:    List[Dict],
    system:      str  = "",
    max_tokens:  int  = _DEFAULT_MAX_TOKENS,
    temperature: float = 0.2,
    model:       str  = QA_MODEL,
) -> str:
    """
    Plain completion — returns the model's text reply as a string.
    Raises RuntimeError on non-200 or missing content.
    """
    full_messages = []
    if system:
        full_messages.append({"role": "system", "content": system})
    full_messages.extend(messages)

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{OPENROUTER_BASE_URL.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "model":       model,
                "messages":    full_messages,
                "max_tokens":  max_tokens,
                "temperature": temperature,
            },
        )

    if resp.status_code != 200:
        raise RuntimeError(f"LLM error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    return data["choices"][0]["message"]["content"] or ""


async def llm_tool_call(
    messages:    List[Dict],
    tools:       List[Dict],
    tool_choice: str | Dict = "required",
    system:      str  = "",
    max_tokens:  int  = _TOOL_MAX_TOKENS,
    temperature: float = 0.1,   # lower = more deterministic structured output
    model:       str  = QA_MODEL,
) -> Optional[Dict[str, Any]]:
    """
    Tool-calling completion — returns the parsed arguments dict of the first
    tool call the model makes, or None if the model didn't call any tool.

    `tool_choice="required"` forces the model to always call a tool.
    Pass `tool_choice={"type":"function","function":{"name":"my_fn"}}` to
    force a specific tool.
    """
    full_messages = []
    if system:
        full_messages.append({"role": "system", "content": system})
    full_messages.extend(messages)

    payload = {
        "model":       model,
        "messages":    full_messages,
        "tools":       tools,
        "tool_choice": tool_choice,
        "max_tokens":  max_tokens,
        "temperature": temperature,
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{OPENROUTER_BASE_URL.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "Content-Type":  "application/json",
            },
            json=payload,
        )

    if resp.status_code != 200:
        raise RuntimeError(f"LLM tool-call error {resp.status_code}: {resp.text[:300]}")

    data    = resp.json()
    message = data["choices"][0]["message"]

    tool_calls = message.get("tool_calls") or []
    if not tool_calls:
        logger.warning("llm_tool_call: model returned no tool calls")
        return None

    raw_args = tool_calls[0].get("function", {}).get("arguments", "{}")
    try:
        return json.loads(raw_args)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse tool arguments: {e}\nRaw: {raw_args[:500]}")
        return None
