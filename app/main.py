# main.py
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import lancedb
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import LANCEDB_PATH, OPENROUTER_KEY, EMBEDDING_MODEL, STATIC_DIR

from storage.lancedb_store  import LanceDBStore
from storage.seaweed        import SeaweedStore
from ingestion.indexer      import DocumentIndexer, OpenRouterEmbedder
from search.engine          import init_search
from qa.agent               import init_agent          # ← was init_qa
from qa.database            import init_db, close_db   # ← new
from qa.session             import init_redis, close_redis  # ← new
from api.ingestion_routes   import router as ingestion_router, init_ingestion_routes
from api.search_routes      import router as search_router
from api.qa_routes          import router as qa_router

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Storage + core blocks ─────────────────────────────────────────────────
    os.makedirs(LANCEDB_PATH, exist_ok=True)

    db       = lancedb.connect(LANCEDB_PATH)
    store    = LanceDBStore(db)
    seaweed  = SeaweedStore()
    embedder = OpenRouterEmbedder(api_key=OPENROUTER_KEY, model=EMBEDDING_MODEL)
    indexer  = DocumentIndexer(store=store, embedder=embedder)

    # ── QA infrastructure (DB + Redis first, then agent) ─────────────────────
    init_db()                          # libSQL — creates tables if needed
    await init_redis()                       # Redis  — fails soft if unavailable

    # ── Wire all blocks ───────────────────────────────────────────────────────
    init_search(store=store, embedder=embedder)
    init_agent(store=store, embedder=embedder)   # ← was init_qa(store=store)
    init_ingestion_routes(indexer=indexer, seaweed=seaweed)

    logger.info("✓ All blocks initialised")
    yield

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    close_db()
    await close_redis()
    logger.info("✓ Clean shutdown complete")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Document Search System", version="3.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"],
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

# Static files
BASE_DIR    = Path(__file__).parent
static_path = BASE_DIR / "static"
static_path.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

# Routers
app.include_router(ingestion_router)
app.include_router(search_router)
app.include_router(qa_router)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
