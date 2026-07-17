# DiffContext Hardened Co-Change Benchmark (eval_v2)

**Date:** 2026-07-10 · **Harness:** `benchmarks/eval_v2_hardened.py` · **Raw data:** `benchmarks/results/eval_v2/*_cases.csv`, `*_summary.json`, `failure_buckets_django.json`

This benchmark supersedes the original Django-only report. It was built specifically to fix five known weaknesses: sample-size inflation, missing baselines, single retrieval budget, anecdotal failure modes, and the incomplete cross-repo table.

## 1. Methodology

**Ground truth.** For each repository we mine *distinct commits* (target 100, scanning up to 6,000 commits of history) where ≥2 functions were modified together in non-test Python files. Function attribution parses the file *as it was at the commit* (`git show <hash>:<path>`), not at HEAD. Each commit yields one query per changed symbol; the other co-changed symbols are that query's ground truth. Symbols that no longer exist at HEAD are dropped; commits with <2 surviving symbols are excluded.

**The unit of statistical analysis is the commit, not the symbol.** Every aggregate below is reported two ways:
- **per-commit** — metrics are averaged over a commit's queries first, then across commits, so a commit counts exactly once (fixes the original benchmark's inflation, where ~40 "cases" came from ~8 commits);
- **per-symbol** — the original format, kept for comparability.

Bootstrap 95% CIs (1,000 resamples) are computed over *commit-level* means.

**Noisy-commit flagging.** Commits changing ≥20 symbols or ≥10 files are flagged as likely mechanical refactors. They stay in the main tables (flagged in the CSVs, listed in each `*_summary.json`), and every per-commit aggregate is also reported with them excluded (`per_commit_excl_noisy`).

**Methods compared** (identical eval set, identical token budget):
- `diffcontext` — call-graph retrieval (bidirectional BFS decay scoring, dynamic score cutoff), the product's core signal
- `hybrid` — graph+BM25+same-file score blend (0.5/0.35/0.15), the configuration eval_v1 identified as best
- `bm25` — BM25Okapi over full function source
- `embedding` — dense retrieval over full function source (added 2026-07-17,
  after an external audit correctly flagged its absence; results in §8):
  sentence-transformers/all-MiniLM-L6-v2 when installed, else a TF-IDF-cosine
  fallback explicitly labeled `tfidf-cosine-approx`. Every summary records
  which encoder ran (`config.embedding_encoder`) — fallback numbers are a
  second lexical-vector baseline, NOT a dense comparison, and are labeled so
- `samefile` — every other symbol in the query's file
- `random_k` — deterministic random sample, **k matched per-query to DiffContext's retrieval count**

**Metrics** reuse the original benchmark's definitions: *hit* = ≥1 ground-truth symbol anywhere in the budgeted retrieval; precision/recall/F1 over the full budgeted list; P@k/R@k over top-k prefixes for k ∈ {10, 20, 30, 50, 70}.

**Explicit deviations from the spec** (per requirements):
1. Token budget is 10,000 estimated tokens — the frozen eval_v1 value — not the 8,000 product default, so results stay comparable with the frozen baseline. Both budgets bind rarely at these candidate counts.
2. The BM25 baseline indexes full function source rather than name+docstring. This is a *stronger* baseline than specified; losses against it are reported as losses.
3. Random-k is seeded per-query for reproducibility.

**Data volumes.** Benchmark clones were originally shallow (100–875 commits), which was itself a root cause of the original benchmark's tiny sample; they were deepened to 1,523–6,000 commits before this run.

| Repo | Commits mined | Valid commits (≥2 symbols alive at HEAD) | Flagged noisy | Symbol queries | Symbols | Graph edges |
|---|---|---|---|---|---|---|
| django | 100 | 86 | 3 | 375 | 9,161 | 46,873 |
| click | 100 | 95 | 7 | 712 | 508 | 2,374 |
| flask | 100 | 74 | 9 | 554 | 354 | 2,379 |
| httpx | 100 | 83 | 11 | 853 | 434 | 1,982 |
| pydantic | 100 | 85 | 4 | 479 | 1,827 | 8,447 |

## 2. Aggregate results (per-commit; a commit counts once)

