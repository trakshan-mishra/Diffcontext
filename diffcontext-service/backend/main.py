"""
DiffContext Web Service — FastAPI Backend (Phase 1 Agentic Upgrade)

New in this version:
  - POST /clone     — clone a GitHub repo by URL, index it, return repo_id
  - GET  /repo      — list all active indexed sessions
  - GET  /search    — BM25 keyword search over symbol names in a repo
  - GET  /resolve   — resolve a plain function name to full symbol IDs
  - GET  /blast_file — file-level blast radius (all symbols in a file)
  - SQLite-backed session store (survives in-container restarts)
"""

import ast
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
import uuid
import zipfile
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="DiffContext API",
    description="Repository intelligence: blast radius, symbol search, call graphs — for AI coding agents.",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── SQLite session store ──────────────────────────────────────────────────────

DB_PATH = os.environ.get("DIFFCONTEXT_DB", "/tmp/diffcontext_sessions.db")

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            repo_id    TEXT PRIMARY KEY,
            repo_path  TEXT NOT NULL,
            name       TEXT,
            git_url    TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    return conn

def _session_save(repo_id: str, repo_path: str, name: str, git_url: str = ""):
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?)",
            (repo_id, repo_path, name, git_url, datetime.utcnow().isoformat())
        )

def _session_get(repo_id: str) -> Optional[dict]:
    with _db() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE repo_id=?", (repo_id,)).fetchone()
        if row:
            return dict(row)
    return None

def _session_list() -> List[dict]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM sessions ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows if os.path.isdir(r["repo_path"])]

def _session_path(repo_id: str) -> str:
    """Return repo_path or raise 404."""
    session = _session_get(repo_id)
    if not session or not os.path.isdir(session["repo_path"]):
        raise HTTPException(
            404,
            f"repo_id '{repo_id}' not found. "
            "Use POST /upload (zip) or POST /clone (GitHub URL) first."
        )
    return session["repo_path"]

# Initialise DB on startup
_db()


# ── Pydantic models ──────────────────────────────────────────────────────────

class FileItem(BaseModel):
    filename: str
    content: str

class InlineRequest(BaseModel):
    """Paste code directly — no upload needed."""
    files: List[FileItem]
    symbol: str

class BlastRequest(BaseModel):
    repo_id: str
    symbol: str

class CompileRequest(BaseModel):
    repo_id: str
    symbol: str
    max_tokens: int = 8000

class CloneRequest(BaseModel):
    """Clone a GitHub (or any public git) repository by URL."""
    git_url: str
    name: Optional[str] = None

class HealthResponse(BaseModel):
    status: str

class RootResponse(BaseModel):
    service: str
    docs: str
    endpoints: List[str]


# ── DiffContext pipeline helpers ──────────────────────────────────────────────

def _try_import_dc():
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from diffcontext.pipeline import index_repository, analyze_impact
    from diffcontext.pipeline import compile as dc_compile
    return index_repository, analyze_impact, dc_compile

def _list_symbols(repo_path: str) -> List[str]:
    symbols = []
    for py_file in Path(repo_path).rglob("*.py"):
        rel = "./" + str(py_file.relative_to(repo_path))
        try:
            source = py_file.read_text(errors="ignore")
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(f"{rel}:{node.name}")
    return sorted(symbols)

def _run_diffcontext(repo_path: str, symbol: str) -> dict:
    try:
        index_repository, analyze_impact, _ = _try_import_dc()
        idx = index_repository(repo_path)
        if symbol not in idx.symbols and symbol not in idx.graph:
            import difflib
            suggestions = difflib.get_close_matches(symbol, list(idx.symbols.keys()), n=3, cutoff=0.4)
            return {"error": f"Symbol '{symbol}' not found.", "suggestions": suggestions}

        impact = analyze_impact(idx, [symbol])
        reverse: Dict[str, List[str]] = {}
        for caller, callees in idx.graph.items():
            for callee in callees:
                reverse.setdefault(callee, []).append(caller)

        by_file: Dict[str, List[str]] = {}
        for sym in impact.blast_radius:
            fp = sym.split(":")[0]
            by_file.setdefault(fp, []).append(sym.split(":")[-1])

        return {
            "symbol": symbol,
            "direct_callers": reverse.get(symbol, []),
            "direct_callees": idx.graph.get(symbol, []),
            "blast_radius_count": len(impact.blast_radius),
            "blast_radius_by_file": by_file,
            "all_affected": impact.blast_radius[:50],
            "scores": {k: round(v, 1) for k, v in list(impact.scores.items())[:30]},
            "total_symbols_in_repo": len(idx.symbols),
        }
    except ImportError:
        return _ast_fallback(repo_path, symbol)

