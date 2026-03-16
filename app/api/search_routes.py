# api/search_routes.py
# Routes: GET /search
# All logic lives in search/engine.py. This file is pure HTTP routing.
import logging
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from search.engine import stream_search, search_once

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/search")
async def search(
    query:       str           = Query(..., min_length=1, description="Search query"),
    limit:       int           = Query(10, ge=1, le=50),
    # Filters
    source_type: Optional[str] = Query(None, description="uploaded | webhook | scoped_ingest"),
    file_type:   Optional[str] = Query(None, description="pdf | docx | txt | xlsx"),
    date_from:   Optional[str] = Query(None, description="ISO datetime e.g. 2026-01-01T00:00:00"),
    date_to:     Optional[str] = Query(None, description="ISO datetime e.g. 2026-03-01T00:00:00"),
    duration:    Optional[str] = Query(None, description="last_24h | last_7d | last_30d | last_90d"),
    # Mode
    stream:      bool          = Query(True,  description="True=SSE stream, False=wait for both and return JSON"),
):
    """
    Smart parallel search.

    stream=True (default) — SSE stream:
      Lexical results arrive first (~100ms).
      Hybrid results arrive after (~400ms).
      Parse on client with EventSource or fetch + ReadableStream.

      Events:
        data: {"type":"lexical","results":[...],"count":N}
        data: {"type":"hybrid", "results":[...],"count":N}
        data: {"type":"done"}

    stream=False — single JSON response (waits for both):
      { query, lexical: [...], hybrid: [...], filters: {...} }

    Filters (all optional):
      source_type, file_type, date_from, date_to, duration
      Applied to documents table first, then chunk search is restricted
      to matching file_ids.
    """
    filter_kwargs = dict(
        source_type=source_type, file_type=file_type,
        date_from=date_from, date_to=date_to, duration=duration,
    )

    if stream:
        return StreamingResponse(
            stream_search(query, limit, **filter_kwargs),
            media_type = "text/event-stream",
            headers    = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    result = await search_once(query, limit, **filter_kwargs)
    return result
