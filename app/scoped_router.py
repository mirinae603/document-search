"""
scoped_router.py
----------------
Tag-based scoped search & Q&A with UI filters and multi-document pinned Q&A.

NEW IN THIS VERSION:
  - SearchFilters       : source_type, file_type, duration (filtered at document level)
  - Multi-doc pinned QA : pin_to_file_ids restricts Q&A to selected documents
  - Two-stage filtering : documents table → matching file_ids → chunk search

MOUNT IN app.py:
    from scoped_router import router as scoped_router, init_scoped_router
    init_scoped_router(embedder=embedder, searcher=searcher, qa_agent=qa_agent, db_conn=db)
    app.include_router(scoped_router, prefix="/scoped", tags=["Scoped Q&A"])
"""

from __future__ import annotations

import hashlib
import logging
import time
import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ── Service references ────────────────────────────────────────────────────────

_embedder = None
_searcher = None
_qa_agent = None
_db       = None


def init_scoped_router(embedder, searcher, qa_agent, db_conn):
    global _embedder, _searcher, _qa_agent, _db
    _embedder = embedder
    _searcher = searcher
    _qa_agent = qa_agent
    _db       = db_conn
    logger.info("✓ Scoped router initialized with existing services")


def _require_services():
    if not all([_embedder, _searcher, _qa_agent, _db]):
        raise HTTPException(
            status_code=500,
            detail="Scoped router not initialized. Call init_scoped_router() first."
        )


# ── Chunking ──────────────────────────────────────────────────────────────────

CHUNK_SIZE    = 1200
CHUNK_OVERLAP = 100

def _sliding_window_chunks(text: str) -> list[str]:
    chunks, start = [], 0
    while start < len(text):
        chunks.append(text[start : start + CHUNK_SIZE].strip())
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return [c for c in chunks if len(c) > 50]


def _infer_file_type(filename: str) -> str:
    """Derive file_type from filename extension."""
    ext = os.path.splitext(filename)[-1].lower().lstrip(".")
    return ext if ext else "unknown"


# ── Pydantic Models ───────────────────────────────────────────────────────────

class ScopeFilter(BaseModel):
    """
    Scope tags — ALL provided tags must match (AND logic).
    At least one tag required; tagless queries use /search or /qa.
    """
    client:  Optional[str] = Field(None, example="unilever")
    project: Optional[str] = Field(None, example="brazil_poc")
    domain:  Optional[str] = Field(None, example="finance")

    def to_where_clause(self) -> str:
        conditions = []
        if self.client:
            conditions.append(f"client_tag = '{self.client}'")
        if self.project:
            conditions.append(f"project_tag = '{self.project}'")
        if self.domain:
            conditions.append(f"domain_tag = '{self.domain}'")
        if not conditions:
            raise ValueError("At least one tag must be provided.")
        conditions.append("client_tag IS NOT NULL")
        conditions.append("client_tag != ''")
        return " AND ".join(conditions)

    def to_display(self) -> dict:
        return {k: v for k, v in self.dict().items() if v is not None}


class SearchFilters(BaseModel):
    """
    UI filters applied at document level before chunk search.
    All fields optional — omitting a field means no filter on that dimension.

    source_type : who/what created the document
                  e.g. "uploaded" | "webhook" | "scoped_ingest" | "sharepoint"
    file_type   : derived from filename extension
                  e.g. "pdf" | "docx" | "txt" | "xlsx"
    duration    : preset window based on document's indexed_at (creation time)
                  "last_24h" | "last_7d" | "last_30d" | "last_90d"
                  OR custom ISO dates via date_from / date_to
    date_from   : custom range start  e.g. "2026-01-01T00:00:00"
    date_to     : custom range end    e.g. "2026-03-01T00:00:00"
    """
    source_type: Optional[str] = Field(None, example="uploaded")
    file_type:   Optional[str] = Field(None, example="pdf")
    duration:    Optional[str] = Field(None, example="last_7d",
                                       description="last_24h | last_7d | last_30d | last_90d")
    date_from:   Optional[str] = Field(None, example="2026-01-01T00:00:00")
    date_to:     Optional[str] = Field(None, example="2026-03-01T00:00:00")

    def is_empty(self) -> bool:
        return not any([self.source_type, self.file_type,
                        self.duration, self.date_from, self.date_to])


