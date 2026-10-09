"""The agent verifies HTTPS backends even where the platform trust store is empty.

Regression for the packaged macOS runtime: its Python had no usable CA store,
so the grant exchange with an HTTPS backend failed with
``CERTIFICATE_VERIFY_FAILED`` and every chat reported "auth required".
"""

from __future__ import annotations

import ast
import ssl
from pathlib import Path

import code4me2_agent
from code4me2_agent import runtime_auth, telemetry, tls
from code4me2_agent.adapters import OpenAICompatibleProvider


def test_https_context_trusts_bundled_roots_when_the_platform_store_is_empty(monkeypatch):
    # What the packaged runtime's default context looks like: verifying, trusting nothing.
    monkeypatch.setattr(
        tls.ssl, "create_default_context", lambda *args, **kwargs: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    )
    tls.https_context.cache_clear()
    try:
        context = tls.https_context()
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        assert context.cert_store_stats()["x509_ca"] > 0
    finally:
        tls.https_context.cache_clear()


def test_every_agent_urlopen_call_passes_the_tls_context():
    package = Path(code4me2_agent.__file__).parent
    calls, missing = 0, []
    for path in sorted(package.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name != "urlopen":
                continue
            calls += 1
            if not any(keyword.arg == "context" for keyword in node.keywords):
                missing.append(f"{path.name}:{node.lineno}")
    assert calls >= 6
    assert missing == []


def test_telemetry_upload_uses_the_tls_context(monkeypatch):
    seen = {}

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(http_request, timeout, context=None):
        seen["url"] = http_request.full_url
        seen["context"] = context
        return _Response()

    monkeypatch.setattr(telemetry.request, "urlopen", fake_urlopen)
    telemetry.upload_event_batch_http(
        {}, [], {}, ingest_url="https://backend.example/api/agent/ingest", timeout_seconds=1.0
    )
    assert seen["url"] == "https://backend.example/api/agent/ingest"
    assert seen["context"] is tls.https_context()


def test_backend_requests_name_the_agent_instead_of_python_urllib(monkeypatch):
    # The Cloudflare edge answers urllib's default ``Python-urllib/3.x`` with 403 (error 1010).
    agents = []

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(http_request, timeout, context=None):
        agents.append(http_request.get_header("User-agent"))
        return _Response()

    monkeypatch.setattr(runtime_auth.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(telemetry.request, "urlopen", fake_urlopen)
    auth = runtime_auth.AcpBackendAuthorization(
        backend_url="https://backend.example", grant=None, acp_token="token"
    )
    auth._post("/api/acp/session/exchange", {"grant": "grant"})
    auth._get("/api/acp/agent-config")
    auth.authorized_json_request("POST", "/api/acp/runs", {})
    telemetry.upload_event_batch_http(
        {}, [], {}, ingest_url="https://backend.example/api/agent/ingest", timeout_seconds=1.0
    )
    assert tls.USER_AGENT.startswith("code4me2-agent/")
    assert agents == [tls.USER_AGENT] * 4


def test_backend_model_calls_name_the_agent_with_the_same_user_agent():
    provider = OpenAICompatibleProvider(
        kind="code4me_backend",
        base_url="https://backend.example",
        model="m",
        api_key_env="",
        timeout_seconds=5.0,
        auth_headers={"Authorization": "Bearer token"},
    )

    assert provider._headers()["User-Agent"] == tls.USER_AGENT
