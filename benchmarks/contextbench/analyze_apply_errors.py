#!/usr/bin/env python3
"""
analyze_apply_errors.py — Phase 1 deliverable: classify the apply_error rows
in glm_pass1_full.jsonl into failure modes, and produce the gen -> apply -> test
funnel per variant. Regenerated from JSONL; no number is typed by hand.

The error_detail field stores the stderr of the LAST fallback command tried by
apply_patch() (git apply -p1, --recount, --3way, --ignore-whitespace, then
patch -p1 --fuzz=3). Every apply_error fell through all of them, so
error_detail is always a `patch` message.

Classification is by root cause inferred from the patch stderr. Precedence was
settled by synthetic reproduction (see applyfix_test): a missing trailing
newline does NOT cause failure — `patch` returns rc=0 and merely warns "ends
in middle of line", so that warning is a CO-SYMPTOM, not a root cause.

  malformed_format   structural error — missing leading space on a context
                      line, hunk line-count mismatch, bad header
                      ("malformed patch at line N" / git "corrupt patch").
                      THE DOMINANT root cause.
  context_mismatch   hunk header parses but context lines don't match the file
                      ("Hunk #N FAILED"). Format is clean; content is wrong.
  truncated          genuine truncation — patch parser hit EOF mid-hunk
                      ("unexpected end of file in patch"). NOT token-cap
                      truncation (only ~6% of all apply errors hit the cap).
  placeholder_header model emitted @@ -XXX,XX +XXX,XX @@ or @@ -<signature>
                      instead of real line numbers ("missing line number").
  already_applied    "Reversed (or previously applied) patch detected".
  no_trailing_newline ONLY the cosmetic "ends in middle of line" warning with
                      no other cause AND patch still failed (rare).
  infrastructure     "Disk quota exceeded" / "write error" — NOT a malformed
                      diff; should be retried, not counted as apply_error.
  other              anything not matched above.

NOTE: error_detail alone cannot fully confirm the exact malformation — the
harness now stores the generated `diff` in the JSONL so it can be inspected
directly on the next run.

Usage:
  python3 benchmarks/contextbench/analyze_apply_errors.py
"""

import argparse
import json
import os
import statistics
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PASS1 = os.path.join(HERE, "results", "glm_pass1", "glm_pass1_full.jsonl")

TOKEN_CAP = 16384  # glm_generate max_tokens


def load_rows(path):
    if not os.path.isfile(path):
        sys.exit(f"MISSING: {path}")
    rows = []
    with open(path) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                sys.exit(f"BAD JSONL at {path}:{i}: {e}")
    return rows


def classify(r):
    """Root-cause classification. Precedence is set by synthetic reproduction
    (see applyfix_test): a missing trailing newline does NOT cause failure —
    `patch` returns rc=0 and merely warns "ends in middle of line". So that
    warning is a CO-SYMPTOM, not a root cause. The real causes, in priority:

      eof_in_patch       genuine truncation — patch parser hit EOF mid-hunk
                         ("unexpected end of file in patch"). This IS truncation.
      malformed_format   structural error — missing leading space on a context
                         line, hunk line-count mismatch, bad header
                         ("malformed patch at line N" / git "corrupt patch").
      context_mismatch   hunk header parses but context lines don't match the
                         file ("Hunk #N FAILED"). Format is clean; content is wrong.
      placeholder_header model emitted @@ -XXX,XX +XXX,XX @@ or @@ -<signature>
                         ("missing line number").
      already_applied    "Reversed (or previously applied) patch detected".
      no_trailing_newline ONLY the cosmetic "ends in middle of line" warning with
                         no other cause AND patch still failed (rare; needs the
                         captured diff to confirm).
      infrastructure     "Disk quota exceeded" / "write error" — NOT a malformed
                         diff; should be retried, not counted as apply_error.
    """
    d = r.get("error_detail") or ""
    if "Disk quota" in d or "write error" in d or "No space" in d:
        return "infrastructure"
    eof = "unexpected end of file in patch" in d
    hunk = "Hunk #" in d and "FAILED" in d
    malformed = "malformed patch at line" in d
    reversed_ = "Reversed" in d or "previously applied" in d
    placeholder = "missing line number" in d or "XXX,XX" in d
    ends_mid = "patch unexpectedly ends in middle of line" in d
    if eof:
        return "truncated"          # genuine EOF mid-parse
    if malformed:
        return "malformed_format"
    if hunk:
        return "context_mismatch"
    if placeholder:
        return "placeholder_header"
    if reversed_:
        return "already_applied"
    if ends_mid:
        return "no_trailing_newline"
    return "other"


