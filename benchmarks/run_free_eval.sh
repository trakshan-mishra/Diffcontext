#!/usr/bin/env bash
# ============================================================================
# run_free_eval.sh — zero-cost downstream eval on Google's free Gemini tier.
#
#   1. Get a FREE key (no card, no UPI):  https://aistudio.google.com/apikey
#   2. Put it in the environment — NEVER in this file, which is tracked in git:
#        export GEMINI_API_KEY=your-key-here
#      or keep it in an untracked .env beside this script (gitignored):
#        echo 'GEMINI_API_KEY=your-key-here' > .env
#   3. Run:   bash run_free_eval.sh
#
# It validates the key, smoke-tests one repo, then runs the FULL sweep across
# every repo UNATTENDED — pacing under the per-minute cap and auto-pausing /
# resuming through the daily quota until every task is measured, then prints
# the pooled report. Safe to Ctrl-C and re-run: it always continues where it
# left off (nothing is ever double-counted or lost).
# ============================================================================

# --- tunables (fine as-is) --------------------------------------------------
MODEL="gemini-2.5-flash"            # flash-latest's daily quota is exhausted; 2.5 has its own
PROVIDERS="diffcontext,bm25,none"   # the 3 arms that carry the claim
TAG="gemini25"                      # fresh tag = clean single-model dataset
SLEEP=4                             # seconds between calls (< per-minute cap)
COOLDOWN=21600                      # 6h nap when the daily quota is spent
# ----------------------------------------------------------------------------

set -uo pipefail
cd "$(dirname "$0")" || { echo "cannot cd to script dir"; exit 1; }
PY="python3"                        # anaconda python3 — has the benchmark deps
RESULTS="benchmarks/downstream/results"
SMOKE_FILE="$RESULTS/requests.$TAG.jsonl"

# --- 0. key present? --------------------------------------------------------
# Read from the environment, falling back to an untracked .env beside this
# script. The key must never be written into this file: it is tracked, so a
# pasted key gets committed and has to be rotated.
if [ -z "${GEMINI_API_KEY:-}" ] && [ -f "$(dirname "$0")/.env" ]; then
  # shellcheck disable=SC1091
  set -a; . "$(dirname "$0")/.env"; set +a
fi
if [ -z "${GEMINI_API_KEY:-}" ]; then
  echo "ERROR: GEMINI_API_KEY is not set. Either:"
  echo "  export GEMINI_API_KEY=your-key-here"
  echo "  or: echo 'GEMINI_API_KEY=your-key-here' > $(dirname "$0")/.env"
  echo "Get a free key at https://aistudio.google.com/apikey"
  exit 1
fi
export GEMINI_API_KEY

# --- 1. validate the key (no generation cost) -------------------------------
echo "[1/4] validating key ..."
code=$(curl -s -o /dev/null -w "%{http_code}" \
  "https://generativelanguage.googleapis.com/v1beta/models?key=${GEMINI_API_KEY}")
if [ "$code" != "200" ]; then
  echo "ERROR: Gemini rejected the key (HTTP $code). Re-check it at"
  echo "       https://aistudio.google.com/apikey"
  exit 1
fi
echo "      key OK."

# --- 2. smoke test: 'requests' only (<=12 calls, free) ----------------------
echo "[2/4] smoke test on 'requests' ..."
$PY benchmarks/downstream/run_eval.py \
  --tasks benchmarks/downstream/tasks/requests.json --repo benchmark_repos/requests \
  --backend gemini --model "$MODEL" --providers "$PROVIDERS" --sleep "$SLEEP" --tag "$TAG"

# only ABORT on a catastrophe (all rate-limited, or model emits no diffs at all);
# a merely-low pass rate is a real result, so we let the full run proceed.
verdict=$($PY - "$SMOKE_FILE" <<'PY'
import json, sys
rows=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]
if not rows: print("EMPTY"); sys.exit()
err   =[r for r in rows if str(r.get("gen_error") or "").startswith("api_error")]
real  =[r for r in rows if r not in err]
nodiff=[r for r in real if r.get("gen_error")=="no_diff_in_output"]
applied=[r for r in real if r.get("applied")]
passed =[r for r in real if r.get("passed")]
if len(err)==len(rows):                         print("RATE_LIMITED")
elif real and len(nodiff)==len(real):           print("NO_DIFFS")
else: print(f"OK applied={len(applied)} passed={len(passed)} "
            f"nodiff={len(nodiff)} of {len(real)} real, {len(err)} rate-limited")
PY
)
case "$verdict" in  
  RATE_LIMITED) echo "      daily quota already spent — re-run this script later; it will resume."; exit 0 ;;
  NO_DIFFS)     echo "ERROR: model returned no usable diffs. Try a different --model."; exit 1 ;;
  EMPTY)        echo "ERROR: smoke produced no rows (see output above)."; exit 1 ;;
  *)            echo "      smoke $verdict" ;;
esac

# --- 3. full unattended sweep across all repos ------------------------------
echo "[3/4] full sweep — unattended; auto-pauses & resumes on the daily cap ..."
echo "      (leave it running; Ctrl-C any time and re-run to continue)"
$PY benchmarks/downstream/auto_free_sweep.py \
  --backend gemini --model "$MODEL" --providers "$PROVIDERS" \
  --tag "$TAG" --sleep "$SLEEP" --cooldown "$COOLDOWN"

# --- 4. pooled report -------------------------------------------------------
echo "[4/4] pooled report:"
$PY benchmarks/downstream/run_eval.py --report "$RESULTS"/*."$TAG".jsonl
echo
echo "DONE. Raw rows in $RESULTS/*.$TAG.jsonl"
