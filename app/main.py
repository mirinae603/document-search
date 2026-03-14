import os, io, hashlib, asyncio, logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional
from contextlib import asynccontextmanager

import httpx
import numpy as np
import requests
import uvicorn
from fastapi import FastAPI, UploadFile, File, HTTPException, Query, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from embeddings import OpenRouterEmbeddings
from indexer import DocumentIndexer, _indexing_in_progress
from search import SearchEngine
from reranker import load_reranker_from_config
from qa_agent import QAAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────
SEAWEED_FILER  = os.getenv("SEAWEED_FILER", "http://localhost:8888")
LANCEDB_PATH   = os.getenv("LANCEDB_PATH", "./data/lancedb")
OPENROUTER_KEY = "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"

# ── Globals ──────────────────────────────────────────────────────
db = indexer = searcher = embedder = reranker = qa_agent = None


def clean_result(result: Dict) -> Dict:
    cleaned = {}
    for key, value in result.items():
        if isinstance(value, np.ndarray):
            cleaned[key] = value.tolist()
        elif isinstance(value, (np.float32, np.float64)):
            cleaned[key] = float(value)
        elif isinstance(value, (np.int32, np.int64)):
            cleaned[key] = int(value)
        elif isinstance(value, dict):
            cleaned[key] = clean_result(value)
        elif isinstance(value, list):
            cleaned[key] = [clean_result(i) if isinstance(i, dict) else i for i in value]
        else:
            cleaned[key] = value
    return cleaned


# ── Lifespan ─────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, indexer, searcher, embedder, reranker, qa_agent
    import lancedb

    os.makedirs(LANCEDB_PATH, exist_ok=True)
    db       = lancedb.connect(LANCEDB_PATH)
    embedder = OpenRouterEmbeddings(api_key=OPENROUTER_KEY)
    reranker = load_reranker_from_config("./config/config.yaml")
    indexer  = DocumentIndexer(db, embedder)
    searcher = SearchEngine(db, embedder, reranker)
    qa_agent = QAAgent(searcher, SEAWEED_FILER, OPENROUTER_KEY)

    logger.info("✓ System initialized with Q&A Agent")
    yield
    logger.info("Shutting down")


# ── App ───────────────────────────────────────────────────────────
app = FastAPI(title="Document Search System", version="2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"]
)

static_path = Path(__file__).parent / "static"
static_path.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_path)), name="static")


# ── Routes ────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    html_file = static_path / "index.html"
    return html_file.read_text() if html_file.exists() else "<h1>Document Search System</h1>"


@app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}


@app.get("/stats")
async def stats():
    return {"system": "Document Search v2.0", "documents": indexer.get_stats()}


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    try:
        content   = await file.read()
        file_path = f"/documents/{file.filename}"
        response  = requests.put(
            f"{SEAWEED_FILER}{file_path}",
            data=content,
            headers={"Content-Type": file.content_type or "application/octet-stream"},
            timeout=30
        )
        if response.status_code not in [200, 201, 204]:
            raise HTTPException(500, "SeaweedFS upload failed")
        return {
            "status": "uploaded", "filename": file.filename,
            "size": len(content), "message": "Indexing will begin shortly via filer webhook"
        }
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        raise HTTPException(500, str(e))


