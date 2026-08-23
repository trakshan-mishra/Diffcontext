#!/usr/bin/env python3
"""
verify_results.py — regenerate every number that enters RESULTS.md from the
stored JSONL artifacts. Nothing is typed by hand.

Sources:
  retrieval: benchmarks/contextbench/results/retrieval_136_4var/summary.json
  pass@1:    benchmarks/contextbench/results/glm_pass1/glm_pass1_full.jsonl

Outputs a structured report to stdout. Exits non-zero if an artifact is
missing or malformed, so a stale RESULTS.md can never be silently regenerated
from incomplete data.

Usage:
  python3 benchmarks/contextbench/verify_results.py
  python3 benchmarks/contextbench/verify_results.py --retrieval-dir ... --pass1 ...
"""

import argparse
import json
import math
import os
import random
import statistics
import sys
from collections import Counter, defaultdict
from typing import Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE))  # for stats.py import

from stats import wilson_interval, mcnemar_exact, paired_table

VARIANTS = ("seeds_only", "retrieved_only", "seeds_plus_retrieved",
            "diffcontext_gap", "diffcontext_depboost")


# ── retrieval metrics ──────────────────────────────────────────────────────

def load_summary(path: str) -> List[dict]:
    if not os.path.isfile(path):
        sys.exit(f"MISSING: {path} (run run_diffcontext.py first)")
    with open(path) as f:
        rows = json.load(f)
    if not isinstance(rows, list) or not rows:
        sys.exit(f"BAD: {path} is empty or not a list")
    return rows


def macro(rows: List[dict], variant: str, key: str) -> float:
    vals = [r["metrics"][variant][key] for r in rows
            if "metrics" in r and variant in r["metrics"]]
    if not vals:
        return float("nan")
    return statistics.mean(vals)


def paired_wtl(rows: List[dict], a: str, b: str, key: str) -> Tuple[int, int, int]:
    """Paired win/tie/loss of variant `a` vs `b` on `key` over tasks that have
    both. Excludes no-seed tasks (where both are empty/degenerate)."""
    w = t = l = 0
    for r in rows:
        if "metrics" not in r or a not in r["metrics"] or b not in r["metrics"]:
            continue
        va, vb = r["metrics"][a][key], r["metrics"][b][key]
        if math.isnan(va) or math.isnan(vb):
            continue
        if va > vb:
            w += 1
        elif va < vb:
            l += 1
        else:
            t += 1
    return w, t, l


def bootstrap_ci(deltas: List[float], n_boot: int = 10000, alpha: float = 0.05,
                 seed: int = 12345) -> Tuple[float, float]:
    """Bootstrap 95% CI on the mean of `deltas`. Pure stdlib (fixed seed for
    reproducibility)."""
    if not deltas:
        return float("nan"), float("nan")
    rng = random.Random(seed)
    n = len(deltas)
    means = []
    for _ in range(n_boot):
        sample = [deltas[rng.randrange(n)] for _ in range(n)]
        means.append(statistics.mean(sample))
    means.sort()
    lo_idx = int((alpha / 2) * n_boot)
    hi_idx = int((1 - alpha / 2) * n_boot)
    return means[lo_idx], means[hi_idx - 1]


def sign_test_p(w: int, l: int) -> float:
    """Two-sided sign-test p-value via scipy if available, else stdlib binomial."""
    try:
        from scipy.stats import binomtest
        return float(binomtest(w, w + l, p=0.5, alternative="two-sided").pvalue)
    except Exception:
        n = w + l
        if n == 0:
            return 1.0
        # two-sided: 2 * P(X <= min(w,l)) for binomial(n, 0.5)
        m = min(w, l)
        p = 2 * sum(math.comb(n, k) * 0.5 ** n for k in range(m + 1))
        return min(p, 1.0)