Hit / precision / recall / F1 are over the full budget-truncated retrieval; R@20 is the top-20 prefix. Bracketed values are bootstrap 95% CIs over commit-level means.

### django (n=86 commits)

| Method | Hit | Prec | Recall | F1 | R@20 |
|---|---|---|---|---|---|
| diffcontext | 0.783 [.70,.86] | 0.045 | 0.660 [.57,.74] | 0.070 | 0.622 [.53,.70] |
| **hybrid** | **0.887** [.83,.93] | 0.050 | **0.782** [.71,.84] | 0.082 | **0.727** [.65,.80] |
| bm25 | 0.787 [.71,.86] | 0.051 | 0.613 [.54,.68] | 0.082 | 0.536 [.46,.61] |
| samefile | 0.743 [.66,.82] | **0.109** | 0.615 [.52,.70] | **0.148** | 0.512 [.41,.61] |
| random_k | 0.012 | 0.000 | 0.001 | 0.000 | 0.001 |

### click (n=95)

| Method | Hit | Prec | Recall | F1 | R@20 |
|---|---|---|---|---|---|
| diffcontext | 0.719 [.64,.80] | 0.074 | 0.572 [.48,.66] | 0.091 | 0.507 [.42,.60] |
| **hybrid** | **0.877** [.82,.93] | 0.074 | **0.727** [.65,.79] | 0.113 | **0.653** [.57,.73] |
| bm25 | 0.855 [.80,.91] | **0.083** | 0.650 [.58,.72] | **0.122** | 0.622 [.55,.70] |
| samefile | 0.638 [.55,.72] | 0.062 | 0.489 [.41,.57] | 0.093 | 0.273 [.20,.35] |
| random_k | 0.178 | 0.013 | 0.059 | 0.010 | 0.032 |

### flask (n=74)

| Method | Hit | Prec | Recall | F1 | R@20 |
|---|---|---|---|---|---|
| diffcontext | 0.763 [.68,.85] | 0.056 | 0.572 [.48,.66] | 0.080 | 0.491 [.40,.59] |
| **hybrid** | **0.831** [.76,.90] | 0.071 | **0.667** [.59,.75] | 0.101 | **0.598** [.51,.69] |
| bm25 | 0.803 [.73,.88] | 0.078 | 0.612 [.53,.70] | 0.109 | 0.583 [.50,.67] |
| samefile | 0.669 [.57,.76] | **0.091** | 0.508 [.41,.61] | **0.113** | 0.419 [.33,.52] |
| random_k | 0.299 | 0.018 | 0.110 | 0.021 | 0.059 |

### httpx (n=83)

| Method | Hit | Prec | Recall | F1 | R@20 |
|---|---|---|---|---|---|
| diffcontext | 0.823 [.75,.89] | 0.060 | 0.576 [.49,.65] | 0.079 | 0.461 [.38,.55] |
| **hybrid** | **0.934** [.89,.97] | 0.094 | **0.756** [.69,.82] | 0.126 | **0.645** [.57,.72] |
| bm25 | 0.902 [.85,.95] | 0.113 | 0.709 [.65,.78] | 0.142 | 0.608 [.54,.69] |
| samefile | 0.860 [.80,.92] | **0.140** | 0.599 [.52,.68] | **0.165** | 0.414 [.32,.51] |
| random_k | 0.470 | 0.022 | 0.165 | 0.028 | 0.054 |

### pydantic (n=85)

| Method | Hit | Prec | Recall | F1 | R@20 |
|---|---|---|---|---|---|
| diffcontext | 0.650 [.56,.73] | 0.065 | 0.411 [.33,.49] | 0.100 | 0.391 [.31,.47] |
| hybrid | 0.753 [.68,.82] | 0.085 | 0.517 [.44,.60] | 0.130 | 0.496 [.42,.57] |
| **bm25** | **0.761** [.69,.83] | **0.107** | 0.513 [.44,.59] | **0.155** | 0.494 [.42,.57] |
| samefile | 0.554 [.47,.63] | 0.103 | 0.320 [.24,.40] | 0.125 | 0.266 [.20,.34] |
| random_k | 0.070 | 0.003 | 0.025 | 0.004 | 0.023 |

