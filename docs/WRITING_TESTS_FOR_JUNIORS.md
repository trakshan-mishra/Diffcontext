# How to verify DiffContext yourself — a guide for interns & juniors

You do NOT need to understand the scoring math to prove DiffContext works.
Every test below is something you can write and run in a few minutes. The
rule: **never trust a number the tool reports about itself — verify it with
a second, independent method.**

## The mental model (one paragraph)

DiffContext looks at a change to one function and picks the OTHER functions
most likely to matter. A good test always has two halves: (1) something you
KNOW is true about the code, and (2) checking the tool agrees. If you can't
state half (1) in plain English first, you can't write the test.

## Pattern 1 — "It must find the obvious caller"

State the truth first: *"`helper()` is called by `main()`, so if I change
`helper`, the tool must list `main` as affected."* Then check it:

```python
def test_finds_direct_caller():
    from diffcontext.pipeline import index_repository, analyze_impact
    idx = index_repository("tests/fixtures/medium_repo")
    impact = analyze_impact(idx, ["./service.py:create_user"])
    # create_user is called by onboard_user — the tool MUST know this
    assert "./service.py:onboard_user" in impact.blast_radius
```
Run: `python -m pytest tests/your_test.py -q`

## Pattern 2 — "It must NOT invent edges that aren't there"

The opposite is just as important. State: *"`unrelated()` never calls
`helper()`, so it must NOT appear."*

```python
def test_no_false_edge():
    idx = index_repository("tests/fixtures/medium_repo")
    impact = analyze_impact(idx, ["./service.py:create_user"])
    # validators.is_positive has nothing to do with create_user
    assert "./validators.py:is_positive" not in impact.blast_radius
```

## Pattern 3 — "The token count it reports must be real"

Don't trust the tool's self-reported token number. Measure the output
yourself with the same chars/4 rule:

```bash
diffcontext compile --changed ./x.py:foo --max-tokens 4000 --json \
  | python3 -c "import json,sys; print('reported:', json.load(sys.stdin)['token_estimate'])"
diffcontext compile --changed ./x.py:foo --max-tokens 4000 > out.txt
wc -c out.txt | awk '{print "actual chars/4:", $1/4}'
# The two numbers must be within a few percent. If not, that's a real bug.
```

## Pattern 4 — "Nothing relevant is silently hidden"

The most important trust test. Grep is dumb but honest — use it as ground
truth for one narrow thing: *who literally calls this function.*

```bash
# 1. What grep finds (independent, can't lie):
grep -rn "convert_currency(" --include="*.py" . | grep -v "def convert_currency"

# 2. What diffcontext dropped:
diffcontext compile --changed ./shop/checkout.py:convert_currency --max-tokens 300 \
  | grep -A50 "DROPPED SYMBOLS"

# RULE: every caller grep finds must be EITHER in the context OR named in
# the dropped list. If grep finds a caller that appears in neither, you have
# found a real "silently missing" bug. Report it.
```

## Pattern 5 — The end-to-end "does it help an LLM" test (no code trust at all)

This is the one you already ran with the JPY demo. Generalize it:
1. Pick a change that spans two files (a function + something in another
   file that must change with it).
2. Give an LLM two contexts for the SAME task: (a) just the obvious file,
   (b) `diffcontext compile` output.
3. Apply each answer to real files and RUN the code / tests.
4. If (b) produces working code and (a) has a bug, the tool added value —
   and you proved it by running code, not by reading claims.

## How to know your OWN test is fair (avoid fooling yourself)

- **Did you state the truth before looking at the output?** If you wrote the
  assertion after seeing what the tool returned, you're rubber-stamping, not
  testing.
- **Does the test fail if you break the tool?** Comment out a line in
  `graph_builder.py`, re-run — a real test goes red. If it stays green, your
  test proves nothing.
- **Is your ground truth independent?** grep, running the code, and real git
  history are independent. The tool's own output is NOT independent of itself.

## Escalation ladder (cheapest → most convincing)

1. Read the meta-header `Graph confidence` — under 80% means be skeptical.
2. Pattern 3 (token count is real).
3. Pattern 1 & 2 (finds real edges, invents none).
4. Pattern 4 (nothing silently hidden) — the trust test.
5. Pattern 5 (real LLM, real code execution) — the value test.

If a change passes 4 and 5 on a repo you cloned yourself, you have earned
the right to trust it — on repos above the size crossover. Below that, the
tool's own negative-reduction number tells you not to bother.
