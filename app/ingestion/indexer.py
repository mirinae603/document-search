# ingestion/indexer.py
import asyncio
import hashlib
import logging
import os
import re
from datetime import datetime
from typing import List, Dict
from storage.seaweed import SeaweedStore
_seaweed = SeaweedStore()

import httpx
import numpy as np

from config                import (
    CHUNK_SIZE, CHUNK_OVERLAP,
    OPENROUTER_KEY, OPENROUTER_BASE_URL, EMBEDDING_MODEL, EMBEDDING_DIM,
)
from storage.models        import Chunk
from storage.lancedb_store import LanceDBStore
from ingestion.extractor   import extract_text, extract_pages

logger = logging.getLogger(__name__)

_indexing_in_progress: set = set()

# Section heading detector — covers numbered, ALL CAPS, markdown
HEADING_RE = re.compile(
    r'^(\d+[\.\d]*\s+[A-Z][^\n]{3,60}|'
    r'[A-Z][A-Z\s]{4,40}|'
    r'#{1,3}\s+.+)$',
    re.MULTILINE
)


# ── Embedder ──────────────────────────────────────────────────────────────────

class OpenRouterEmbedder:
    def __init__(self, api_key: str = OPENROUTER_KEY, model: str = EMBEDDING_MODEL):
        if not api_key:
            raise ValueError("OPENROUTER_KEY is required")
        self.api_key  = api_key
        self.model    = model
        self.base_url = OPENROUTER_BASE_URL.rstrip("/")

    async def embed(self, text: str) -> np.ndarray:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
        }
        payload = {"model": self.model, "input": [text]}
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(f"{self.base_url}/embeddings", json=payload, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    if "data" in data and data["data"]:
                        return np.array(data["data"][0]["embedding"], dtype=np.float32)
                    raise RuntimeError(f"Unexpected embed response: {data}")
                if resp.status_code == 429 and attempt == 0:
                    logger.warning("Embed rate-limited — retrying in 2s")
                    await asyncio.sleep(2)
                    continue
                raise RuntimeError(f"Embed API {resp.status_code}: {resp.text[:200]}")
            except httpx.TimeoutException:
                if attempt == 0:
                    logger.warning("Embed timeout — retrying")
                    await asyncio.sleep(1)
                    continue
                raise RuntimeError("Embed API timed out after retry")
        raise RuntimeError("Embed failed after 2 attempts")

    async def embed_batch(self, texts: List[str], batch_size: int = 8) -> List[np.ndarray]:
        results = []
        for i in range(0, len(texts), batch_size):
            batch   = texts[i:i+batch_size]
            headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
            payload = {"model": self.model, "input": batch}
            for attempt in range(2):
                try:
                    async with httpx.AsyncClient(timeout=60) as client:
                        resp = await client.post(f"{self.base_url}/embeddings", json=payload, headers=headers)
                    if resp.status_code == 200:
                        data  = resp.json()
                        items = sorted(data["data"], key=lambda x: x.get("index", 0))
                        vecs  = [np.array(it["embedding"], dtype=np.float32) for it in items]
                        results.extend(vecs)
                        logger.info(f"  Embedded batch {i//batch_size+1} ({len(batch)} chunks)")
                        break
                    if resp.status_code == 429 and attempt == 0:
                        logger.warning("Batch embed rate-limited — retrying in 3s")
                        await asyncio.sleep(3)
                        continue
                    raise RuntimeError(f"Batch embed API {resp.status_code}: {resp.text[:200]}")
                except httpx.TimeoutException:
                    if attempt == 0:
                        await asyncio.sleep(2)
                        continue
                    raise RuntimeError("Batch embed timed out after retry")
        return results


# ── Indexer ───────────────────────────────────────────────────────────────────

class DocumentIndexer:

    def __init__(self, store: LanceDBStore, embedder: OpenRouterEmbedder = None):
        self.store    = store
        self.embedder = embedder or OpenRouterEmbedder()

    # ── Chunking ──────────────────────────────────────────────────────────────

    def _chunk_pages(self, pages: List[Dict]) -> List[Dict]:
        """
        Sliding window chunker that preserves page_number + section_heading.

        Strategy:
          - Build full text with a page boundary map
          - Slide window over full text
          - For each chunk, look up which page it starts on
          - Detect section headings within chunk text
        """
        if not pages:
            return []

        # Build full text + page boundary map [(char_offset, page_num), ...]
        full_text   = ""
        page_breaks = []
        for p in pages:
            page_breaks.append((len(full_text), p["page_num"]))
            full_text += p["text"] + "\n"

        def page_at(char_pos: int) -> int:
            page = page_breaks[0][1] if page_breaks else 1
            for offset, pnum in page_breaks:
                if char_pos >= offset:
                    page = pnum
            return page

        chunks          = []
        pos             = 0
        current_section = ""

        while pos < len(full_text):
            end        = min(pos + CHUNK_SIZE, len(full_text))
            chunk_text = full_text[pos:end].strip()

            if chunk_text:
                # Update current section if heading found in this chunk
                headings = HEADING_RE.findall(chunk_text)
                if headings:
                    current_section = headings[0].strip()

                chunks.append({
                    "text":            chunk_text,
                    "char_start":      pos,
                    "char_end":        end,
                    "page_number":     page_at(pos),
                    "section_heading": current_section,
                })

            pos += CHUNK_SIZE - CHUNK_OVERLAP
            if pos >= len(full_text):
                break

        return chunks

    # ── Deduplication ─────────────────────────────────────────────────────────

    def _delete_existing(self, file_id: str, filename: str):
        try:
            self.store.chunks.delete(f"file_id = '{file_id}'")
            logger.info(f"[{filename}] Deleted old chunks")
        except Exception as e:
            logger.warning(f"[{filename}] Could not delete old chunks: {e}")
        try:
            self.store.documents.delete(f"file_id = '{file_id}'")
            logger.info(f"[{filename}] Deleted old document record")
        except Exception as e:
            logger.warning(f"[{filename}] Could not delete old document record: {e}")

    # ── Index rebuild ─────────────────────────────────────────────────────────

    async def _rebuild_indexes(self, filename: str):
        loop = asyncio.get_event_loop()
        def rebuild():
            try:
                self.store.chunks.create_fts_index("text", replace=True)
                logger.info(f"[{filename}] ✓ FTS index rebuilt")
            except Exception as e:
                logger.error(f"[{filename}] FTS index rebuild failed: {e}")
            try:
                total = self.store.chunks.count_rows()
                if total >= 256:
                    self.store.chunks.create_index(
                        "vector", index_type="IVF_PQ",
                        num_partitions=min(8, max(1, total // 50)),
                        num_sub_vectors=16, replace=True,
                    )
                    logger.info(f"[{filename}] ✓ Vector index rebuilt ({total} rows)")
                else:
                    logger.info(f"[{filename}] Vector index skipped ({total} rows — flat scan)")
            except Exception as e:
                logger.warning(f"[{filename}] Vector index rebuild skipped: {e}")
        await loop.run_in_executor(None, rebuild)

    # ── Main pipeline ─────────────────────────────────────────────────────────

    async def index_document(
        self,
        file_path:    str,
        content:      bytes,
        filename:     str,
        content_type: str,
        source_type:  str = "uploaded",
        client_tag:   str = "",
        project_tag:  str = "",
        domain_tag:   str = "",
    ) -> str:
        file_id = hashlib.sha256(file_path.encode()).hexdigest()[:16]

        if file_id in _indexing_in_progress:
            logger.info(f"[{filename}] Already indexing — skipping duplicate")
            return file_id
        _indexing_in_progress.add(file_id)

        try:
            # Step 1 — Extract with page boundaries
            logger.info(f"[{filename}] Step 1/5 — Extracting pages…")
            data = await extract_pages(content, content_type)

            pages        = data["pages"]
            images_bytes = data.get("images_bytes", [])

            if not pages:
                # Hard fallback — extract flat text and wrap as single page
                logger.warning(f"[{filename}] extract_pages returned empty — falling back to flat extract")
                flat = await extract_text(content, content_type)
                pages = [{"page_num": 1, "text": flat or f"[No extractable text in {filename}]"}]

            total_chars = sum(len(p["text"]) for p in pages)
            logger.info(f"[{filename}] ✓ {len(pages)} pages, {total_chars:,} chars total")

            # Step 2 — Chunk with page + section metadata
            logger.info(f"[{filename}] Step 2/5 — Chunking…")
            raw_chunks = self._chunk_pages(pages)
            if not raw_chunks:
                raise ValueError("No chunks produced — document may be empty")
            logger.info(f"[{filename}] ✓ {len(raw_chunks)} chunks (size={CHUNK_SIZE}, overlap={CHUNK_OVERLAP})")

            # Step 3 — Embed
            logger.info(f"[{filename}] Step 3/5 — Embedding {len(raw_chunks)} chunks…")
            texts   = [
                f"Section: {c['section_heading']}\n{c['text']}" if c['section_heading'] else c['text'] 
                for c in raw_chunks
            ]
            vectors = await self.embedder.embed_batch(texts, batch_size=8)
            logger.info(f"[{filename}] ✓ {len(vectors)} embeddings (dim={len(vectors[0])})")

            # Build Chunk rows
            file_type  = os.path.splitext(filename)[-1].lower().lstrip(".") or "unknown"
            chunk_rows = []
            for i, (c, vec) in enumerate(zip(raw_chunks, vectors)):
                chunk_rows.append(Chunk(
                    chunk_id        = f"{file_id}_chunk_{i}",
                    file_id         = file_id,
                    document_id     = file_id,
                    filename        = filename,
                    file_path       = file_path,
                    text            = c["text"],
                    vector          = vec.tolist(),
                    chunk_index     = i,
                    char_start      = c["char_start"],
                    char_end        = c["char_end"],
                    token_count     = len(c["text"].split()),
                    page_number     = c["page_number"],        # ← NEW
                    section_heading = c["section_heading"],    # ← NEW
                    client_tag      = client_tag,
                    project_tag     = project_tag,
                    domain_tag      = domain_tag,
                ))
            image_metadata = []
        
            for idx, img_bytes in enumerate(images_bytes):
                # Upload image
                img_path = _seaweed.upload_image(img_bytes, file_id, idx)

                # Find exactly which chunks contain this specific image placeholder
                target_marker = f'path="img_{idx}"'
                
                vector_ids = [
                    chunk.chunk_id for chunk in chunk_rows 
                    if target_marker in chunk.text
                ]

                image_metadata.append({
                    "image_path": img_path,
                    "vector_ids": vector_ids
                })

            # Save metadata in Seaweed
            _seaweed.save_image_metadata(file_id, image_metadata)

            # Step 4 — Delete stale + write fresh
            logger.info(f"[{filename}] Step 4/5 — Storing…")
            self._delete_existing(file_id, filename)
            self.store.upsert_document(
                file_id, filename, file_path, content_type, len(content),
                source_type=source_type, file_type=file_type,
            )
            self.store.upsert_chunks(file_id, chunk_rows)
            logger.info(f"[{filename}] ✓ {len(chunk_rows)} chunks written")

            # Step 5 — Rebuild indexes
            logger.info(f"[{filename}] Step 5/5 — Rebuilding indexes…")
            await self._rebuild_indexes(filename)

            logger.info(f"[{filename}] ✓ Indexing complete — file_id={file_id}")
            return file_id

        except Exception as e:
            logger.error(f"[{filename}] Indexing failed: {e}", exc_info=True)
            raise
        finally:
            _indexing_in_progress.discard(file_id)

    def get_stats(self) -> Dict:
        return self.store.get_stats()