def _ast_fallback(repo_path: str, symbol: str) -> dict:
    all_functions: Dict[str, str] = {}
    call_graph: Dict[str, List[str]] = {}

    for py_file in Path(repo_path).rglob("*.py"):
        rel = "./" + str(py_file.relative_to(repo_path))
        try:
            source = py_file.read_text(errors="ignore")
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fid = f"{rel}:{node.name}"
                all_functions[fid] = ""
                calls = []
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        if isinstance(child.func, ast.Name):
                            calls.append(child.func.id)
                        elif isinstance(child.func, ast.Attribute):
                            calls.append(child.func.attr)
                call_graph[fid] = calls

    name_to_ids: Dict[str, List[str]] = {}
    for fid in all_functions:
        name_to_ids.setdefault(fid.split(":")[-1], []).append(fid)

    resolved: Dict[str, List[str]] = {}
    for fid, raw in call_graph.items():
        resolved[fid] = [c for name in raw for c in name_to_ids.get(name, []) if c != fid]

    reverse: Dict[str, List[str]] = {}
    for caller, callees in resolved.items():
        for callee in callees:
            reverse.setdefault(callee, []).append(caller)

    if symbol not in all_functions:
        import difflib
        suggestions = difflib.get_close_matches(symbol, list(all_functions.keys()), n=3, cutoff=0.4)
        return {"error": f"Symbol '{symbol}' not found.", "suggestions": suggestions,
                "all_symbols": list(all_functions.keys())}

    visited = {symbol}
    blast, frontier = [], [symbol]
    while frontier:
        nxt = []
        for node in frontier:
            for caller in reverse.get(node, []):
                if caller not in visited:
                    visited.add(caller)
                    blast.append(caller)
                    nxt.append(caller)
        frontier = nxt

    by_file: Dict[str, List[str]] = {}
    for sym in blast:
        fp = sym.split(":")[0]
        by_file.setdefault(fp, []).append(sym.split(":")[-1])

    return {
        "symbol": symbol,
        "direct_callers": reverse.get(symbol, []),
        "direct_callees": resolved.get(symbol, []),
        "blast_radius_count": len(blast),
        "blast_radius_by_file": by_file,
        "all_affected": blast[:50],
        "scores": {},
        "total_symbols_in_repo": len(all_functions),
    }


# ── BM25 keyword search helper ────────────────────────────────────────────────

def _bm25_search(repo_path: str, query: str, top_k: int = 20) -> List[str]:
    try:
        from rank_bm25 import BM25Okapi
    except ImportError:
        # Fallback: simple substring match
        symbols = _list_symbols(repo_path)
        q = query.lower()
        return [s for s in symbols if q in s.lower()][:top_k]

    symbols = _list_symbols(repo_path)
    if not symbols:
        return []

    # Tokenise each symbol ID into words
    def tokenise(s: str) -> List[str]:
        # Split on / : . _ uppercase boundaries
        return re.split(r"[/:\._]|(?<=[a-z])(?=[A-Z])", s.lower())

    corpus = [tokenise(s) for s in symbols]
    bm25 = BM25Okapi(corpus)
    query_tokens = tokenise(query)
    scores = bm25.get_scores(query_tokens)
    ranked = sorted(zip(scores, symbols), reverse=True)
    return [sym for score, sym in ranked if score > 0][:top_k]


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", response_model=RootResponse)
def root():
    return {
        "service": "DiffContext API v2",
        "docs": "/docs",
        "endpoints": [
            "POST /upload      — upload a .zip of your project",
            "POST /clone       — clone a GitHub repo by URL",
            "GET  /repo        — list all active indexed sessions",
            "POST /inline      — paste code directly (no upload needed)",
            "GET  /search      — BM25 keyword search over symbols",
            "GET  /resolve     — resolve a plain function name to symbol IDs",
            "GET  /blast_file  — file-level blast radius",
            "POST /blast       — get blast radius for a function",
            "GET  /symbols     — list all functions in uploaded repo",
            "POST /compile     — get LLM-ready context string",
            "GET  /health      — service health check",
        ],
    }


@app.get("/health", response_model=HealthResponse)
def health():
    return {"status": "ok"}


@app.get("/repo")
def list_repos():
    """List all active indexed repository sessions."""
    sessions = _session_list()
    return {"count": len(sessions), "repos": sessions}


