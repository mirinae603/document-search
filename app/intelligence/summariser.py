# intelligence/summariser.py
# Generates human-readable summaries of indexed Outlook emails and Teams
# channel conversations, grouped by thread / channel.
#
# How it works:
#   1. Pull chunks from LanceDB filtered by platform ("outlook" | "teams")
#   2. Group chunks by their thread_id (email) or channel/section (teams)
#   3. Build a compact text representation of each group (capped at token budget)
#   4. One LLM call per group → {summary, key_points, tone}
#   5. Roll everything into a single structured response
#
# The summariser does NOT use vector search — it does a full filtered scan
# because summarisation needs breadth, not relevance ranking.
from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from intelligence.llm import llm_call

logger = logging.getLogger(__name__)

# How many characters of chunk text to include per group before truncating
_MAX_CHARS_PER_GROUP  = 6000
# How many groups to include in a single summary pass before rolling up
_MAX_GROUPS           = 20
# Summary LLM token budget
_SUMMARY_MAX_TOKENS   = 1200


# ── Public API ────────────────────────────────────────────────────────────────

async def summarise_outlook(store, hours: int = 24) -> dict:
    """
    Digest of all indexed Outlook email threads received in the last `hours`.

    Returns:
    {
      "platform":      "outlook",
      "period_hours":  24,
      "generated_at":  "<iso>",
      "thread_count":  N,
      "threads": [
        {
          "thread_id":    "...",
          "subject":      "Email: Q3 Budget",
          "participants": ["alice@...", "bob@..."],
          "message_count": 4,
          "latest_at":    "<iso>",
          "summary":      "...",
          "key_points":   ["...", "..."],
          "action_items": ["...", "..."],
        }, ...
      ],
      "overall_digest": "One paragraph covering all threads."
    }
    """
    cutoff = _cutoff_iso(hours)
    where  = f"platform = 'outlook' "
    rows   = _scan(store, where, limit=500)

    if not rows:
        return _empty("outlook", hours, "No Outlook emails indexed in this period.")

    groups = _group_by_thread(rows)
    logger.info(f"summarise_outlook: {len(rows)} chunks → {len(groups)} threads")

    thread_summaries = []
    for thread_id, thread_rows in list(groups.items())[:_MAX_GROUPS]:
        summary = await _summarise_group(
            group_rows   = thread_rows,
            platform     = "outlook",
            group_label  = _label(thread_rows, "channel_name"),
            extra_prompt = (
                "This is an email thread. Focus on: the main topic, any decisions made, "
                "action items assigned, and deadlines mentioned."
            ),
        )
        thread_summaries.append({
            "thread_id":    thread_id,
            "subject":      _label(thread_rows, "channel_name"),
            "participants": _participants(thread_rows),
            "message_count": len(thread_rows),
            "latest_at":    _latest_at(thread_rows),
            **summary,
        })

    # Sort newest first
    thread_summaries.sort(key=lambda t: t.get("latest_at", ""), reverse=True)

    overall = await _overall_digest(thread_summaries, "Outlook email digest")

    return {
        "platform":       "outlook",
        "period_hours":   hours,
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "thread_count":   len(thread_summaries),
        "threads":        thread_summaries,
        "overall_digest": overall,
    }


async def summarise_teams(store, channel_filter: Optional[str] = None, hours: Optional[int] = None) -> dict:
    """
    Summary of indexed Teams conversations, grouped by channel.

    `channel_filter` — if set, only summarise channels whose name contains this string.
    `hours`          — if set, only include messages from the last N hours.

    Returns the same shape as summarise_outlook but with `channels` instead of `threads`.
    """
    parts = ["platform = 'teams'"]
    if hours:
        parts.append(f"sent_at >= '{_cutoff_iso(hours)}'")
    where = " AND ".join(parts)

    rows = _scan(store, where, limit=500)

    if not rows:
        return _empty("teams", hours, "No Teams messages indexed.")

    groups = _group_by_channel(rows, channel_filter)
    logger.info(f"summarise_teams: {len(rows)} chunks → {len(groups)} channels")

    channel_summaries = []
    for channel_name, ch_rows in list(groups.items())[:_MAX_GROUPS]:
        summary = await _summarise_group(
            group_rows   = ch_rows,
            platform     = "teams",
            group_label  = channel_name,
            extra_prompt = (
                "This is a Microsoft Teams channel. Focus on: main topics discussed, "
                "any decisions or agreements reached, action items assigned to specific people, "
                "and any blockers or open questions raised."
            ),
        )
        channel_summaries.append({
            "channel":       channel_name,
            "participants":  _participants(ch_rows),
            "message_count": len(ch_rows),
            "latest_at":     _latest_at(ch_rows),
            **summary,
        })

    channel_summaries.sort(key=lambda c: c.get("latest_at", ""), reverse=True)

    overall = await _overall_digest(channel_summaries, "Teams activity digest")

    return {
        "platform":       "teams",
        "period_hours":   hours,
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "channel_count":  len(channel_summaries),
        "channels":       channel_summaries,
        "overall_digest": overall,
    }


# ── LLM helpers ───────────────────────────────────────────────────────────────