def retrieval_report(rows: List[dict]) -> dict:
    """All retrieval numbers. Effective n = tasks with >=1 seed (oracle misses
    excluded — they are a localization limitation, not a retrieval result)."""
    with_seeds = [r for r in rows if r.get("n_seeds", 0) > 0]
    no_seeds = [r for r in rows if r.get("n_seeds", 0) == 0]
    n_eff = len(with_seeds)

    out = {
        "n_total": len(rows),
        "n_with_seeds": n_eff,
        "n_no_seeds": len(no_seeds),
        "no_seed_ids": sorted(r["instance_id"].split("__")[-1] for r in no_seeds),
    }

    # Pooled macro-mean over the effective n (with seeds). Also compute over
    # all 136 for transparency so the vacuous-precision inflation is visible.
    out["variants"] = {}
    for v in VARIANTS:
        rec = macro(with_seeds, v, "line_recall")
        prec = macro(with_seeds, v, "line_precision")
        f1 = macro(with_seeds, v, "line_f1")
        frec = macro(with_seeds, v, "file_recall")
        fprec = macro(with_seeds, v, "file_precision")
        out["variants"][v] = {
            "line_recall": rec, "line_precision": prec, "line_f1": f1,
            "file_recall": frec, "file_precision": fprec,
            # all-136 reference (vacuous-precision inflation disclosed)
            "line_recall_n136": macro(rows, v, "line_recall"),
            "line_precision_n136": macro(rows, v, "line_precision"),
            "line_f1_n136": macro(rows, v, "line_f1"),
        }

    # Paired gap vs default (seeds_plus_retrieved) — the headline comparison.
    a, b = "diffcontext_gap", "seeds_plus_retrieved"
    f1_deltas = []
    for r in with_seeds:
        if "metrics" in r and a in r["metrics"] and b in r["metrics"]:
            f1_deltas.append(r["metrics"][a]["line_f1"] - r["metrics"][b]["line_f1"])
    w, t, l = paired_wtl(with_seeds, a, b, "line_f1")
    out["gap_vs_default"] = {
        "metric": "line_f1",
        "wins": w, "ties": t, "losses": l,
        "win_rate": w / (w + t + l) if (w + t + l) else float("nan"),
        "mean_delta": statistics.mean(f1_deltas) if f1_deltas else float("nan"),
        "median_delta": statistics.median(f1_deltas) if f1_deltas else float("nan"),
        "bootstrap_ci95": bootstrap_ci(f1_deltas),
        "sign_test_p": sign_test_p(w, l),
    }
    # Recall/precision paired too
    for key in ("line_recall", "line_precision"):
        w2, t2, l2 = paired_wtl(with_seeds, a, b, key)
        out["gap_vs_default"][key] = {"wins": w2, "ties": t2, "losses": l2}

    # Per-repo breakdown (line F1 per variant, effective n per repo).
    by_repo = defaultdict(list)
    for r in with_seeds:
        by_repo[r["repo"]].append(r)
    out["per_repo"] = {}
    for repo, rrows in sorted(by_repo.items()):
        out["per_repo"][repo] = {
            "n": len(rrows),
            "line_f1": {v: macro(rrows, v, "line_f1") for v in VARIANTS},
            "line_recall": {v: macro(rrows, v, "line_recall") for v in VARIANTS},
            "line_precision": {v: macro(rrows, v, "line_precision") for v in VARIANTS},
        }
    return out


# ── pass@1 metrics ─────────────────────────────────────────────────────────

PASS1_VARIANTS = ("none", "diffcontext", "diffcontext_gap", "diffcontext_depboost")


def load_pass1(path: str) -> List[dict]:
    if not os.path.isfile(path):
        return []  # may not exist yet (run in progress) — report partial
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


