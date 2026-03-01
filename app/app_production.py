import os, httpx
import io, json
import logging
import hashlib
import asyncio
from datetime import datetime
from pathlib import Path
from typing import List, Dict
import lancedb
from lancedb.pydantic import LanceModel, Vector
import requests
import numpy as np
from fastapi import FastAPI, UploadFile, File, HTTPException, Query, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import uvicorn
from reranker import load_reranker_from_config
from qa_agent import QAAgent
from typing import Optional

from embeddings import OpenRouterEmbeddings
from extractor import extract_document_text

def clean_result(result: Dict) -> Dict:
    """Convert numpy types to Python native types"""
    cleaned = {}
    for key, value in result.items():
        if isinstance(value, np.ndarray):
            cleaned[key] = value.tolist()
        elif isinstance(value, (np.float32, np.float64)):
            cleaned[key] = float(value)
        elif isinstance(value, (np.int32, np.int64)):
            cleaned[key] = int(value)
        elif isinstance(value, dict):
            cleaned[key] = clean_result(value)
        elif isinstance(value, list):
            cleaned[key] = [clean_result(item) if isinstance(item, dict) else item for item in value]
        else:
            cleaned[key] = value
    return cleaned

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global variables
db = None
indexer = None
searcher = None
embedder = None
reranker = None
qa_agent = None 
_indexing_in_progress: set = set()

# Configuration
SEAWEED_FILER = os.getenv("SEAWEED_FILER", "http://localhost:8888")
LANCEDB_PATH = os.getenv("LANCEDB_PATH", "./data/lancedb")
OPENROUTER_KEY = "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"
EMBEDDING_DIM = 1536

# Document Schema
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