### Cross-repo mean (unweighted over 5 repos)

| Method | Hit | Prec | Recall | F1 | R@20 |
|---|---|---|---|---|---|
| diffcontext | 0.748 | 0.060 | 0.558 | 0.084 | 0.494 |
| **hybrid** | **0.856** | 0.075 | **0.690** | 0.110 | **0.624** |
| bm25 | 0.822 | 0.086 | 0.619 | **0.122** | 0.569 |
| samefile | 0.693 | **0.101** | 0.506 | 0.129 | 0.377 |
| random_k | 0.206 | 0.011 | 0.072 | 0.013 | 0.034 |

**Reading the random_k row honestly:** with k matched to DiffContext's retrieval count (28–60 symbols), random draws achieve 30–47% *hit rate* in the small repos (flask, httpx) simply because k is a large fraction of the codebase. Hit rate alone is therefore a weak headline metric on small repos; recall and R@20 gaps versus random remain 4–10×.

**Per-symbol vs per-commit matters.** The unit of analysis changes results materially — e.g., click's diffcontext recall is 0.572 per-commit but 0.300 per-symbol, because many-symbol commits contribute dozens of hard queries that dominate a per-symbol average. All per-symbol aggregates are in `*_summary.json` (`per_symbol`) and every underlying case is a CSV row. The original benchmark's "40 cases from ~8 commits" would have looked like an n=40 sample when it was effectively n≈8; per-commit CIs here are honest about that.

## 3. Precision–recall sweep (diffcontext, per-commit, mean over 5 repos)

| Budget | Precision | Recall |
|---|---|---|
| top-10 | 0.125 | 0.427 |
| top-20 | 0.079 | 0.494 |
| top-30 | 0.059 | 0.530 |
| top-50 | 0.038 | 0.552 |
| top-70 | 0.028 | 0.557 |

**Is there a knee?** Not a sharp one — the tradeoff is a smooth concave curve — but recall visibly plateaus from top-30 onward: going 30→70 buys +2.7 recall points while cutting precision in half. **Top-20 retains 89% of the top-70 recall at 2.8× its precision** and is the sensible operating point; the original benchmark's 30–70-symbol budget spends most of its extra tokens on noise. Per-repo sweeps (same shape everywhere) are in section 2 tables' R@20 and in each `*_summary.json` (`p@k`/`r@k`, k ∈ {10,20,30,50,70}).

## 4. Stratification by ground-truth set size (recall, per-symbol)

| Repo | GT 1–2 (dc / hyb / bm25) | GT 3–5 | GT 6+ |
|---|---|---|---|
| django | 0.77 / 0.87 / 0.67 | 0.68 / 0.76 / 0.60 | 0.31 / 0.51 / 0.48 |
| click | 0.65 / 0.79 / 0.77 | 0.54 / 0.65 / 0.49 | 0.17 / 0.35 / 0.25 |
| flask | 0.64 / 0.75 / 0.70 | 0.54 / 0.64 / 0.53 | 0.28 / 0.30 / 0.27 |
| httpx | 0.75 / 0.87 / 0.80 | 0.58 / 0.80 / 0.69 | 0.21 / 0.39 / 0.44 |
| pydantic | 0.43 / 0.56 / 0.58 | 0.51 / 0.59 / 0.55 | 0.24 / 0.30 / 0.30 |

Recall degrades steeply as the co-change set grows: on 6+ commits every method recovers under half the set, and graph-only drops to 0.17–0.31. Large co-change sets are dominated by API-wide changes with many lexically-similar-but-graph-distant members — the regime where BM25 overtakes the graph (django 6+: bm25 0.48 vs dc 0.31; httpx 6+: 0.44 vs 0.21).

## 5. Targeted failure-mode buckets (Django, 20 pairs each)

Pairs were criteria-mined from ~250 distinct Django commits and manually audited (full pair lists with commit messages: `failure_buckets_django.json`). Each pair (query → target) was co-changed in one commit; trivial pairs (same file, or direct call edge) are excluded by construction, so DiffContext's score measures whether *anything else* in its retrieval recovers the pair. Selection criteria per bucket:

