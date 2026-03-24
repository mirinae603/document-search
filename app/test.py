#!/usr/bin/env python3
"""
test_api.py — Interactive API tester for Document Search System
Run: python test_api.py
"""

import json, os, sys, time, httpx
from pathlib import Path

BASE    = os.getenv("API_BASE", "http://localhost:8000")
TIMEOUT = 60

G="\033[92m"; R="\033[91m"; Y="\033[93m"; B="\033[94m"
C="\033[96m"; DIM="\033[90m"; NC="\033[0m"; BOLD="\033[1m"

def hdr(t):  print(f"\n{B}{'─'*58}{NC}\n{BOLD}  {t}{NC}\n{B}{'─'*58}{NC}")
def ok(m):   print(f"  {G}✓{NC}  {m}")
def err(m):  print(f"  {R}✗{NC}  {m}")
def info(m): print(f"  {C}→{NC}  {m}")
def dim(m):  print(f"     {DIM}{m}{NC}")
def ask(m, default=""): 
    v = input(f"  {Y}?{NC}  {m}{f' [{default}]' if default else ''}: ").strip()
    return v or default

def pjson(d): print(f"\n{DIM}{json.dumps(d, indent=2, default=str)[:2000]}{NC}\n")

client = httpx.Client(base_url=BASE, timeout=TIMEOUT)

# ─────────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────────

def ep_health():
    hdr("GET /health")
    r = client.get("/health")
    ok(f"HTTP {r.status_code}") if r.status_code == 200 else err(f"HTTP {r.status_code}")
    pjson(r.json())

def ep_stats():
    hdr("GET /stats")
    r = client.get("/stats")
    ok(f"HTTP {r.status_code}") if r.status_code == 200 else err(f"HTTP {r.status_code}")
    pjson(r.json())

def ep_upload():
    hdr("POST /upload")
    path = ask("Path to file (pdf/docx/txt)")
    if not path:
        err("No path given"); return

    fp = Path(path).expanduser()
    if not fp.exists():
        err(f"File not found: {fp}"); return

    mime = {
        ".pdf":  "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".txt":  "text/plain",
    }.get(fp.suffix.lower(), "application/octet-stream")

    info(f"Uploading {fp.name} ({fp.stat().st_size} bytes) as {mime}")
    t0 = time.time()
    r  = client.post("/upload", files={"file": (fp.name, fp.read_bytes(), mime)})
    ms = int((time.time()-t0)*1000)

    if r.status_code in (200, 201):
        ok(f"HTTP {r.status_code}  ({ms}ms)")
        pjson(r.json())
    else:
        err(f"HTTP {r.status_code}  ({ms}ms)")
        print(r.text[:500])

def ep_documents():
    hdr("GET /documents")
    r = client.get("/documents")
    if r.status_code == 200:
        docs = r.json()
        ok(f"HTTP {r.status_code} — {len(docs)} documents")
        pjson(docs)
    elif r.status_code == 404:
        info("Endpoint not implemented on this server")
    else:
        err(f"HTTP {r.status_code}")
        print(r.text[:300])

def ep_delete():
    hdr("DELETE /documents/{file_id}")
    file_id = ask("file_id to delete")
    if not file_id: err("No file_id given"); return
    r = client.delete(f"/documents/{file_id}")
    ok(f"HTTP {r.status_code}") if r.status_code in (200,204) else err(f"HTTP {r.status_code}")
    try: pjson(r.json())
    except: print(r.text[:200])

def ep_search_stream():
    hdr("GET /search  (SSE streaming)")
    q     = ask("Query", "mixture of experts")
    limit = ask("Limit", "5")
    src   = ask("source_type filter [uploaded/webhook/reindex] or blank", "")
    ft    = ask("file_type filter [pdf/docx/txt] or blank", "")
    dur   = ask("duration [last_24h/last_7d/last_30d] or blank", "")

    params = {"query": q, "limit": limit, "stream": "true"}
    if src: params["source_type"] = src
    if ft:  params["file_type"]   = ft
    if dur: params["duration"]    = dur

    info(f"Connecting to SSE stream…")
    t0 = time.time()
    try:
        url = "/search?" + "&".join(f"{k}={v}" for k,v in params.items())
        with client.stream("GET", url) as r:
            ok(f"HTTP {r.status_code}")
            for line in r.iter_lines():
                if not line.startswith("data:"): continue
                try:   msg = json.loads(line[5:].strip())
                except: continue
                t = msg.get("type")
                ms = int((time.time()-t0)*1000)
                if t == "lexical":
                    res = msg.get("results",[])
                    ok(f"[{ms}ms] LEXICAL — {len(res)} results")
                    for i,x in enumerate(res[:5]):
                        ms_score = x.get("match_score",{})
                        dim(f"[{i+1}] {x.get('filename')}  chunk#{x.get('chunk_index')}  "
                            f"bm25={ms_score.get('fts_score','–')}  "
                            f"cov={ms_score.get('query_coverage_pct','–')}%  "
                            f"matches={ms_score.get('total_matches','–')}")
                elif t == "hybrid":
                    res = msg.get("results",[])
                    ok(f"[{ms}ms] HYBRID  — {len(res)} results")
                    for i,x in enumerate(res[:5]):
                        hi = x.get("hybrid_info",{})
                        dim(f"[{i+1}] {x.get('filename')}  chunk#{x.get('chunk_index')}  "
                            f"fused={hi.get('lancedb_fused_score','–')}  "
                            f"rank={hi.get('rank','–')}")
                elif t == "done":
                    ok(f"[{ms}ms] DONE")
                elif t == "error":
                    err(f"Server error: {msg.get('message')}")
    except Exception as e:
        err(str(e))