CATEGORIES = ("malformed_format", "context_mismatch", "truncated",
             "placeholder_header", "already_applied", "no_trailing_newline",
             "infrastructure", "other")
VARIANTS = ("none", "diffcontext", "diffcontext_gap")
NON_EVALUABLE = {"setup_error", "no_seeds", "skipped_no_llm"}


def funnel(rows):
    """gen -> apply -> test funnel per variant (attempted = LLM actually ran)."""
    out = {}
    for v in VARIANTS:
        vr = [r for r in rows if r.get("variant") == v]
        attempted = [r for r in vr if r.get("error_class") not in NON_EVALUABLE]
        gen_ok = [r for r in attempted if r.get("error_class") != "gen_error"]
        apply_ok = [r for r in gen_ok if r.get("applied") is True
                    or (r.get("passed") is not None)]
        # apply_ok = rows where a diff was applied (passed True OR test_error)
        applied = [r for r in gen_ok
                   if r.get("applied") is True
                   or r.get("error_class") in ("test_error",)
                   or r.get("passed") is True]
        passed = [r for r in vr if r.get("passed") is True]
        out[v] = {
            "n_total": len(vr),
            "attempted": len(attempted),
            "gen_ok": len(gen_ok),
            "applied": len(applied),
            "passed": len(passed),
            "gen_error": len(attempted) - len(gen_ok),
            "apply_error": sum(1 for r in gen_ok if r.get("error_class") == "apply_error"),
            "test_fail": sum(1 for r in applied if r.get("passed") is False),
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pass1", default=DEFAULT_PASS1)
    args = ap.parse_args()
    rows = load_rows(args.pass1)

    print("=" * 74)
    print("PHASE 1 — APPLY-ERROR ANALYSIS (regenerated from JSONL)")
    print("=" * 74)

    # ── Funnel ────────────────────────────────────────────────────────────
    f = funnel(rows)
    print("\n## Funnel: generation -> apply -> test (per variant)")
    print(f"  {'variant':<18}{'total':>7}{'attempt':>9}{'gen_ok':>8}"
          f"{'applied':>9}{'passed':>8}{'gen_err':>9}{'apply_err':>11}{'testfail':>10}")
    print("  " + "-" * 81)
    for v in VARIANTS:
        d = f[v]
        print(f"  {v:<18}{d['n_total']:>7}{d['attempted']:>9}{d['gen_ok']:>8}"
              f"{d['applied']:>9}{d['passed']:>8}{d['gen_error']:>9}"
              f"{d['apply_error']:>11}{d['test_fail']:>10}")

    # ── Classification ────────────────────────────────────────────────────
    ae = [r for r in rows if r.get("error_class") == "apply_error"]
    print(f"\n## Apply-error classification (n={len(ae)} apply errors across "
          f"{len({r['instance_id'] for r in ae})} unique instances)")

    # by variant
    by_v = Counter()
    for r in ae:
        by_v[(r["variant"], classify(r))] += 1
    print(f"\n  {'variant':<18}" + "".join(f"{c:>18}" for c in CATEGORIES)
          + f"{'TOTAL':>8}")
    print("  " + "-" * (18 + 18 * len(CATEGORIES) + 8))
    for v in VARIANTS:
        row = f"  {v:<18}" + "".join(f"{by_v[(v, c)]:>18}" for c in CATEGORIES)
        row += f"{sum(by_v[(v, c)] for c in CATEGORIES):>8}"
        print(row)
    tot = Counter(classify(r) for r in ae)
    print("  " + "-" * (18 + 18 * len(CATEGORIES) + 8))
    print(f"  {'TOTAL':<18}" + "".join(f"{tot[c]:>18}" for c in CATEGORIES)
          + f"{sum(tot.values()):>8}")
    print(f"\n  share of apply errors:  "
          + ", ".join(f"{c}={tot[c]/len(ae):.0%}" for c in CATEGORIES if tot[c]))

    # ── Root cause is structural, not token-cap truncation ───────────────
    # Synthetic reproduction (applyfix_test) proved: a missing trailing
    # newline does NOT cause failure — `patch` returns rc=0 and merely warns
    # "ends in middle of line". So the dominant "ends_mid_line" co-symptom is
    # NOT the root cause. The real causes are malformed_format (missing
    # leading space / hunk overcount -> "corrupt patch at line N") and
    # context_mismatch (clean format, wrong context). Genuine truncation is the
    # small "unexpected end of file in patch" subset.
    all_ct = [r.get("completion_tokens") or 0 for r in ae]
    near_cap_all = sum(1 for t in all_ct if t >= TOKEN_CAP - 384)
    print("\n## Root cause is structural malformation, not truncation")
    print(f"  Across all {len(ae)} apply errors, completion_tokens: "
          f"median={statistics.median(all_ct):.0f}, max={max(all_ct)} "
          f"(cap={TOKEN_CAP})")
    print(f"  Hit token cap (>= {TOKEN_CAP-384}): {near_cap_all}/{len(ae)} "
          f"= {near_cap_all/len(ae):.0%} — truncation by the cap is rare.")
    fr = Counter(r.get("finish_reason") for r in ae)
    print(f"  finish_reason across apply errors: {dict(fr)}  (length = cut by "
          f"cap; stop = model finished normally)")
    trunc = [r for r in ae if classify(r) == "truncated"]
    if trunc:
        print(f"  Genuine truncation (\"unexpected end of file in patch\"): "
              f"{len(trunc)}/{len(ae)} = {len(trunc)/len(ae):.0%}.")
    print("  NOTE: the leading `ends in middle of line` warning is cosmetic "
          "(patch returns rc=0\n        with it on well-formed diffs). The "
          "dominant real cause is `malformed_format`\n        (missing leading "
          "space on a context line / hunk line-count mismatch), which both "
          "git apply\n        (\"corrupt patch at line N\") and patch "
          "(\"malformed patch at line N\") reject. The\n        captured "
          "`diff` field (added to the harness) is required to confirm the "
          "exact malformation;\n        the harness now stores it for future "
          "runs.")

    # ── Cross-variant systematic vs sporadic ──────────────────────────────
    inst_v = defaultdict(set)
    for r in ae:
        inst_v[r["instance_id"]].add(r["variant"])
    dist = Counter(len(s) for s in inst_v.values())
    print(f"\n## Cross-variant pattern (apply error in N variants per instance)")
    print(f"  {len(inst_v)} unique instances affected; variants-failed distribution: "
          + ", ".join(f"{k}v->{v}" for k, v in sorted(dist.items())))
    print(f"  {dist.get(3, 0)} instances fail apply in ALL 3 variants "
          "(systematic — hard for any context); "
          f"{dist.get(1, 0)} in only 1 (sporadic).")

    # ── Infrastructure (should not be counted as apply_error) ─────────────
    infra = [r for r in ae if classify(r) == "infrastructure"]
    if infra:
        print(f"\n## Infrastructure contamination (exclude from apply_error)")
        for r in infra:
            print(f"  {r['instance_id']} {r['variant']}: "
                  f"{(r.get('error_detail') or '').strip()[:80]}")

    print("\n" + "=" * 74)
    print("Reproducible: rerun `python3 benchmarks/contextbench/analyze_apply_errors.py`")
    print("=" * 74)


if __name__ == "__main__":
    main()