class ScopedIngestRequest(BaseModel):
    file_id:     str  = Field(..., description="Unique file identifier")
    filename:    str
    content:     str  = Field(..., description="Raw text content to chunk and index")
    tags:        ScopeFilter
    source:      Optional[str] = None
    source_type: Optional[str] = Field("scoped_ingest",
                                        description="Source system: uploaded | webhook | scoped_ingest | sharepoint")


class ScopedSearchRequest(BaseModel):
    query:   str
    tags:    ScopeFilter
    filters: Optional[SearchFilters] = Field(None, description="UI filters: source_type, file_type, duration")
    mode:    str = Field("hybrid", description="semantic | lexical | hybrid")
    limit:   int = Field(5, ge=1, le=20)


class ScopedQARequest(BaseModel):
    question:             str
    tags:                 ScopeFilter
    filters:              Optional[SearchFilters] = Field(None, description="Same UI filters as search")
    pin_to_file_ids:      Optional[list[str]]     = Field(None,
                          description="Lock Q&A to these specific file_ids from search results. "
                                      "Pass multiple for multi-document Q&A.")
    mode:                 str = Field("hybrid", description="semantic | lexical | hybrid")
    top_k:                int = Field(5, ge=1, le=10)
    qa_mode:              str = Field("document", description="document | general")
    conversation_history: list[dict] = Field(default_factory=list)


# ── Filtered table proxy ──────────────────────────────────────────────────────

class _FilteredTable:
    """
    Wraps a LanceDB table, injecting a WHERE clause on every .search() call.
    Used to transparently scope SearchEngine / QAAgent without modifying them.
    """
    def __init__(self, table, where_clause: str):
        self._table = table
        self._where = where_clause

    def __getattr__(self, name):
        return getattr(self._table, name)

    def search(self, query=None, query_type=None):
        if query is None:
            base = self._table.search()
        elif query_type:
            base = self._table.search(query, query_type=query_type)
        else:
            base = self._table.search(query)
        return base.where(self._where)


# ── Document-level filter helpers ─────────────────────────────────────────────

def _duration_to_iso(duration: str) -> str:
    """Convert duration preset to ISO datetime string for the start of the window."""
    now = datetime.utcnow()
    mapping = {
        "last_24h": now - timedelta(hours=24),
        "last_7d":  now - timedelta(days=7),
        "last_30d": now - timedelta(days=30),
        "last_90d": now - timedelta(days=90),
    }
    if duration not in mapping:
        raise ValueError(f"Invalid duration '{duration}'. Use: last_24h | last_7d | last_30d | last_90d")
    return mapping[duration].isoformat()


def _resolve_file_ids_from_filters(filters: SearchFilters) -> Optional[list[str]]:
    """
    Stage 1 of two-stage filtering.
    Queries the documents table with source_type / file_type / duration filters.
    Returns list of matching file_ids, or None if no filters active.

    None means "no document-level filter applied" — all file_ids are eligible.
    Empty list means "filters were active but no documents matched" — return nothing.
    """
    if not filters or filters.is_empty():
        return None  # No filter — all documents eligible

    if "documents" not in _db.table_names():
        return None

    doc_table = _db.open_table("documents")

    # Build WHERE clause for documents table
    conditions = []

    if filters.source_type:
        conditions.append(f"source_type = '{filters.source_type}'")

    if filters.file_type:
        conditions.append(f"file_type = '{filters.file_type}'")

    # Duration filter on indexed_at
    if filters.duration:
        since = _duration_to_iso(filters.duration)
        conditions.append(f"indexed_at >= '{since}'")
    elif filters.date_from:
        conditions.append(f"indexed_at >= '{filters.date_from}'")

    if filters.date_to:
        conditions.append(f"indexed_at <= '{filters.date_to}'")

    if not conditions:
        return None

    where = " AND ".join(conditions)

    try:
        rows = doc_table.search().where(where).to_list()
        file_ids = [r.get("file_id") for r in rows if r.get("file_id")]
        logger.info(f"Document filter matched {len(file_ids)} file_ids: {where}")
        return file_ids  # May be empty list — means no matches
    except Exception as e:
        logger.warning(f"Document filter query failed (skipping filter): {e}")
        return None