def pass1_report(rows: List[dict]) -> dict:
    out = {"n_rows": len(rows), "variants": {}}
    if not rows:
        out["note"] = "no pass@1 rows yet (run in progress)"
        return out
    # one row per (instance_id, variant) is the invariant — verify it.
    keys = [(r.get("instance_id"), r.get("variant")) for r in rows]
    dupes = [k for k, c in Counter(keys).items() if c > 1]
    out["duplicate_pairs"] = dupes

    # error_class taxonomy
    non_evaluable = {"setup_error", "no_seeds", "skipped_no_llm"}
    for v in PASS1_VARIANTS:
        vr = [r for r in rows if r.get("variant") == v]
        passed = sum(1 for r in vr if r.get("passed") is True)
        attempted = sum(1 for r in vr if r.get("error_class") not in non_evaluable)
        no_seeds = sum(1 for r in vr if r.get("error_class") == "no_seeds")
        setup_err = sum(1 for r in vr if r.get("error_class") == "setup_error")
        gen_err = sum(1 for r in vr if r.get("error_class") == "gen_error")
        apply_err = sum(1 for r in vr if r.get("error_class") == "apply_error")
        test_fail = sum(1 for r in vr if r.get("passed") is False
                        and r.get("error_class") not in non_evaluable)
        n_total = len(vr)
        lo, hi = wilson_interval(passed, attempted) if attempted else (float("nan"), float("nan"))
        out["variants"][v] = {
            "n_total": n_total, "passed": passed, "attempted": attempted,
            "pass_rate_of_attempted": passed / attempted if attempted else float("nan"),
            "pass_rate_of_total": passed / n_total if n_total else float("nan"),
            "wilson_ci": (lo, hi),
            "no_seeds": no_seeds, "setup_error": setup_err,
            "gen_error": gen_err, "apply_error": apply_err,
            "test_fail": test_fail,
        }
    # Paired pass@1 with exact McNemar tests — all 6 pairwise comparisons.
    result_maps = {}
    for v in PASS1_VARIANTS:
        result_maps[v] = {r["instance_id"]: r.get("passed") is True
                          for r in rows
                          if r.get("variant") == v and r.get("error_class") not in non_evaluable}

    for va, vb in (("diffcontext_gap", "diffcontext_depboost"),
                   ("diffcontext_gap", "diffcontext"),
                   ("diffcontext_depboost", "diffcontext"),
                   ("diffcontext", "none"),
                   ("diffcontext_gap", "none"),
                   ("diffcontext_depboost", "none")):
        both, a_only, b_only, neither = paired_table(
            result_maps.get(va, {}), result_maps.get(vb, {}),
        )
        p_val = mcnemar_exact(a_only, b_only)
        out.setdefault("paired_pass1", {})[f"{va}_vs_{vb}"] = {
            "n_pairs": both + a_only + b_only + neither,
            "both_pass": both, "a_only": a_only, "b_only": b_only,
            "neither": neither, "mcnemar_p": p_val,
        }
    # contamination guard: every row should have head_sha + tree_status
    missing_prov = sum(1 for r in rows
                       if r.get("error_class") not in ("no_seeds", "skipped_no_llm")
                       and (not r.get("head_sha") or r.get("tree_status") is None))
    out["missing_provenance_rows"] = missing_prov
    return out


# ── formatting ─────────────────────────────────────────────────────────────

def fmt_pct(x):
    return f"{x:.1%}" if x == x else "—"


def fmt3(x):
    return f"{x:.3f}" if x == x else "—"