- **thematic_no_edge** — same subsystem, different files, no edge, no path within 3 undirected hops (e.g., `admin/checks.py:BaseModelAdminChecks.check` ↔ `admin/options.py:InlineModelAdmin.get_formset`)
- **backend_dispatch** — same method name overridden in different files, or pair touching `db/backends/` vendor dirs (e.g., `Field.get_choices` ↔ `ForeignObjectRel.get_choices`; `base/schema.py` ↔ `sqlite3/schema.py:_alter_field`)
- **cross_subsystem** — different Django subsystems, no edge, no 3-hop path (e.g., `conf/__init__.py:Settings.__init__` ↔ `core/signing.py:_unsign_cookie` from a CVE fix)

| Bucket | n | diffcontext | bm25 | hybrid |
|---|---|---|---|---|
| thematic_no_edge | 20 | **0%** | 50% | 15% |
| backend_dispatch | 20 | **0%** | 30% | 30% |
| cross_subsystem | 20 | **0%** | 0% | 0% |

**Caveats:** (1) DiffContext's 0% is partly by construction — the buckets are *defined* by graph-unreachability, which is precisely the anecdotal failure claim being tested; the informative comparison is what other signals recover. (2) Pairs cluster within a few commits (12 of 20 backend pairs come from one BitAnd/BitOr commit; 14 of 20 cross-subsystem pairs from one admindocs refactor), so per-bucket rates are coarse.

**Fixable vs structural:**
- *Thematic* — **fixable, but only by lexical signal**: BM25 already recovers half of these; the hybrid's current 0.35 BM25 weight recovers only 15%, so the blend under-weights BM25 exactly where the graph is blind. A rank-fusion or fallback rule ("when graph confidence is low, trust BM25 more") is a concrete, testable fix.
- *Backend/dispatch* — **partially fixable in the graph itself**: these are same-name overrides, which a static analyzer can link explicitly (synthetic "override" edges between same-named methods in related class hierarchies). BM25 recovers 30% incidentally via shared names; a targeted graph edge should do better.
- *Cross-subsystem* — **structural ceiling for all content-based signals**: 0% for graph, BM25, *and* hybrid. Nothing in the code's text or structure connects `Settings.__init__` to `_unsign_cookie`; only historical co-change mining (or issue/PR metadata) could surface these. This bounds what any single-snapshot retrieval can do.

## 6. Noisy ground-truth commits

29 of 423 valid commits (6.9%) were flagged as likely mechanical refactors (≥20 symbols or ≥10 files) — e.g., click's "use modern typing features" (232 symbols), flask's "add `__future__` annotations" (124), httpx's "Use `__future__.annotations`" (195). They are *included* in all tables above and flagged per-row in the CSVs; excluding them (see `per_commit_excl_noisy` in the summaries) shifts recall by +2 to +6 points for every method and changes no ranking or conclusion.

## 7. Honest conclusion

**Where DiffContext's graph signal wins, and by how much:**
- vs **random-k** (matched retrieval size): everywhere, by 4–10× on recall. The graph is far better than chance.
- vs **same-file**: on recall and R@20 in 5/5 repos (mean R@20 0.494 vs 0.377), though same-file has ~1.7× its precision and higher F1 on 3/5 repos.
- vs **BM25**: **nowhere decisively.** Graph-only recall beats BM25 only on Django (0.660 vs 0.613) and CIs overlap; BM25 has the higher hit rate in 5/5 repos, higher F1 in 5/5, and higher recall in 3/5 (the click hit-rate gap, 0.855 vs 0.719, is significant at 95%).

**The defensible claim is the hybrid, not the graph.** Graph+BM25+file leads recall in 4/5 repos and ties pydantic; on Django the R@20 gap over BM25 (0.727 vs 0.536) is significant at 95%. The graph's marginal value grows with codebase size and graph density (Django: hybrid > bm25 by +17 recall points; pydantic, where metaclass-driven code blinds static analysis: +0.4 points ≈ nothing).

**Precision is the product's real problem.** At the recall-optimal budgets, 88–94% of retrieved symbols are not in the ground truth, on every method. If the 8K-token context this feeds is judged on signal-to-noise, budget should drop to ~top-20 (89% of achievable recall at 2.8× the precision of top-70).

