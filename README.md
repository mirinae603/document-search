# Document Search System 📚🔍

A production-ready **hybrid document search and Q&A system** combining lexical search, semantic search, and AI-powered question answering. Built with FastAPI, LanceDB, and OpenRouter.

---

## Features ✨

- **Hybrid Search** — Lexical (full-text) + semantic (vector) + combined scoring
- **Document Ingestion** — Support for PDF, text, images, and more via Kreuzberg extractor + OCR
- **Q&A Agent** — Context-aware question answering with multi-turn conversation support
- **Reranking** — Jina/Cohere reranking for improved result relevance
- **Multi-Embedding Providers** — OpenRouter, local sentence-transformers, or custom
- **File Storage** — Distributed file hosting via SeaweedFS
- **Vector DB** — LanceDB with full-text search indexing
- **Webhook Ingestion** — Automatic indexing on file upload via SeaweedFS webhooks
- **Production Ready** — Async/await, error handling, comprehensive logging

---

## Architecture 🏗️

```
┌─────────────┐
│   FastAPI   │  (Main API server)
└──────┬──────┘
       │
   ┌───┴─────────────────┬──────────────┐
   │                     │              │
┌──▼────────┐  ┌─────────▼──┐  ┌──────▼─────┐
│  LanceDB  │  │ SeaweedFS  │  │ OpenRouter │
│ (Vectors) │  │  (Storage) │  │   (LLM)    │
└───────────┘  └────────────┘  └────────────┘
   │                                  │
   └──────────┬───────────────────────┘
              │
        ┌─────▼──────┐
        │  Q&A Agent │
        └────────────┘
```

### Core Components

- **Embeddings** — `embeddings.py` — OpenRouter or local sentence transformers
- **Indexer** — `indexer.py` — Text chunking, embedding, and LanceDB storage
- **Search Engine** — `search.py` — Lexical, semantic, and hybrid search with reranking
- **Q&A Agent** — `qa_agent.py` — Context retrieval and LLM-powered answering
- **Reranker** — `reranker.py` — Jina/Cohere reranking for top-k results
- **Document Extractor** — `extractor.py` — Multi-format text extraction (PDF, OCR, etc.)

---

## Prerequisites 📋

- **Python 3.10+**
- **Tesseract OCR** — for scanned PDF support (macOS: `brew install tesseract`)
- **Redis** (optional but recommended) — for caching (macOS: `brew install redis`)
- **API Keys**:
  - OpenRouter API key (for embeddings/LLM)
  - Jina API key (optional, for reranking)

---

## Quick Start 🚀

### 1️⃣ Setup (One-time)

```bash
cd document-search
./scripts/setup.sh
```

This will:
- ✅ Create Python virtual environment
- ✅ Install all dependencies
- ✅ Download/verify SeaweedFS binary (auto-detects OS & arch)
- ✅ Create required directories
- ✅ Warn about missing API keys

### 2️⃣ Configure

Edit `config/config.yaml` to set your API keys:

### 3️⃣ Start Services

```bash
./scripts/dev.sh all
```

This will start **all services**:
- 🗄️ Redis (:6379)
- 📦 SeaweedFS Master (:9333)
- 📂 SeaweedFS Volume (:8080)
- 📋 SeaweedFS Filer (:8888)
- 🚀 FastAPI Server (:8000)

**Server available at:** `http://localhost:8000`

---

## Development Commands 🎯

```bash

  ./scripts/dev.sh all           — start all services
  ./scripts/dev.sh fastapi       — hot-reload FastAPI in foreground
  ./scripts/dev.sh fastapi bg    — restart FastAPI in background
  ./scripts/dev.sh seaweed       — restart SeaweedFS only
  ./scripts/dev.sh redis         — restart Redis only
  ./scripts/dev.sh status        — show what's running
  ./scripts/dev.sh logs          — tail all log files

```

---

## What Gets Started? ✨

| Service | Port | Purpose |
|---------|------|---------|
| **FastAPI** | 8000 | Main API (search, Q&A, upload) |
| **Redis** | 6379 | Caching layer (optional) |
| **SeaweedFS Master** | 9333 | Distributed file storage coordinator |
| **SeaweedFS Volume** | 8080 | File data volumes |
| **SeaweedFS Filer** | 8888 | HTTP file API + webhooks |
| **LanceDB** | Local | Vector database (in-process) |

---

## API Endpoints 🔗

### Search

#### `GET /search` — Hybrid Search

```bash
curl "http://localhost:8000/search?query=error+handling&type=hybrid&limit=10"
```

- **query**: Search string
- **type**: `lexical` | `semantic` | `hybrid` (default)
- **limit**: Results to return (1-50, default 10)

**Response:**

```json
{
  "query": "error handling",
  "type": "hybrid",
  "count": 5,
  "results": [
    {
      "document_id": "doc-123",
      "filename": "guide.pdf",
      "relevance_score": 0.95,
      "text": "Error handling in production...",
      "file_path": "/documents/guide.pdf"
    }
  ]
}
```

#### `GET /search/compare` — Compare Search Methods

```bash
curl "http://localhost:8000/search/compare?query=database+optimization&limit=5"
```

Returns results from lexical, semantic, and hybrid methods for comparison.

### Document Management

