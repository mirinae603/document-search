# config.py
# Every env var and constant lives here. Nothing else calls os.getenv().
import os

# ── SeaweedFS ─────────────────────────────────────────────────────────────────
SEAWEED_FILER = os.getenv("SEAWEED_FILER", "http://localhost:8888")

# ── LanceDB ───────────────────────────────────────────────────────────────────
LANCEDB_PATH  = os.getenv("LANCEDB_PATH", "/home/azureuser/document-search/data/lancedb")
IMAGE_CHUNK_THRESHOLD = int(os.getenv("IMAGE_CHUNK_THRESHOLD", "2"))
LLM_PROVIDER        = os.getenv("LLM_PROVIDER", "azure")
# ── OpenRouter ────────────────────────────────────────────────────────────────
OPENROUTER_KEY      = os.getenv("OPENROUTER_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
EMBEDDING_MODEL     = os.getenv("EMBEDDING_MODEL", "openai/text-embedding-3-small")
LLM_MODEL           = os.getenv("LLM_MODEL",       "openai/gpt-4o-mini")
EMBEDDING_DIM       = int(os.getenv("EMBEDDING_DIM", "1536"))

AZURE_OPENAI_KEY         = os.getenv("AZURE_OPENAI_KEY", "")
AZURE_OPENAI_ENDPOINT    = os.getenv("AZURE_OPENAI_ENDPOINT", "https://kv-test-4-0-mini.openai.azure.com/")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
AZURE_DEPLOYMENT         = os.getenv("AZURE_DEPLOYMENT", "gpt-4o-mini")


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

# ── Teams Connector ───────────────────────────────────────────────────────────
TEAMS_CLIENT_ID     = os.getenv("TEAMS_CLIENT_ID", "")
TEAMS_CLIENT_SECRET = os.getenv("TEAMS_CLIENT_SECRET", "")
TEAMS_TENANT_ID     = os.getenv("TEAMS_TENANT_ID","")
TEAMS_REDIRECT_URI  = os.getenv(
    "TEAMS_REDIRECT_URI", "http://localhost:3001/oauth/callback"
)
TEAMS_SCOPES = [
    "User.Read",
    "Chat.Read",
    "Mail.Read",
    "ChannelMessage.Read.All",
    "Team.ReadBasic.All",
    # Read-only calendar access for the /calendar/prep meeting-prep endpoint.
    # NOTE: adding this scope requires the user to RE-CONSENT on the OAuth app
    # before the existing Microsoft Graph token will grant calendar access.
    "Calendars.Read",
    # Read-only directory lookup, required for GET /users/{email} to resolve
    # meeting attendees (other users in the tenant) to their Graph UIDs so the
    # meeting-prep participant filter can match Teams chunks. User.Read (above)
    # already covers GET /me. NOTE: also requires re-consent on the OAuth app.
    "User.ReadBasic.All",
]
# How often the background scheduler polls for new messages (minutes)
CONNECTOR_SYNC_INTERVAL_MINUTES = int(os.getenv("CONNECTOR_SYNC_INTERVAL_MINUTES", "15"))
# Max messages to pull per chat per sync run (caps API cost)
CONNECTOR_MAX_MESSAGES_PER_CHAT = int(os.getenv("CONNECTOR_MAX_MESSAGES_PER_CHAT", "500"))
# Turn-boundary chunk size (characters)
CHAT_CHUNK_CHARS   = int(os.getenv("CHAT_CHUNK_CHARS",   "1500"))
CHAT_CHUNK_OVERLAP = int(os.getenv("CHAT_CHUNK_OVERLAP", "2"))    # turns
CONNECTOR_MAX_THREADS           = int(os.getenv("CONNECTOR_MAX_THREADS",           "200"))
# Max individual emails per thread
CONNECTOR_MAX_EMAILS_PER_THREAD = int(os.getenv("CONNECTOR_MAX_EMAILS_PER_THREAD", "50"))

OUTLOOK_SYNC_FOLDERS = [
    f.strip() for f in
    os.getenv("OUTLOOK_SYNC_FOLDERS", "Inbox,Sent Items").split(",")
    if f.strip()
]
INTEL_SUMMARY_HOURS    = int(os.getenv("INTEL_SUMMARY_HOURS",    "24"))
# Default look-back window for priority emails (hours)
INTEL_PRIORITY_HOURS   = int(os.getenv("INTEL_PRIORITY_HOURS",   "48"))
# Max email threads to include in one priority ranking call
INTEL_PRIORITY_TOP_N   = int(os.getenv("INTEL_PRIORITY_TOP_N",   "10"))