def _build_chunk_where(
    scope_where: str,
    file_ids: Optional[list[str]],
    pin_to_file_ids: Optional[list[str]] = None,
) -> str:
    """
    Stage 2 of two-stage filtering.
    Combines scope tag WHERE clause with file_id restrictions.

    Priority: pin_to_file_ids > file_ids from document filters > scope only
    """
    conditions = [scope_where]

    # Pin overrides document filters — user explicitly chose these files
    effective_file_ids = pin_to_file_ids if pin_to_file_ids else file_ids

    if effective_file_ids is not None:
        if not effective_file_ids:
            # Filters were active but matched nothing — force zero results
            conditions.append("file_id = '__no_match__'")
        elif len(effective_file_ids) == 1:
            conditions.append(f"file_id = '{effective_file_ids[0]}'")
        else:
            id_list = ", ".join(f"'{fid}'" for fid in effective_file_ids)
            conditions.append(f"file_id IN ({id_list})")

    return " AND ".join(conditions)


# ── Scoped search core ────────────────────────────────────────────────────────

async def _scoped_search(
    query: str,
    where: str,
    mode: str,
    limit: int,
) -> list[dict]:
    """Runs tag+filter scoped search via SearchEngine."""
    searcher = _searcher

    if not searcher.chunks_table:
        searcher._refresh_table()
    if not searcher.chunks_table:
        raise HTTPException(status_code=500, detail="chunks table unavailable")

    original_table = searcher.chunks_table
    try:
        searcher.chunks_table = _FilteredTable(original_table, where)
        if mode == "semantic":
            results = await searcher.semantic_search(query, limit=limit)
        elif mode == "lexical":
            results = await searcher.lexical_search(query, limit=limit)
        else:
            results = await searcher.hybrid_search(query, limit=limit)
    finally:
        searcher.chunks_table = original_table

    return results


# ── Router ────────────────────────────────────────────────────────────────────

router = APIRouter()


# ── 1. Scoped Ingest ──────────────────────────────────────────────────────────

@router.post("/ingest", summary="Ingest a document with scope tags")
async def scoped_ingest(req: ScopedIngestRequest):
    """
    Chunks + embeds a document and stores it with scope tags.
    source_type and file_type are stored on the document record
    so they can be used as UI filters later.
    """
    _require_services()

    try:
        req.tags.to_where_clause()
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    now        = int(time.time())
    doc_id     = req.file_id
    file_type  = _infer_file_type(req.filename)
    source_type = req.source_type or "scoped_ingest"
    raw_chunks = _sliding_window_chunks(req.content)

    if not raw_chunks:
        raise HTTPException(status_code=422, detail="Document produced no chunks after splitting.")

    # Embed via existing OpenRouterEmbeddings
    chunk_records = []
    for idx, chunk_text in enumerate(raw_chunks):
        try:
            embedding = await _embedder.embed_text(chunk_text)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Embedding failed on chunk {idx}: {e}")

        chunk_id = hashlib.md5(f"{doc_id}_{idx}_{chunk_text[:50]}".encode()).hexdigest()
        chunk_records.append({
            # Exact Chunk LanceModel fields
            "chunk_id":    chunk_id,
            "file_id":     doc_id,
            "document_id": doc_id,
            "filename":    req.filename,
            "file_path":   req.source or "",
            "text":        chunk_text,
            "vector":      embedding.astype("float32").tolist(),
            "chunk_index": idx,
            "char_start":  idx * (CHUNK_SIZE - CHUNK_OVERLAP),
            "char_end":    idx * (CHUNK_SIZE - CHUNK_OVERLAP) + len(chunk_text),
            "token_count": len(chunk_text.split()),
            "client_tag":  req.tags.client  or "",
            "project_tag": req.tags.project or "",
            "domain_tag":  req.tags.domain  or "",
        })

    try:
        if "chunks" in _db.table_names():
            _db.open_table("chunks").add(chunk_records)
        else:
            _db.create_table("chunks", data=chunk_records)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write chunks: {e}")

    # Document record — includes source_type + file_type for UI filters
    # NOTE: source_type and file_type are extra columns beyond base Document model.
    # They will be stored if the documents table already has these columns
    # (added by migrate_add_tags.py or on first scoped_ingest create).
    doc_record = {
        # Base Document LanceModel fields
        "file_id":      doc_id,
        "filename":     req.filename,
        "file_path":    req.source or "",
        "content_type": f"text/{file_type}",
        "file_size":    len(req.content),
        "indexed_at":   datetime.utcnow().isoformat(),
        # Filter columns
        "source_type":  source_type,
        "file_type":    file_type,
    }
    try:
        if "documents" in _db.table_names():
            _db.open_table("documents").add([doc_record])
        else:
            _db.create_table("documents", data=[doc_record])
    except Exception as e:
        logger.warning(f"Document record write failed (non-fatal): {e}")

    _searcher._refresh_table()

    return {
        "status":         "success",
        "document_id":    doc_id,
        "filename":       req.filename,
        "file_type":      file_type,
        "source_type":    source_type,
        "chunks_created": len(chunk_records),
        "tags":           req.tags.to_display(),
    }