@app.post("/clone")
def clone_repo(req: CloneRequest):
    """
    Clone a public GitHub (or any public git) repository by URL,
    index it, and return a repo_id for use in all other endpoints.

    Example body:
    {
      "git_url": "https://github.com/pallets/flask.git",
      "name": "flask"
    }
    """
    git_url = req.git_url.strip()
    if not git_url.startswith(("https://", "http://", "git@")):
        raise HTTPException(400, "Invalid git URL. Must start with https:// or git@")

    name = req.name or git_url.rstrip("/").split("/")[-1].replace(".git", "")
    tmp_dir = tempfile.mkdtemp(prefix="diffctx_clone_")
    repo_path = os.path.join(tmp_dir, name)

    try:
        result = subprocess.run(
            ["git", "clone", "--depth=1", git_url, repo_path],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise HTTPException(400, f"git clone failed: {result.stderr.strip()}")
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(408, "git clone timed out (120s). Try a smaller repo.")

    py_files = list(Path(repo_path).rglob("*.py"))
    if not py_files:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(400, "No Python files found in repository.")

    repo_id = "repo_" + str(uuid.uuid4())[:8]
    _session_save(repo_id, repo_path, name, git_url)
    symbols = _list_symbols(repo_path)

    return {
        "repo_id": repo_id,
        "name": name,
        "git_url": git_url,
        "message": f"Cloned and indexed '{name}'. Found {len(symbols)} functions.",
        "symbol_count": len(symbols),
        "sample_symbols": symbols[:10],
        "next_steps": [
            f"GET /search?repo_id={repo_id}&query=<keyword>",
            f"GET /resolve?repo_id={repo_id}&name=<function_name>",
            f"POST /blast with {{\"repo_id\": \"{repo_id}\", \"symbol\": \"<symbol_id>\"}}",
        ],
    }


@app.post("/upload")
async def upload_repo(file: UploadFile = File(...)):
    """
    Upload a .zip of your Python project.
    Returns a repo_id you use in all other calls.
    """
    if not file.filename.endswith(".zip"):
        raise HTTPException(400, "Please upload a .zip file of your project folder.")

    tmp_dir = tempfile.mkdtemp(prefix="diffctx_")
    zip_path = os.path.join(tmp_dir, "repo.zip")
    extract_path = os.path.join(tmp_dir, "repo")

    content = await file.read()
    with open(zip_path, "wb") as f:
        f.write(content)

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_path)
    except zipfile.BadZipFile:
        shutil.rmtree(tmp_dir)
        raise HTTPException(400, "Could not open zip file.")

    py_files = list(Path(extract_path).rglob("*.py"))
    if not py_files:
        shutil.rmtree(tmp_dir)
        raise HTTPException(400, "No Python (.py) files found in your zip.")

    repo_root = str(min(py_files, key=lambda p: len(p.parts)).parent)
    repo_id = "repo_" + str(uuid.uuid4())[:8]
    name = Path(file.filename).stem
    _session_save(repo_id, repo_root, name)
    symbols = _list_symbols(repo_root)

    return {
        "repo_id": repo_id,
        "name": name,
        "message": f"Uploaded successfully! Found {len(symbols)} functions.",
        "symbol_count": len(symbols),
        "sample_symbols": symbols[:10],
        "next_step": f'GET /search?repo_id={repo_id}&query=<keyword>',
    }


@app.get("/search")
def search_symbols(
    repo_id: str = Query(..., description="The repo_id from /upload or /clone"),
    query: str = Query(..., description="Keyword to search for (e.g. 'authentication', 'cache', 'jwt')"),
    top_k: int = Query(20, description="Max results to return"),
):
    """
    BM25 keyword search over all symbol names and file paths in the repository.
    Use this to find functions related to a concept without knowing the exact name.
    """
    repo_path = _session_path(repo_id)
    results = _bm25_search(repo_path, query, top_k)
    return {
        "repo_id": repo_id,
        "query": query,
        "count": len(results),
        "results": results,
    }


@app.get("/resolve")
def resolve_symbol(
    repo_id: str = Query(..., description="The repo_id from /upload or /clone"),
    name: str = Query(..., description="Plain function name to resolve, e.g. 'create_user'"),
):
    """
    Resolve a plain function name to all matching full symbol IDs.
    Use this before calling /blast when you know the function name but not the file.
    Returns all matches, e.g. ['./users.py:create_user', './service.py:create_user'].
    """
    repo_path = _session_path(repo_id)
    symbols = _list_symbols(repo_path)
    # Match by exact function name (last part after the colon)
    exact = [s for s in symbols if s.split(":")[-1] == name]
    # Fuzzy match if no exact hits
    if not exact:
        import difflib
        exact = difflib.get_close_matches(name, [s.split(":")[-1] for s in symbols], n=5, cutoff=0.6)
        exact = [s for s in symbols if s.split(":")[-1] in exact]
    return {
        "repo_id": repo_id,
        "name": name,
        "match_count": len(exact),
        "matches": exact,
        "tip": "Use a match from 'matches' as the 'symbol' in POST /blast",
    }


