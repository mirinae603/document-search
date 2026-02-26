import os, json, asyncio, hashlib, logging
from datetime import datetime
from typing import List, Dict, Optional

import numpy as np
import lancedb
from lancedb.pydantic import LanceModel, Vector

from embeddings import OpenRouterEmbeddings
from extractor import extract_document_text

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 1536
_indexing_in_progress: set = set()


class Document(LanceModel):
    file_id: str
    filename: str
    file_path: str
    text: str
    vector: Vector(EMBEDDING_DIM)
    content_type: str
    file_size: int
    indexed_at: str
    metadata: Optional[str] = "{}"


class DocumentIndexer:
    def __init__(self, db_conn, embedder: OpenRouterEmbeddings):
        self.db = db_conn
        self.embedder = embedder
        self.table_name = "documents"
        self._init_table()

    def _init_table(self):
        try:
            self.table = self.db.open_table(self.table_name)
            logger.info("✓ Opened existing table")
        except Exception:
            self.table = self.db.create_table(self.table_name, schema=Document)
            self.table.create_fts_index("text", replace=True)
            logger.info("✓ Created new table with FTS index")

    def _chunk_text(self, text: str, max_chars: int = 6000) -> List[str]:
        if len(text) <= max_chars:
            return [text]

        chunks, current, current_len = [], [], 0
        for word in text.split():
            if current_len + len(word) > max_chars:
                chunks.append(" ".join(current))
                current, current_len = [word], len(word)
            else:
                current.append(word)
                current_len += len(word) + 1

        if current:
            chunks.append(" ".join(current))
        return chunks

    async def index_document(
        self, file_path: str, content: bytes, filename: str, content_type: str
    ) -> str:
        file_id = hashlib.sha256(file_path.encode()).hexdigest()[:16]

        if file_id in _indexing_in_progress:
            logger.info(f"⏭ Already indexing {filename}, skipping duplicate")
            return file_id
        _indexing_in_progress.add(file_id)

        try:
            logger.info(f"Extracting text from {filename}...")
            text = await extract_document_text(content, content_type)
            if not text or len(text.strip()) < 10:
                text = f"[No text extracted from {filename}]"
            logger.info(f"✓ Extracted {len(text)} chars")

            chunks = self._chunk_text(text)
            logger.info(f"Split into {len(chunks)} chunks")

            embeddings = []
            for i, chunk in enumerate(chunks, 1):
                logger.info(f"Embedding chunk {i}/{len(chunks)}...")
                embeddings.append(await self.embedder.embed_text(chunk))

            avg_vector = np.mean(embeddings, axis=0).tolist()
            metadata_json = json.dumps({
                "chunks": len(chunks),
                "char_count": len(text),
                "upload_timestamp": datetime.now().isoformat()
            })

            doc = Document(
                file_id=file_id,
                filename=filename,
                file_path=file_path,
                text=text,
                vector=avg_vector,
                content_type=content_type,
                file_size=len(content),
                indexed_at=datetime.now().isoformat(),
                metadata=metadata_json
            )

            try:
                self.table.delete(f'file_id = "{file_id}"')
                logger.info(f"  Removed old index entry for {file_id}")
            except Exception:
                pass

            self.table.add([doc])

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: self.table.create_fts_index("text", replace=True)
            )

            logger.info(f"✓ Indexed + FTS rebuilt: {filename} → {file_id}")
            return file_id

        except Exception as e:
            logger.error(f"Failed to index {filename}: {e}")
            raise
        finally:
            _indexing_in_progress.discard(file_id)

    def get_stats(self) -> Dict:
        try:
            df = self.table.to_pandas()
            return {
                "total_documents": len(df),
                "total_size_bytes": int(df["file_size"].sum()) if len(df) > 0 else 0
            }
        except Exception:
            return {"total_documents": 0, "total_size_bytes": 0}
