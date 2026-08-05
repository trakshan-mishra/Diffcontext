# I Benchmarked My Own Tool, Then Audited the Benchmark. Three of My Published Numbers Were Wrong.

I build a tool called DiffContext. It answers one question: when you change a
function, which other code does an LLM need to see to change it correctly? It
parses a repo into a dependency graph, blends that with BM25 keyword
similarity and same-file co-location, and packs the top candidates into a
token budget.

I benchmarked it against real commit history — a developer changed these
functions together in one commit; shown one, can the tool find the others? —
and the numbers were good, so I published them.

Then I spent a pass attacking the benchmark instead of the tool. Three
published claims did not survive. This is what broke.

It is worth writing up because the failure mode is close to universal in side
projects: you benchmark your own work, the number looks good, and you stop.
Nobody else is going to check. The only person who can catch you is you, and
you are not motivated to.

## The number I was proudest of measured nothing

DiffContext ships a "sufficiency score" from 0 to 100: did the compiled
context actually contain what the change needed? The only calibration on
record was a Pearson correlation of r=0.274 between that score and measured
recall, on about 25 cases. Small sample, but a real positive signal, and the
only citable number I had.

It was measured on a polluted index. Re-measured on clean indexes at scale —
1,080 mined cases across nine Python repos, all pushed through the real
product path — the same legacy formula gives **r=0.016, p=0.60**. No
relationship with measured recall at all.

The root cause was boring, as they usually are. Score components with zero
observations behind them — no direct neighbors, no outgoing edges, no
ranker-relevant symbols — defaulted to a perfect 1.0. Absence of evidence was
scoring as perfect evidence. The whole distribution crammed against 100:
Python mean 94.9 with standard deviation 9.9, TypeScript mean 99.2 with
standard deviation 5.0. I had a separate open bug that said "the score is
always 100 on TypeScript." Same defect, just visible enough to report.

The fix is to shrink the score toward 50 — "don't know" — in proportion to
how much evidence is missing. Same data, after the fix: **r=0.287, p=0.0001**
on Python, significant in 7 of 9 repos, and r=0.286 on TypeScript, where the
spread finally opens up (mean 81.0, standard deviation 17.3). Express, a
CommonJS repo my adapter barely parses, now scores around 55 with a
low-evidence flag and a measured recall of 0.0, instead of a confident 100.

Even fixed, it is not a probability. As a direct prediction of recall it
loses to just predicting the training mean — mean absolute error 0.485 versus
0.345. The reliability table shows why: mean recall is 0.07 in the 40–60
score band and 0.47 in the 80–100 band. It *ranks* risk usefully but its
absolute values over-promise. So the docs now say "ranking signal" and never
say "confidence."

## The sophisticated part was the over-weighted part

The retrieval blend has three legs — graph, BM25, same-file — at weights
[0.5, 0.35, 0.15]. Those weights were tuned on the same five repos I then
evaluated on. That is the oldest mistake in the book and I made it anyway.

So I re-selected the weights leave-one-repo-out: tune on four repos, measure
on the fifth, rotate. Every single fold picked a less graph-heavy blend than
the one I shipped. Global best: **[0.3, 0.5, 0.2]**.

Here is the part that stung. Standalone, on cross-repo mean recall, plain
BM25 beats the call graph — **0.619 versus 0.558** in the per-signal
ablation, and 0.624 versus 0.555 in the hardened re-run. The graph is the
reason this project exists: AST parsing, import resolution to real
definitions, inheritance edges, decorators, function references passed as
arguments, dispatch-sibling override edges. Keyword search over function
bodies scores higher than all of it, alone.

The damage from the bad weights turned out to be small, and that matters too.
The leave-one-repo-out choice beat the shipped weights on the held-out repo
in 4 of 5 folds, by 1.2 to 2.4 recall points, with no fold reaching p<0.05.
On four repos never used for any selection at all, the frozen global-best
weights land within ±1.1 points of shipped, none significant. So the headline
conclusions were not an artifact of same-repo tuning. But the honest
recommendation is now [0.3, 0.5, 0.2], and I changed it.

## My baseline was a stand-in, and it flattered the wrong method

An earlier evaluation used TF-IDF cosine similarity as a stand-in for dense
embedding retrieval, so I wouldn't have to take a heavy dependency. The
stand-in scored 0.664 mean recall and beat BM25 on 5 of 5 repos. I published
two conclusions on top of it.

Running the real encoder — `all-MiniLM-L6-v2` — dense retrieval scores
**0.597 and beats BM25 on only 2 of 5**. Both conclusions are corrected on
the record:

- "The embedding baseline is the strongest single baseline." No. With a real
  dense encoder, BM25 is again the strongest single method on these repos.