async def _summarise_group(
    group_rows:   List[dict],
    platform:     str,
    group_label:  str,
    extra_prompt: str,
) -> dict:
    """Summarise one thread / channel group. Returns {summary, key_points, action_items}."""
    text = _build_group_text(group_rows)
    if not text.strip():
        return {"summary": "No content.", "key_points": [], "action_items": []}

    system = (
        "You are an intelligent assistant that summarises workplace communications. "
        "Be concise, neutral, and factual. Use bullet points only inside key_points and action_items.\n\n"
        f"{extra_prompt}\n\n"
        "Respond in this exact JSON format (no markdown fences):\n"
        '{"summary": "1-3 sentence prose summary", '
        '"key_points": ["point 1", "point 2"], '
        '"action_items": ["task 1 → owner", "task 2 → owner"]}'
    )

    user_prompt = (
        f"Communication from: {group_label}\n\n"
        f"Content:\n{text}"
    )

    try:
        raw = await llm_call(
            messages    = [{"role": "user", "content": user_prompt}],
            system      = system,
            max_tokens  = _SUMMARY_MAX_TOKENS,
            temperature = 0.2,
        )
        # Parse the JSON the model returns
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        import json
        parsed = json.loads(cleaned)
        return {
            "summary":      parsed.get("summary", ""),
            "key_points":   parsed.get("key_points", []),
            "action_items": parsed.get("action_items", []),
        }
    except Exception as e:
        logger.warning(f"_summarise_group failed for '{group_label}': {e}")
        # Fallback: return first 300 chars of text
        return {
            "summary":      text[:300].strip() + "…",
            "key_points":   [],
            "action_items": [],
        }


async def _overall_digest(summaries: List[dict], label: str) -> str:
    """Roll up all individual summaries into one overview paragraph."""
    if not summaries:
        return "Nothing to summarise."

    bullets = []
    for s in summaries[:_MAX_GROUPS]:
        name    = s.get("channel") or s.get("subject", "Thread")
        summary = s.get("summary", "")
        if summary:
            bullets.append(f"- {name}: {summary}")

    combined = "\n".join(bullets)
    if not combined.strip():
        return "Nothing to summarise."

    try:
        return await llm_call(
            messages = [{"role": "user", "content":
                f"Write a single paragraph that gives an executive-level overview "
                f"of the following {label}. Be concise (3-5 sentences max).\n\n{combined}"}],
            max_tokens  = 300,
            temperature = 0.3,
        )
    except Exception as e:
        logger.warning(f"_overall_digest failed: {e}")
        return combined[:500]


# ── LanceDB scan helpers ──────────────────────────────────────────────────────

def _scan(store, where: str, limit: int = 500) -> List[dict]:
    """Full filtered table scan — no vector search."""
    try:
        return (
            store.chunks
            .search()
            .where(where, prefilter=True)
            .limit(limit)
            .to_list()
        )
    except Exception as e:
        logger.error(f"LanceDB scan failed (where={where!r}): {e}")
        return []


def _group_by_thread(rows: List[dict]) -> Dict[str, List[dict]]:
    """Group Outlook rows by thread_id."""
    groups: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        key = r.get("thread_id") or r.get("file_id", "unknown")
        groups[key].append(r)
    return dict(groups)


def _group_by_channel(rows: List[dict], channel_filter: Optional[str]) -> Dict[str, List[dict]]:
    """Group Teams rows by section_heading (= channel name)."""
    groups: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        channel = (r.get("section_heading") or r.get("channel_name") or "General").strip()
        if channel_filter and channel_filter.lower() not in channel.lower():
            continue
        groups[channel].append(r)
    return dict(groups)


# ── Text builders ─────────────────────────────────────────────────────────────

def _build_group_text(rows: List[dict]) -> str:
    """
    Concatenate chunk texts for a group, capped at _MAX_CHARS_PER_GROUP.
    Sort by sent_at so the LLM sees messages in chronological order.
    """
    sorted_rows = sorted(rows, key=lambda r: r.get("sent_at", ""))
    parts = []
    total = 0
    for r in sorted_rows:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        remaining = _MAX_CHARS_PER_GROUP - total
        if remaining <= 0:
            break
        parts.append(text[:remaining])
        total += len(text)
    return "\n\n".join(parts)


# ── Metadata helpers ──────────────────────────────────────────────────────────

def _label(rows: List[dict], field: str) -> str:
    for r in rows:
        val = r.get(field, "")
        if val:
            return str(val)
    return "Unknown"


def _participants(rows: List[dict]) -> List[str]:
    import json as _json
    seen = set()
    result = []
    for r in rows:
        raw = r.get("participants", "")
        try:
            names = _json.loads(raw) if raw else []
        except Exception:
            names = [raw] if raw else []
        for n in names:
            if n and n not in seen:
                seen.add(n)
                result.append(n)
    return result[:10]


def _latest_at(rows: List[dict]) -> str:
    dates = [r.get("sent_at", "") for r in rows if r.get("sent_at")]
    return max(dates) if dates else ""


def _cutoff_iso(hours: int) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).strftime("%Y-%m-%dT%H:%M:%S")


def _empty(platform: str, hours, message: str) -> dict:
    return {
        "platform":      platform,
        "period_hours":  hours,
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "thread_count":  0,
        "threads":       [],
        "overall_digest": message,
    }