@app.post("/webhook/seaweed")
async def seaweed_webhook(request: Request):
    try:
        payload    = await request.json()
        event_type = (payload.get("event_type") or payload.get("EventType") or "PUT").upper()
        file_path  = payload.get("key") or payload.get("Path") or payload.get("path") or ""

        logger.info(f"Webhook received: {event_type} → {file_path}")

        if event_type not in ("PUT", "CREATE"):
            return {"status": "ignored", "reason": f"event={event_type}"}
        if not file_path or not file_path.startswith("/documents/"):
            return {"status": "ignored", "reason": f"path='{file_path}'"}

        file_id = hashlib.sha256(file_path.encode()).hexdigest()[:16]
        if file_id in _indexing_in_progress:
            return {"status": "skipped", "reason": "already indexing"}

        message      = payload.get("message", {})
        new_entry    = message.get("new_entry", {})
        attributes   = new_entry.get("attributes", {})
        content_type = attributes.get("mime", "application/octet-stream")
        filename     = file_path.split("/")[-1]

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(f"{SEAWEED_FILER}{file_path}")
        if resp.status_code != 200:
            raise HTTPException(500, f"Filer fetch failed: {resp.status_code}")

        asyncio.create_task(
            indexer.index_document(file_path, resp.content, filename, content_type)
        )
        return {"status": "indexing", "filename": filename, "file_id": file_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Webhook failed: {e}")
        raise HTTPException(500, str(e))


@app.get("/search")
async def search(
    query: str = Query(...),
    type:  str = Query("hybrid", regex="^(lexical|semantic|hybrid)$"),
    limit: int = Query(10, ge=1, le=50)
):
    try:
        if type == "lexical":
            results = await searcher.lexical_search(query, limit)
        elif type == "semantic":
            results = await searcher.semantic_search(query, limit)
        else:
            results = await searcher.hybrid_search(query, limit)
        return {"query": query, "type": type, "count": len(results), "results": [clean_result(r) for r in results]}
    except Exception as e:
        logger.error(f"Search failed: {e}")
        raise HTTPException(500, str(e))


@app.get("/search/compare")
async def compare_search(query: str = Query(...), limit: int = Query(10, ge=1, le=20)):
    try:
        data = await searcher.comparison_search(query, limit)
        return {
            "query": data["query"],
            "query_stats": data["query_stats"],
            "lexical":  [clean_result(r) for r in data["lexical"]],
            "semantic": [clean_result(r) for r in data["semantic"]],
            "hybrid":   [clean_result(r) for r in data["hybrid"]]
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/preview")
async def preview(file_path: str = Query(...)):
    try:
        response = requests.get(f"{SEAWEED_FILER}{file_path}", timeout=10)
        if response.status_code != 200:
            raise HTTPException(404, "Not found")
        content_type = response.headers.get("Content-Type", "application/octet-stream")
        if "pdf" in content_type.lower():
            return StreamingResponse(
                io.BytesIO(response.content), media_type="application/pdf",
                headers={"Content-Disposition": f"inline; filename={file_path.split('/')[-1]}"}
            )
        return {"content": response.text[:5000], "type": content_type}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/qa/ask")
async def ask_question(
    question:        str  = Query(...),
    top_k:           int  = Query(5, ge=1, le=10),
    return_sources:  bool = Query(True),
    include_excerpts: bool = Query(True),
    mode:            str  = Query("document", regex="^(document|general)$")
):
    try:
        return await qa_agent.answer_question(question, top_k, return_sources, include_excerpts, mode)
    except Exception as e:
        logger.error(f"Q&A failed: {e}")
        raise HTTPException(500, str(e))


@app.post("/qa/conversation")
async def conversation(
    messages: List[Dict[str, str]],
    top_k: int = Query(5, ge=1, le=10),
    mode:  str = Query("document", regex="^(document|general)$")
):
    try:
        return await qa_agent.multi_turn_conversation(messages, top_k, mode)
    except Exception as e:
        logger.error(f"Conversation failed: {e}")
        raise HTTPException(500, str(e))


@app.get("/qa/sources")
async def get_sources(question: str = Query(..., min_length=3), top_k: int = Query(5, ge=1, le=10)):
    try:
        docs = await qa_agent._retrieve_relevant_documents(question, top_k)
        return {
            "question": question,
            "count": len(docs),
            "sources": [{
                "document_id":    doc["id"],
                "filename":       doc["filename"],
                "file_path":      doc["file_path"],
                "file_id":        doc["file_id"],
                "relevance_score": round(doc["relevance_score"], 4),
                "preview_url":    f"/preview?file_path={doc['file_path']}",
                "text_preview":   doc["text"][:500] + ("..." if len(doc["text"]) > 500 else "")
            } for doc in docs]
        }
    except Exception as e:
        logger.error(f"Source retrieval failed: {e}")
        raise HTTPException(500, str(e))


@app.post("/admin/reindex")
async def reindex_documents(
    background_tasks: BackgroundTasks,
    mode:       str           = Query("all",  regex="^(all|file|date_range)$"),
    file_path:  Optional[str] = Query(None),
    date_from:  Optional[str] = Query(None),
    date_to:    Optional[str] = Query(None),
    background: bool          = Query(True)
):
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

        async with httpx.AsyncClient(timeout=15) as client:
            list_resp = await client.get(
                f"{SEAWEED_FILER}/documents/",
                headers={"Accept": "application/json"},
                params={"limit": 1000}
            )
        if list_resp.status_code != 200:
            raise HTTPException(500, f"Could not list /documents/: {list_resp.status_code}")

        logger.info(f"Filer listing response: {list_resp.text[:500]}")
        filer_data  = list_resp.json()
        all_entries = filer_data.get("Entries") or filer_data.get("Files") or []

        if not all_entries:
            return {"status": "nothing_to_index", "raw_keys": list(filer_data.keys())}

        if mode == "all":
            entries_to_process = all_entries
        elif mode == "file":
            entries_to_process = [
                e for e in all_entries
                if (e.get("FullPath") or f"/documents/{e.get('name','')}") == file_path
            ]
            if not entries_to_process:
                raise HTTPException(404, f"File not found: {file_path}")
        else:  # date_range
            entries_to_process = []
            for e in all_entries:
                raw = e.get("Crtime") or e.get("crtime", "")
                try:
                    entry_dt = datetime.fromisoformat(raw).replace(tzinfo=None) if raw else None
                except ValueError:
                    entry_dt = None
                if entry_dt and dt_from <= entry_dt <= dt_to:
                    entries_to_process.append(e)
            if not entries_to_process:
                return {"status": "nothing_to_index", "message": f"No files between {date_from} and {date_to}"}

        logger.info(f"Reindex mode={mode} → {len(entries_to_process)} file(s)")

        async def _process_entry(entry: dict) -> dict:
            full_path = entry.get("FullPath", "")
            fname     = full_path.split("/")[-1]
            if not fname:
                return {"path": full_path, "status": "skipped", "reason": "no filename"}

            fid = hashlib.sha256(full_path.encode()).hexdigest()[:16]
            if fid in _indexing_in_progress:
                return {"path": full_path, "status": "skipped", "reason": "already indexing"}

            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    file_resp = await client.get(f"{SEAWEED_FILER}{full_path}")
                if file_resp.status_code != 200:
                    return {"path": full_path, "status": "failed", "reason": f"filer {file_resp.status_code}"}

                content_type = entry.get("Mime") or file_resp.headers.get("Content-Type", "application/octet-stream")
                file_id = await indexer.index_document(full_path, file_resp.content, fname, content_type)
                return {"path": full_path, "status": "indexed", "file_id": file_id, "filename": fname}
            except Exception as e:
                logger.error(f"Reindex failed for {full_path}: {e}")
                return {"path": full_path, "status": "failed", "reason": str(e)}

        if background:
            async def _run_all():
                results = await asyncio.gather(*[_process_entry(e) for e in entries_to_process])
                indexed = [r for r in results if r["status"] == "indexed"]
                failed  = [r for r in results if r["status"] == "failed"]
                logger.info(f"✓ Reindex done — indexed={len(indexed)} failed={len(failed)}")
                for f in failed:
                    logger.error(f"  ✗ {f['path']}: {f['reason']}")

            background_tasks.add_task(_run_all)
            return {"status": "queued", "mode": mode, "queued": len(entries_to_process)}

        results = await asyncio.gather(*[_process_entry(e) for e in entries_to_process])
        indexed = [r for r in results if r["status"] == "indexed"]
        failed  = [r for r in results if r["status"] == "failed"]
        skipped = [r for r in results if r["status"] == "skipped"]

        return {
            "status": "complete", "mode": mode,
            "summary": {"total": len(entries_to_process), "indexed": len(indexed), "failed": len(failed), "skipped": len(skipped)},
            "details": {"indexed": indexed, "failed": failed, "skipped": skipped}
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Reindex failed: {e}")
        raise HTTPException(500, str(e))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
