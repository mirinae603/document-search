# import os, json, asyncio, hashlib, logging
# from datetime import datetime
# from typing import List, Dict, Optional

# import numpy as np
# import lancedb
# from lancedb.pydantic import LanceModel, Vector

# from embeddings import OpenRouterEmbeddings
# from extractor import extract_document_text

# logger = logging.getLogger(__name__)

# EMBEDDING_DIM = 1536
# _indexing_in_progress: set = set()


# class Document(LanceModel):
#     file_id: str
#     filename: str
#     file_path: str
#     text: str
#     vector: Vector(EMBEDDING_DIM)
#     content_type: str
#     file_size: int
#     indexed_at: str
#     metadata: Optional[str] = "{}"


# class DocumentIndexer:
#     def __init__(self, db_conn, embedder: OpenRouterEmbeddings):
#         self.db = db_conn
#         self.embedder = embedder
#         self.table_name = "documents"
#         self._init_table()

#     def _init_table(self):
#         try:
#             self.table = self.db.open_table(self.table_name)
#             logger.info("✓ Opened existing table")
#         except Exception:
#             self.table = self.db.create_table(self.table_name, schema=Document)
#             self.table.create_fts_index("text", replace=True)
#             logger.info("✓ Created new table with FTS index")

#     def _chunk_text(self, text: str, max_chars: int = 6000) -> List[str]:
#         if len(text) <= max_chars:
#             return [text]

#         chunks, current, current_len = [], [], 0
#         for word in text.split():
#             if current_len + len(word) > max_chars:
#                 chunks.append(" ".join(current))
#                 current, current_len = [word], len(word)
#             else:
#                 current.append(word)
#                 current_len += len(word) + 1

#         if current:
#             chunks.append(" ".join(current))
#         return chunks

#     async def index_document(
#         self, file_path: str, content: bytes, filename: str, content_type: str
#     ) -> str:
#         file_id = hashlib.sha256(file_path.encode()).hexdigest()[:16]

#         if file_id in _indexing_in_progress:
#             logger.info(f"⏭ Already indexing {filename}, skipping duplicate")
#             return file_id
#         _indexing_in_progress.add(file_id)

#         try:
#             logger.info(f"Extracting text from {filename}...")
#             text = await extract_document_text(content, content_type)
#             if not text or len(text.strip()) < 10:
#                 text = f"[No text extracted from {filename}]"
#             logger.info(f"✓ Extracted {len(text)} chars")

#             chunks = self._chunk_text(text)
#             logger.info(f"Split into {len(chunks)} chunks")

#             embeddings = []
#             for i, chunk in enumerate(chunks, 1):
#                 logger.info(f"Embedding chunk {i}/{len(chunks)}...")
#                 embeddings.append(await self.embedder.embed_text(chunk))

#             avg_vector = np.mean(embeddings, axis=0).tolist()
#             metadata_json = json.dumps({
#                 "chunks": len(chunks),
#                 "char_count": len(text),
#                 "upload_timestamp": datetime.now().isoformat()
#             })

#             doc = Document(
#                 file_id=file_id,
#                 filename=filename,
#                 file_path=file_path,
#                 text=text,
#                 vector=avg_vector,
#                 content_type=content_type,
#                 file_size=len(content),
#                 indexed_at=datetime.now().isoformat(),
#                 metadata=metadata_json
#             )

#             try:
#                 self.table.delete(f'file_id = "{file_id}"')
#                 logger.info(f"  Removed old index entry for {file_id}")
#             except Exception:
#                 pass

#             self.table.add([doc])

#             loop = asyncio.get_event_loop()
#             await loop.run_in_executor(
#                 None, lambda: self.table.create_fts_index("text", replace=True)
#             )

#             logger.info(f"✓ Indexed + FTS rebuilt: {filename} → {file_id}")
#             return file_id

#         except Exception as e:
#             logger.error(f"Failed to index {filename}: {e}")
#             raise
#         finally:
#             _indexing_in_progress.discard(file_id)

#     def get_stats(self) -> Dict:
#         try:
#             df = self.table.to_pandas()
#             return {
#                 "total_documents": len(df),
#                 "total_size_bytes": int(df["file_size"].sum()) if len(df) > 0 else 0
#             }
#         except Exception:
#             return {"total_documents": 0, "total_size_bytes": 0}

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
    content_type: str
    file_size: int
    indexed_at: str


