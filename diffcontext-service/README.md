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
