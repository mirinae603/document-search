# api/ingestion_routes.py
import asyncio
import hashlib
import io
import logging
from datetime import datetime
from typing import Optional
from pathlib import Path

import httpx
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse

from config             import SEAWEED_FILER, STATIC_DIR
from ingestion.indexer  import _indexing_in_progress
from storage.seaweed    import SeaweedStore

logger = logging.getLogger(__name__)
router = APIRouter()

_indexer = None
_seaweed = None

# source_type memory: file_path → source_type
# Populated by /upload, consumed by /webhook/seaweed
_pending_source_types: dict = {}


def init_ingestion_routes(indexer, seaweed: SeaweedStore):
    global _indexer, _seaweed
    _indexer = indexer
    _seaweed = seaweed


# ── Health / root ─────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def root():
    html_file = Path(__file__).parent.parent / "static" / "index.html"
    return html_file.read_text() if html_file.exists() else "<h1>Document Search System</h1>"


@router.get("/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}


@router.get("/stats")
async def stats():
    return {"system": "Document Search v2.0", "documents": _indexer.get_stats()}


# ── Upload ────────────────────────────────────────────────────────────────────

@router.post("/upload")
async def upload(
    file:        UploadFile = File(...),
    source_type: str        = Form("uploaded"),
):
    """
    Upload a file to SeaweedFS and trigger indexing directly.
    Does NOT wait for webhook — indexes inline as background task.
    source_type is passed through to index_document.

    Accepted source_type values: uploaded | scoped_ingest
    file_type and indexed_at are auto-derived — never set by caller.
    """
    try:
        content   = await file.read()
        file_path = f"/documents/{file.filename}"

        # Validate source_type
        valid_source_types = {"uploaded", "scoped_ingest"}
        if source_type not in valid_source_types:
            source_type = "uploaded"

        # Push to SeaweedFS
        ok = _seaweed.upload(file_path, content, file.content_type or "application/octet-stream")
        if not ok:
            raise HTTPException(500, "SeaweedFS upload failed")

        # Store source_type so webhook handler can pick it up if it fires
        _pending_source_types[file_path] = source_type

        # Also trigger indexing directly — don't rely solely on webhook
        # (webhook may be delayed or misconfigured in dev)
        file_id = hashlib.sha256(file_path.encode()).hexdigest()[:16]
        if file_id not in _indexing_in_progress:
            asyncio.create_task(
                _indexer.index_document(
                    file_path,
                    content,
                    file.filename,
                    file.content_type or "application/octet-stream",
                    source_type=source_type,
                )
            )
            logger.info(f"Upload: triggered indexing for '{file.filename}' source_type={source_type}")

        return {
            "status":      "uploaded",
            "filename":    file.filename,
            "file_id":     file_id,
            "source_type": source_type,
            "size":        len(content),
            "message":     "Indexing started in background",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        raise HTTPException(500, str(e))


# ── Webhook ───────────────────────────────────────────────────────────────────

@router.post("/webhook/seaweed")
async def seaweed_webhook(request: Request):
    """
    Receives PUT/CREATE events from SeaweedFS filer notification.
    If the file was uploaded via /upload, its source_type is preserved.
    Webhook-originated files default to source_type='webhook'.
    """
    try:
        payload    = await request.json()
        event_type = (payload.get("event_type") or payload.get("EventType") or "PUT").upper()
        file_path  = payload.get("key") or payload.get("Path") or payload.get("path") or ""

        logger.info(f"Webhook: {event_type} → {file_path}")

        if event_type not in ("PUT", "CREATE", "RENAME"):
            return {"status": "ignored", "reason": f"event={event_type}"}
        if not file_path or not file_path.startswith("/documents/"):
            return {"status": "ignored", "reason": f"path='{file_path}'"}

        file_id = hashlib.sha256(file_path.encode()).hexdigest()[:16]
        if file_id in _indexing_in_progress:
            # Already kicked off by /upload — consume pending source_type and exit
            _pending_source_types.pop(file_path, None)
            return {"status": "skipped", "reason": "already indexing via upload"}

        # Resolve source_type:
        # - If uploaded via UI → use what /upload stored
        # - If fired by external write → "webhook"
        source_type = _pending_source_types.pop(file_path, "webhook")

        message      = payload.get("message", {})
        new_entry    = message.get("new_entry", {})
        attributes   = new_entry.get("attributes", {})
        content_type = attributes.get("mime", "application/octet-stream")
        filename     = file_path.split("/")[-1]

        content = await _seaweed.fetch_async(file_path)

        asyncio.create_task(
            _indexer.index_document(
                file_path,
                content,
                filename,
                content_type,
                source_type=source_type,
            )
        )
        return {"status": "indexing", "filename": filename, "file_id": file_id, "source_type": source_type}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Webhook failed: {e}")
        raise HTTPException(500, str(e))


# ── Preview ───────────────────────────────────────────────────────────────────

@router.get("/preview")
async def preview(file_path: str = Query(...)):
    """Proxy a file from SeaweedFS for inline preview."""
    try:
        response = _seaweed.preview(file_path)
        if response.status_code != 200:
            raise HTTPException(404, "Not found")
        content_type = response.headers.get("Content-Type", "application/octet-stream")
        if "pdf" in content_type.lower():
            return StreamingResponse(
                io.BytesIO(response.content),
                media_type="application/pdf",
                headers={"Content-Disposition": f"inline; filename={file_path.split('/')[-1]}"},
            )
        return {"content": response.text[:5000], "type": content_type}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Reindex ───────────────────────────────────────────────────────────────────

@router.post("/admin/reindex")
async def reindex_documents(
    background_tasks: BackgroundTasks,
    mode:       str           = Query("all", regex="^(all|file|date_range)$"),
    file_path:  Optional[str] = Query(None),
    date_from:  Optional[str] = Query(None),
    date_to:    Optional[str] = Query(None),
    background: bool          = Query(True),
):
    """
    Re-index documents from SeaweedFS /documents/ directory.
    mode=all         → all files
    mode=file        → single file by file_path
    mode=date_range  → files indexed between date_from and date_to (ISO)
    background=True  → fire and forget (default)
    background=False → wait and return detailed results
    """
    try:
        if mode == "file" and not file_path:
            raise HTTPException(400, "file_path required for mode=file")
        if mode == "date_range":
            if not date_from or not date_to:
                raise HTTPException(400, "date_from and date_to required for mode=date_range")
            try:
                dt_from = datetime.fromisoformat(date_from)
                dt_to   = datetime.fromisoformat(date_to)
            except ValueError:
                raise HTTPException(400, "Invalid date format — use ISO: 2026-01-01T00:00:00")

        all_entries = await _seaweed.list_directory("/documents/")
        if not all_entries:
            return {"status": "nothing_to_index"}

        if mode == "all":
            entries = all_entries
        elif mode == "file":
            entries = [
                e for e in all_entries
                if (e.get("FullPath") or f"/documents/{e.get('name','')}") == file_path
            ]
            if not entries:
                raise HTTPException(404, f"File not found: {file_path}")
        else:  # date_range
            entries = []
            for e in all_entries:
                raw = e.get("Crtime") or e.get("crtime", "")
                try:
                    entry_dt = datetime.fromisoformat(raw).replace(tzinfo=None) if raw else None
                except ValueError:
                    entry_dt = None
                if entry_dt and dt_from <= entry_dt <= dt_to:
                    entries.append(e)
            if not entries:
                return {"status": "nothing_to_index", "message": f"No files in range {date_from}–{date_to}"}

        logger.info(f"Reindex mode={mode} → {len(entries)} file(s)")

        async def _process(entry: dict) -> dict:
            full_path = entry.get("FullPath", "")
            fname     = full_path.split("/")[-1]
            if not fname:
                return {"path": full_path, "status": "skipped", "reason": "no filename"}

            fid = hashlib.sha256(full_path.encode()).hexdigest()[:16]
            if fid in _indexing_in_progress:
                return {"path": full_path, "status": "skipped", "reason": "already indexing"}

            try:
                content      = await _seaweed.fetch_async(full_path)
                content_type = entry.get("Mime", "application/octet-stream")
                file_id      = await _indexer.index_document(
                    full_path, content, fname, content_type, source_type="reindex"
                )
                return {"path": full_path, "status": "indexed", "file_id": file_id}
            except Exception as e:
                logger.error(f"Reindex failed {full_path}: {e}")
                return {"path": full_path, "status": "failed", "reason": str(e)}

        if background:
            async def _run_all():
                results = await asyncio.gather(*[_process(e) for e in entries])
                ok  = [r for r in results if r["status"] == "indexed"]
                bad = [r for r in results if r["status"] == "failed"]
                logger.info(f"✓ Reindex done — indexed={len(ok)} failed={len(bad)}")
            background_tasks.add_task(_run_all)
            return {"status": "queued", "mode": mode, "queued": len(entries)}

        results = await asyncio.gather(*[_process(e) for e in entries])
        indexed = [r for r in results if r["status"] == "indexed"]
        failed  = [r for r in results if r["status"] == "failed"]
        skipped = [r for r in results if r["status"] == "skipped"]
        return {
            "status": "complete", "mode": mode,
            "summary": {
                "total":   len(entries),
                "indexed": len(indexed),
                "failed":  len(failed),
                "skipped": len(skipped),
            },
            "details": {"indexed": indexed, "failed": failed, "skipped": skipped},
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Reindex error: {e}")
        raise HTTPException(500, str(e))
