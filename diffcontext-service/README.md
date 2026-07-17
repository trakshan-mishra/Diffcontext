# DiffContext Web Service — Complete Setup Guide

## What you just built

```
diffcontext-service/
├── backend/
│   └── main.py          ← FastAPI REST API server
├── frontend/
│   └── index.html       ← Web UI (open in browser, no server needed)
└── README.md            ← This file
```

---

## Option A — Web UI (easiest, for beginners)

### Step 1: Install dependencies
```bash
pip install fastapi uvicorn python-multipart aiofiles
```

### Step 2: Start the API server
```bash
cd /path/to/Diffcontext
uvicorn diffcontext-service.backend.main:app --reload --port 8000
```

### Step 3: Open the web UI
Just open `diffcontext-service/frontend/index.html` in your browser.
(Double-click it, or drag it into Chrome/Firefox.)

### Step 4: Use it
1. Zip your Python project folder
2. Upload the zip on the "Upload Project" tab
3. Pick a function from the list
4. Click "Analyse Blast Radius"
5. See what breaks!

---

## Option B — REST API (for developers)

Once the server is running at `http://localhost:8000`:

```python
import requests

# 1. Upload your project
with open("myproject.zip", "rb") as f:
    r = requests.post("http://localhost:8000/upload", files={"file": f})
repo_id = r.json()["repo_id"]

# 2. List all functions
r = requests.get(f"http://localhost:8000/symbols?repo_id={repo_id}")
print(r.json()["symbols"][:5])

# 3. Get blast radius
r = requests.post("http://localhost:8000/blast", json={
    "repo_id": repo_id,
    "symbol": "./src/auth.py:validate_jwt"
})
print(r.json())

# 4. Get LLM context to paste into Claude
r = requests.post("http://localhost:8000/compile", json={
    "repo_id": repo_id,
    "symbol": "./src/auth.py:validate_jwt",
    "max_tokens": 8000
})
print(r.json()["context_text"])
```

### Or use curl:
```bash
# Upload
curl -X POST http://localhost:8000/upload -F "file=@myproject.zip"

# Blast radius
curl -X POST http://localhost:8000/blast \
  -H "Content-Type: application/json" \
  -d '{"repo_id": "abc123", "symbol": "./main.py:my_function"}'

# Paste code directly (no upload)
curl -X POST http://localhost:8000/inline \
  -H "Content-Type: application/json" \
  -d '{
    "files": {"main.py": "def greet(name):\n  msg = build_message(name)\n  print(msg)\n\ndef build_message(name):\n  return f\"Hello {name}\""},
    "symbol": "./main.py:greet"
  }'
```

Interactive docs: http://localhost:8000/docs

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Connection refused` on localhost:8000 | Run the uvicorn command first |
| `No Python files found` | Make sure your zip contains .py files |
| Symbol not found | Use format `./filename.py:function_name` (no parentheses) |

---

## API response format

```json
{
  "symbol": "./service.py:onboard_user",
  "direct_callers": ["./api.py:handle_signup"],
  "direct_callees": ["./service.py:create_user", "./service.py:create_order"],
  "blast_radius_count": 5,
  "blast_radius_by_file": {
    "./api.py": ["handle_signup", "process_request"],
    "./tests/test_service.py": ["test_onboard"]
  },
  "all_affected": ["./api.py:handle_signup", "..."],
  "total_symbols_in_repo": 47
}
```

---

## Security notes for self-hosters

The `/clone` endpoint accepts arbitrary git URLs, which makes it a real
attack surface anywhere the service can reach things you care about. The
backend enforces a floor of protections; know what they do and do not cover
before deploying outside a sandboxed environment.

**What the code enforces:**

- **Private-address rejection.** The hostname in a clone URL is resolved
  before any git process runs; if any resolved address is loopback,
  link-local, RFC1918-private, reserved, multicast, or unspecified, the
  request is rejected with 400. This blocks the obvious SSRF-adjacent
  cases (internal git servers, `localhost`, cloud metadata IPs like
  `169.254.169.254`).
- **Post-clone size cap.** After a successful `--depth=1` clone, the
  on-disk size is measured and clones over the limit are deleted and
  rejected with 413. Default 500MB; configure with
  `DIFFCONTEXT_MAX_CLONE_MB`. (`--depth=1` bounds history, not blob size —
  a single-commit repo with huge files still transfers fully, which is why
  the check runs post-clone rather than pretending to predict size.)
- **Per-IP rate limiting on `/clone`.** Default 5 clones per 10 minutes
  per client IP; configure with `DIFFCONTEXT_CLONE_RATE_LIMIT` and
  `DIFFCONTEXT_CLONE_RATE_WINDOW_S`. The limiter is in-memory and
  per-process: correct for a single-instance deployment, useless across
  replicas. A multi-instance deployment needs a shared store (e.g. Redis)
  behind the same seam (`_RateLimiter` in `backend/main.py`).

**What is NOT covered — add these yourself if you deploy publicly:**

- **No authentication.** Every endpoint is open. Anyone who can reach the
  service can clone repos onto your disk and burn CPU indexing them. Put
  the service behind an auth proxy or add a token check before exposing it
  beyond localhost / a sandboxed Space.
- **DNS rebinding.** The private-address check resolves the hostname once,
  before cloning; a resolver that returns a public address to the check and
  a private one to git afterwards defeats it. Closing this requires pinning
  the resolved IP for git's actual connection, which git doesn't expose.
- **Rate limiting trusts the socket peer address.** Behind a reverse proxy,
  every request appears to come from the proxy's IP; the limiter then
  throttles all users collectively rather than per-user. Terminate this at
  the proxy layer (or extend the limiter to read a trusted
  `X-Forwarded-For`).
- **Disk cleanup relies on the size check and container recycling.**
  Accepted clones accumulate in the temp dir for the life of the instance.
