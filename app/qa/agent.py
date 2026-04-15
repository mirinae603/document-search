# qa/agent.py
import asyncio
import json
import logging
import os
import uuid
from typing import AsyncGenerator, List, Optional, Dict, Tuple

import httpx
import numpy as np

from config import OPENROUTER_KEY, OPENROUTER_BASE_URL, QA_MODEL
from qa.repository import save_message, get_session_with_messages, update_session_title

logger = logging.getLogger(__name__)

_store    = None
_embedder = None

HIGH_CONF             = float(os.getenv("HIGH_CONF",           "0.40"))
LOW_CONF              = float(os.getenv("LOW_CONF",            "0.20"))
QA_MAX_CONTEXT_CHUNKS = int(os.getenv("QA_MAX_CONTEXT_CHUNKS", "30"))
DATASET_MAX_CHUNKS    = int(os.getenv("DATASET_MAX_CHUNKS",    "40"))
CHAT_MODEL            = os.getenv("QA_MODEL", "openai/gpt-4o-mini")

_META_INTENTS = [
    "what documents", "what files", "list documents", "list files",
    "what's indexed", "whats indexed", "what is indexed",
    "how many documents", "how many files", "show documents",
    "available documents", "available files", "what do you have",
    "what can you search", "documents available",
]


def init_agent(store, embedder):
    global _store, _embedder
    _store    = store
    _embedder = embedder


# ── Intent detection ──────────────────────────────────────────────────────────

def _is_metadata_query(question: str) -> bool:
    return any(kw in question.lower() for kw in _META_INTENTS)


# ── Document list ─────────────────────────────────────────────────────────────

async def _stream_document_list(session_id: str) -> AsyncGenerator[str, None]:
    try:
        docs = _store.get_all_documents()
    except Exception as e:
        docs = []
        logger.warning(f"get_all_documents failed: {e}")

    if not docs:
        answer = "No documents are indexed yet. Upload documents using the upload button to get started."
    else:
        lines = [f"You have **{len(docs)}** document{'s' if len(docs) != 1 else ''} indexed:\n"]
        for d in docs:
            name     = d.get("filename", "unknown")
            size_b   = d.get("file_size", 0)
            size_str = f"{size_b/1048576:.1f} MB" if size_b >= 1048576 else f"{size_b/1024:.0f} KB"
            indexed  = (d.get("indexed_at") or "")[:10]
            ftype    = (d.get("file_type") or d.get("content_type", "")).split("/")[-1].upper()
            lines.append(f"• **{name}** — {ftype or 'FILE'} · {size_str} · indexed {indexed}")
        lines.append("\nAsk me anything about any of these documents.")
        answer = "\n".join(lines)

    for char in answer:
        yield _sse({"type": "token", "token": char})
    await asyncio.sleep(0)

    save_message(session_id, "assistant", answer, sources=[])
    yield _sse({"type": "done"})


# ── Embedding ─────────────────────────────────────────────────────────────────

async def _embed_async(text: str) -> np.ndarray:
    return await _embedder.embed(text)


# ── Retrieval ─────────────────────────────────────────────────────────────────

def _dist_to_score(row: dict) -> float:
    d = row.get("_distance", 1.0)
    return round(max(0.0, min(1.0, 1.0 - float(d or 1.0))), 4)

def _row_to_chunk(row: dict, score: float, is_semantic: bool = True) -> dict:
    return {
        "chunk_id":        row.get("chunk_id", ""),
        "file_id":         row.get("file_id", ""),
        "filename":        row.get("filename", ""),
        "text":            row.get("text", ""),
        "chunk_index":     row.get("chunk_index", 0),
        "score":           score,
        "section_heading": row.get("section_heading", ""),
        "page_number":     row.get("page_number", 0),
        "is_semantic":     is_semantic  # Track if this was an actual match or just padding
    }