# ── 2. Scoped Search ──────────────────────────────────────────────────────────

@router.post("/search", summary="Search within scoped context with UI filters")
async def scoped_search(req: ScopedSearchRequest):
    """
    Two-stage filtered search:
      Stage 1 — documents table filtered by source_type / file_type / duration
                → produces eligible file_ids
      Stage 2 — chunks table searched with scope tags + file_id restriction

    If no filters provided, falls back to tag-only scoping.
    """
    _require_services()

    try:
        scope_where = req.tags.to_where_clause()
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Stage 1: resolve file_ids from document-level filters
    try:
        file_ids = _resolve_file_ids_from_filters(req.filters)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Stage 2: build combined WHERE for chunk search
    chunk_where = _build_chunk_where(scope_where, file_ids)

    results = await _scoped_search(req.query, chunk_where, req.mode, req.limit)

    cleaned = []
    for r in results:
        r.pop("_distance", None)
        r.pop("_score",    None)
        r.pop("vector",    None)
        cleaned.append(r)

    # Surface unique documents found — UI uses this to populate the pin selector
    unique_docs = {}
    for r in cleaned:
        fid = r.get("file_id")
        if fid and fid not in unique_docs:
            unique_docs[fid] = {
                "file_id":  fid,
                "filename": r.get("filename", ""),
            }

    return {
        "query":          req.query,
        "mode":           req.mode,
        "scope":          req.tags.to_display(),
        "filters_applied": req.filters.dict() if req.filters else {},
        "result_count":   len(cleaned),
        "results":        cleaned,
        # UI uses documents_found to populate "Ask about this document" selector
        "documents_found": list(unique_docs.values()),
    }


# ── 3. Scoped Q&A ─────────────────────────────────────────────────────────────