**Ceilings.** Cross-subsystem conceptual co-changes are unreachable by all tested signals (0/20). Recall on large co-change sets (6+ symbols) is under 0.51 for every method. These bound this entire family of approaches, not just DiffContext.

**Bottom line:** the call graph is not a competitive standalone retriever — full-code BM25 matches or beats it on most measures across five repos. It *is* a complementary signal that makes a hybrid the best available method, decisively so on the largest, most call-dense codebase tested. Claims should be phrased accordingly.

**2026-07-17 update:** this conclusion was written against five methods with
no dense-retrieval baseline. §8 adds one (with an honestly-labeled lexical
fallback in the environment it ran in): the new baseline beats BM25 in 5/5
repos, the hybrid still leads in 4/5, and **the hybrid loses to it on
pydantic** — the graph-blind regime. "Best available method" now carries
that qualification, and no claim is made against true dense embeddings
until someone runs them (§8 explains exactly how).

## 8. Embedding baseline re-run (2026-07-17)

An external audit correctly flagged that none of the five methods above is a
dense/embedding retriever — the comparison a developer evaluating this tool
against modern RAG-for-code tooling actually wants. `EmbeddingBaseline` was
added as a sixth method, run through the identical per-commit pipeline
(same mining, budgets, CIs, strata, buckets).

**What this run is, and is not.** The baseline prefers
sentence-transformers/all-MiniLM-L6-v2 (local, no paid API). The environment
this re-run executed in blocks huggingface.co downloads, so it fell back to
the explicitly-labeled **`tfidf-cosine-approx`** encoder: pure-Python TF-IDF
cosine over the same identifier tokenization BM25 uses. **These numbers are
therefore a second lexical-vector baseline, NOT a dense-embedding
comparison.** They still answer a real question — does a differently-weighted
lexical vector space beat BM25 and the hybrid? — but the true dense run
remains open, and every summary records `config.embedding_encoder` so the two
can never be conflated. Anyone with unrestricted network can produce the
dense numbers by installing sentence-transformers and re-running.

**Snapshot pinning.** The §2 tables above are the frozen 2026-07-10 run;
repo HEADs have moved since, and a different HEAD mines different commits.
Mixing one new column into those tables would corrupt them, so this section
reports a **complete six-method re-run on one pinned snapshot** (now recorded
in every summary): click `7df2f823`, django `274df4df`, flask `36e4a824`,
httpx `b5addb64`, pydantic `859945e4`. Existing methods' numbers shift a few
points vs §2 for exactly this reason; rankings are consistent.

### Per-commit aggregates, all six methods (hit / prec / recall / F1 / R@20)

