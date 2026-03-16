# search/engine.py
import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import numpy as np
from lancedb.rerankers import RRFReranker, CrossEncoderReranker, ColbertReranker

logger = logging.getLogger(__name__)

_store    = None
_embedder = None
_reranker = RRFReranker()

def init_search(store, embedder):
    global _store, _embedder
    _store    = store
    _embedder = embedder
    logger.info("✓ Search engine initialised")


# ─────────────────────────────────────────────────────────────────────────────
# QUERY EXPANSION
# ─────────────────────────────────────────────────────────────────────────────

# Common acronym/synonym map — extend this for your domain
_SYNONYMS = {
    "moe":       ["mixture of experts", "sparse gating"],
    "llm":       ["large language model", "language model"],
    "rag":       ["retrieval augmented generation", "retrieval augmented"],
    "nlp":       ["natural language processing"],
    "cv":        ["computer vision"],
    "ml":        ["machine learning"],
    "dl":        ["deep learning"],
    "nn":        ["neural network"],
    "bert":      ["bidirectional encoder representations"],
    "gpt":       ["generative pretrained transformer"],
    "api":       ["application programming interface"],
    "db":        ["database"],
    "vector db": ["vector database", "embedding store"],
}

def _expand_query(query: str) -> Dict:
    """
    Expand query with synonyms/acronyms.
    Returns original query for search + expanded terms for UI display.
    """
    q_lower  = query.lower()
    expanded = []
    for key, synonyms in _SYNONYMS.items():
        if key in q_lower:
            expanded.extend(synonyms)
    # Deduplicate and filter already-present terms
    expanded = [e for e in expanded if e.lower() not in q_lower]
    return {
        "original":      query,
        "expanded_terms": expanded,
        # For FTS: append expanded terms to boost recall
        "fts_query":     query + (" OR " + " OR ".join(expanded) if expanded else ""),
    }


# ─────────────────────────────────────────────────────────────────────────────
# FILTERS
# ─────────────────────────────────────────────────────────────────────────────

_DURATION_MAP = {
    "last_24h": timedelta(hours=24),
    "last_7d":  timedelta(days=7),
    "last_30d": timedelta(days=30),
    "last_90d": timedelta(days=90),
}

def _duration_to_iso(duration: str) -> str:
    if duration not in _DURATION_MAP:
        raise ValueError(f"Invalid duration '{duration}'")
    return (datetime.utcnow() - _DURATION_MAP[duration]).isoformat()

def _resolve_file_ids(
    source_type: Optional[str] = None,
    file_type:   Optional[str] = None,
    date_from:   Optional[str] = None,
    date_to:     Optional[str] = None,
    duration:    Optional[str] = None,
) -> Optional[List[str]]:
    has_filter = any([source_type, file_type, date_from, date_to, duration])
    if not has_filter:
        return None
    if "documents" not in _store.db.table_names():
        return None

    conditions = []
    if source_type: conditions.append(f"source_type = '{source_type}'")
    if file_type:   conditions.append(f"file_type = '{file_type}'")
    if duration:    conditions.append(f"indexed_at >= '{_duration_to_iso(duration)}'")
    elif date_from: conditions.append(f"indexed_at >= '{date_from}'")
    if date_to:     conditions.append(f"indexed_at <= '{date_to}'")

    if not conditions:
        return None

    where = " AND ".join(conditions)
    try:
        rows = _store.db.open_table("documents").search().where(where).to_list()
        ids  = [r["file_id"] for r in rows if r.get("file_id")]
        logger.info(f"Filter resolved {len(ids)} file_ids")
        return ids
    except Exception as e:
        logger.warning(f"Filter resolution failed: {e}")
        return None

def _build_where(file_ids: Optional[List[str]]) -> Optional[str]:
    if file_ids is None:   return None
    if len(file_ids) == 0: return "file_id = '__no_match__'"
    if len(file_ids) == 1: return f"file_id = '{file_ids[0]}'"
    quoted = ", ".join(f"'{i}'" for i in file_ids)
    return f"file_id IN ({quoted})"


