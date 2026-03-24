# storage/lancedb_store.py
import logging
from datetime import datetime
from typing import List, Dict

from storage.models import Document, Chunk

logger = logging.getLogger(__name__)

MIN_ROWS_FOR_IVF = 256   # IVF_PQ needs at least this many rows


class LanceDBStore:
    """
    Wraps LanceDB connection.
    Owns: table open/create, document upsert, chunk upsert, FTS rebuild, stats.
    """

    def __init__(self, db_conn):
        self.db = db_conn
        self._init_tables()


    def _init_tables(self):
        # ── documents ─────────────────────────────────────────────────────────
        try:
            self.documents = self.db.open_table("documents")
            existing = set(self.documents.schema.names)
            required = {"file_id","filename","file_path","content_type",
                        "file_size","indexed_at","source_type","file_type"}
            if not required.issubset(existing):
                logger.warning("documents schema outdated — dropping and recreating")
                self.db.drop_table("documents")
                raise Exception("recreate")
            logger.info("✓ documents table ready")
        except Exception:
            self.documents = self.db.create_table("documents", schema=Document)
            logger.info("✓ Created documents table")

        # ── chunks ────────────────────────────────────────────────────────────
        try:
            self.chunks = self.db.open_table("chunks")
            logger.info("✓ chunks table ready")
        except Exception:
            self.chunks = self.db.create_table("chunks", schema=Chunk)
            logger.info("✓ Created chunks table")

        self._ensure_indexes()


    def _ensure_indexes(self):
        self._ensure_fts_index()
        self._ensure_vector_index()


    def _ensure_fts_index(self):
        """Only rebuild FTS if it doesn't already exist."""
        try:
            existing = {idx.name for idx in self.chunks.list_indices()}
            if "text_idx" in existing:
                logger.info("✓ FTS index already exists — skipping rebuild")
                return
            logger.info("Building FTS index on chunks.text …")
            self.chunks.create_fts_index("text", replace=False)
            logger.info("✓ FTS index ready on chunks.text")
        except Exception as e:
            logger.error(f"FTS index creation failed: {e}")


    def _ensure_vector_index(self):
        """Build IVF_PQ if enough rows, skip silently if not."""
        try:
            row_count = self.chunks.count_rows()
        except Exception:
            row_count = 0

        if row_count < MIN_ROWS_FOR_IVF:
            logger.warning(
                f"Vector index skipped — only {row_count} rows "
                f"(need {MIN_ROWS_FOR_IVF}+). Using flat scan until corpus grows."
            )
            return

        try:
            existing = {idx.name for idx in self.chunks.list_indices()}
            if "vector_idx" in existing:
                logger.info("✓ Vector index already exists — skipping rebuild")
                return

            self.chunks.create_index(
                metric="cosine",         # ← was wrong: "vector" is a column, not a metric
                num_partitions=32,       # safe default; raise to 256 at 100k+ chunks
                num_sub_vectors=16,      # must divide embedding_dim (1536 → 16,32,48,96…)
                replace=True,
            )
            logger.info("✓ IVF_PQ vector index ready on chunks.vector")
        except Exception as e:
            logger.warning(f"IVF_PQ index failed ({e}) — flat scan will be used")


    # ── Documents ─────────────────────────────────────────────────────────────

    def upsert_document(
        self, file_id: str, filename: str, file_path: str,
        content_type: str, file_size: int,
        source_type: str = "uploaded", file_type: str = "",
    ):
        try:
            self.documents.delete(f'file_id = "{file_id}"')
        except Exception:
            pass
        self.documents.add([Document(
            file_id      = file_id,
            filename     = filename,
            file_path    = file_path,
            content_type = content_type,
            file_size    = file_size,
            indexed_at   = datetime.utcnow().isoformat(),
            source_type  = source_type,
            file_type    = file_type,
        )])


    def get_all_documents(self) -> List[Dict]:
        try:
            return self.documents.search().to_list()
        except Exception:
            return []


    # ── Chunks ────────────────────────────────────────────────────────────────

    def upsert_chunks(self, file_id: str, rows: List[Chunk]):
        try:
            self.chunks.delete(f'file_id = "{file_id}"')
        except Exception:
            pass
        self.chunks.add(rows)
        # Rebuild indexes after new data is added
        self._ensure_fts_index_after_upsert()
        self._ensure_vector_index()


    def _ensure_fts_index_after_upsert(self):
        """
        After upsert, FTS must be rebuilt to include new chunks.
        Unlike vector index, FTS does NOT auto-update on row insert.
        """
        try:
            self.chunks.create_fts_index("text", replace=True)
            logger.info("✓ FTS index rebuilt after upsert")
        except Exception as e:
            logger.warning(f"FTS rebuild after upsert failed: {e}")


    def rebuild_fts_index(self):
        """Manual FTS rebuild — call after bulk indexing jobs."""
        try:
            self.chunks.create_fts_index("text", replace=True)
            logger.info("✓ FTS index rebuilt")
        except Exception as e:
            logger.warning(f"FTS rebuild failed: {e}")


    def refresh(self):
        """Re-open table references after external writes."""
        try:
            self.chunks    = self.db.open_table("chunks")
            self.documents = self.db.open_table("documents")
        except Exception as e:
            logger.warning(f"Store refresh failed: {e}")


    # ── Stats ─────────────────────────────────────────────────────────────────

    def get_stats(self) -> Dict:
        try:
            docs   = self.documents.to_pandas()
            chunks = self.chunks.to_pandas()
            return {
                "total_documents":  len(docs),
                "total_chunks":     len(chunks),
                "total_size_bytes": int(docs["file_size"].sum()) if len(docs) > 0 else 0,
            }
        except Exception:
            return {"total_documents": 0, "total_chunks": 0, "total_size_bytes": 0}