async def _fetch_chunks(
    file_ids: Optional[List[str]],
    question: str,
    top_k:    int,
) -> Tuple[List[Dict], str]:
    if not _store:
        return [], "C"

    table      = _store.chunks
    max_chunks = DATASET_MAX_CHUNKS if file_ids is None else QA_MAX_CONTEXT_CHUNKS
    where      = None

    if file_ids:
        if len(file_ids) == 1:
            where = f"file_id = '{file_ids[0]}'"
        else:
            ids_str = ", ".join(f"'{i}'" for i in file_ids)
            where   = f"file_id IN ({ids_str})"

    try:
        vec    = await _embed_async(question)
        search = table.search(vec.tolist(), query_type="vector").metric("cosine")
        if where:
            search = search.where(where, prefilter=True)
        rows      = search.limit(top_k * 6).to_list()
        top_score = _dist_to_score(rows[0]) if rows else 0.0
        logger.info(f"TOP MATCH SCORE: {top_score}")
        
        if top_score >= HIGH_CONF:
            chunks = [_row_to_chunk(r, _dist_to_score(r), True) for r in rows]
            return sorted(chunks, key=lambda c: c["score"], reverse=True)[:max_chunks], "A"

        full_search = table.search()
        if where:
            full_search = full_search.where(where, prefilter=True)
        all_rows = full_search.limit(500).to_list()
        all_rows.sort(key=lambda r: (r.get("file_id", ""), r.get("chunk_index", 0)))
        total = len(all_rows)

        if top_score >= LOW_CONF:
            seen, result = {}, []
            for idx, r in enumerate(rows):
                score = _dist_to_score(r)
                # STRICTER CHECK: Only flag as true if it's in the actual top_k AND has a passing score
                is_real_hit = (idx < top_k) and (score >= LOW_CONF)
                
                cid = r.get("chunk_id") or f"{r.get('file_id')}_{r.get('chunk_index')}"
                if cid not in seen:
                    seen[cid] = True
                    result.append(_row_to_chunk(r, score, is_real_hit))
            
            for idx, r in enumerate(all_rows):
                cid = r.get("chunk_id") or f"{r.get('file_id')}_{r.get('chunk_index')}"
                if cid not in seen:
                    seen[cid] = True
                    result.append(_row_to_chunk(r, round(0.5*(1-idx/max(total,1)), 4), False))
            return result[:max_chunks], "B"

        return [
            # Tag fallback rows as False
            _row_to_chunk(r, round(1.0*(1-idx/max(total*2,1)), 4), False)
            for idx, r in enumerate(all_rows)
        ][:max_chunks], "C"

    except Exception as e:
        logger.error(f"_fetch_chunks failed: {e}", exc_info=True)
        return [], "C"


# ── Auto title ────────────────────────────────────────────────────────────────

async def _auto_title(session_id: str, question: str):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{OPENROUTER_BASE_URL.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
                json={
                    "model":      CHAT_MODEL,
                    "messages":   [{"role": "user", "content":
                        f"Generate a 5-7 word title for a conversation starting with: {question[:200]}\nTitle only, no quotes."}],
                    "max_tokens": 20,
                    "temperature": 0.3,
                },
            )
        if resp.status_code == 200:
            title = resp.json()["choices"][0]["message"]["content"].strip()
            update_session_title(session_id, title)
    except Exception as e:
        logger.warning(f"Auto-title failed: {e}")


# ── Core streaming engine ─────────────────────────────────────────────────────