# ─────────────────────────────────────────────────────────────────────────────
# DOCUMENT METADATA JOIN
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_doc_meta_batch(file_ids: List[str]) -> Dict[str, Dict]:
    if not file_ids:
        return {}
    try:
        if len(file_ids) == 1:
            where = f"file_id = '{file_ids[0]}'"
        else:
            quoted = ", ".join(f"'{i}'" for i in file_ids)
            where  = f"file_id IN ({quoted})"

        rows = _store.db.open_table("documents").search().where(where).to_list()
        return {
            r["file_id"]: {
                "file_size":    r.get("file_size",    0),
                "content_type": r.get("content_type", ""),
                "indexed_at":   r.get("indexed_at",   ""),
                "source_type":  r.get("source_type",  ""),
                "file_type":    r.get("file_type",    ""),
                # These come from Round 2 (ingestion) — safe to default empty
                "doc_summary":  r.get("doc_summary",  ""),
                "total_pages":  r.get("total_pages",  None),
                "author":       r.get("author",       ""),
            }
            for r in rows if r.get("file_id")
        }
    except Exception as e:
        logger.warning(f"Doc meta batch fetch failed: {e}")
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# RESULT CLEANER
# ─────────────────────────────────────────────────────────────────────────────

_STRIP_FIELDS = {"vector", "_distance", "_score", "_relevance_score"}

def _clean(row: Dict) -> Dict:
    out = {}
    for k, v in row.items():
        if k in _STRIP_FIELDS:
            continue
        if isinstance(v, np.ndarray):                 out[k] = v.tolist()
        elif isinstance(v, (np.float32, np.float64)): out[k] = float(v)
        elif isinstance(v, (np.int32,   np.int64)):   out[k] = int(v)
        elif isinstance(v, dict):                     out[k] = _clean(v)
        elif isinstance(v, list):
            out[k] = [_clean(i) if isinstance(i, dict) else i for i in v]
        else:
            out[k] = v
    return out


# ─────────────────────────────────────────────────────────────────────────────
# LEXICAL ENRICHMENT
# ─────────────────────────────────────────────────────────────────────────────

STOPWORDS = {"a","an","the","is","in","it","of","to","and","or","for","on","at","by","with","be","are","was","were","has","have"}

def _tokenize(text: str) -> List[str]:
    tokens = re.split(r'\W+', text.lower())
    return [t for t in tokens if t and len(t) > 1 and t not in STOPWORDS]

def _compute_missing_terms(terms: List[str], text: str) -> List[str]:
    """Return query terms that don't appear anywhere in the chunk."""
    tl = text.lower()
    return [t for t in terms if t not in tl]

def _compute_match_score(text: str, terms: List[str], fts_score: float) -> Dict:
    tl     = text.lower()
    twords = re.split(r'\W+', tl)

    freqs     = {t: tl.count(t) for t in terms if tl.count(t) > 0}
    matched   = len(freqs)
    total     = sum(freqs.values())
    missing   = [t for t in terms if t not in freqs]

    positions = {}
    for t in terms:
        idx = tl.find(t)
        if idx != -1:
            positions[t] = idx

    proximity = 0.0
    if len(positions) > 1:
        pos_list  = sorted(positions.values())
        gaps      = [pos_list[i+1] - pos_list[i] for i in range(len(pos_list)-1)]
        avg_gap   = sum(gaps) / len(gaps) if gaps else 0
        proximity = round(max(0, 100 - avg_gap / 10), 1)

    return {
        "total_matches":          total,
        "unique_terms_matched":   matched,
        "total_query_terms":      len(terms),
        "query_coverage_pct":     round(matched / len(terms) * 100, 1) if terms else 0,
        "missing_terms":          missing,                   # ← NEW: missing terms
        "match_density":          round(total / len(text) * 1000, 3) if text else 0.0,
        "text_length_chars":      len(text),
        "text_length_tokens":     len(twords),
        "term_frequencies":       freqs,
        "term_positions":         positions,
        "term_proximity_score":   proximity,
        "fts_score":              round(float(fts_score), 6),
    }

