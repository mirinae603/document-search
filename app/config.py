# config.py
# Every env var and constant lives here. Nothing else calls os.getenv().
import os

# ── SeaweedFS ─────────────────────────────────────────────────────────────────
SEAWEED_FILER = os.getenv("SEAWEED_FILER", "http://localhost:8888")

# ── LanceDB ───────────────────────────────────────────────────────────────────
LANCEDB_PATH  = os.getenv("LANCEDB_PATH", "./data/lancedb")
IMAGE_CHUNK_THRESHOLD = int(os.getenv("IMAGE_CHUNK_THRESHOLD", "2"))
# ── OpenRouter ────────────────────────────────────────────────────────────────
OPENROUTER_KEY      = os.getenv("OPENROUTER_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
EMBEDDING_MODEL     = os.getenv("EMBEDDING_MODEL", "openai/text-embedding-3-small")
LLM_MODEL           = os.getenv("LLM_MODEL",       "openai/gpt-4o")
EMBEDDING_DIM       = int(os.getenv("EMBEDDING_DIM", "1536"))
# add to config.py
QA_MODEL            = os.getenv("QA_MODEL",             LLM_MODEL)
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL",  "https://openrouter.ai/api/v1")

# ── Chunking ──────────────────────────────────────────────────────────────────
CHUNK_SIZE    = int(os.getenv("CHUNK_SIZE",    "500"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "50"))

# ── Reranker ──────────────────────────────────────────────────────────────────
RERANKER_CONFIG_PATH = os.getenv("RERANKER_CONFIG_PATH", "./config/config.yaml")

# ── Static files ──────────────────────────────────────────────────────────────
STATIC_DIR = os.getenv("STATIC_DIR", "./static")

# ── Q&A ───────────────────────────────────────────────────────────────────────
QA_MAX_CONTEXT_CHUNKS = int(os.getenv("QA_MAX_CONTEXT_CHUNKS", "20"))
QA_MAX_CHUNK_CHARS    = int(os.getenv("QA_MAX_CHUNK_CHARS",    "1500"))
QA_MEMORY_WINDOW      = int(os.getenv("QA_MEMORY_WINDOW",      "6"))
QA_TEMPERATURE        = float(os.getenv("QA_TEMPERATURE",      "0.2"))
