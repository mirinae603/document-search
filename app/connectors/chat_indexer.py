# connectors/chat_indexer.py
# ChatDocumentIndexer — indexes ConversationDoc objects into the same
# LanceDB/SeaweedFS pipeline that DocumentIndexer uses for files.
#
# Key differences from DocumentIndexer:
#   - Splits at turn boundaries, not at arbitrary character positions
#   - Adds a context header to every chunk (platform, participants, channel)
#     so the LLM can answer "what did Alice say to Bob about X?"
#   - Stores raw JSON in SeaweedFS at /conversations/{platform}/{chat_id}.json
#   - Sets source_type="{platform}_connector" and file_type="conversation"
#     so existing search filters work without modification
#   - Writes conversation metadata fields: participants, thread_id, platform, sent_at
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Dict, List

from config import CHAT_CHUNK_CHARS, CHAT_CHUNK_OVERLAP
from connectors.models import ConversationDoc, MessageTurn
from ingestion.indexer import DocumentIndexer, OpenRouterEmbedder
from storage.lancedb_store import LanceDBStore
from storage.models import Chunk
from storage.seaweed import SeaweedStore

logger = logging.getLogger(__name__)


class ChatDocumentIndexer:
    """
    Ingestion pipeline for conversation data.

    Usage:
        indexer = ChatDocumentIndexer(store, embedder, seaweed)
        file_id = await indexer.index_conversation(doc)
    """

    def __init__(
        self,
        store:    LanceDBStore,
        embedder: OpenRouterEmbedder,
        seaweed:  SeaweedStore,
    ):
        self.store    = store
        self.embedder = embedder
        self.seaweed  = seaweed
        # Reuse the base indexer solely for _rebuild_indexes
        self._base = DocumentIndexer(store=store, embedder=embedder)

    # ── Public API ────────────────────────────────────────────────────────────

    async def index_conversation(self, doc: ConversationDoc) -> str:
        """
        Full pipeline:
          1. Serialise conversation → SeaweedFS JSON
          2. Chunk by turn boundaries (with overlap)
          3. Embed each chunk (prepend context header)
          4. Upsert Document + Chunk rows to LanceDB
          5. Rebuild FTS + vector indexes
        Returns the stable file_id for this conversation.
        """
        file_id   = self._file_id(doc)
        file_path = self._seaweed_path(doc)
        filename  = f"{doc.platform}_{doc.chat_id}.json"

        logger.info(
            f"[{filename}] Indexing {len(doc.messages)} messages "
            f"({doc.participants_str()})"
        )

        # Step 1 — Persist raw JSON to SeaweedFS ─────────────────────────────
        raw_json = json.dumps(doc.to_dict(), ensure_ascii=False, indent=2).encode()
        ok = self.seaweed.upload(file_path, raw_json, "application/json")
        if not ok:
            logger.warning(f"[{filename}] SeaweedFS upload failed — indexing anyway")

        # Step 2 — Chunk ──────────────────────────────────────────────────────
        raw_chunks = self._chunk_conversation(doc)
        if not raw_chunks:
            logger.warning(f"[{filename}] No chunks produced — skipping")
            return file_id

        logger.info(f"[{filename}] {len(raw_chunks)} chunks")

        # Step 3 — Embed ──────────────────────────────────────────────────────
        context_header = (
            f"Platform: {doc.platform} | "
            f"Participants: {doc.participants_str()} | "
            f"Channel: {doc.channel_name or 'Direct Message'}"
        )
        texts   = [f"{context_header}\n\n{c['text']}" for c in raw_chunks]
        vectors = await self.embedder.embed_batch(texts, batch_size=8)
        logger.info(f"[{filename}] {len(vectors)} embeddings")

        # Step 4 — Build Chunk rows ───────────────────────────────────────────
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
                token_count     = c["token_count"],
                page_number     = i + 1,
                section_heading = c["section_heading"],
                client_tag      = "",
                project_tag     = "",
                domain_tag      = "",
                # Conversation-specific metadata
                participants    = c["participants"],
                thread_id       = c["thread_id"],
                platform        = c["platform"],
                sent_at         = c["sent_at"],
            ))

        # Step 5 — Upsert ─────────────────────────────────────────────────────
        self.store.upsert_document(
            file_id      = file_id,
            filename     = filename,
            file_path    = file_path,
            content_type = "application/json",
            file_size    = len(raw_json),
            source_type  = f"{doc.platform}_connector",
            file_type    = "conversation",
        )
        self.store.upsert_chunks(file_id, chunk_rows)
        logger.info(f"[{filename}] ✓ {len(chunk_rows)} chunks written — file_id={file_id}")

        return file_id

    # ── Turn-boundary chunking ────────────────────────────────────────────────

    def _chunk_conversation(self, doc: ConversationDoc) -> List[Dict]:
        """
        Sliding window over message turns.

        Strategy:
          - Accumulate turns until the buffer exceeds CHAT_CHUNK_CHARS
          - Flush the buffer as a chunk, keeping the last CHAT_CHUNK_OVERLAP
            turns as the overlap window for the next chunk
          - Each chunk is formatted as plain text with speaker attribution
          - section_heading is the channel/DM label (improves retrieval)

        Why not character splitting?
          Splitting at an arbitrary offset can cut mid-sentence, mid-reply,
          or mid-attribution.  Turn-boundary splitting guarantees each chunk
          starts with a speaker label and ends at a natural sentence boundary.
        """
        if not doc.messages:
            return []

        chunks:     List[Dict]       = []
        buf_turns:  List[MessageTurn] = []
        buf_chars   = 0
        chunk_index = 0

        def _format_turn(t: MessageTurn) -> str:
            name = doc.display_names.get(t.sender_id, t.sender_name)
            return f"[{name} | {t.sent_at[:16]}] {t.content}\n"

        def _flush(turns: List[MessageTurn]) -> Dict:
            nonlocal chunk_index
            lines = [_format_turn(t) for t in turns]
            text  = "".join(lines)
            result = {
                "text":          text,
                "chunk_index":   chunk_index,
                "char_start":    0,          # relative to this chunk
                "char_end":      len(text),
                "token_count":   len(text.split()),
                "participants":  json.dumps(doc.participants),
                "thread_id":     doc.chat_id,
                "platform":      doc.platform,
                "channel_name":  doc.channel_name or "",
                "sent_at":       turns[-1].sent_at if turns else "",
                "section_heading": (
                    doc.channel_name or doc.participants_str()
                ),
            }
            chunk_index += 1
            return result

        for turn in doc.messages:
            turn_chars = len(_format_turn(turn))

            day_changed = False
            if buf_turns:
                # Compare "YYYY-MM-DD" of current message vs previous
                if turn.sent_at[:10] != buf_turns[-1].sent_at[:10]:
                    day_changed = True

            # Flush when buffer exceeds the char limit OR the day changes
            if (buf_chars + turn_chars > CHAT_CHUNK_CHARS or day_changed) and buf_turns:
                chunks.append(_flush(buf_turns))
                
                # If the day changed, start a completely fresh box
                if day_changed:
                    buf_turns = []
                    buf_chars = 0
                else:
                    # Normal overlap for long conversations on the same day
                    buf_turns = buf_turns[-CHAT_CHUNK_OVERLAP:]
                    buf_chars = sum(len(_format_turn(t)) for t in buf_turns)

            buf_turns.append(turn)
            buf_chars += turn_chars

        # Flush any remaining turns
        if buf_turns:
            chunks.append(_flush(buf_turns))

        return chunks

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _file_id(doc: ConversationDoc) -> str:
        """Deterministic 16-char ID — same key always produces same ID."""
        return hashlib.sha256(doc.document_key().encode()).hexdigest()[:16]

    @staticmethod
    def _seaweed_path(doc: ConversationDoc) -> str:
        return f"/conversations/{doc.platform}/{doc.chat_id}.json"
