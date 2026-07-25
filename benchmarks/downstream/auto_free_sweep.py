#!/usr/bin/env python3
"""
auto_free_sweep.py — unattended, zero-cost downstream sweep on a free tier.

Runs the rung-5 sweep across every repo, throttled *under* the per-minute
request cap (via run_eval's --sleep). When the per-DAY quota is exhausted the
backend starts returning per_day_quota, which run_eval records as a transient
row (not counted as done, dropped from --report). This driver notices that a
pass made no forward progress, sleeps through the cooldown, and resumes — until
every (task, provider, sample) that isn't gold-gate-skipped has a real
measurement. No babysitting, no money.

It is a thin loop over `run_eval.py`: it starts no API calls of its own and
relies entirely on run_eval's resume/dedupe, so killing it (Ctrl-C, reboot) and
re-launching is always safe.

  python benchmarks/downstream/auto_free_sweep.py            # defaults below
  python benchmarks/downstream/auto_free_sweep.py --samples 3 --cooldown 3600

Stop anytime; re-run to continue. Delete the *.gemini.jsonl results to start
over.
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
TASKS_DIR = os.path.join(HERE, "tasks")
RESULTS_DIR = os.path.join(HERE, "results")

# Repos with a validated task set (httpx has 0 tasks — excluded). Smallest
# first so the very first pass exercises the whole path in a couple of minutes.
DEFAULT_REPOS = ["requests", "click", "flask", "rich", "starlette"]


def _result_path(repo: str, tag: str) -> str:
    return os.path.join(RESULTS_DIR, f"{repo}.{tag}.jsonl")


def _skip_path(repo: str, tag: str) -> str:
    return os.path.join(RESULTS_DIR, f"{repo}.{tag}.skipped.jsonl")


def _real_done(repo: str, tag: str) -> set:
    """(commit, provider, sample) keys with a genuine measurement — transient
    infra rows (api_error:*) are NOT counted, exactly as run_eval's resume does."""
    path, keys = _result_path(repo, tag), set()
    if not os.path.exists(path):
        return keys
    with open(path, encoding="utf-8") as f:
        for ln in f:
            if not ln.strip():
                continue
            r = json.loads(ln)
            if str(r.get("gen_error") or "").startswith("api_error"):
                continue
            keys.add((r["commit"], r["provider"], r["sample"]))
    return keys


def _skipped_commits(repo: str, tag: str) -> set:
    """Commits the gold gate declared un-judgeable — permanently excluded."""
    path, out = _skip_path(repo, tag), set()
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for ln in f:
            if ln.strip():
                out.add(json.loads(ln)["commit"])
    return out


def _screened_out_commits(repo: str, tag: str) -> set:
    """Commits the sensitivity gate retired (solved without context, so they
    cannot separate the arms). They will never receive per-arm measurements, so
    the driver must not keep counting them as outstanding work — otherwise a
    gated sweep never reaches 0 and loops through the cooldown forever."""
    path = _result_path(repo, tag)[:-len(".jsonl")] + ".screen.jsonl"
    out = set()
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for ln in f:
            if ln.strip():
                r = json.loads(ln)
                if not str(r.get("gen_error") or "").startswith("api_error") \
                        and r.get("passed"):
                    out.add(r["commit"])
    return out


def _commits(repo: str) -> list:
    with open(os.path.join(TASKS_DIR, f"{repo}.json"), encoding="utf-8") as f:
        return [t["commit"] for t in json.load(f)["tasks"]]


def remaining(repos, providers, samples, tag) -> int:
    """How many real measurements are still missing across all repos."""
    total = 0
    for repo in repos:
        done = _real_done(repo, tag)
        skip = _skipped_commits(repo, tag) | _screened_out_commits(repo, tag)
        for c in _commits(repo):
            if c in skip:
                continue
            for pv in providers:
                for s in range(samples):
                    if (c, pv, s) not in done:
                        total += 1
    return total


