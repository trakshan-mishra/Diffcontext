"""
tests/test_service_clone.py — hardening tests for the web service's /clone.

Covers the three protections added after the external audit flagged /clone
as an unmitigated SSRF/DoS surface:
  1. hostnames resolving to private/loopback/link-local addresses are
     rejected BEFORE any git process runs,
  2. clones over the configurable size cap are rejected and cleaned up,
  3. per-IP rate limiting on repeated clone calls,
plus a mocked end-to-end pass showing a legitimate public GitHub URL still
flows through validation -> clone -> size check -> session creation. The
end-to-end case mocks the git subprocess (tests must not depend on network);
the real-network path is exercised by deploying and calling /clone once.

The service is optional (FastAPI isn't a core dependency), so the whole
module skips when fastapi isn't installed.
"""

import os
import socket
import sys

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "diffcontext-service", "backend")
)
import main as service_main  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Isolated session DB per test; generous rate limit unless a test lowers it.
    monkeypatch.setattr(service_main, "DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setattr(
        service_main, "_clone_limiter", service_main._RateLimiter(100, 600)
    )
    return TestClient(service_main.app)


def _addrinfo(ip):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]


class TestPrivateHostRejection:
    @pytest.mark.parametrize("ip", ["10.0.0.5", "192.168.1.1", "172.16.0.9",
                                    "127.0.0.1", "169.254.169.254", "0.0.0.0"])
    def test_private_resolving_hostname_rejected(self, client, monkeypatch, ip):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _addrinfo(ip))
        r = client.post("/clone", json={"git_url": "https://internal.example/repo.git"})
        assert r.status_code == 400
        assert "private/internal" in r.json()["detail"]

    def test_mixed_public_and_private_rejected(self, client, monkeypatch):
        # One private record among public ones is enough to reject.
        monkeypatch.setattr(
            socket, "getaddrinfo",
            lambda *a, **k: _addrinfo("140.82.112.3") + _addrinfo("10.0.0.5"),
        )
        r = client.post("/clone", json={"git_url": "https://sneaky.example/repo.git"})
        assert r.status_code == 400

    def test_git_at_syntax_host_extracted(self, client, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _addrinfo("127.0.0.1"))
        r = client.post("/clone", json={"git_url": "git@localhost:owner/repo.git"})
        assert r.status_code == 400

    def test_unresolvable_hostname_rejected(self, client, monkeypatch):
        def boom(*a, **k):
            raise socket.gaierror("no such host")
        monkeypatch.setattr(socket, "getaddrinfo", boom)
        r = client.post("/clone", json={"git_url": "https://nope.invalid/repo.git"})
        assert r.status_code == 400
        assert "does not resolve" in r.json()["detail"]

    def test_bad_scheme_rejected(self, client):
        r = client.post("/clone", json={"git_url": "file:///etc/passwd"})
        assert r.status_code == 400


def _fake_git_clone(repo_content_bytes=200):
    """subprocess.run replacement that fakes a successful shallow clone."""
    class FakeResult:
        returncode = 0
        stderr = ""

    def run(cmd, **kwargs):
        assert cmd[:3] == ["git", "clone", "--depth=1"]
        repo_path = cmd[-1]
        os.makedirs(repo_path)
        with open(os.path.join(repo_path, "app.py"), "w") as f:
            f.write("def handler():\n    return 1\n")
            f.write("#" * repo_content_bytes)
        return FakeResult()

    return run


class TestSizeCap:
    def test_oversized_clone_rejected_and_cleaned(self, client, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _addrinfo("140.82.112.3"))
        monkeypatch.setattr(service_main.subprocess, "run", _fake_git_clone(4096))
        # Simulate the cap being smaller than the cloned content.
        monkeypatch.setattr(service_main, "MAX_CLONE_MB", 0)
        created = []
        real_mkdtemp = service_main.tempfile.mkdtemp
        monkeypatch.setattr(
            service_main.tempfile, "mkdtemp",
            lambda **k: created.append(real_mkdtemp(**k)) or created[-1],
        )
        r = client.post("/clone", json={"git_url": "https://github.com/x/big.git"})
        assert r.status_code == 413
        assert "over the" in r.json()["detail"]
        assert created and not os.path.exists(created[0]), "oversized clone not cleaned up"

    def test_within_cap_end_to_end(self, client, monkeypatch):
        """Legitimate public URL passes validation, clone, size check, and
        creates a queryable session (git subprocess mocked, no network)."""
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _addrinfo("140.82.112.3"))
        monkeypatch.setattr(service_main.subprocess, "run", _fake_git_clone())
        r = client.post("/clone", json={"git_url": "https://github.com/pallets/flask.git"})
        assert r.status_code == 200, r.json()
        body = r.json()
        assert body["symbol_count"] == 1
        assert body["name"] == "flask"
        # The session is queryable afterwards
        r2 = client.get("/symbols", params={"repo_id": body["repo_id"]})
        assert r2.status_code == 200
        assert r2.json()["count"] == 1


class TestRateLimit:
    def test_per_ip_limit_enforced(self, client, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _addrinfo("140.82.112.3"))
        monkeypatch.setattr(service_main.subprocess, "run", _fake_git_clone())
        monkeypatch.setattr(service_main, "_clone_limiter", service_main._RateLimiter(2, 600))
        ok1 = client.post("/clone", json={"git_url": "https://github.com/x/a.git"})
        ok2 = client.post("/clone", json={"git_url": "https://github.com/x/b.git"})
        blocked = client.post("/clone", json={"git_url": "https://github.com/x/c.git"})
        assert ok1.status_code == 200
        assert ok2.status_code == 200
        assert blocked.status_code == 429
        assert "Rate limit" in blocked.json()["detail"]

    def test_limiter_window_expiry(self):
        rl = service_main._RateLimiter(1, window_s=0)
        assert rl.allow("1.2.3.4")
        # window of 0s: the previous hit is already expired
        assert rl.allow("1.2.3.4")