async def stream_chat(
    question:   str,
    user_id:    str,
    session_id: str,
    file_ids:   Optional[List[str]],
    top_k:      int = 8,
) -> AsyncGenerator[str, None]:
    try:
        session = get_session_with_messages(session_id, user_id)
        if not session:
            yield _sse({"type": "error", "message": "Session not found"})
            return

        # Save user message
        save_message(session_id, "user", question)
        is_first_message = session["message_count"] == 0

        # Intent routing — metadata queries skip vector search
        if _is_metadata_query(question):
            async for event in _stream_document_list(session_id):
                yield event
            if is_first_message:
                asyncio.create_task(_auto_title(session_id, question))
            return

        # Retrieval
        chunks, _ = await _fetch_chunks(file_ids, question, top_k)
        if not chunks:
            yield _sse({"type": "error", "message": "No relevant content found in the documents"})
            return
        retrieved_ids = [c["chunk_id"] for c in chunks]
        logger.info(f"RETRIEVED CHUNKS FOR QUESTION: {retrieved_ids}")

        from storage.seaweed import SeaweedStore
        _seaweed = SeaweedStore()
        file_ids = list(set(c["file_id"] for c in chunks))

        image_paths = []
        metadata_cache = {}
        
        # 1. ONLY pull images from chunks that were an actual semantic match
        semantic_chunk_ids = {c["chunk_id"] for c in chunks if c.get("is_semantic", True)}

        for fid in file_ids:
            metadata = _seaweed.fetch_image_metadata(fid)
            metadata_cache[fid] = metadata
            for img in metadata:
                if set(img["vector_ids"]) & semantic_chunk_ids:
                    image_paths.append(img["image_path"])

        image_paths = list(set(image_paths))
        logger.info(f"IMAGE PATHS FOUND (Semantic Only): {image_paths}")

        # Sources event
        sources = _build_clean_sources(chunks)
        yield _sse({"type": "sources", "sources": sources})

        # Build chat history for context
        history = [
            {"role": m["role"], "content": m["content"]}
            for m in session.get("messages", [])[-10:]   # last 10 messages
        ]

        # Build prompt
        chunk_context = _build_chunk_context(chunks)
        
        # 2. Map the real SeaweedFS path into the context so the LLM can render it 
        for fid, metadata in metadata_cache.items():
            for img in metadata:
                # Extracts 'img_0' from '/images/xyz/img_0.png'
                filename = img["image_path"].split("/")[-1]
                placeholder = filename.split(".")[0]
                chunk_context = chunk_context.replace(f'path="{placeholder}"', f'path="{img["image_path"]}"')

        system_prompt = (
            "You are a document assistant. Answer questions using only the provided document excerpts. "
            "Be clear and concise. If the answer isn't in the documents, say so directly.\n"
            "If an excerpt contains an image tag (e.g., <IMAGE path=\"/images/...\" />) that is highly relevant "
            "to your explanation, include it inline in your answer using Markdown format: ![Image](/images/...)\n\n"
            f"DOCUMENT EXCERPTS:\n{chunk_context}"
        )

        messages_for_llm = [{"role": "system", "content": system_prompt}]
        messages_for_llm += history
        messages_for_llm.append({"role": "user", "content": question})

        # Stream LLM
        answer_parts = []

        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST",
                f"{OPENROUTER_BASE_URL.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
                json={
                    "model":       CHAT_MODEL,
                    "messages":    messages_for_llm,
                    "stream":      True,
                    "temperature": 0.2,
                    "max_tokens":  2000,
                },
            ) as resp:
                if resp.status_code != 200:
                    yield _sse({"type": "error", "message": f"LLM error {resp.status_code}"})
                    return
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"): continue
                    raw = line[5:].strip()
                    if raw == "[DONE]": break
                    try:
                        token = json.loads(raw)["choices"][0]["delta"].get("content", "")
                        if token:
                            answer_parts.append(token)
                            yield _sse({"type": "token", "token": token})
                    except Exception:
                        continue

        full_answer = "".join(answer_parts)
        
        # 3. Fallback: Only append highly relevant images, and format them as Markdown so they render
        if image_paths:
            full_answer += "\n\n## Related Images\n"
            for path in image_paths:
                full_answer += f"![Image]({path})\n"
                
        logger.info(f"FINAL ANSWER:\n{full_answer}")

        # Save assistant message with sources embedded
        save_message(session_id, "assistant", full_answer, sources=sources)

        yield _sse({"type": "done"})

        if is_first_message:
            asyncio.create_task(_auto_title(session_id, question))

    except asyncio.CancelledError:
        logger.info(f"Stream cancelled: session={session_id}")
    except Exception as e:
        logger.error(f"stream_chat failed: {e}", exc_info=True)
        yield _sse({"type": "error", "message": str(e)})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"

def _build_clean_sources(chunks: List[Dict]) -> List[Dict]:
    seen = {}
    for c in chunks:
        fid = c["file_id"]
        if fid not in seen:
            seen[fid] = {"file_id": fid, "filename": c["filename"], "excerpts": []}
        if len(seen[fid]["excerpts"]) < 3:
            seen[fid]["excerpts"].append({
                "chunk_index": c["chunk_index"],
                "text":        c["text"][:200],
            })
    return list(seen.values())

def _build_chunk_context(chunks: List[Dict]) -> str:
    return "\n\n---\n\n".join(
        f"[Source {i}: {c['filename']} | Section: {c.get('section_heading', 'Unknown')} | Page {c.get('page_number', '?')}]\n{c['text']}"
        for i, c in enumerate(chunks, 1)
    )