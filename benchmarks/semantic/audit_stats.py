#!/usr/bin/env python3
"""
audit_stats.py — label-quality + inter-rater stats over labeled audit CSV(s).

Reads one or more sample.csv files with the `label` column filled in
(related / incidental / unsure; 1/0/? and y/n also accepted) and reports:

  * proxy precision = related / (related + incidental), with a Wilson 95% CI,
    OVERALL and per stratum (repo, cross-file, gt bucket). This is the headline
    validity number: what fraction of the co-change ground truth is a genuine
    relevance link, and where the proxy is strong vs weak. Expect same-file
    links to be near-perfect and cross-file / large-gt links to carry most of
    the incidental noise — which is precisely the Item-3 adversarial-gap story.

  * inter-rater agreement (mean pairwise Cohen's kappa + raw % agreement) when
    two or more label files are supplied, computed over the links both raters
    labeled.

`unsure` rows are excluded from precision (reported as a coverage caveat) and
kept for the agreement calc (disagreeing on unsure is real disagreement).

Usage:
  python -m benchmarks.semantic.audit_stats benchmarks/semantic/audit/sample.csv
  python -m benchmarks.semantic.audit_stats rater1.csv rater2.csv
"""

import argparse
import csv
import math
import os
from collections import defaultdict
from itertools import combinations
from typing import Dict, List, Optional, Tuple

_ALIASES = {
    "1": "related", "y": "related", "yes": "related", "rel": "related", "related": "related",
    "0": "incidental", "n": "incidental", "no": "incidental", "inc": "incidental",
    "incidental": "incidental",
    "?": "unsure", "u": "unsure", "unsure": "unsure",
}


def _norm(v: Optional[str]) -> str:
    return _ALIASES.get((v or "").strip().lower(), (v or "").strip().lower())


def load_labeled(path: str) -> Dict[str, dict]:
    """link_id -> {label, repo, cross_file, gt_bucket}. Unlabeled rows dropped."""
    rows: Dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            lab = _norm(r.get("label"))
            if lab == "":
                continue
            rows[r["link_id"]] = {
                "label": lab, "repo": r.get("repo", "?"),
                "cross_file": r.get("cross_file", "?"), "gt_bucket": r.get("gt_bucket", "?"),
            }
    return rows


def wilson(k: int, n: int, z: float = 1.96) -> Tuple[float, float, float]:
    """(point estimate, lo, hi) Wilson score interval for k successes in n."""
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = k / n
    z2 = z * z
    d = 1 + z2 / n
    center = (p + z2 / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / d
    return (p, max(0.0, center - half), min(1.0, center + half))


def precision(labels: List[str]) -> Tuple[int, int, Tuple[float, float, float]]:
    rel = sum(1 for lb in labels if lb == "related")
    inc = sum(1 for lb in labels if lb == "incidental")
    return rel, inc, wilson(rel, rel + inc)


def cohen_kappa(a: List[str], b: List[str]) -> Tuple[float, float]:
    """(kappa, raw agreement) for two parallel label lists over their categories."""
    n = len(a)
    if n == 0:
        return (0.0, 0.0)
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    cats = set(a) | set(b)
    pa = {c: a.count(c) / n for c in cats}
    pb = {c: b.count(c) / n for c in cats}
    pe = sum(pa[c] * pb[c] for c in cats)
    kappa = 1.0 if pe >= 1.0 else (po - pe) / (1 - pe)
    return (kappa, po)


def _fmt(rel: int, inc: int, ci: Tuple[float, float, float], label: str) -> str:
    p, lo, hi = ci
    n = rel + inc
    return f"  {label:28s} {p:6.3f}  [{lo:.3f}, {hi:.3f}]  (rel {rel}/{n})"


def report_file(path: str) -> None:
    rows = load_labeled(path)
    labels = [r["label"] for r in rows.values()]
    n_unsure = sum(1 for lb in labels if lb == "unsure")
    scorable = {lid: r for lid, r in rows.items() if r["label"] != "unsure"}

    print(f"===== {os.path.basename(path)} =====")
    print(f"{len(rows)} labeled links ({n_unsure} unsure, excluded from precision)\n")
    rel, inc, ci = precision([r["label"] for r in scorable.values()])
    print("proxy precision = related / (related + incidental), Wilson 95% CI:")
    print(_fmt(rel, inc, ci, "OVERALL"))

    for dim in ("repo", "cross_file", "gt_bucket"):
        groups: Dict[str, List[str]] = defaultdict(list)
        for r in scorable.values():
            groups[str(r[dim])].append(r["label"])
        print(f"\n  by {dim}:")
        for key in sorted(groups):
            rel, inc, ci = precision(groups[key])
            print(_fmt(rel, inc, ci, f"{dim}={key}"))
    print()


def report_interrater(paths: List[str]) -> None:
    labeled = {p: load_labeled(p) for p in paths}
    print("===== inter-rater agreement =====")
    kappas = []
    for a, b in combinations(paths, 2):
        common = sorted(set(labeled[a]) & set(labeled[b]))
        if not common:
            print(f"  {os.path.basename(a)} vs {os.path.basename(b)}: no shared links")
            continue
        la = [labeled[a][lid]["label"] for lid in common]
        lb = [labeled[b][lid]["label"] for lid in common]
        k, po = cohen_kappa(la, lb)
        kappas.append(k)
        print(f"  {os.path.basename(a)} vs {os.path.basename(b)}: "
              f"kappa={k:+.3f}  agreement={po:.3f}  (n={len(common)})")
    if len(kappas) > 1:
        print(f"\n  mean pairwise kappa = {sum(kappas) / len(kappas):+.3f}")
    if kappas:
        k = sum(kappas) / len(kappas)
        band = ("poor" if k < 0.2 else "fair" if k < 0.4 else "moderate"
                if k < 0.6 else "substantial" if k < 0.8 else "almost perfect")
        print(f"  (Landis-Koch: {band})")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="labeled sample.csv file(s)")
    args = ap.parse_args()
    for p in args.files:
        report_file(p)
    if len(args.files) >= 2:
        report_interrater(args.files)


if __name__ == "__main__":
    main()