@router.post("/qa", summary="Q&A scoped to tags, filters, and/or pinned documents")
async def scoped_qa(req: ScopedQARequest):
    """
    Three levels of context restriction (all combinable):
      Level 1 — Scope tags      : client/project/domain
      Level 2 — UI filters      : source_type / file_type / duration
      Level 3 — Pinned file_ids : user selected specific docs from search results

    Pin overrides filters when both are provided.
    Multi-document pin supported — QAAgent gets context from all pinned files.
    """
    _require_services()

    try:
        scope_where = req.tags.to_where_clause()
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Resolve document-level filters (skip if pin_to_file_ids provided)
    file_ids_from_filters = None
    if not req.pin_to_file_ids:
        try:
            file_ids_from_filters = _resolve_file_ids_from_filters(req.filters)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    # Build final WHERE combining scope + filters + pin
    chunk_where = _build_chunk_where(
        scope_where,
        file_ids_from_filters,
        pin_to_file_ids=req.pin_to_file_ids,
    )

    original_table = _searcher.chunks_table
    if not original_table:
        _searcher._refresh_table()
        original_table = _searcher.chunks_table
    if not original_table:
        raise HTTPException(status_code=500, detail="chunks table unavailable")

    try:
        _searcher.chunks_table = _FilteredTable(original_table, chunk_where)

        if req.conversation_history:
            result = await _qa_agent.multi_turn_conversation(
                messages=req.conversation_history + [{"role": "user", "content": req.question}],
                top_k=req.top_k,
                mode=req.qa_mode,
            )
        else:
            result = await _qa_agent.answer_question(
                question=req.question,
                top_k=req.top_k,
                mode=req.qa_mode,
            )
    finally:
        _searcher.chunks_table = original_table

    result["scope"]           = req.tags.to_display()
    result["pinned_file_ids"] = req.pin_to_file_ids or []
    result["filters_applied"] = req.filters.dict() if req.filters else {}
    return result


# ── 4. List Available Tags ────────────────────────────────────────────────────

@router.get("/tags", summary="List all distinct tag values in the scoped corpus")
async def list_tags():
    _require_services()

    if "chunks" not in _db.table_names():
        return {"tags": {"client": [], "project": [], "domain": []}, "total_scoped_chunks": 0}

    try:
        rows = (
            _db.open_table("chunks")
               .search()
               .where("client_tag IS NOT NULL AND client_tag != ''")
               .to_list()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Tag query failed: {e}")

    return {
        "tags": {
            "client":  sorted({r.get("client_tag",  "") for r in rows if r.get("client_tag")}),
            "project": sorted({r.get("project_tag", "") for r in rows if r.get("project_tag")}),
            "domain":  sorted({r.get("domain_tag",  "") for r in rows if r.get("domain_tag")}),
        },
        "total_scoped_chunks": len(rows),
    }


# ── 5. List Filter Options ────────────────────────────────────────────────────

@router.get("/filters/options", summary="Get available filter values for the UI dropdowns")
async def list_filter_options():
    """
    Returns all distinct source_type and file_type values present in the
    documents table. UI uses this to populate the filter dropdowns dynamically.
    """
    _require_services()

    if "documents" not in _db.table_names():
        return {"source_types": [], "file_types": [], "total_documents": 0}

    try:
        rows = _db.open_table("documents").search().to_list()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Filter options query failed: {e}")

    source_types = sorted({r.get("source_type", "") for r in rows if r.get("source_type")})
    file_types   = sorted({r.get("file_type",   "") for r in rows if r.get("file_type")})

    return {
        "source_types":   source_types,
        "file_types":     file_types,
        "duration_presets": ["last_24h", "last_7d", "last_30d", "last_90d"],
        "total_documents": len(rows),
    }


# ── 6. Delete Scoped Document ─────────────────────────────────────────────────

@router.delete("/document/{document_id}", summary="Delete a scoped document and its chunks")
async def delete_scoped_document(document_id: str):
    _require_services()

    deleted_chunks = 0
    deleted_docs   = 0

    if "chunks" in _db.table_names():
        try:
            table  = _db.open_table("chunks")
            before = table.count_rows()
            table.delete(
                f"file_id = '{document_id}' "
                f"AND client_tag IS NOT NULL AND client_tag != ''"
            )
            deleted_chunks = before - table.count_rows()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Chunk deletion failed: {e}")

    if "documents" in _db.table_names():
        try:
            table  = _db.open_table("documents")
            before = table.count_rows()
            table.delete(f"file_id = '{document_id}'")
            deleted_docs = before - table.count_rows()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Document deletion failed: {e}")

    if deleted_chunks == 0 and deleted_docs == 0:
        raise HTTPException(
            status_code=404,
            detail=f"Document '{document_id}' not found or is not a scoped document."
        )

    _searcher._refresh_table()

    return {
        "status":             "deleted",
        "document_id":        document_id,
        "chunks_deleted":     deleted_chunks,
        "doc_record_deleted": deleted_docs > 0,
    }