# Document Indexer
class DocumentIndexer:
    def __init__(self, db_conn, embedder):
        self.db = db_conn
        self.embedder = embedder
        self.table_name = "documents"
        self._init_table()
    
    def _init_table(self):
        try:
            self.table = self.db.open_table(self.table_name)
            logger.info("✓ Opened existing table")
        except:
            self.table = self.db.create_table(self.table_name, schema=Document)
            self.table.create_fts_index("text", replace=True)
            logger.info("✓ Created new table with FTS index")
    
    def _chunk_text(self, text: str, max_chars: int = 6000) -> List[str]:
        """Split text into chunks"""
        if len(text) <= max_chars:
            return [text]
        
        chunks = []
        words = text.split()
        current = []
        current_len = 0
        
        for word in words:
            if current_len + len(word) > max_chars:
                chunks.append(" ".join(current))
                current = [word]
                current_len = len(word)
            else:
                current.append(word)
                current_len += len(word) + 1
        
        if current:
            chunks.append(" ".join(current))
        
        return chunks
    
    async def index_document(self, file_path: str, content: bytes, filename: str, content_type: str) -> str:
        global _indexing_in_progress

        file_id = hashlib.sha256(file_path.encode()).hexdigest()[:16]

        # Idempotency guard
        if file_id in _indexing_in_progress:
            logger.info(f"⏭ Already indexing {filename}, skipping duplicate")
            return file_id
        _indexing_in_progress.add(file_id)

        try:
            # Extract text
            logger.info(f"Extracting text from {filename}...")
            text = await extract_document_text(content, content_type)
            if not text or len(text.strip()) < 10:
                text = f"[No text extracted from {filename}]"
            logger.info(f"✓ Extracted {len(text)} chars")

            # Chunk and embed
            chunks = self._chunk_text(text)
            logger.info(f"Split into {len(chunks)} chunks")
            embeddings = []
            for i, chunk in enumerate(chunks, 1):
                logger.info(f"Embedding chunk {i}/{len(chunks)}...")
                embeddings.append(await self.embedder.embed_text(chunk))

            avg_vector = np.mean(embeddings, axis=0).tolist()

            metadata_json = json.dumps({
                "chunks":           len(chunks),
                "char_count":       len(text),
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

            # Dedup: delete existing row for this file before adding
            try:
                self.table.delete(f'file_id = "{file_id}"')
                logger.info(f"  Removed old index entry for {file_id}")
            except Exception:
                pass  # fine — row just doesn't exist yet

            self.table.add([doc])

            # Rebuild FTS off the event loop (blocking call)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: self.table.create_fts_index("text", replace=True)
            )

            logger.info(f"✓ Indexed + FTS rebuilt: {filename} → {file_id}")
            return file_id

        except Exception as e:
            logger.error(f"Failed to index {filename}: {e}")
            raise

        finally:
            _indexing_in_progress.discard(file_id)


    
    def get_stats(self) -> Dict:
        """Get statistics"""
        try:
            df = self.table.to_pandas()
            return {
                "total_documents": len(df),
                "total_size_bytes": int(df['file_size'].sum()) if len(df) > 0 else 0
            }
        except:
            return {"total_documents": 0, "total_size_bytes": 0}

# Search Engine - WITH RERANKING
class SearchEngine:
    def __init__(self, db_conn, embedder, reranker=None):
        self.db = db_conn
        self.embedder = embedder
        self.reranker = reranker
        self.table_name = "documents"
        try:
            self.table = self.db.open_table(self.table_name)
        except:
            self.table = None
    
    def _refresh_table(self):
        try:
            self.table = self.db.open_table(self.table_name)
        except Exception as e:
            logger.warning(f"Table refresh failed: {e}")
            self.table = None

    def _calculate_match_score(self, text: str, query: str) -> Dict:
        """Calculate detailed match statistics"""
        text_lower = text.lower()
        query_lower = query.lower()
        query_terms = query_lower.split()
        
        # Term frequency
        term_frequencies = {}
        total_matches = 0
        
        for term in query_terms:
            count = text_lower.count(term)
            if count > 0:
                term_frequencies[term] = count
                total_matches += count
        
        # Match positions
        match_positions = []
        for term in query_terms:
            pos = 0
            while True:
                idx = text_lower.find(term, pos)
                if idx == -1:
                    break
                match_positions.append({
                    "term": term,
                    "position": idx,
                    "char_position": idx
                })
                pos = idx + 1
        
        # Match density (matches per 1000 chars)
        text_length = len(text)
        match_density = (total_matches / text_length * 1000) if text_length > 0 else 0
        
        # Coverage (what % of query terms found)
        matched_terms = len([t for t in query_terms if t in text_lower])
        coverage = (matched_terms / len(query_terms) * 100) if len(query_terms) > 0 else 0
        
        return {
            "total_matches": total_matches,
            "unique_terms_matched": matched_terms,
            "term_frequencies": term_frequencies,
            "match_positions": match_positions[:20],
            "match_density": round(match_density, 2),
            "query_coverage": round(coverage, 2),
            "text_length": text_length
        }
    
    def _highlight_text(self, text: str, query: str, max_snippets: int = 5) -> List[Dict]:
        """Find query matches in text with context"""
        snippets = []
        query_lower = query.lower()
        text_lower = text.lower()
        query_terms = query_lower.split()
        
        # Find all term positions
        all_positions = []
        for term in query_terms:
            pos = 0
            while True:
                idx = text_lower.find(term, pos)
                if idx == -1:
                    break
                all_positions.append({
                    "term": term,
                    "start": idx,
                    "end": idx + len(term)
                })
                pos = idx + 1
        
        # Sort by position
        all_positions.sort(key=lambda x: x["start"])
        
        # Create snippets around matches
        used_ranges = set()
        for match in all_positions[:max_snippets]:
            start = max(0, match["start"] - 150)
            end = min(len(text), match["end"] + 150)
            
            # Avoid overlapping snippets
            range_key = f"{start}-{end}"
            if range_key in used_ranges:
                continue
            used_ranges.add(range_key)
            
            snippet = text[start:end]
            
            # Add ellipsis
            if start > 0:
                snippet = "..." + snippet
            if end < len(text):
                snippet = snippet + "..."
            
            # Highlight the matched term in snippet
            highlighted = snippet
            for term in query_terms:
                import re
                highlighted = re.sub(
                    f'({re.escape(term)})',
                    r'<mark>\1</mark>',
                    highlighted,
                    flags=re.IGNORECASE
                )
            
            snippets.append({
                "text": snippet,
                "highlighted": highlighted,
                "position": match["start"],
                "matched_term": match["term"],
                "context_chars": end - start
            })
        
        return snippets
    
    def _rerank_results(self, query: str, results: List[Dict], top_n: int = 10) -> List[Dict]:
        """Apply reranking to results"""
        if not self.reranker or not results:
            return results
        
        try:
            # Extract texts for reranking (first 2000 chars)
            documents = [r.get('text', '')[:2000] for r in results]
            
            # Rerank
            reranked = self.reranker.rerank(query, documents, top_n=top_n)
            
            # Map back to original results with scores
            reranked_results = []
            for item in reranked:
                idx = item['index']
                if idx < len(results):
                    result = results[idx].copy()
                    result['rerank_score'] = round(item['relevance_score'], 4)
                    reranked_results.append(result)
            
            logger.info(f"✓ Reranked {len(reranked_results)} results")
            return reranked_results
        
        except Exception as e:
            logger.error(f"Reranking failed: {e}")
            return results
    
    async def lexical_search(self, query: str, limit: int = 10) -> List[Dict]:
        self._refresh_table()
        if not self.table:
            return []

        try:
            fetch_limit = limit * 3 if self.reranker else limit
            results = self.table.search(query, query_type="fts").limit(fetch_limit).to_list()

            for r in results:
                text = r.get('text', '')
                r['match_score'] = self._calculate_match_score(text, query)
                r['highlights']  = self._highlight_text(text, query)
                r['search_type'] = 'lexical'
                r['query_terms'] = query.split()

            if self.reranker:
                results = self._rerank_results(query, results, top_n=limit)

            return results[:limit]

        except Exception as e:
            logger.error(f"Lexical search failed: {e}")
            return []

    
    async def semantic_search(self, query: str, limit: int = 10) -> List[Dict]:
        self._refresh_table()
        if not self.table:
            return []

        try:
            fetch_limit  = limit * 3 if self.reranker else limit
            query_vector = await self.embedder.embed_text(query)
            vector_list  = query_vector.tolist()

            results = self.table.search(vector_list, query_type="vector").limit(fetch_limit).to_list()

            for r in results:
                distance = r.get('_distance', 1.0)
                r['semantic_info'] = {
                    "distance":         round(distance, 6),
                    "similarity_score": round(1 / (1 + distance) * 100, 2),
                    "embedding_dim":    len(vector_list)
                }
                r['highlights']           = []
                r['search_type']          = 'semantic'
                r['query_embedding_norm'] = round(np.linalg.norm(query_vector), 4)

            if self.reranker:
                results = self._rerank_results(query, results, top_n=limit)

            return results[:limit]

        except Exception as e:
            logger.error(f"Semantic search failed: {e}")
            return []

    
    async def hybrid_search(self, query: str, limit: int = 10) -> List[Dict]:
        """Hybrid search with automatic reranking"""
        if not self.table:
            return []
        
        try:
            # Get both results (fetch more for better hybrid merging)
            fetch_limit = limit * 2
            lexical_results = await self.lexical_search(query, fetch_limit)
            semantic_results = await self.semantic_search(query, fetch_limit)
            
            # Create scoring dictionary
            scores = {}
            
            # Lexical scores
            for idx, r in enumerate(lexical_results):
                fid = r.get('file_id')
                if fid:
                    lexical_score = r.get('match_score', {}).get('match_density', 0)
                    scores[fid] = {
                        'doc': r,
                        'lexical_score': lexical_score,
                        'lexical_rank': idx + 1,
                        'semantic_score': 0,
                        'semantic_rank': 0
                    }
            
            # Semantic scores
            for idx, r in enumerate(semantic_results):
                fid = r.get('file_id')
                if fid:
                    semantic_score = r.get('semantic_info', {}).get('similarity_score', 0)
                    if fid in scores:
                        scores[fid]['semantic_score'] = semantic_score
                        scores[fid]['semantic_rank'] = idx + 1
                    else:
                        scores[fid] = {
                            'doc': r,
                            'lexical_score': 0,
                            'lexical_rank': 0,
                            'semantic_score': semantic_score,
                            'semantic_rank': idx + 1
                        }
            
            # Calculate hybrid scores
            for fid, data in scores.items():
                # Normalize ranks (lower is better)
                lex_rank_norm = 1 / (data['lexical_rank'] + 1) if data['lexical_rank'] > 0 else 0
                sem_rank_norm = 1 / (data['semantic_rank'] + 1) if data['semantic_rank'] > 0 else 0
                
                # Combine scores (weighted)
                hybrid_score = (
                    (data['lexical_score'] * 0.3) + 
                    (lex_rank_norm * 100 * 0.2) +
                    (data['semantic_score'] * 0.3) +
                    (sem_rank_norm * 100 * 0.2)
                )
                
                data['hybrid_score'] = round(hybrid_score, 2)
                
                # Add hybrid info to doc
                data['doc']['hybrid_info'] = {
                    'combined_score': round(hybrid_score, 2),
                    'lexical_contribution': round(data['lexical_score'], 2),
                    'semantic_contribution': round(data['semantic_score'], 2),
                    'lexical_rank': data['lexical_rank'],
                    'semantic_rank': data['semantic_rank']
                }
                data['doc']['search_type'] = 'hybrid'
            
            # Sort by hybrid score
            sorted_results = sorted(scores.values(), key=lambda x: x['hybrid_score'], reverse=True)
            results = [item['doc'] for item in sorted_results]
            
            # Apply reranking to top results
            if self.reranker:
                results = self._rerank_results(query, results[:limit * 2], top_n=limit)
            
            return results[:limit]
        
        except Exception as e:
            logger.error(f"Hybrid search failed: {e}")
            return []
    
    async def comparison_search(self, query: str, limit: int = 10) -> Dict:
        """Get all three search types for comparison"""
        lexical, semantic, hybrid = await asyncio.gather(
            self.lexical_search(query, limit),
            self.semantic_search(query, limit),
            self.hybrid_search(query, limit)
        )
        
        return {
            "query": query,
            "query_stats": {
                "term_count": len(query.split()),
                "char_count": len(query),
                "terms": query.split()
            },
            "lexical": lexical,
            "semantic": semantic,
            "hybrid": hybrid
        }


# Startup
@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, indexer, searcher, embedder, reranker, qa_agent  # Add qa_agent
    
    # Connect to LanceDB
    os.makedirs(LANCEDB_PATH, exist_ok=True)
    db = lancedb.connect(LANCEDB_PATH)
    
    # Initialize embedder
    embedder = OpenRouterEmbeddings(api_key=OPENROUTER_KEY)
    
    # Initialize reranker
    reranker = load_reranker_from_config("./config/config.yaml")
    
    # Initialize indexer and searcher
    indexer = DocumentIndexer(db, embedder)
    searcher = SearchEngine(db, embedder, reranker)
    
    # Initialize QA Agent - ADD THIS
    qa_agent = QAAgent(searcher, SEAWEED_FILER, OPENROUTER_KEY)
    
    logger.info("✓ System initialized with Q&A Agent")
    yield
    logger.info("Shutting down")



# FastAPI App
app = FastAPI(title="Document Search System", version="2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files
static_path = Path(__file__).parent / "static"
static_path.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

@app.get("/", response_class=HTMLResponse)
async def root():
    html_file = static_path / "index.html"
    if html_file.exists():
        return html_file.read_text()
    return "<h1>Document Search System</h1><p>Create static/index.html</p>"

@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    """Upload file to SeaweedFS — indexing triggered by filer webhook"""
    try:
        content = await file.read()
        file_path = f"/documents/{file.filename}"

        # Just PUT to SeaweedFS — webhook handles indexing
        response = requests.put(
            f"{SEAWEED_FILER}{file_path}",
            data=content,
            headers={"Content-Type": file.content_type or "application/octet-stream"},
            timeout=30
        )

        if response.status_code not in [200, 201, 204]:
            raise HTTPException(500, "SeaweedFS upload failed")

        return {
            "status": "uploaded",        # not "indexed" yet
            "filename": file.filename,
            "size": len(content),
            "message": "Indexing will begin shortly via filer webhook"
        }

    except Exception as e:
        logger.error(f"Upload failed: {e}")
        raise HTTPException(500, str(e))
    
@app.post("/webhook/seaweed")
async def seaweed_webhook(request: Request):
    try:
        payload    = await request.json()
        event_type = (payload.get("event_type") or payload.get("EventType") or "PUT").upper()

        # 3.95 uses "key" for full file path
        file_path  = (
            payload.get("key") or
            payload.get("Path") or
            payload.get("path") or ""
        )

        logger.info(f"Webhook received: {event_type} → {file_path}")

        if event_type not in ("PUT", "CREATE"):
            return {"status": "ignored", "reason": f"event={event_type}"}

        if not file_path or not file_path.startswith("/documents/"):
            return {"status": "ignored", "reason": f"path='{file_path}'"}

        file_id = hashlib.sha256(file_path.encode()).hexdigest()[:16]
        if file_id in _indexing_in_progress:
            return {"status": "skipped", "reason": "already indexing"}

        # Extract mime from payload — avoids extra HTTP call for content-type
        message      = payload.get("message", {})
        new_entry    = message.get("new_entry", {})
        attributes   = new_entry.get("attributes", {})
        content_type = attributes.get("mime", "application/octet-stream")
        filename     = file_path.split("/")[-1]

        # Fetch file content from filer
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(f"{SEAWEED_FILER}{file_path}")

        if resp.status_code != 200:
            raise HTTPException(500, f"Filer fetch failed: {resp.status_code}")

        asyncio.create_task(
            indexer.index_document(file_path, resp.content, filename, content_type)
        )

        logger.info(f"✓ Webhook: indexing task fired for {filename}")
        return {"status": "indexing", "filename": filename, "file_id": file_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Webhook failed: {e}")
        raise HTTPException(500, str(e))



@app.get("/search")
async def search(
    query: str = Query(...),
    type: str = Query("hybrid", regex="^(lexical|semantic|hybrid)$"),
    limit: int = Query(10, ge=1, le=50)
):
    """Search documents"""
    try:
        if type == "lexical":
            results = await searcher.lexical_search(query, limit)
        elif type == "semantic":
            results = await searcher.semantic_search(query, limit)
        else:
            results = await searcher.hybrid_search(query, limit)
        
        # Clean results
        cleaned_results = [clean_result(r) for r in results]
        
        return {
            "query": query,
            "type": type,
            "count": len(cleaned_results),
            "results": cleaned_results
        }
    
    except Exception as e:
        logger.error(f"Search failed: {e}")
        raise HTTPException(500, str(e))

@app.get("/search/compare")
async def compare_search(query: str = Query(...), limit: int = Query(10, ge=1, le=20)):
    """Compare all search types"""
    try:
        data = await searcher.comparison_search(query, limit)
        
        # Clean all results
        return {
            "query": data["query"],
            "query_stats": data["query_stats"],
            "lexical": [clean_result(r) for r in data["lexical"]],
            "semantic": [clean_result(r) for r in data["semantic"]],
            "hybrid": [clean_result(r) for r in data["hybrid"]]
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/preview")
async def preview(file_path: str = Query(...)):
    """Preview document"""
    try:
        response = requests.get(f"{SEAWEED_FILER}{file_path}", timeout=10)
        if response.status_code != 200:
            raise HTTPException(404, "Not found")
        
        content_type = response.headers.get("Content-Type", "application/octet-stream")
        
        if "pdf" in content_type.lower():
            return StreamingResponse(
                io.BytesIO(response.content),
                media_type="application/pdf",
                headers={"Content-Disposition": f"inline; filename={file_path.split('/')[-1]}"}
            )
        
        return {"content": response.text[:5000], "type": content_type}
    
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/stats")
async def stats():
    """System statistics"""
    return {
        "system": "Document Search v2.0",
        "documents": indexer.get_stats()
    }

@app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}


@app.post("/qa/ask")
async def ask_question(
    question: str = Query(...),   # ← remove min_length=3
    top_k: int = Query(5, ge=1, le=10),
    return_sources: bool = Query(True),
    include_excerpts: bool = Query(True),
    mode: str = Query("document", regex="^(document|general)$")
):

    """Ask a question — document-grounded or general LLM"""
    try:
        result = await qa_agent.answer_question(question, top_k, return_sources, include_excerpts, mode)
        return result
    except Exception as e:
        logger.error(f"Q&A failed: {e}")
        raise HTTPException(500, str(e))


@app.post("/qa/conversation")
async def conversation(
    messages: List[Dict[str, str]],
    top_k: int = Query(5, ge=1, le=10),
    mode: str = Query("document", regex="^(document|general)$")  # ADD THIS
):
    """Multi-turn conversation — document-grounded or general LLM"""
    try:
        result = await qa_agent.multi_turn_conversation(messages, top_k, mode)
        return result
    except Exception as e:
        logger.error(f"Conversation failed: {e}")
        raise HTTPException(500, str(e))



@app.get("/qa/sources")
async def get_sources_for_question(
    question: str = Query(..., min_length=3),
    top_k: int = Query(5, ge=1, le=10)
):
    """Get relevant document sources for a question (without generating answer)"""
    try:
        documents = await qa_agent._retrieve_relevant_documents(question, top_k)
        
        sources = [{
            'document_id': doc['id'],
            'filename': doc['filename'],
            'file_path': doc['file_path'],
            'file_id': doc['file_id'],
            'relevance_score': round(doc['relevance_score'], 4),
            'preview_url': f"/preview?file_path={doc['file_path']}",
            'text_preview': doc['text'][:500] + "..." if len(doc['text']) > 500 else doc['text']
        } for doc in documents]
        
        return {
            'question': question,
            'sources': sources,
            'count': len(sources)
        }
    except Exception as e:
        logger.error(f"Source retrieval failed: {e}")
        raise HTTPException(500, str(e))
    
@app.post("/admin/reindex")
async def reindex_documents(
    background_tasks: BackgroundTasks,
    mode:       str           = Query("all", regex="^(all|file|date_range)$"),
    file_path:  Optional[str] = Query(None,  description="e.g. /documents/report.pdf — required for mode=file"),
    date_from:  Optional[str] = Query(None,  description="ISO format: 2026-01-01T00:00:00 — required for mode=date_range"),
    date_to:    Optional[str] = Query(None,  description="ISO format: 2026-02-26T23:59:59 — required for mode=date_range"),
    background: bool          = Query(True,  description="True = fast response, False = wait for full results")
):
    """
    Manual re-index trigger.

    Modes:
      all         → re-index every file in SeaweedFS /documents/
      file        → re-index one specific file  (needs file_path)
      date_range  → re-index files between date_from and date_to (needs both)
    """
    try:
        # ── Validate params per mode ──────────────────────────────
        if mode == "file" and not file_path:
            raise HTTPException(400, "file_path is required for mode=file")

        if mode == "date_range":
            if not date_from or not date_to:
                raise HTTPException(400, "date_from and date_to required for mode=date_range")
            try:
                dt_from = datetime.fromisoformat(date_from)
                dt_to   = datetime.fromisoformat(date_to)
            except ValueError:
                raise HTTPException(400, "Invalid date format. Use ISO: 2026-01-01T00:00:00")

        # ── Fetch file listing from SeaweedFS Filer ───────────────
        async with httpx.AsyncClient(timeout=15) as client:
            list_resp = await client.get(
                f"{SEAWEED_FILER}/documents/",
                headers={"Accept": "application/json"},
                params={"limit": 1000}
            )

        if list_resp.status_code != 200:
            raise HTTPException(500, f"Could not list /documents/ from filer: {list_resp.status_code}")

        # Log raw response for debugging
        logger.info(f"Filer listing response: {list_resp.text[:500]}")

        # SeaweedFS returns { "Directory": "/documents/", "Files": [...] }
        filer_data  = list_resp.json()
        all_entries = filer_data.get("Entries") or filer_data.get("Files") or []

        if not all_entries:
            return {
                "status":  "nothing_to_index",
                "message": "No files found in /documents/",
                "raw_keys": list(filer_data.keys())   # helps debug key names
            }

        # ── Filter entries by mode ────────────────────────────────
        entries_to_process = []

        if mode == "all":
            entries_to_process = all_entries

        elif mode == "file":
            for e in all_entries:
                fname     = e.get("name") or e.get("FileName") or e.get("Name", "")
                full_path = e.get("FullPath") or f"/documents/{fname}"
                if full_path == file_path:
                    entries_to_process.append(e)
                    break
            if not entries_to_process:
                raise HTTPException(404, f"File not found in filer: {file_path}")

        elif mode == "date_range":
            for e in all_entries:
                crtime_raw = e.get("Crtime") or e.get("crtime", "")
                try:
                    # SeaweedFS returns ISO string: "2026-02-15T09:27:55+05:30"
                    entry_dt = datetime.fromisoformat(crtime_raw).replace(tzinfo=None) if crtime_raw else None
                except ValueError:
                    entry_dt = None

                if entry_dt and dt_from <= entry_dt <= dt_to:
                    entries_to_process.append(e)

            if not entries_to_process:
                return {
                    "status":  "nothing_to_index",
                    "message": f"No files found between {date_from} and {date_to}"
                }

        logger.info(f"Reindex mode={mode} → {len(entries_to_process)} file(s) to process")

        # ── Per-entry processor ───────────────────────────────────
        async def _process_entry(entry: dict) -> dict:
            # SeaweedFS gives FullPath — extract filename from it
            full_path = entry.get("FullPath", "")
            fname     = full_path.split("/")[-1]   # "2458_250210_220407.pdf"
            fpath     = full_path                  # "/documents/2458_250210_220407.pdf"

            if not fname:
                return {"path": fpath, "status": "skipped", "reason": "no filename"}

            fid = hashlib.sha256(fpath.encode()).hexdigest()[:16]
            if fid in _indexing_in_progress:
                return {"path": fpath, "status": "skipped", "reason": "already indexing"}

            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    file_resp = await client.get(f"{SEAWEED_FILER}{fpath}")

                if file_resp.status_code != 200:
                    return {
                        "path":   fpath,
                        "status": "failed",
                        "reason": f"filer returned {file_resp.status_code}"
                    }

                # Use Mime from filer entry directly — avoids content-type guessing
                content_type = entry.get("Mime") or file_resp.headers.get("Content-Type", "application/octet-stream")
                file_id      = await indexer.index_document(
                    fpath, file_resp.content, fname, content_type
                )

                return {
                    "path":     fpath,
                    "status":   "indexed",
                    "file_id":  file_id,
                    "filename": fname
                }

            except Exception as e:
                logger.error(f"Reindex failed for {fpath}: {e}")
                return {"path": fpath, "status": "failed", "reason": str(e)}


        # ── Background mode ───────────────────────────────────────
        if background:
            async def _run_all():
                results = await asyncio.gather(*[_process_entry(e) for e in entries_to_process])
                indexed = [r for r in results if r["status"] == "indexed"]
                failed  = [r for r in results if r["status"] == "failed"]
                skipped = [r for r in results if r["status"] == "skipped"]
                logger.info(
                    f"✓ Reindex complete → "
                    f"indexed={len(indexed)} failed={len(failed)} skipped={len(skipped)}"
                )
                # Log any failures explicitly
                for f in failed:
                    logger.error(f"  ✗ {f['path']}: {f['reason']}")

            background_tasks.add_task(_run_all)

            return {
                "status":  "queued",
                "mode":    mode,
                "queued":  len(entries_to_process),
                "message": f"{len(entries_to_process)} file(s) queued for re-indexing. Check logs for progress."
            }

        # ── Foreground mode (background=false) ───────────────────
        results = await asyncio.gather(*[_process_entry(e) for e in entries_to_process])

        indexed = [r for r in results if r["status"] == "indexed"]
        failed  = [r for r in results if r["status"] == "failed"]
        skipped = [r for r in results if r["status"] == "skipped"]

        return {
            "status": "complete",
            "mode":   mode,
            "summary": {
                "total":   len(entries_to_process),
                "indexed": len(indexed),
                "failed":  len(failed),
                "skipped": len(skipped)
            },
            "details": {
                "indexed": indexed,
                "failed":  failed,
                "skipped": skipped
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Reindex endpoint failed: {e}")
        raise HTTPException(500, str(e))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
