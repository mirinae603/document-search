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

from config import OPENROUTER_BASE_URL, OPENROUTER_KEY, QA_MODEL, LLM_PROVIDER, AZURE_OPENAI_KEY , AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_VERSION, AZURE_DEPLOYMENT

logger = logging.getLogger(__name__)

# Slightly higher cap for intelligence tasks which need more reasoning space
_DEFAULT_MAX_TOKENS = 2000
_TOOL_MAX_TOKENS    = 3000
_TIMEOUT            = 90   # seconds


def _openai_compatible_url() -> str:
    """Returns the chat/completions URL for OpenRouter or Azure."""
    provider = LLM_PROVIDER
    if provider == "azure":
        base = AZURE_OPENAI_ENDPOINT.rstrip("/")
        # Always use AZURE_DEPLOYMENT — the caller's model string is irrelevant for Azure
        #return f"{base}/openai/deployments/{AZURE_DEPLOYMENT}/chat/completions?api-version={AZURE_OPENAI_API_VERSION}"
        return 
    # openrouter
    return f"{OPENROUTER_BASE_URL.rstrip('/')}/chat/completions"
 
 
def _openai_compatible_headers() -> dict:
    provider = LLM_PROVIDER
    if provider == "azure":
        return {"api-key": AZURE_OPENAI_KEY, "Content-Type": "application/json"}
    return {"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"}
 


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

    payload: Dict[str, Any] = {
        "messages":    full_messages,
        "max_tokens":  max_tokens,
        "temperature": temperature,
    }
    # Azure uses deployment name in the URL, OpenRouter needs model in body
    if LLM_PROVIDER.lower() != "azure":
        payload["model"] = model

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            _openai_compatible_url(),
            headers=_openai_compatible_headers(),
            json=payload,
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
        "messages":    full_messages,
        "tools":       tools,
        "tool_choice": tool_choice,
        "max_tokens":  max_tokens,
        "temperature": temperature,
    }
    if LLM_PROVIDER.lower() != "azure":
        payload["model"] = model
    url_to_call = _openai_compatible_url()
    logger.error(f"DEBUG URL: '{url_to_call}'") # Add this line
    logger.error(f"DEBUG ENDPOINT: '{AZURE_OPENAI_ENDPOINT}'")
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            _openai_compatible_url(),
            headers=_openai_compatible_headers(),
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