def _compute_highlights(text: str, terms: List[str], max_snippets: int = 5) -> List[Dict]:
    """
    Returns highlighted snippets AND flat highlight_spans for char-offset rendering.
    """
    tl       = text.lower()
    snippets = []
    seen     = set()

    for term in terms:
        pos = 0
        while len(snippets) < max_snippets and (idx := tl.find(term, pos)) != -1:
            s   = max(0, idx - 150)
            e   = min(len(text), idx + len(term) + 150)
            key = f"{s // 100}"
            if key not in seen:
                seen.add(key)
                raw_snippet = ("…" if s > 0 else "") + text[s:e] + ("…" if e < len(text) else "")
                hi = raw_snippet
                for t in terms:
                    hi = re.sub(f"({re.escape(t)})", r"<mark>\1</mark>", hi, flags=re.IGNORECASE)
                snippets.append({
                    "text":           raw_snippet,
                    "highlighted":    hi,
                    "matched_term":   term,
                    "char_offset":    idx,
                    "snippet_length": len(raw_snippet),
                })
            pos = idx + 1

    return snippets

def _compute_highlight_spans(text: str, terms: List[str]) -> List[Dict]:
    """
    Character-level span offsets for every term match in the full text.
    UI can use these to do its own rendering without innerHTML.
    Format: [{"start": 0, "end": 7, "term": "mixture"}, ...]
    """
    spans = []
    for term in terms:
        for m in re.finditer(re.escape(term), text, re.IGNORECASE):
            spans.append({"start": m.start(), "end": m.end(), "term": term})
    return sorted(spans, key=lambda x: x["start"])

def _enrich_lexical(row: Dict, query: str, expanded: Dict) -> Dict:
    text      = row.get("text", "")
    terms     = _tokenize(query)
    fts_score = float(row.get("_score", 0.0))

    row["query_terms"]       = terms
    row["expanded_terms"]    = expanded["expanded_terms"]   # ← what got added
    row["match_score"]       = _compute_match_score(text, terms, fts_score)
    row["highlights"]        = _compute_highlights(text, terms, max_snippets=5)
    row["highlight_spans"]   = _compute_highlight_spans(text, terms)  # ← NEW
    row["semantic_info"]     = {}
    row["hybrid_info"]       = {}
    row["search_type"]       = "lexical"
    return row


# ─────────────────────────────────────────────────────────────────────────────
# HYBRID ENRICHMENT
# ─────────────────────────────────────────────────────────────────────────────

def _enrich_hybrid(row: Dict, rank: int, query: str, expanded: Dict) -> Dict:
    score = float(row.get("_relevance_score", row.get("_score", 0.0)))
    dist  = row.get("_distance", None)

    row["hybrid_info"] = {
        "lancedb_fused_score": round(score, 6),
        "rank":                rank,
        "search_type":         "hybrid (rrf)",
    }

    if dist is not None:
        dist_f = float(dist)
        row["semantic_info"] = {
            "vector_distance":  round(dist_f, 6),
            "similarity_score": round(1 / (1 + dist_f) * 100, 2),
            "cosine_like_score": round(max(0.0, 1.0 - dist_f), 4),
        }
    else:
        row["semantic_info"] = {}

    terms = _tokenize(query)
    text  = row.get("text", "")
    row["query_terms"]      = terms
    row["expanded_terms"]   = expanded["expanded_terms"]
    row["match_score"]      = _compute_match_score(text, terms, fts_score=score)
    row["highlights"]       = _compute_highlights(text, terms, max_snippets=3)
    row["highlight_spans"]  = _compute_highlight_spans(text, terms)   # ← NEW
    row["search_type"]      = "hybrid"
    return row


# ─────────────────────────────────────────────────────────────────────────────
# FILE-LEVEL AGGREGATION
# ─────────────────────────────────────────────────────────────────────────────