def ep_search_nostream():
    hdr("GET /search  (non-streaming)")
    q     = ask("Query", "mixture of experts")
    limit = ask("Limit", "5")
    src   = ask("source_type filter or blank", "")
    ft    = ask("file_type filter or blank", "")
    dur   = ask("duration or blank", "")

    params = {"query": q, "limit": limit, "stream": "false"}
    if src: params["source_type"] = src
    if ft:  params["file_type"]   = ft
    if dur: params["duration"]    = dur

    t0 = time.time()
    r  = client.get("/search", params=params)
    ms = int((time.time()-t0)*1000)
    if r.status_code == 200:
        ok(f"HTTP {r.status_code}  ({ms}ms)")
        pjson(r.json())
    else:
        err(f"HTTP {r.status_code}  ({ms}ms)")
        print(r.text[:400])

def ep_qa_sources():
    hdr("POST /qa/sources")
    q       = ask("Question", "summarize this document")
    fids    = ask("file_ids (comma separated)")
    top_k   = ask("top_k", "5")

    if not fids: err("No file_ids given"); return
    payload = {
        "question": q,
        "file_ids": [f.strip() for f in fids.split(",")],
        "top_k":    int(top_k),
    }
    t0 = time.time()
    r  = client.post("/qa/sources", json=payload)
    ms = int((time.time()-t0)*1000)
    if r.status_code == 200:
        ok(f"HTTP {r.status_code}  ({ms}ms)")
        pjson(r.json())
    else:
        err(f"HTTP {r.status_code}  ({ms}ms)")
        print(r.text[:400])

def ep_qa_ask():
    hdr("POST /qa/ask  (streaming)")
    q     = ask("Question", "summarize this document")
    fids  = ask("file_ids (comma separated)")
    top_k = ask("top_k", "5")

    if not fids: err("No file_ids given"); return
    payload = {
        "question":             q,
        "file_ids":             [f.strip() for f in fids.split(",")],
        "top_k":                int(top_k),
        "conversation_history": [],
    }
    info("Streaming answer…\n")
    try:
        with client.stream("POST", "/qa/ask?stream=true", json=payload) as r:
            ok(f"HTTP {r.status_code}\n")
            buf = ""
            answer = ""
            for raw in r.iter_bytes():
                buf += raw.decode("utf-8", errors="ignore")
                lines = buf.split("\n")
                buf   = lines.pop()
                for line in lines:
                    if not line.startswith("data:"): continue
                    try:   msg = json.loads(line[5:].strip())
                    except: continue
                    t = msg.get("type")
                    if t == "sources":
                        info(f"Sources ({len(msg.get('sources',[]))} chunks):")
                        for s in msg.get("sources",[]):
                            dim(f"{s.get('filename')}  score={s.get('score','–')}")
                        print()
                    elif t == "token":
                        tok = msg.get("token","")
                        print(tok, end="", flush=True)
                        answer += tok
                    elif t == "done":
                        print(f"\n\n{ok('Stream complete')}")
                    elif t == "error":
                        err(f"\nServer error: {msg.get('message')}")
    except Exception as e:
        err(str(e))

def ep_qa_ask_nostream():
    hdr("POST /qa/ask  (non-streaming)")
    q     = ask("Question", "summarize this document")
    fids  = ask("file_ids (comma separated)")
    top_k = ask("top_k", "5")

    if not fids: err("No file_ids given"); return
    payload = {
        "question":             q,
        "file_ids":             [f.strip() for f in fids.split(",")],
        "top_k":                int(top_k),
        "conversation_history": [],
    }
    t0 = time.time()
    r  = client.post("/qa/ask?stream=false", json=payload)
    ms = int((time.time()-t0)*1000)
    if r.status_code == 200:
        ok(f"HTTP {r.status_code}  ({ms}ms)")
        pjson(r.json())
    else:
        err(f"HTTP {r.status_code}  ({ms}ms)")
        print(r.text[:400])

