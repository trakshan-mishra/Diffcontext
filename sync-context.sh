#!/bin/bash
# Make sure to replace these two lines with your actual Cloudflare URL and API Key!
CTXSYNC_KEY=c91982be3bed40799efd2b578a9088f9
CTXSYNC_URL=https://ctxsync.trakshanmishra477.workers.dev

echo "🧠 Compiling DiffContext..."
# 1. Let DiffContext calculate the exact blast radius and compile the text
CONTEXT=$(diffcontext compile --ref HEAD~1)

echo "☁️ Pushing to CtxSync Cloud..."
# 2. Push that perfectly optimized text straight to your CtxSync backend
curl -X POST "$CTXSYNC_URL/event" \
  -H "Authorization: Bearer $CTXSYNC_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "diffcontext_update",
    "project": "my-project",
    "text": '"$(jq -Rs . <<< "$CONTEXT")"'
  }'

echo "✅ Live context updated in the cloud!"