def run_pass(repos, providers, samples, sleep, tag, backend, model,
             sensitivity_gate=False) -> None:
    for repo in repos:
        cmd = [
            sys.executable, os.path.join(HERE, "run_eval.py"),
            "--tasks", os.path.join(TASKS_DIR, f"{repo}.json"),
            "--repo", os.path.join(REPO_ROOT, "benchmark_repos", repo),
            "--backend", backend, "--model", model,
            "--providers", ",".join(providers),
            "--samples", str(samples), "--sleep", str(sleep), "--tag", tag,
        ]
        if sensitivity_gate:
            cmd.append("--sensitivity-gate")
        print(f"[driver] === {repo} ===", flush=True)
        subprocess.run(cmd, cwd=REPO_ROOT)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repos", default=",".join(DEFAULT_REPOS))
    ap.add_argument("--providers", default="diffcontext,bm25,none",
                    help="the three arms that carry the claim (add "
                         "diffcontext_gap,samefile for the full 5)")
    ap.add_argument("--samples", type=int, default=1)
    ap.add_argument("--backend", default="gemini")
    ap.add_argument("--model", default="gemini-flash-latest")
    ap.add_argument("--sleep", type=float, default=6.0,
                    help="seconds between calls — keep under the per-minute cap")
    ap.add_argument("--cooldown", type=int, default=6 * 3600,
                    help="seconds to sleep when a pass makes no progress "
                         "(daily quota spent); default 6h")
    ap.add_argument("--tag", default="gemini")
    ap.add_argument("--max-passes", type=int, default=0,
                    help="stop after N passes (0 = run until complete)")
    ap.add_argument("--sensitivity-gate", action="store_true",
                    help="keep only tasks the model FAILS without context (see "
                         "run_eval --sensitivity-gate). Spends 'none' as the "
                         "screen, so it stops being a measured arm")
    args = ap.parse_args()

    repos = args.repos.split(",")
    providers = args.providers.split(",")
    if args.sensitivity_gate:
        if "none" not in providers:
            sys.exit("--sensitivity-gate screens with the 'none' arm; add it to "
                     "--providers")
        # run_eval spends `none` on the screen and measures the rest, so the
        # progress accounting must expect rows for the remaining arms only.
        measured = [p for p in providers if p != "none"]
        if len(measured) < 2:
            sys.exit("--sensitivity-gate leaves fewer than 2 measured arms")
    else:
        measured = providers

    passes = 0
    while True:
        rem_before = remaining(repos, measured, args.samples, args.tag)
        if rem_before == 0:
            print("[driver] ALL DONE — every task/provider/sample measured.")
            print("[driver] report:\n  python benchmarks/downstream/run_eval.py "
                  "--report benchmarks/downstream/results/*." + args.tag + ".jsonl")
            return
        passes += 1
        print(f"[driver] pass {passes}: {rem_before} measurements remaining", flush=True)
        run_pass(repos, providers, args.samples, args.sleep, args.tag,
                 args.backend, args.model, args.sensitivity_gate)
        rem_after = remaining(repos, measured, args.samples, args.tag)
        made = rem_before - rem_after
        print(f"[driver] pass {passes} done: +{made} measured, {rem_after} left",
              flush=True)

        if rem_after == 0:
            print("[driver] ALL DONE.")
            return
        if args.max_passes and passes >= args.max_passes:
            print(f"[driver] hit --max-passes={args.max_passes}; {rem_after} left. "
                  "Re-run to continue.")
            return
        if made <= 0:
            # No forward progress → the daily quota is spent. Sleep it off and
            # resume; run_eval will retry exactly the un-measured rows.
            wake = time.strftime("%H:%M", time.localtime(time.time() + args.cooldown))
            print(f"[driver] no progress (daily cap reached). Sleeping "
                  f"{args.cooldown}s, resuming ~{wake}. Safe to Ctrl-C and re-run.",
                  flush=True)
            time.sleep(args.cooldown)
        else:
            # Progress but not finished → cap likely hit mid-pass. Brief pause,
            # then another pass (which will either find room or trip the cap and
            # trigger the cooldown branch above).
            time.sleep(30)


if __name__ == "__main__":
    main()
