#!/usr/bin/env python3
"""
embed_symbols.py — one-time bulk embedding of repo symbols -> cached vectors.

Run this ONCE per repo, ideally on a free Colab/Kaggle GPU: the SFR-Embedding-
Code-400M_R forward pass is ~1-2s/symbol on an i5 (tens of minutes/repo) but
seconds/repo on a GPU. It writes an L2-normalized vector cache keyed by symbol
content-hash; the ablation (ablation.py) is then CPU-only and just loads the
cache. Re-running re-embeds only symbols whose code changed (hash miss), so it
is incremental.

The encoder is the ONLY place that imports sentence-transformers, and only
inside encode_texts(), so ablation.py and the tests never need the heavy
dependency or the model download.

MODEL NOTE: confirm the model card's recommended prefix / pooling when you run
this. Code retrieval models sometimes want an instruction prefix; here query and
documents are both code and encoded identically (symmetric code->code retrieval,
matching how the pairs were mined). Override with --model / --prefix as the card
requires.

Usage (Colab/Kaggle GPU):
  pip install -q sentence-transformers
  python -m benchmarks.semantic.embed_symbols benchmark_repos/click \
      --model Salesforce/SFR-Embedding-Code-400M_R --device cuda
Usage (CPU, small repos only):
  python -m benchmarks.semantic.embed_symbols benchmark_repos/starlette --device cpu

Output:
  benchmarks/semantic/embeddings/<repo>.npz        ids, hashes, vectors (float32, L2-norm)
  benchmarks/semantic/embeddings/<repo>.meta.json  model, dim, prefix
"""

import argparse
import hashlib
import json
import os
import sys
import time
from typing import Dict, List, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diffcontext.pipeline import index_repository

EMB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "embeddings")
DEFAULT_MODEL = "Salesforce/SFR-Embedding-Code-400M_R"


def content_hash(code: str) -> str:
    return hashlib.sha1(code.encode("utf-8", "replace")).hexdigest()


def load_embeddings(path: str) -> Tuple[Dict[str, np.ndarray], dict]:
    """-> (id -> vector, meta). Missing cache -> ({}, {})."""
    if not os.path.exists(path):
        return {}, {}
    z = np.load(path, allow_pickle=True)
    ids = list(z["ids"])
    vecs = z["vectors"]
    id2vec = {sid: vecs[k] for k, sid in enumerate(ids)}
    meta_path = path[:-4] + ".meta.json"
    meta = json.load(open(meta_path, encoding="utf-8")) if os.path.exists(meta_path) else {}
    return id2vec, meta


def encode_texts(texts: List[str], model: str, device: str, batch_size: int,
                 prefix: str) -> np.ndarray:
    """Encode with sentence-transformers (imported lazily, here only)."""
    from sentence_transformers import SentenceTransformer
    st = SentenceTransformer(model, trust_remote_code=True, device=device)
    if prefix:
        texts = [prefix + t for t in texts]
    return st.encode(texts, batch_size=batch_size, normalize_embeddings=True,
                     show_progress_bar=True, convert_to_numpy=True).astype("float32")


def embed_repo(repo: str, out_dir: str, model: str, device: str,
               batch_size: int, prefix: str) -> None:
    repo = os.path.abspath(repo)
    name = os.path.basename(repo.rstrip("/"))
    idx = index_repository(repo)
    items = sorted(idx.symbols.items())
    if not items:
        print(f"{name}: no symbols, skipping")
        return
    ids = [i for i, _ in items]
    codes = [s.code for _, s in items]
    hashes = [content_hash(c) for c in codes]
    out = os.path.join(out_dir, name + ".npz")

    # reuse cached vectors for unchanged content (hash hit), only if same model
    prev_hash2vec: Dict[str, np.ndarray] = {}
    _, prev_meta = load_embeddings(out)
    if prev_meta.get("model") == model and os.path.exists(out):
        z = np.load(out, allow_pickle=True)
        for h, v in zip(list(z["hashes"]), z["vectors"]):
            prev_hash2vec.setdefault(h, v)

    todo_idx = [k for k, h in enumerate(hashes) if h not in prev_hash2vec]
    print(f"{name}: {len(ids)} symbols — {len(todo_idx)} to encode, "
          f"{len(ids) - len(todo_idx)} reused")
    new_hash2vec: Dict[str, np.ndarray] = {}
    if todo_idx:
        vecs = encode_texts([codes[k] for k in todo_idx], model, device, batch_size, prefix)
        for k, v in zip(todo_idx, vecs):
            new_hash2vec[hashes[k]] = v

    sample = next(iter(new_hash2vec.values()), None)
    if sample is None:
        sample = next(iter(prev_hash2vec.values()), None)
    if sample is None:
        print(f"{name}: nothing encoded and no reusable cache — skipping")
        return
    dim = int(sample.shape[0])
    vectors = np.zeros((len(ids), dim), dtype="float32")
    for k, h in enumerate(hashes):
        vec = new_hash2vec.get(h)
        if vec is None:
            vec = prev_hash2vec.get(h)
        if vec is None:
            # Only reachable from a truncated/corrupted .npz; re-embedding the
            # repo from scratch is the fix, and silently shipping a zero vector
            # would poison every cosine it takes part in.
            raise RuntimeError(
                f"{name}: no vector for symbol {ids[k]!r} (hash {h[:8]}) in either "
                f"the fresh or cached set — delete {os.path.relpath(out)} and re-run")
        vectors[k] = vec

    os.makedirs(out_dir, exist_ok=True)
    np.savez(out, ids=np.array(ids, dtype=object),
             hashes=np.array(hashes, dtype=object), vectors=vectors)
    json.dump({"model": model, "dim": dim, "n": len(ids), "normalized": True,
               "prefix": prefix, "ts": int(time.time())},
              open(out[:-4] + ".meta.json", "w", encoding="utf-8"), indent=2)
    print(f"  wrote {os.path.relpath(out)} ({len(ids)}x{dim})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repos", nargs="+", help="paths to git repo clones to embed")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--device", default="cpu", help="cpu | cuda (default cpu)")
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--prefix", default="", help="optional instruction prefix per the model card")
    ap.add_argument("--out-dir", default=EMB_DIR)
    args = ap.parse_args()
    for r in args.repos:
        embed_repo(r, args.out_dir, args.model, args.device, args.batch_size, args.prefix)


if __name__ == "__main__":
    main()
