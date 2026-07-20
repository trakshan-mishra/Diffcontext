# Research positioning — what it takes to make DiffContext a publishable SE research artifact

Written 2026-07. Audience: anyone preparing an ICSE / FSE / ASE (or
workshop-track) submission out of this codebase. This is the honest gap
analysis: where DiffContext sits in the current literature, what is
genuinely novel, and exactly what a reviewer will reject it for today.

## 1. Where the field is (2024–2026)

Repository-level context retrieval for LLM coding has become a crowded,
fast-moving area. The systems closest to DiffContext:

| System | Venue | Graph | Retrieval trigger | Signals |
|---|---|---|---|---|
| [GraphCoder](https://arxiv.org/abs/2406.07003) | ASE 2024 | statement-level code context graph (control/data dependence) | completion at cursor | graph similarity, coarse-to-fine |
| [RepoGraph](https://github.com/YerbaPage/Awesome-Repo-Level-Code-Generation) | ICLR 2025 | line-level reference graph | issue / agent plugin | graph traversal |
| [LocAgent](https://arxiv.org/pdf/2503.09089) | ACL 2025 | heterogeneous graph (contain/import/invoke/inherit) | issue text | LLM agent multi-hop traversal |
| [CodexGraph](https://arxiv.org/pdf/2408.03910) | 2024 | code graph database (schema + Cypher) | agent queries | LLM-issued graph queries |
| [RepoHyper](https://arxiv.org/abs/2403.06095) | 2024 | repo-level semantic graph | completion | expand-and-refine + link prediction |
| [CoCoMIC](https://arxiv.org/abs/2212.10007) / CCFinder | LREC-COLING 2024 | project dependency context | completion | static analysis retrieval |
| [CoSIL / ARISE](https://arxiv.org/html/2605.03117) | 2025–26 | module + function call graphs | fault localization | iterative call-graph search |
| Aider repo-map | practitioner | ctags/tree-sitter symbol graph | chat | PageRank-style ranking |
| **DiffContext** | — | function-level dependency graph (calls, inheritance, overrides, decorators, fn-refs) | **a code change (diff)** | **graph + BM25 + co-location + git co-change, token-budgeted, honesty-audited** |

Two surveys frame the space:
[Retrieval-Augmented Code Generation: A Survey with Focus on Repository-Level Approaches](https://arxiv.org/html/2510.04905v1)
and the repo-level generation list at
[Awesome-Repo-Level-Code-Generation](https://github.com/YerbaPage/Awesome-Repo-Level-Code-Generation).
Context-window management for agents is now its own subfield (e.g.
[SWE-Pruner](https://arxiv.org/pdf/2601.16746), self-adaptive context
pruning, 2026).

## 2. What is actually novel here (claim candidates)

Ordered by defensibility — each claim names the evidence that backs it
in this repo today.

1. **Change-impact framing.** Nearly all of the table above retrieves
   context for *completion at a cursor* or *issue-text localization*.
   DiffContext retrieves for a *change*: given symbols a developer (or
   agent) just edited, find what else must be seen or touched. The
   ground truth follows the framing: real co-change sets mined from
   commit history, per-commit aggregated, noise-flagged
   (`benchmarks/eval_v2_hardened.py`). This is the paper's identity;
   guard it.
2. **Leakage-controlled historical signal.** Git co-change
   (Zimmermann et al.'s classic result) as a *fourth retrieval signal*
   (`diffcontext/history.py`), evaluated with every test commit
   excluded from the mined history — the co-change index can never
   contain the commit it is scored on. Few current LLM-context papers
   use evolutionary coupling at all; none we found do the exclusion
   properly when their ground truth is *also* co-change.
3. **Honesty by construction.** The compiled context leads with a
   disclosure of everything dropped, and the benchmark audits it: at a
   2k-token budget, 0% of missed ground-truth symbols were silently
   invisible. "Auditable non-omniscience" is a fresh angle the
   context-pruning literature does not measure.
4. **Self-grading on the user's repo.** `diffcontext verify
   --from-history --calibrate` mines the *user's* history into test
   cases and will print NULL RESULT when the tool does not fit the
   repo. As a methodological artifact ("benchmarks that ship with the
   tool and admit failure"), this is unusual and reviewable.
5. **Measured failure taxonomy.** Criteria-mined buckets (thematic
   no-edge, backend/dispatch, cross-subsystem) with per-bucket hit
   rates — a ready-made error-analysis section, and the roadmap is
   literally ordered by it.

## 3. What shipped in the bottleneck pass (2026-07)

Mapped to the failure taxonomy that motivated each:

| Bottleneck (measured) | Fix | Where |
|---|---|---|
| Dispatch/override pairs invisible when the base class doesn't define the method, or hierarchy is large | Dispatch-sibling override edges (phase 1G): same method name across subclasses of one resolved base, pairwise, family-capped at 6 | `graph_builder.py` |
| Graph weight wasted when blast radius is sparse (thematic siblings bucket) | Adaptive blend: graph weight scaled by candidate count, freed weight moves to BM25; exactly equal to the frozen blend when the graph is confident | `pipeline._adaptive_weights` |
| Cross-subsystem links: every static signal scored 0/20 | Git co-change association as an optional fourth signal (`CoChangeIndex`), CLI `--with-history`, benchmark methods `hybrid_cochange`/`hybrid_full` with test-commit exclusion | `history.py`, `pipeline.py`, `eval_v2_hardened.py` |
| "A beats B" claims rested on eyeballed CI overlap | Paired two-sided Wilcoxon signed-rank over per-commit metrics, Holm-Bonferroni adjusted, pure stdlib | `benchmarks/significance.py` |
| Benchmark clones were silently `--depth=100` while claiming full history — starving both ground-truth mining (24 vs 74 commits on flask) and the co-change signal | Full-history clones; unshallowed existing checkouts | `benchmark_runner.py` |

**Measured outcome (2026-07 run, honest version):** the additions gain
~1–1.4pt hit/recall on the weakest repo (pydantic, dynamic dispatch) and
are within noise elsewhere; `hybrid_full` vs `hybrid` is not
significantly different in aggregate on any repo. The Django
cross-subsystem bucket — the motivating ceiling — **stays 0/20** with
file-level co-change at a 3,000-commit window: a real negative result,
written up in [BENCHMARKS.md](BENCHMARKS.md) with the follow-up
hypotheses (symbol-level coupling, deeper windows, recency weighting).
For a paper, this is Section 5 material, not a defeat: the taxonomy
predicted where each mechanism would and would not help, and the
measurements agree.

## 4. What a reviewer rejects this for today (the remaining gap)

In priority order. Items 1–3 are necessary for any main-track
submission; 4–6 decide between main track and workshop.

1. **No extrinsic (downstream) evaluation.** The entire evaluation is
   intrinsic (retrieval vs. co-change proxy). The field's bar in 2026
   is: *does better context make an LLM fix/edit code better?* A
   SWE-bench-style experiment — same model, same harness, context
   from DiffContext vs. BM25 vs. embedding vs. Aider repo-map,
   measured on patch success — is the single highest-value addition.
   Without it, the paper is "we predict co-change well," which is a
   proxy claim.
2. **Train/test repo leakage** (docs/PLAN.md §3.1, still open): the
   scoring constants were tuned on the same repos the headline table
   reports. black + requests exist as held-out validation, which is
   the right instinct — but the split must be formalized: freeze the
   dev set, expand the held-out set, and make held-out numbers the
   headline.
3. **Scale and diversity of subjects:** 5 tuning + 2 validation repos,
   all Python libraries of similar character. Needs a documented,
   non-cherry-picked sample of ~20+ repos across categories, pinned to
   SHAs.
4. **The dense baseline must be a real encoder.** eval_v2's embedding
   baseline honestly falls back to TF-IDF when sentence-transformers
   is absent — fine for CI, but the paper needs the real thing (plus,
   ideally, one commercial embedding API) at benchmark time.
5. **Single-language evidence.** The TS adapter exists with honest
   per-style numbers (including a 0% failure mode). Either scope the
   paper to Python explicitly or bring TS up to benchmark grade.
6. **Precision remains the weak axis** (~0.07): most retrieved
   symbols are supporting context, not co-change partners. Either (a)
   improve ranking so precision@budget rises, or (b) *reframe*: for
   LLM consumption, "high-recall within a budget with disclosed
   drops" may be the right objective — but then the extrinsic eval
   (item 1) has to prove that framing.

## 5. Realistic venue path

- **Now → +1 month:** arXiv technical report from the current
  intrinsic results (all tables regenerate from
  `benchmarks/eval_v2_hardened.py` + `significance.py`).
- **+1–3 months:** workshop paper (ICSE/FSE co-located, LLM4Code
  etc.): needs items 2–4 above. The honesty-audit and
  verify-with-NULL-RESULT angles are strong workshop material.
- **Main track (ICSE/FSE/ASE):** requires the extrinsic evaluation
  (item 1) and repo-scale (item 3). Budget a real GPU/API spend for
  the SWE-bench-style runs and the dense baselines.

## 6. Reproduction

```bash
python benchmarks/benchmark_runner.py --clone   # full-history clones
python benchmarks/eval_v2_hardened.py           # all repos + Django failure buckets
python benchmarks/significance.py               # paired Wilcoxon vs. every baseline
python benchmarks/check_regression.py           # frozen quality floors (CI gate)
```

Sources for the survey table are linked inline above.
