#!/usr/bin/env bash
# demo.sh — run DiffContext against a real repo in under 2 minutes
# No setup beyond: pip install -e . (already done if you're reading this)

set -e

DEMO_REPO="https://github.com/openai/whisper.git"
DEMO_DIR="/tmp/diffcontext_demo_whisper"
DEMO_ROOT="$DEMO_DIR/whisper"                  # where the Python code lives
DEMO_FUNCTION="./transcribe.py:transcribe"      # the core function — everyone knows this one

echo ""
echo "════════════════════════════════════════════════"
echo "  DiffContext — Live Demo"
echo "  repo: openai/whisper"
echo "════════════════════════════════════════════════"
echo ""
echo "Step 1: Cloning a real Python repo (openai/whisper)..."
if [ ! -d "$DEMO_DIR" ]; then
    git clone --depth=10 "$DEMO_REPO" "$DEMO_DIR" --quiet
    echo "  ✓ Cloned to $DEMO_DIR"
else
    echo "  ✓ Already cloned, reusing"
fi

echo ""
echo "Step 2: Indexing the repository..."
diffcontext index "$DEMO_ROOT"

echo ""
echo "Step 3: Blast radius for transcribe()"
echo "  (who calls this? what does it call?)"
echo ""
diffcontext blast \
    --changed "$DEMO_FUNCTION" \
    --repo "$DEMO_ROOT" \
    --depth 2

echo ""
echo "Step 4: Compiling LLM-ready context (token budget: 4000)"
echo "  (showing function list + stats — full source goes to your LLM)"
echo ""

# Capture full compile output
COMPILED=$(diffcontext compile \
    --changed "$DEMO_FUNCTION" \
    --repo "$DEMO_ROOT" \
    --max-tokens 4000)

# Show just function headers and stats (not 600 lines of source code)
echo "$COMPILED" | grep -E "^(FILE:|FUNCTION:|CHANGED SYMBOL|=== |--- Stats ---|Symbols |Tokens |Reduction)"

echo ""
echo "════════════════════════════════════════════════"
echo "  That compiled context is what you paste"
echo "  into Claude / ChatGPT — not the whole repo."
echo "  (17 functions with full source code)"
echo "════════════════════════════════════════════════"
echo ""
echo "Try it on YOUR repo:"
echo "  diffcontext blast --changed ./path/to/file.py:function_name"
echo "  diffcontext compile --changed ./path/to/file.py:function_name --max-tokens 4000"
echo ""