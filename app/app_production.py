import os
import io
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
from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import uvicorn
from reranker import load_reranker_from_config


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
        """Index a document"""
        try:
            file_id = hashlib.sha256(file_path.encode()).hexdigest()[:16]
            
            # Extract text
            logger.info(f"Extracting text from {filename}...")
            text = await extract_document_text(content, content_type)
            
            if not text or len(text.strip()) < 10:
                text = f"[No text extracted from {filename}]"
            
            logger.info(f"✓ Extracted {len(text)} characters")
            
            # Chunk and embed
            chunks = self._chunk_text(text)
            logger.info(f"Split into {len(chunks)} chunks")
            
            embeddings = []
            for i, chunk in enumerate(chunks, 1):
                logger.info(f"Embedding chunk {i}/{len(chunks)}...")
                emb = await self.embedder.embed_text(chunk)
                embeddings.append(emb)
            
            # Average embeddings
            avg_embedding = np.mean(embeddings, axis=0)
            vector_list = avg_embedding.tolist()
            
            # Create document
            doc = Document(
                file_id=file_id,
                filename=filename,
                file_path=file_path,
                text=text,
                vector=vector_list,
                content_type=content_type,
                file_size=len(content),
                indexed_at=datetime.now().isoformat()
            )
            
            # Add to database
            self.table.add([doc])
            self.table.create_fts_index("text", replace=True)
            
            logger.info(f"✓ Indexed {filename}")
            return file_id
        
        except Exception as e:
            logger.error(f"Failed to index: {e}")
            raise
    
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
        """Full-text search with automatic reranking"""
        if not self.table:
            return []
        
        try:
            # Fetch 3x more results for reranking
            fetch_limit = limit * 3 if self.reranker else limit
            
            results = self.table.search(query, query_type="fts").limit(fetch_limit).to_list()
            
            for r in results:
                text = r.get('text', '')
                
                # Calculate match scores
                match_info = self._calculate_match_score(text, query)
                highlights = self._highlight_text(text, query)
                
                # Add data
                r['match_score'] = match_info
                r['highlights'] = highlights
                r['search_type'] = 'lexical'
                r['query_terms'] = query.split()
            
            # Apply reranking
            if self.reranker:
                results = self._rerank_results(query, results, top_n=limit)
            
            return results[:limit]
        
        except Exception as e:
            logger.error(f"Lexical search failed: {e}")
            return []
    
    async def semantic_search(self, query: str, limit: int = 10) -> List[Dict]:
        """Vector search with automatic reranking"""
        if not self.table:
            return []
        
        try:
            # Fetch 3x more results for reranking
            fetch_limit = limit * 3 if self.reranker else limit
            
            query_vector = await self.embedder.embed_text(query)
            vector_list = query_vector.tolist()
            
            results = self.table.search(vector_list, query_type="vector").limit(fetch_limit).to_list()
            
            for r in results:
                # Calculate similarity
                distance = r.get('_distance', 1.0)
                similarity = 1 / (1 + distance)
                
                # Add semantic info
                r['semantic_info'] = {
                    "distance": round(distance, 6),
                    "similarity_score": round(similarity * 100, 2),
                    "embedding_dim": len(vector_list)
                }
                r['highlights'] = []
                r['search_type'] = 'semantic'
                r['query_embedding_norm'] = round(np.linalg.norm(query_vector), 4)
            
            # Apply reranking
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
    global db, indexer, searcher, embedder, reranker
    
    # Connect to LanceDB
    os.makedirs(LANCEDB_PATH, exist_ok=True)
    db = lancedb.connect(LANCEDB_PATH)
    
    # Initialize embedder
    embedder = OpenRouterEmbeddings(api_key=OPENROUTER_KEY)
    
    # Initialize reranker
    reranker = load_reranker_from_config("./config/config.yaml")  # ADD THIS
    
    # Initialize indexer and searcher
    indexer = DocumentIndexer(db, embedder)
    searcher = SearchEngine(db, embedder, reranker)  # ADD reranker param
    
    logger.info("✓ System initialized")
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
    """Upload and index document"""
    try:
        content = await file.read()
        file_path = f"/documents/{file.filename}"
        
        # Upload to SeaweedFS
        try:
            requests.post(f"{SEAWEED_FILER}/documents/", data="", timeout=5)
        except:
            pass
        
        response = requests.put(
            f"{SEAWEED_FILER}{file_path}",
            data=content,
            headers={"Content-Type": file.content_type or "application/octet-stream"},
            timeout=30
        )
        
        if response.status_code not in [200, 201, 204]:
            raise HTTPException(500, "SeaweedFS upload failed")
        
        # Index
        file_id = await indexer.index_document(file_path, content, file.filename, file.content_type)
        
        return {
            "status": "success",
            "file_id": file_id,
            "filename": file.filename,
            "size": len(content)
        }
    
    except Exception as e:
        logger.error(f"Upload failed: {e}")
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

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
