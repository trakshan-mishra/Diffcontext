#!/usr/bin/env bash
# demos.sh — Try DiffContext on YOUR repo or pick a famous open-source project
# Usage: bash demos.sh [your-repo-path]
#
# No arguments? Pick from the menu. Pass a path? We'll analyze it.

set -e

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
DIM='\033[2m'
BOLD='\033[1m'
RESET='\033[0m'

banner() {
    echo ""
    echo -e "${BOLD}════════════════════════════════════════════════════${RESET}"
    echo -e "${BOLD}  DiffContext — Interactive Demo${RESET}"
    echo -e "${DIM}  Static-analysis-powered context compiler for LLMs${RESET}"
    echo -e "${BOLD}════════════════════════════════════════════════════${RESET}"
    echo ""
}

# Check diffcontext is installed
if ! command -v diffcontext &>/dev/null; then
    echo "❌ diffcontext not found. Install first:"
    echo "   git clone https://github.com/trakshan-mishra/Diffcontext.git"
    echo "   cd Diffcontext && pip install -e ."
    exit 1
fi

# ─── If user passed a repo path, use that ───────────────────────────
if [ -n "$1" ]; then
    REPO_PATH="$1"
    if [ ! -d "$REPO_PATH" ]; then
        echo "❌ Directory not found: $REPO_PATH"
        exit 1
    fi

    banner
    echo -e "${CYAN}Using your repo:${RESET} $REPO_PATH"
    echo ""

    echo -e "${YELLOW}Step 1: Indexing...${RESET}"
    diffcontext index "$REPO_PATH"

    echo ""
    echo -e "${YELLOW}Step 2: Finding symbols...${RESET}"
    echo -e "${DIM}Top functions in your repo:${RESET}"
    echo ""
    grep -rn "^def \|^    def " "$REPO_PATH" --include="*.py" 2>/dev/null | head -15 | while read -r line; do
        echo "  $line"
    done

    echo ""
    echo -e "${GREEN}Now try:${RESET}"
    echo "  diffcontext blast --changed ./file.py:function_name --repo $REPO_PATH"
    echo "  diffcontext compile --changed ./file.py:function_name --repo $REPO_PATH --max-tokens 4000"
    echo ""
    exit 0
fi

# ─── Interactive menu ───────────────────────────────────────────────
banner

echo "Pick a repo to demo (or pass your own path: bash demos.sh /path/to/repo)"
echo ""
echo -e "  ${BOLD}1${RESET}) openai/whisper     — Speech-to-text (153 symbols)"
echo -e "  ${BOLD}2${RESET}) pallets/click      — CLI framework (506 symbols)"
echo -e "  ${BOLD}3${RESET}) psf/requests       — HTTP library (everyone knows this)"
echo -e "  ${BOLD}4${RESET}) pallets/flask       — Web framework"
echo -e "  ${BOLD}5${RESET}) encode/httpx        — Async HTTP client"
echo ""
read -rp "Choice [1-5]: " choice

# Config for each repo
case "$choice" in
    1)
        REPO_URL="https://github.com/openai/whisper.git"
        REPO_DIR="/tmp/diffcontext_demo_whisper"
        CODE_ROOT="whisper"
        DEMO_FUNC="./transcribe.py:transcribe"
        REPO_NAME="openai/whisper"
        ;;
    2)
        REPO_URL="https://github.com/pallets/click.git"
        REPO_DIR="/tmp/diffcontext_demo_click"
        CODE_ROOT="src/click"
        DEMO_FUNC="./core.py:Command.main"
        REPO_NAME="pallets/click"
        ;;
    3)
        REPO_URL="https://github.com/psf/requests.git"
        REPO_DIR="/tmp/diffcontext_demo_requests"
        CODE_ROOT="src/requests"
        DEMO_FUNC="./api.py:request"
        REPO_NAME="psf/requests"
        ;;
    4)
        REPO_URL="https://github.com/pallets/flask.git"
        REPO_DIR="/tmp/diffcontext_demo_flask"
        CODE_ROOT="src/flask"
        DEMO_FUNC="./app.py:Flask.wsgi_app"
        REPO_NAME="pallets/flask"
        ;;
    5)
        REPO_URL="https://github.com/encode/httpx.git"
        REPO_DIR="/tmp/diffcontext_demo_httpx"
        CODE_ROOT="httpx"
        DEMO_FUNC="./_api.py:request"
        REPO_NAME="encode/httpx"
        ;;
    *)
        echo "Invalid choice. Run: bash demos.sh /path/to/your/repo"
        exit 1
        ;;
esac

FULL_ROOT="$REPO_DIR/$CODE_ROOT"

echo ""
echo -e "${CYAN}━━━ Step 1: Clone ━━━${RESET}"
if [ ! -d "$REPO_DIR" ]; then
    echo "Cloning $REPO_NAME..."
    git clone --depth=10 "$REPO_URL" "$REPO_DIR" --quiet
    echo -e "  ${GREEN}✓${RESET} Cloned to $REPO_DIR"
else
    echo -e "  ${GREEN}✓${RESET} Already cloned, reusing"
fi

echo ""
echo -e "${CYAN}━━━ Step 2: Index ━━━${RESET}"
diffcontext index "$FULL_ROOT"

echo ""
echo -e "${CYAN}━━━ Step 3: Blast Radius ━━━${RESET}"
echo -e "  Analyzing: ${BOLD}$DEMO_FUNC${RESET}"
echo ""
diffcontext blast \
    --changed "$DEMO_FUNC" \
    --repo "$FULL_ROOT" \
    --depth 2

echo ""
echo -e "${CYAN}━━━ Step 4: Compile for LLM ━━━${RESET}"
echo -e "  ${DIM}(showing function list + stats — full source goes to your LLM)${RESET}"
echo ""

COMPILED=$(diffcontext compile \
    --changed "$DEMO_FUNC" \
    --repo "$FULL_ROOT" \
    --max-tokens 4000)

echo "$COMPILED" | grep -E "^(FILE:|FUNCTION:|CHANGED SYMBOL|=== |--- Stats ---|Symbols |Tokens |Reduction)"

echo ""
echo -e "${GREEN}════════════════════════════════════════════════════${RESET}"
echo -e "${GREEN}  ✓ Done! That's what you'd paste into your LLM.${RESET}"
echo -e "${GREEN}════════════════════════════════════════════════════${RESET}"
echo ""
echo "Try another function in this repo:"
echo -e "  diffcontext blast --changed ${YELLOW}<symbol_id>${RESET} --repo $FULL_ROOT"
echo ""
echo "Try on YOUR repo:"
echo -e "  bash demos.sh ${YELLOW}/path/to/your/python/project${RESET}"
echo ""
echo "Find symbol IDs with:"
echo -e "  grep -rn \"^def \\|^    def \" $FULL_ROOT --include=\"*.py\" | head -20"
echo ""
