# qa/context.py
# Context window management — token counting, history windowing, rolling summary.
import logging
import os
from typing import List, Dict, Optional, Tuple

import httpx

from config import OPENROUTER_KEY, OPENROUTER_BASE_URL

logger = logging.getLogger(__name__)

# Context window config
MAX_HISTORY_TURNS       = int(os.getenv("MAX_HISTORY_TURNS",        "10"))
SUMMARY_TRIGGER_TURNS   = int(os.getenv("SUMMARY_TRIGGER_TURNS",    "8"))
MAX_CONTEXT_TOKENS      = int(os.getenv("MAX_CONTEXT_TOKENS",        "6000"))
SUMMARY_MODEL           = os.getenv("SUMMARY_MODEL", "openai/gpt-4o-mini")


def count_tokens_approx(text: str) -> int:
    """
    Approximate token count — 1 token ≈ 4 chars.
    Replace with tiktoken for exact counts.
    """
    return max(1, len(text) // 4)


def build_context_window(
    messages:       List[Dict],
    summary:        str,
    max_tokens:     int = MAX_CONTEXT_TOKENS,
) -> Tuple[List[Dict], int]:
    """
    Build context window from messages + rolling summary.
    Returns (windowed_messages, total_tokens).

    Strategy:
      1. Always include summary if present (compressed old history)
      2. Add most recent messages first until token budget exhausted
      3. Return in chronological order
    """
    budget  = max_tokens
    window  = []
    tokens  = 0

    # Summary counts against budget
    if summary:
        summary_tokens = count_tokens_approx(summary)
        if summary_tokens < budget:
            budget -= summary_tokens
            tokens += summary_tokens

    # Walk messages newest → oldest, add until budget runs out
    for msg in reversed(messages):
        msg_tokens = count_tokens_approx(msg.get("content", ""))
        if tokens + msg_tokens > budget:
            break
        window.insert(0, msg)
        tokens += msg_tokens

    return window, tokens


def should_summarize(
    messages:       List[Dict],
    summary_at_turn: int,
    current_turn:   int,
) -> bool:
    """Trigger summarization every SUMMARY_TRIGGER_TURNS new turns."""
    turns_since = current_turn - summary_at_turn
    return turns_since >= SUMMARY_TRIGGER_TURNS and len(messages) > SUMMARY_TRIGGER_TURNS


async def generate_summary(
    messages:    List[Dict],
    existing_summary: str = "",
) -> str:
    """
    Compress conversation history into a rolling summary via LLM.
    Called async AFTER response is sent — never blocks the user.
    """
    if not messages:
        return existing_summary

    history_text = "\n".join([
        f"{m['role'].upper()}: {m['content'][:500]}"
        for m in messages
    ])

    prompt = f"""Summarize this conversation history concisely (max 300 words).
Preserve: key questions asked, key answers given, documents referenced, decisions made.
Discard: filler, repetition, pleasantries.

{"Previous summary:\n" + existing_summary if existing_summary else ""}

Conversation:
{history_text}

Summary:"""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{OPENROUTER_BASE_URL.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_KEY}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":      SUMMARY_MODEL,
                    "messages":   [{"role": "user", "content": prompt}],
                    "max_tokens": 400,
                    "temperature": 0.3,
                },
            )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.warning(f"Summary generation failed: {e}")

    return existing_summary   # fall back to existing if LLM fails


def build_system_prompt(mode: str, summary: str, file_context: str = "") -> str:
    """Build the system prompt for the LLM based on QA mode."""
    base = """You are an expert document analyst. Answer questions based ONLY on the provided document context.

Rules:
- Be precise and cite specific documents when making claims
- If information is not in the provided context, say "This information is not available in the selected documents"
- Structure complex answers with clear sections
- For analytical questions, reason step by step
- Always indicate which document(s) support each claim"""

    if mode == "dataset":
        scope = "\nScope: You have access to the ENTIRE document corpus. Synthesize across all relevant documents."
    else:
        scope = "\nScope: You are restricted to the SELECTED documents only."

    history_ctx = f"\n\nConversation summary so far:\n{summary}" if summary else ""
    file_ctx    = f"\n\nDocument context:\n{file_context}" if file_context else ""

    return base + scope + history_ctx + file_ctx