@app.get("/blast_file")
def blast_radius_file(
    repo_id: str = Query(..., description="The repo_id from /upload or /clone"),
    file: str = Query(..., description="Relative file path, e.g. './auth.py' or 'src/auth.py'"),
):
    """
    Get the aggregated blast radius for all public symbols in a given file.
    Use this when the user asks 'what breaks if I change auth.py?' or 'show imports of auth.py'.
    """
    repo_path = _session_path(repo_id)
    # Normalise file path
    if not file.startswith("./"):
        file = "./" + file.lstrip("/")

    all_symbols = _list_symbols(repo_path)
    file_symbols = [s for s in all_symbols if s.startswith(file + ":")]

    if not file_symbols:
        raise HTTPException(404, f"No symbols found in '{file}'. Check the file path.")

    affected_modules = set()
    affected_symbols = []

    for symbol in file_symbols:
        result = _run_diffcontext(repo_path, symbol)
        if "error" not in result:
            for f in result.get("blast_radius_by_file", {}).keys():
                if f != file:
                    affected_modules.add(f)
            affected_symbols.extend(result.get("all_affected", []))

    affected_symbols = list(set(affected_symbols))

    return {
        "repo_id": repo_id,
        "file": file,
        "public_symbols": [s.split(":")[-1] for s in file_symbols],
        "symbol_count": len(file_symbols),
        "affected_modules": sorted(affected_modules),
        "affected_module_count": len(affected_modules),
        "affected_symbol_count": len(affected_symbols),
        "all_affected": affected_symbols[:50],
    }


@app.post("/inline")
def analyze_inline(req: InlineRequest):
    """
    Paste code directly — no upload needed. Great for quick analysis of snippets.

    Example body:
    {
      "files": [
        {"filename": "service.py", "content": "def create_user(name): pass\\ndef onboard(name): create_user(name)"}
      ],
      "symbol": "./service.py:onboard"
    }
    """
    tmp_dir = tempfile.mkdtemp(prefix="diffctx_inline_")
    try:
        for f in req.files:
            safe_name = Path(f.filename).name
            with open(os.path.join(tmp_dir, safe_name), "w") as fp:
                fp.write(f.content)
        return _run_diffcontext(tmp_dir, req.symbol)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.get("/symbols")
def list_symbols(repo_id: str = Query(..., description="The repo_id from /upload or /clone")):
    """List all functions in your indexed repo."""
    repo_path = _session_path(repo_id)
    symbols = _list_symbols(repo_path)
    return {"repo_id": repo_id, "count": len(symbols), "symbols": symbols}


@app.post("/blast")
def blast_radius(req: BlastRequest):
    """
    Get the blast radius for a specific function symbol.
    Use /resolve first if you only know the function name (not the file path).
    Symbol format: './relative/path.py:function_name'
    """
    repo_path = _session_path(req.repo_id)
    return _run_diffcontext(repo_path, req.symbol)


@app.post("/compile")
def compile_context(req: CompileRequest):
    """Get an LLM-ready context string optimised for token budget."""
    repo_path = _session_path(req.repo_id)
    try:
        index_repository, analyze_impact, dc_compile = _try_import_dc()
        idx = index_repository(repo_path)
        impact = analyze_impact(idx, [req.symbol])
        ctx = dc_compile(idx, impact, max_tokens=req.max_tokens)
        return {
            "context_text": ctx.text,
            "symbol_count": ctx.symbol_count,
            "token_estimate": ctx.token_estimate,
            "reduction_pct": round(ctx.reduction_pct, 1),
            "usage_tip": "Paste context_text into Claude/ChatGPT with a specific question about your change.",
        }
    except ImportError:
        result = _run_diffcontext(repo_path, req.symbol)
        context_text = f"""=== DIFFCONTEXT ANALYSIS ===
Symbol: {req.symbol}
Direct callers: {', '.join(result.get('direct_callers', [])) or 'none'}
Direct callees: {', '.join(result.get('direct_callees', [])) or 'none'}
Blast radius ({result.get('blast_radius_count', 0)} functions):
{chr(10).join(f'  - {s}' for s in result.get('all_affected', [])[:20]) or 'none'}
"""
        return {
            "context_text": context_text,
            "symbol_count": result.get("blast_radius_count", 0),
            "token_estimate": len(context_text) // 4,
            "reduction_pct": 0,
        }


# ── Custom OpenAPI schema (ChatGPT Actions) ───────────────────────────────────
from fastapi.openapi.utils import get_openapi

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title="DiffContext API",
        version="2.0.0",
        description="Repository intelligence: blast radius, symbol search, call graphs — for AI coding agents.",
        routes=app.routes,
    )

    schema["servers"] = [{"url": "https://trakshan-diffcontext.hf.space"}]


    app.openapi_schema = schema
    return schema

app.openapi = custom_openapi