def _aggregate_by_file(results: List[Dict]) -> List[Dict]:
    """
    Group chunks by file_id. For each file:
    - Pick the best chunk (highest score) as representative
    - Aggregate stats across all chunks
    - Return file-ranked list (rank #1, #2, #3 — always sequential)
    """
    groups: Dict[str, Dict] = {}

    for r in results:
        fid = r.get("file_id", "")
        if fid not in groups:
            groups[fid] = {
                "file_id":         fid,
                "filename":        r.get("filename", ""),
                "search_type":     r.get("search_type", ""),
                "doc_meta": {
                    "file_size":    r.get("file_size", 0),
                    "content_type": r.get("content_type", ""),
                    "indexed_at":   r.get("indexed_at", ""),
                    "source_type":  r.get("source_type", ""),
                    "file_type":    r.get("file_type", ""),
                    "doc_summary":  r.get("doc_summary", ""),
                    "total_pages":  r.get("total_pages", None),
                    "author":       r.get("author", ""),
                },
                "chunks":          [],
                "top_score":       0.0,
                "best_chunk":      None,
                "all_missing_terms": set(),
            }

        # Score for this chunk
        score = (
            r.get("hybrid_info", {}).get("lancedb_fused_score")
            or r.get("match_score", {}).get("fts_score")
            or 0.0
        )

        groups[fid]["chunks"].append(r)
        groups[fid]["all_missing_terms"].update(
            r.get("match_score", {}).get("missing_terms", [])
        )

        if score > groups[fid]["top_score"]:
            groups[fid]["top_score"]  = score
            groups[fid]["best_chunk"] = r

    # Sort by top score → sequential file rank
    sorted_groups = sorted(groups.values(), key=lambda g: g["top_score"], reverse=True)

    for file_rank, g in enumerate(sorted_groups, start=1):
        g["file_rank"]          = file_rank
        g["chunk_count"]        = len(g["chunks"])
        g["top_score"]          = round(g["top_score"], 6)
        # Terms missing from ALL chunks of this file = truly not in the doc
        g["missing_terms"]      = list(g["all_missing_terms"])
        g["query_coverage_pct"] = g["best_chunk"].get("match_score", {}).get("query_coverage_pct", 0)
        del g["all_missing_terms"]

    return sorted_groups


# ─────────────────────────────────────────────────────────────────────────────
# TRACK A: LEXICAL SEARCH
# ─────────────────────────────────────────────────────────────────────────────

