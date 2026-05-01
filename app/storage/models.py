# storage/models.py
# LanceDB table schemas.
# To add a column: add the field here, then update lancedb_store.py upsert methods.
from typing import Optional
from lancedb.pydantic import LanceModel, Vector
from config import EMBEDDING_DIM



class Document(LanceModel):
    """
    One row per indexed file — metadata only, no text or vector.
    source_type and file_type are used by search filters.
    """
    file_id:      str
    filename:     str
    file_path:    str
    content_type: str
    file_size:    int
    indexed_at:   str             # ISO-8601 UTC
    source_type:  Optional[str] = "uploaded"   # uploaded | webhook | scoped_ingest
    file_type:    Optional[str] = ""           # pdf | docx | txt | xlsx …



class Chunk(LanceModel):
    """
    One row per text chunk.
    FTS index on `text`      → powers lexical search.
    Vector index on `vector` → powers semantic / hybrid search.
    """
    chunk_id:        str
    file_id:         str
    document_id:     str              # same as file_id — kept for API symmetry
    filename:        str
    file_path:       str
    text:            str
    vector:          Vector(EMBEDDING_DIM)
    chunk_index:     int
    char_start:      int
    char_end:        int
    token_count:     int
    # Scope tags — empty string = unscoped document
    client_tag:      str = ""
    project_tag:     str = ""
    domain_tag:      str = ""
    # Page + section location — populated by extract_pages() at ingestion
    page_number:     int = 0          # ← NEW: physical page (1-based); 0 = unknown
    section_heading: str = ""         # ← NEW: nearest heading above this chunk
    # Conversation connector fields — populated by ChatDocumentIndexer
    # Empty string on all regular document chunks; non-empty on Teams/Slack chunks.
    participants:    str = ""         # JSON list: '["user_a@co.com","user_b@co.com"]'
    thread_id:       str = ""         # chat_id or team_id+channel_id
    platform:        str = ""         # "teams" | "slack" | ""
    sent_at:         str = ""         # ISO-8601 of the earliest message in this chunk