#### `POST /upload` — Upload Document

```bash
curl -X POST -F "file=@document.pdf" http://localhost:8000/upload
```

**Response:**

```json
{
  "status": "uploaded",
  "filename": "document.pdf",
  "size": 102400,
  "message": "Indexing will begin shortly via filer webhook"
}
```

#### `GET /preview` — Preview Document

```bash
curl "http://localhost:8000/preview?file_path=/documents/guide.pdf" -o guide.pdf
```

### Q&A

#### `POST /qa/ask` — Ask Question

```bash
curl -X POST "http://localhost:8000/qa/ask?question=How+to+handle+errors?&top_k=5&mode=document"
```

**Response:**

```json
{
  "question": "How to handle errors?",
  "answer": "Error handling involves try-catch blocks...",
  "sources": [
    {
      "document_id": "doc-123",
      "filename": "guide.pdf",
      "relevance_score": 0.92,
      "excerpt": "..."
    }
  ]
}
```

#### `POST /qa/conversation` — Multi-turn Conversation

```bash
curl -X POST http://localhost:8000/qa/conversation \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "user", "content": "What is async/await?"},
      {"role": "assistant", "content": "Async/await is..."},
      {"role": "user", "content": "How to use it in FastAPI?"}
    ],
    "top_k": 5,
    "mode": "document"
  }'
```

#### `GET /qa/sources` — Retrieve Source Documents

```bash
curl "http://localhost:8000/qa/sources?question=caching+strategies&top_k=5"
```

### Admin

#### `GET /stats` — System Statistics

```bash
curl http://localhost:8000/stats
```

#### `GET /health` — Health Check

```bash
curl http://localhost:8000/health
```

#### `POST /admin/reindex` — Reindex Documents

```bash
# Reindex all documents
curl -X POST "http://localhost:8000/admin/reindex?mode=all"

# Reindex specific file
curl -X POST "http://localhost:8000/admin/reindex?mode=file&file_path=/documents/guide.pdf"

# Reindex by date range
curl -X POST "http://localhost:8000/admin/reindex?mode=date_range&date_from=2025-01-01T00:00:00&date_to=2025-12-31T23:59:59"
```

---

## WebHook Integration 🔄

When a file is uploaded to SeaweedFS, a webhook is triggered to automatically index it:

```bash
POST /webhook/seaweed
Content-Type: application/json

{
  "event_type": "PUT",
  "key": "/documents/myfile.pdf",
  "message": {
    "new_entry": {
      "attributes": {
        "mime": "application/pdf"
      }
    }
  }
}
```

---
## File Structure 📁

```
document-search/
├── app/
│   ├── main.py                    # FastAPI application
│   ├── config.py                  # Configuration loader
│   ├── embeddings.py              # Embedding providers (OpenRouter, local)
│   ├── indexer.py                 # Document indexing logic
│   ├── search.py                  # Hybrid search with reranking
│   ├── reranker.py                # Reranker integration (Jina, Cohere)
│   ├── qa_agent.py                # Q&A engine with multi-turn support
│   ├── extractor.py               # Document text extraction
│   ├── scoped_router.py           # Scoped Q&A routes
│   └── static/                    # Frontend assets (HTML, JS)
├── config/
│   ├── config.yaml                # Main configuration (edit this!)
│   ├── filer.toml                 # SeaweedFS filer config
│   └── notification.toml          # Webhook notifications config
├── data/
│   ├── lancedb/                   # Vector database storage
│   └── seaweedfs/                 # File storage
├── logs/                          # Application logs (check here for errors)
├── scripts/
│   ├── setup.sh                   # ⭐ One-time setup
│   ├── dev.sh                     # ⭐ Start/stop/manage all services
│   ├── logs.sh                    # View logs
│   └── status.sh                  # Check status
├── requirements.txt               # Python dependencies
├── sample_test.py                 # Testing utilities
└── README.md                      # This file
```

---



## Troubleshooting 🐛

**Check service status:**
```bash
./scripts/dev.sh status
```

**View logs:**
```bash
./scripts/dev.sh logs
```

**Port conflicts (e.g., 8888 already in use):**
```bash
# Automatically handled by dev.sh — it will kill and restart
./scripts/dev.sh restart
```

**SeaweedFS issues:**
- Logs: `tail -f logs/seaweed.log`
- Reset data: `./scripts/dev.sh reset` (stops and clears all data)

---

## Development 🛠️


### Viewing Logs

```bash
# Tail all logs
./scripts/dev.sh logs

# Or view specific logs
tail -f logs/fastapi.log     # FastAPI server
tail -f logs/seaweed.log     # SeaweedFS
```

### Resetting Everything

```bash
# Stop all services and clear data
./scripts/dev.sh reset

# Re-run setup
./scripts/setup.sh

# Start fresh
./scripts/dev.sh up
```


---

## Quick Reference

| Command | Purpose |
|---------|---------|
| `./scripts/setup.sh` | One-time setup (Python, deps, SeaweedFS) |
| `./scripts/dev.sh up` | Start all services |
| `./scripts/dev.sh down` | Stop all services |
| `./scripts/dev.sh status` | Show running services |
| `./scripts/dev.sh logs` | View live logs |
| `./scripts/dev.sh reset` | Stop + clear all data |