| Repo (n) | diffcontext | hybrid | bm25 | embedding | samefile | random_k |
|---|---|---|---|---|---|---|
| click (95) | .723/.064/.574/.084/.509 | **.881**/.069/**.734**/.112/**.647** | .856/.075/.653/.120/.625 | .868/.069/.683/.113/.625 | .637/.056/.488/.091/.270 | .203/.008/.075/.010/.053 |
| django (87) | .795/.040/.664/.064/.629 | **.899**/.052/**.781**/.086/**.737** | .791/.051/.613/.082/.544 | .861/.045/.723/.071/.580 | .762/.120/.631/.159/.530 | .019/.000/.004/.001/.001 |
| flask (74) | .773/.057/.579/.081/.493 | **.834**/.071/**.670**/.101/**.614** | .803/.078/.612/.109/.583 | .823/.068/.634/.098/.587 | .669/.091/.508/.113/.419 | .300/.018/.115/.021/.059 |
| httpx (83) | .823/.060/.579/.079/.462 | **.934**/.092/**.755**/.124/**.646** | .902/.113/.709/.142/.608 | .911/.105/.719/.134/.602 | .860/.140/.599/.165/.414 | .470/.022/.165/.028/.054 |
| pydantic (85) | .621/.058/.380/.090/.353 | .752/.078/.524/.123/.502 | .763/.104/.531/.154/.512 | **.781**/.094/**.561**/.141/**.532** | .547/.093/.312/.112/.256 | .067/.003/.025/.004/.023 |

### Cross-repo mean (unweighted over 5 repos, this snapshot)

| Method | Hit | Prec | Recall | F1 | R@20 |
|---|---|---|---|---|---|
| diffcontext | 0.747 | 0.056 | 0.555 | 0.080 | 0.489 |
| **hybrid** | **0.860** | 0.072 | **0.693** | 0.109 | **0.629** |
| embedding | 0.849 | 0.076 | 0.664 | 0.111 | 0.585 |
| bm25 | 0.823 | 0.084 | 0.624 | 0.121 | 0.574 |
| samefile | 0.695 | 0.100 | 0.508 | 0.128 | 0.378 |
| random_k | 0.212 | 0.010 | 0.077 | 0.013 | 0.038 |

### Failure-mode buckets (Django, same criteria as §5)

| Bucket | n | diffcontext | bm25 | embedding | hybrid |
|---|---|---|---|---|---|
| thematic_no_edge | 20 | 0% | 50% | 50% | 15% |
| backend_dispatch | 20 | 0% | 30% | 30% | 25% |
| cross_subsystem | 20 | 0% | 0% | **15%** | 0% |

### Honest reading

- **The embedding baseline is the strongest single baseline tested.** It
  beats BM25 on recall in **5/5 repos** (mean 0.664 vs 0.624) and on hit
  rate in 5/5. The "beats BM25" bar the earlier sections cleared is
  therefore no longer the relevant bar.
- **The hybrid still leads — in 4 of 5 repos, not 5.** Mean recall 0.693 vs
  0.664 and R@20 0.629 vs 0.585 favor the hybrid, with the largest margins
  where the call graph is densest (django +5.8 recall points, click +5.1).
  On **pydantic the hybrid loses to the embedding baseline outright**
  (recall 0.524 vs 0.561, hit 0.752 vs 0.781): where metaclass-generated
  code blinds the graph, the graph's 0.5 blend weight actively costs recall
  against a good lexical-vector retriever. That is the adaptive-blend
  roadmap item stated as a measured loss, not a hypothesis.
- **Cross-subsystem: 3/20 vs everyone else's 0/20.** The TF-IDF vector's
  soft term weighting recovered 3 pairs that BM25, graph, and hybrid all
  miss. With n=20 and pairs clustered in few commits this is weak evidence,
  but it suggests the "structural ceiling" of §5 is a ceiling for the
  *shipped* signals, and a semantic leg may lower it — one more reason the
  missing true-dense run matters.
- **What a true dense model would change is unknown.** all-MiniLM-L6-v2
  typically beats TF-IDF on natural-language similarity; on code the gap is
  less predictable. Until someone runs it, the defensible statement is:
  the hybrid beats every lexical method tested, including a TF-IDF-cosine
  stand-in for dense retrieval, in 4/5 repos — and loses where its graph
  leg is blind. No claim is made against actual embedding models yet.

---
*Reproduce: `python benchmarks/eval_v2_hardened.py` (all repos + Django buckets). Raw per-case data: `benchmarks/results/eval_v2/<repo>_cases.csv` (one row per commit × query × method). Summaries with CIs and strata: `<repo>_summary.json`, `all_summaries.json`.*

## Postscript (2026-07-10): hybrid shipped in the product

Following these results, the winning configuration was wired into the product itself:
- `diffcontext/lexical.py` — dependency-free BM25 (inverted index, rank_bm25-compatible math with a positive-idf-floor fix for tiny corpora)
- `analyze_impact(hybrid=True)` is now the default: graph 0.5 / BM25 0.35 / same-file 0.15 (`hybrid=False` restores graph-only)
- `select_context(top_k=...)` + CLI `--top-k` (default 20/changed symbol) implement the benchmarked precision operating point
- `benchmarks/check_regression.py` + a CI `retrieval-quality-gate` job freeze these numbers as floors

Validation: running the **actual product path** (`index_repository` + `analyze_impact`) over the same ground truth reproduces the benchmark hybrid numbers exactly — flask hit 0.831 / recall 0.667 / R@20 0.598; django hit 0.887 / recall 0.782 / R@20 0.727 (vs 0.783/0.660/0.622 graph-only). The product's default retrieval improved by +12 recall points on Django.