async def _lexical_search(
    query: str, limit: int, where: Optional[str], expanded: Dict
) -> List[Dict]:
    try:
        table  = _store.chunks
        # Use expanded FTS query for better recall
        search = table.search(expanded["fts_query"], query_type="fts").limit(limit)
        if where:
            search = search.where(where, prefilter=True)
        rows = search.to_list()

        unique_fids = list({r["file_id"] for r in rows if r.get("file_id")})
        doc_meta    = _fetch_doc_meta_batch(unique_fids)

        results = []
        for row in rows:
            row = _enrich_lexical(row, query, expanded)
            row.update(doc_meta.get(row["file_id"], {}))
            results.append(_clean(row))

        logger.info(f"Lexical → {len(results)} chunks across {len(unique_fids)} files")
        return results

    except Exception as e:
        logger.error(f"Lexical search error: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# TRACK B: HYBRID SEARCH
# ─────────────────────────────────────────────────────────────────────────────

async def _hybrid_search(
    query: str, limit: int, where: Optional[str], expanded: Dict
) -> List[Dict]:
    try:
        table = _store.chunks
        vec   = await _embedder.embed(query)

        search = (
            table
            .search(query_type="hybrid")
            .vector(vec.tolist())
            .text(expanded["fts_query"])   # expanded query for FTS leg
            .rerank(reranker=_reranker)
            .limit(limit)
        )
        if where:
            search = search.where(where, prefilter=True)

        rows = search.to_list()

        unique_fids = list({r["file_id"] for r in rows if r.get("file_id")})
        doc_meta    = _fetch_doc_meta_batch(unique_fids)

        results = []
        for rank, row in enumerate(rows):
            row = _enrich_hybrid(row, rank + 1, query, expanded)
            row.update(doc_meta.get(row["file_id"], {}))
            results.append(_clean(row))

        logger.info(f"Hybrid → {len(results)} chunks across {len(unique_fids)} files")
        return results

    except Exception as e:
        logger.error(f"Hybrid search error: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# SSE STREAMING RESPONSE
# ─────────────────────────────────────────────────────────────────────────────

async def stream_search(
    query:       str,
    limit:       int,
    source_type: Optional[str] = None,
    file_type:   Optional[str] = None,
    date_from:   Optional[str] = None,
    date_to:     Optional[str] = None,
    duration:    Optional[str] = None,
):
    """
    SSE events:
      { type: "meta",    query_info: { original, expanded_terms, fts_query } }
      { type: "lexical", results: [...file-grouped...], count: N, elapsed_ms: N }
      { type: "hybrid",  results: [...file-grouped...], count: N, elapsed_ms: N }
      { type: "done",    total_elapsed_ms: N }
    """
    t0 = time.monotonic()

    expanded = _expand_query(query)
    file_ids = _resolve_file_ids(source_type, file_type, date_from, date_to, duration)
    where    = _build_where(file_ids)

    # Emit query metadata immediately so UI can show "Also searched: MoE, sparse gating"
    yield f"data: {json.dumps({'type': 'meta', 'query_info': expanded})}\n\n"

    lex_task = asyncio.create_task(_lexical_search(query, limit, where, expanded))
    hyb_task = asyncio.create_task(_hybrid_search(query, limit, where, expanded))

    lex_chunks  = await lex_task
    lex_grouped = _aggregate_by_file(lex_chunks)   # ← file-level aggregation
    lex_ms      = round((time.monotonic() - t0) * 1000)
    yield f"data: {json.dumps({'type': 'lexical', 'results': lex_grouped, 'count': len(lex_grouped), 'elapsed_ms': lex_ms})}\n\n"

    hyb_chunks  = await hyb_task
    hyb_grouped = _aggregate_by_file(hyb_chunks)   # ← file-level aggregation
    hyb_ms      = round((time.monotonic() - t0) * 1000)
    yield f"data: {json.dumps({'type': 'hybrid', 'results': hyb_grouped, 'count': len(hyb_grouped), 'elapsed_ms': hyb_ms})}\n\n"

    total_ms = round((time.monotonic() - t0) * 1000)
    yield f"data: {json.dumps({'type': 'done', 'total_elapsed_ms': total_ms})}\n\n"


# ─────────────────────────────────────────────────────────────────────────────
# NON-STREAMING
# ─────────────────────────────────────────────────────────────────────────────

async def search_once(
    query:       str,
    limit:       int,
    source_type: Optional[str] = None,
    file_type:   Optional[str] = None,
    date_from:   Optional[str] = None,
    date_to:     Optional[str] = None,
    duration:    Optional[str] = None,
) -> Dict:
    t0 = time.monotonic()

    expanded = _expand_query(query)
    file_ids = _resolve_file_ids(source_type, file_type, date_from, date_to, duration)
    where    = _build_where(file_ids)

    lex_chunks, hyb_chunks = await asyncio.gather(
        _lexical_search(query, limit, where, expanded),
        _hybrid_search(query, limit, where, expanded),
    )

    elapsed = round((time.monotonic() - t0) * 1000)
    return {
        "query":            query,
        "query_info":       expanded,
        "lexical":          _aggregate_by_file(lex_chunks),
        "hybrid":           _aggregate_by_file(hyb_chunks),
        "lexical_count":    len(lex_chunks),
        "hybrid_count":     len(hyb_chunks),
        "total_elapsed_ms": elapsed,
        "filters": {
            "source_type": source_type,
            "file_type":   file_type,
            "date_from":   date_from,
            "date_to":     date_to,
            "duration":    duration,
        },
    }