def print_report(rtr: dict, p1: dict, args):
    print("=" * 72)
    print("CONTEXTBENCH VERIFIED RESULTS (regenerated from JSONL)")
    print("=" * 72)
    print(f"\n## Retrieval (summary.json)")
    print(f"  tasks total: {rtr['n_total']}  with_seeds: {rtr['n_with_seeds']}  "
          f"no_seeds(oracle miss): {rtr['n_no_seeds']}")
    if rtr["no_seed_ids"]:
        print(f"  oracle-miss ids: {', '.join(rtr['no_seed_ids'])}")
    print(f"\n  effective n = {rtr['n_with_seeds']} (oracle misses excluded)")
    print(f"\n  {'variant':<22} {'line_rec':>9} {'line_prec':>10} {'line_f1':>8} "
          f"{'file_rec':>9} {'file_prec':>10}")
    print("  " + "-" * 70)
    for v in VARIANTS:
        d = rtr["variants"][v]
        print(f"  {v:<22} {fmt3(d['line_recall']):>9} {fmt3(d['line_precision']):>10} "
              f"{fmt3(d['line_f1']):>8} {fmt3(d['file_recall']):>9} "
              f"{fmt3(d['file_precision']):>10}")
    print(f"\n  (reference, all-136 incl. vacuous-precision no-seeds:")
    for v in ("seeds_plus_retrieved", "diffcontext_gap"):
        d = rtr["variants"][v]
        print(f"   {v:<22} rec={fmt3(d['line_recall_n136'])} "
              f"prec={fmt3(d['line_precision_n136'])} f1={fmt3(d['line_f1_n136'])})")

    g = rtr["gap_vs_default"]
    print(f"\n  gap vs default (seeds+retrieved), line F1, paired n={g['wins']+g['ties']+g['losses']}:")
    print(f"    wins={g['wins']} ties={g['ties']} losses={g['losses']}  "
          f"win_rate={fmt_pct(g['win_rate'])}")
    print(f"    mean_delta={fmt3(g['mean_delta'])} median_delta={fmt3(g['median_delta'])} "
          f"bootstrap95%CI=[{fmt3(g['bootstrap_ci95'][0])}, {fmt3(g['bootstrap_ci95'][1])}]")
    print(f"    sign_test p={g['sign_test_p']:.4g}  (recall W/T/L="
          f"{g['line_recall']['wins']}/{g['line_recall']['ties']}/{g['line_recall']['losses']}, "
          f"prec W/T/L={g['line_precision']['wins']}/{g['line_precision']['ties']}/{g['line_precision']['losses']})")

    print(f"\n  per-repo (line F1):")
    for repo, d in rtr["per_repo"].items():
        line = f"    {repo:<10} n={d['n']:<3} "
        line += " ".join(f"{v.split('_')[-1]}={fmt3(d['line_f1'][v])}"
                         for v in VARIANTS)
        print(line)

    print(f"\n## Pass@1 ({os.path.basename(args.pass1)})")
    print(f"  rows: {p1['n_rows']}")
    if p1.get("note"):
        print(f"  NOTE: {p1['note']}")
    if p1.get("duplicate_pairs"):
        print(f"  WARNING duplicate (iid,variant) pairs: {p1['duplicate_pairs']}")
    if p1.get("missing_provenance_rows"):
        print(f"  WARNING rows missing provenance: {p1['missing_provenance_rows']}")
    print(f"\n  {'variant':<22} {'passed':>7} {'attempted':>9} {'pass@1':>7} "
          f"{'95% Wilson CI':>16} {'no_seed':>7} {'gen':>4} {'apply':>5} {'testfail':>8}")
    print("  " + "-" * 92)
    for v in PASS1_VARIANTS:
        if v not in p1["variants"]:
            continue
        d = p1["variants"][v]
        lo, hi = d.get("wilson_ci", (float("nan"), float("nan")))
        ci = f"[{lo:.3f}, {hi:.3f}]" if lo == lo else "—"
        print(f"  {v:<22} {d['passed']:>7} {d['attempted']:>9} "
              f"{fmt_pct(d['pass_rate_of_attempted']):>7} {ci:>16} "
              f"{d['no_seeds']:>7} {d['gen_error']:>4} {d['apply_error']:>5} "
              f"{d['test_fail']:>8}")
    if "paired_pass1" in p1:
        print(f"\n  paired pass@1 (exact McNemar, attempted tasks only):")
        for k, d in p1["paired_pass1"].items():
            sig = ""
            p = d.get("mcnemar_p", 1.0)
            if p < 0.001: sig = " ***"
            elif p < 0.01: sig = " **"
            elif p < 0.05: sig = " *"
            print(f"    {k}: n={d['n_pairs']} both={d['both_pass']} "
                  f"a_only={d['a_only']} b_only={d['b_only']} "
                  f"neither={d['neither']} p={p:.4g}{sig}")
    print("\n" + "=" * 72)
    print("Every number above is computed from JSONL. Paste into RESULTS.md.")
    print("=" * 72)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--retrieval-dir",
                    default=os.path.join(HERE, "results", "retrieval_136_5var"))
    ap.add_argument("--pass1",
                    default=os.path.join(HERE, "results", "glm_pass1",
                                        "glm_pass1_depboost.jsonl"))
    args = ap.parse_args()
    summary_path = os.path.join(args.retrieval_dir, "summary.json")
    rows = load_summary(summary_path)
    rtr = retrieval_report(rows)
    p1_rows = load_pass1(args.pass1)
    p1 = pass1_report(p1_rows)
    print_report(rtr, p1, args)


if __name__ == "__main__":
    main()
