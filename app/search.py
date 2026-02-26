import re, logging, asyncio
from typing import List, Dict, Optional

import numpy as np

logger = logging.getLogger(__name__)


class SearchEngine:
    def __init__(self, db_conn, embedder, reranker=None):
        self.db = db_conn
        self.embedder = embedder
        self.reranker = reranker
        self.table_name = "documents"
        self._refresh_table()

    def _refresh_table(self):
        try:
            self.table = self.db.open_table(self.table_name)
        except Exception as e:
            logger.warning(f"Table refresh failed: {e}")
            self.table = None

    # ── Scoring helpers ──────────────────────────────────────────

    def _calculate_match_score(self, text: str, query: str) -> Dict:
        text_lower, query_lower = text.lower(), query.lower()
        query_terms = query_lower.split()

        term_frequencies, total_matches = {}, 0
        for term in query_terms:
            count = text_lower.count(term)
            if count > 0:
                term_frequencies[term] = count
                total_matches += count

        match_positions = []
        for term in query_terms:
            pos = 0
            while True:
                idx = text_lower.find(term, pos)
                if idx == -1:
                    break
                match_positions.append({"term": term, "position": idx, "char_position": idx})
                pos = idx + 1

        text_length = len(text)
        matched_terms = len([t for t in query_terms if t in text_lower])

        return {
            "total_matches": total_matches,
            "unique_terms_matched": matched_terms,
            "term_frequencies": term_frequencies,
            "match_positions": match_positions[:20],
            "match_density": round(total_matches / text_length * 1000, 2) if text_length else 0,
            "query_coverage": round(matched_terms / len(query_terms) * 100, 2) if query_terms else 0,
            "text_length": text_length
        }

    def _highlight_text(self, text: str, query: str, max_snippets: int = 5) -> List[Dict]:
        query_lower, text_lower = query.lower(), text.lower()
        query_terms = query_lower.split()

        all_positions = []
        for term in query_terms:
            pos = 0
            while True:
                idx = text_lower.find(term, pos)
                if idx == -1:
                    break
                all_positions.append({"term": term, "start": idx, "end": idx + len(term)})
                pos = idx + 1

        all_positions.sort(key=lambda x: x["start"])

        snippets, used_ranges = [], set()
        for match in all_positions[:max_snippets]:
            start = max(0, match["start"] - 150)
            end = min(len(text), match["end"] + 150)
            range_key = f"{start}-{end}"
            if range_key in used_ranges:
                continue
            used_ranges.add(range_key)

            snippet = ("..." if start > 0 else "") + text[start:end] + ("..." if end < len(text) else "")
            highlighted = snippet
            for term in query_terms:
                highlighted = re.sub(
                    f"({re.escape(term)})", r"<mark>\1</mark>",
                    highlighted, flags=re.IGNORECASE
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
        if not self.reranker or not results:
            return results
        try:
            documents = [r.get("text", "")[:2000] for r in results]
            reranked = self.reranker.rerank(query, documents, top_n=top_n)
            out = []
            for item in reranked:
                idx = item["index"]
                if idx < len(results):
                    r = results[idx].copy()
                    r["rerank_score"] = round(item["relevance_score"], 4)
                    out.append(r)
            logger.info(f"✓ Reranked {len(out)} results")
            return out
        except Exception as e:
            logger.error(f"Reranking failed: {e}")
            return results

    # ── Search methods ───────────────────────────────────────────

    async def lexical_search(self, query: str, limit: int = 10) -> List[Dict]:
        self._refresh_table()
        if not self.table:
            return []
        try:
            fetch_limit = limit * 3 if self.reranker else limit
            results = self.table.search(query, query_type="fts").limit(fetch_limit).to_list()
            for r in results:
                text = r.get("text", "")
                r["match_score"] = self._calculate_match_score(text, query)
                r["highlights"] = self._highlight_text(text, query)
                r["search_type"] = "lexical"
                r["query_terms"] = query.split()
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
            fetch_limit = limit * 3 if self.reranker else limit
            query_vector = await self.embedder.embed_text(query)
            vector_list = query_vector.tolist()
            results = self.table.search(vector_list, query_type="vector").limit(fetch_limit).to_list()
            for r in results:
                distance = r.get("_distance", 1.0)
                r["semantic_info"] = {
                    "distance": round(distance, 6),
                    "similarity_score": round(1 / (1 + distance) * 100, 2),
                    "embedding_dim": len(vector_list)
                }
                r["highlights"] = []
                r["search_type"] = "semantic"
                r["query_embedding_norm"] = round(np.linalg.norm(query_vector), 4)
            if self.reranker:
                results = self._rerank_results(query, results, top_n=limit)
            return results[:limit]
        except Exception as e:
            logger.error(f"Semantic search failed: {e}")
            return []

    async def hybrid_search(self, query: str, limit: int = 10) -> List[Dict]:
        if not self.table:
            return []
        try:
            fetch_limit = limit * 2
            lexical_results, semantic_results = await asyncio.gather(
                self.lexical_search(query, fetch_limit),
                self.semantic_search(query, fetch_limit)
            )

            scores = {}
            for idx, r in enumerate(lexical_results):
                fid = r.get("file_id")
                if fid:
                    scores[fid] = {
                        "doc": r,
                        "lexical_score": r.get("match_score", {}).get("match_density", 0),
                        "lexical_rank": idx + 1,
                        "semantic_score": 0,
                        "semantic_rank": 0
                    }
            for idx, r in enumerate(semantic_results):
                fid = r.get("file_id")
                if fid:
                    sem_score = r.get("semantic_info", {}).get("similarity_score", 0)
                    if fid in scores:
                        scores[fid]["semantic_score"] = sem_score
                        scores[fid]["semantic_rank"] = idx + 1
                    else:
                        scores[fid] = {
                            "doc": r,
                            "lexical_score": 0, "lexical_rank": 0,
                            "semantic_score": sem_score, "semantic_rank": idx + 1
                        }

            for fid, data in scores.items():
                lex_rank_norm = 1 / (data["lexical_rank"] + 1) if data["lexical_rank"] > 0 else 0
                sem_rank_norm = 1 / (data["semantic_rank"] + 1) if data["semantic_rank"] > 0 else 0
                hybrid_score = (
                    data["lexical_score"] * 0.3 +
                    lex_rank_norm * 100 * 0.2 +
                    data["semantic_score"] * 0.3 +
                    sem_rank_norm * 100 * 0.2
                )
                data["hybrid_score"] = round(hybrid_score, 2)
                data["doc"]["hybrid_info"] = {
                    "combined_score": round(hybrid_score, 2),
                    "lexical_contribution": round(data["lexical_score"], 2),
                    "semantic_contribution": round(data["semantic_score"], 2),
                    "lexical_rank": data["lexical_rank"],
                    "semantic_rank": data["semantic_rank"]
                }
                data["doc"]["search_type"] = "hybrid"

            results = [d["doc"] for d in sorted(scores.values(), key=lambda x: x["hybrid_score"], reverse=True)]
            if self.reranker:
                results = self._rerank_results(query, results[:limit * 2], top_n=limit)
            return results[:limit]
        except Exception as e:
            logger.error(f"Hybrid search failed: {e}")
            return []

    async def comparison_search(self, query: str, limit: int = 10) -> Dict:
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