def ep_reindex():
    hdr("POST /admin/reindex")
    mode = ask("mode [all/file]", "all")
    bg   = ask("background [true/false]", "true")
    params = {"mode": mode, "background": bg}
    if mode == "file":
        fid = ask("file_id")
        if fid: params["file_id"] = fid
    r = client.post("/admin/reindex", params=params)
    ok(f"HTTP {r.status_code}") if r.status_code == 200 else err(f"HTTP {r.status_code}")
    try: pjson(r.json())
    except: print(r.text[:200])

def ep_preview():
    hdr("GET /preview")
    fp = ask("file_path (e.g. /documents/myfile.pdf)")
    if not fp: err("No file_path given"); return
    r = client.get("/preview", params={"file_path": fp})
    ok(f"HTTP {r.status_code}")
    ct = r.headers.get("content-type","")
    info(f"Content-Type: {ct}")
    if "json" in ct:
        pjson(r.json())
    elif "pdf" in ct:
        info(f"PDF binary received — {len(r.content)} bytes")
    else:
        print(r.text[:500])

def ep_webhook():
    hdr("POST /webhook/seaweed  (manual trigger)")
    info("Simulate a SeaweedFS webhook event")
    fp   = ask("file_path on seaweed (e.g. /documents/test.txt)")
    evt  = ask("event_type [upload/delete]", "upload")
    payload = {"event": evt, "file_path": fp, "size": 1024}
    r = client.post("/webhook/seaweed", json=payload)
    ok(f"HTTP {r.status_code}") if r.status_code in (200,202) else err(f"HTTP {r.status_code}")
    try: pjson(r.json())
    except: print(r.text[:200])

def ep_custom():
    hdr("CUSTOM REQUEST")
    method = ask("Method [GET/POST/DELETE]", "GET").upper()
    path   = ask("Path (e.g. /health)")
    params_raw = ask("Query params as JSON or blank", "")
    body_raw   = ask("JSON body or blank", "")

    params = json.loads(params_raw) if params_raw else {}
    body   = json.loads(body_raw)   if body_raw   else None

    try:
        if method == "GET":
            r = client.get(path, params=params)
        elif method == "POST":
            r = client.post(path, params=params, json=body)
        elif method == "DELETE":
            r = client.delete(path, params=params)
        else:
            err("Unknown method"); return

        ok(f"HTTP {r.status_code}")
        info(f"Headers: {dict(r.headers)}")
        try:    pjson(r.json())
        except: print(r.text[:800])
    except Exception as e:
        err(str(e))

# ─────────────────────────────────────────────────────────────────
# MENU
# ─────────────────────────────────────────────────────────────────

MENU = [
    ("Health check",               ep_health),
    ("Stats",                      ep_stats),
    ("Upload file",                ep_upload),
    ("List documents",             ep_documents),
    ("Delete document",            ep_delete),
    ("Search — SSE streaming",     ep_search_stream),
    ("Search — non-streaming",     ep_search_nostream),
    ("Q&A — preview sources",      ep_qa_sources),
    ("Q&A — ask (streaming)",      ep_qa_ask),
    ("Q&A — ask (non-streaming)",  ep_qa_ask_nostream),
    ("Admin reindex",              ep_reindex),
    ("Preview file",               ep_preview),
    ("Webhook trigger",            ep_webhook),
    ("Custom request",             ep_custom),
]

def menu():
    print(f"\n{BOLD}{B}  DOC SEARCH — API TESTER   {BASE}{NC}")
    while True:
        print(f"\n{B}{'─'*40}{NC}")
        for i, (label, _) in enumerate(MENU, 1):
            print(f"  {Y}{i:>2}{NC}  {label}")
        print(f"  {Y} 0{NC}  Exit")
        print(f"{B}{'─'*40}{NC}")
        choice = ask("Pick endpoint")
        if choice == "0":
            print(f"\n{G}  bye{NC}\n")
            sys.exit(0)
        if not choice.isdigit() or not (1 <= int(choice) <= len(MENU)):
            err("Invalid choice"); continue
        try:
            MENU[int(choice)-1][1]()
        except KeyboardInterrupt:
            info("Cancelled")
        except Exception as e:
            err(f"Unexpected error: {e}")

if __name__ == "__main__":
    menu()
