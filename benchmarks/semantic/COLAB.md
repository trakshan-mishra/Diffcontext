# Embedding the corpus on a free GPU (Colab / Kaggle)

`ablation.py` is CPU-only and fast, but it needs a vector cache first. Building
that cache is the one GPU step in the semantic pipeline: ~14.5k symbols across
9 repos, a few minutes on a free T4.

**Why a pinned clone matters.** Vectors are keyed by symbol id (`./path.py:name`)
at the repo's HEAD. A plain `git clone` fetches today's HEAD, whose symbols no
longer match the ones the pairs were mined from — the vectors then fail to join
and the ablation silently scores a shrunken corpus. `pin_repos.py` checks out
the exact commits in `repo_pins.json`, which is what makes this reproducible.

| repo | symbols | | repo | symbols |
|---|---:|---|---|---:|
| django | 9,164 | | rich | 888 |
| pydantic | 1,827 | | black | 648 |
| click | 517 | | starlette | 484 |
| httpx | 434 | | flask | 354 |
| requests | 248 | | **total** | **14,564** |

---

## Colab

Runtime → Change runtime type → **T4 GPU**. Then four cells:

**1 — code**

```python
!git clone -b eval/rigor-pass https://github.com/trakshan-mishra/Diffcontext.git
%cd Diffcontext
!pip install -q sentence-transformers
```

DiffContext core has **zero runtime dependencies**, so nothing else is needed to
index Python repos.

**2 — corpus at the pinned commits**

```python
!python -m benchmarks.semantic.pin_repos --depth 1
```

Blobless clones (`--depth 1` here selects `--filter=blob:none`) — enough to
embed HEAD, and much faster than full history. Every line must read `OK`.

**3 — embed**

```python
!python -m benchmarks.semantic.embed_symbols \
    benchmark_repos/requests benchmark_repos/flask benchmark_repos/starlette \
    benchmark_repos/click benchmark_repos/httpx benchmark_repos/black \
    benchmark_repos/rich benchmark_repos/pydantic benchmark_repos/django \
    --device cuda --batch-size 32
```

Small repos first so a failure surfaces in seconds rather than after django.
On CUDA OOM drop to `--batch-size 8`. The step is incremental and content-hash
keyed: re-running re-encodes only symbols whose code changed, so an interrupted
session resumes cheaply.

Check the model card before a long run — if `SFR-Embedding-Code-400M_R` wants an
instruction prefix, pass `--prefix "..."`. Query and document are both code here
(symmetric code→code retrieval, matching how the pairs were mined), so the same
prefix applies to both.

**4 — retrieve**

```python
!cd benchmarks/semantic && zip -qr /content/embeddings.zip embeddings
from google.colab import files; files.download('/content/embeddings.zip')
```

## Kaggle

Same four cells; swap the last one for writing to `/kaggle/working/` and
download from the Output tab. Settings → Accelerator → GPU T4 ×2 (one is used).

---

## Back on your machine

```bash
unzip -o ~/Downloads/embeddings.zip -d benchmarks/semantic/
python -m benchmarks.semantic.pin_repos --check     # local clones still at the pins?
python -m benchmarks.semantic.ablation benchmark_repos/*/ --k 10
```

The ablation prints NDCG@10 / MRR / Recall@10 for semantic, structural and
hybrid on both query sets, plus a paired bootstrap of hybrid − semantic, and
writes `results/ablation.json`.

**Read the coverage line first.** If it reports scoring far fewer queries than
the ~1,552 mined pairs, the join is partial — check `pin_repos.py --check`
before reading anything into the metrics. If no repo has vectors at all the run
now fails loudly rather than writing an empty summary.

On the **gap** set, keep the `adversarial_gap.py` framing: structural recovers
real dependency edges roughly by construction, so the honest read is *how blind
semantic is there, and whether the hybrid closes it* — not "structural won".