- "The hybrid loses to the embedding baseline on pydantic." No. True dense
  scores 0.441 on pydantic against the hybrid's 0.524.

The caveat gets stated with equal weight: MiniLM is a natural-language
encoder, and a code-tuned model could shift these numbers. I make no claim
beyond the encoder I actually ran.

Dense retrieval does earn its place, somewhere specific. On the
cross-subsystem failure bucket — a settings flag and the security check that
reads it, no call edge, no shared vocabulary — graph, BM25, and hybrid all
score 0 out of 20. Real dense gets 5. It is not a better general retriever
here; it reaches exactly where structure and keywords are blind.

## A null, reported as the null it is

One more, because it belongs in the list. Adaptive per-query blending: scale
the graph's weight by how much graph evidence a given query actually has, and
push the freed weight to BM25. It sounds obviously right.

Every fold's tuning picked the least-adaptive setting available, and the
held-out metrics equal the static blend **to four decimal places, p=1.000**.
It ships, it is on by default, and it does nothing measurable. The docs say
so.

## What survived

Against grep at matched token budgets, on 30 real co-change queries from
black's history:

| Token budget | grep-packing | DiffContext |
|---|---|---|
| 1,000 | 0.083 | 0.122 |
| 2,000 | 0.145 | 0.282 |
| 4,000 | 0.215 | 0.408 |
| 8,000 | 0.215 (plateau) | 0.576 |

The shape matters more than the ratio. Grep **plateaus** — past about 4k
tokens, more budget buys it nothing, because name-matching cannot find
co-change partners that don't mention the name. Retrieval keeps climbing.

The core numbers held: 0.868 hit rate and 0.705 recall across 423 real
commits in five repos, and they hold on four repos never used for tuning or
weight selection — black 0.897/0.712, requests 0.953/0.762, rich 0.844/0.760,
starlette 0.929/0.776.

And the honesty property I care about most: at a deliberately starved 2,000
token budget with 128 ground-truth symbols, 34% made it into the context, 66%
were explicitly disclosed as dropped in the output's meta header, and **0%
were silently invisible**. The model is never quietly missing something.

The weak spot, stated plainly: cross-repo mean precision is under 0.10. Most
of what comes back is structurally adjacent supporting context rather than
the exact co-change set. I checked whether incomplete ground truth was hiding
real precision — crediting every symbol changed within the next ten commits
still leaves it under 0.15 everywhere, so that is not the excuse. There is a
lever: cutting the ranking at its largest relative score drop instead of a
fixed top-k moves precision from roughly 0.09–0.15 to roughly 0.30–0.43, at
6–9 retrieved symbols instead of 20, and costs about a third of the recall.
Not a free lunch, so top-k stays the recall-first default.

## The last one, which is why I keep doing this

The most recent evaluation was a symbol-level retrieval ablation, built to
test one hypothesis: that embeddings are blind to real code relationships
when the two functions don't share vocabulary. The test set is 7,393 pairs
where the dependency graph has an actual edge — one function genuinely calls
or references the other — filtered to the lowest-overlap tail by shared
identifier tokens.

The hypothesis held, hard. Semantic retrieval gets **2.2% recall** on those
pairs. Structural gets 96.2%.

Then the useful part. I fused the two arms with unweighted reciprocal-rank
fusion, which is the standard thing to do, and recall fell from 0.962 to
**0.307**. Fusing a strong retriever with a blind one, giving both equal say,
produced something dramatically worse than the strong one alone. That is a
design mistake, and I found it in an evaluation harness instead of in
production.

The harness carries its own caveat, written before the numbers came back: the
structural arm recovers graph edges nearly by construction here, so this is
not a fair fight that structure won. The non-circular quantity is the size of
the embedding blind spot.

## The thing I'd actually tell you

A benchmark is a piece of software and it has bugs like any other. What makes
benchmark bugs different is that they are not randomly distributed. Every one
of the three errors above flattered my tool. That isn't dishonesty — it is
gradient. You keep debugging while the number looks wrong and you stop when
it looks right, so your bugs survive in exactly one direction.

The only defense I know is to go after the number you like most, on purpose,
when you have no reason to. My best number was r=0.274 on 25 cases. It was
0.016 on 1,080.

---

*DiffContext is open source: [github.com/trakshan-mishra/Diffcontext](https://github.com/trakshan-mishra/Diffcontext)
· [docs](https://diffcontext-docs.pages.dev/). Every number above is
reproducible from the scripts in `benchmarks/`; the full methodology pass,
including two threats-to-validity checks not covered here, is in
`benchmarks/RIGOR_REPORT_2026-07.md`.*