class Chunk(LanceModel):
    chunk_id: str
    file_id: str
    document_id: str
    filename: str
    file_path: str
    text: str
    vector: Vector(EMBEDDING_DIM)
    chunk_index: int
    char_start: int
    char_end: int
    token_count: int
    client_tag: str
    project_tag: str
    domain_tag: str


class DocumentIndexer:
    def __init__(self, db_conn, embedder: OpenRouterEmbeddings):
        self.db = db_conn
        self.embedder = embedder
        self.document_table_name = "documents"
        self.chunk_table_name = "chunks"
        self._init_table()
        

    def _init_table(self):
        try:
            self.document_table = self.db.open_table(self.document_table_name)
        except Exception:
            self.document_table = self.db.create_table(self.document_table_name, schema=Document)

        try:
            self.chunk_table = self.db.open_table(self.chunk_table_name)

        except Exception:
            self.chunk_table = self.db.create_table(self.chunk_table_name, schema=Chunk)
            self.chunk_table.create_fts_index("text", replace=True)


    def _chunk_text(self, text: str, max_chars: int = 1200, overlap: int = 100) -> List[Dict]:
        chunks = []
        position = 0

        while position < len(text):
            char_start = position
            char_end = position + max_chars

            chunk_text = text[char_start:char_end]

            chunks.append({
                "text": chunk_text,
                "char_start": char_start,
                "char_end": char_end
            })

            position = position + max_chars - overlap
            
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
            # SECTION 3 — Extract text
            logger.info(f"Extracting text from {filename}...")
            text = await extract_document_text(content, content_type)
            if not text or len(text.strip()) < 10:
                text = f"[No text extracted from {filename}]"
            logger.info(f"✓ Extracted {len(text)} chars")


            # SECTION 4 — Chunk text
            chunks = self._chunk_text(text)
            logger.info(f"✓ Split into {len(chunks)} chunks")

            # SECTION 5A — Store document row

            try:
                self.document_table.delete(f'file_id = "{file_id}"')
            except Exception:
                pass
                


            doc = Document(
                file_id=file_id,
                filename=filename,
                file_path=file_path,
                content_type=content_type,
                file_size=len(content),
                indexed_at=datetime.now().isoformat()
            )
            
            self.document_table.add([doc])
            logger.info(f"✓ Stored document row: {filename}")

            # SECTION 5B — Embed and store each chunk
            try:
                self.chunk_table.delete(f'file_id = "{file_id}"')
            except Exception:
                pass

            chunk_rows = []
            for i, chunk in enumerate(chunks):
                logger.info(f"Embedding chunk {i+1}/{len(chunks)}...")
                vector = await self.embedder.embed_text(chunk['text'])
                chunk_rows.append(Chunk(
                    chunk_id=f"{file_id}_chunk_{i}",
                    file_id=file_id,
                    document_id=file_id,
                    filename=filename,
                    file_path=file_path,
                    text=chunk['text'],
                    vector=vector.tolist(),
                    chunk_index=i,
                    char_start=chunk['char_start'],
                    char_end=chunk['char_end'],
                    token_count=len(chunk['text']),
                    client_tag="",
                    project_tag="",
                    domain_tag=""
                ))




                 

            self.chunk_table.add(chunk_rows)
            logger.info(f"✓ Stored {len(chunk_rows)} chunks")

            # SECTION 5C — Rebuild FTS index
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: self.chunk_table.create_fts_index("text", replace=True)
            )
            logger.info(f"✓ FTS index rebuilt")

            return file_id

        except Exception as e:
            logger.error(f"Failed to index {filename}: {e}")
            raise
        finally:
            _indexing_in_progress.discard(file_id)

    def get_stats(self) -> Dict:
        try:
            docs = self.document_table.to_pandas()
            chunks = self.chunk_table.to_pandas()

            return {
                "total_documents": len(docs),
                "total_chunks": len(chunks),
                "total_size_bytes": int(docs["file_size"].sum()) if len(docs) > 0 else 0
            }

        except Exception:
            return {
                "total_documents": 0,
                "total_chunks": 0,
                "total_size_bytes": 0
            }