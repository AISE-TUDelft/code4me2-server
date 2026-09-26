"""Focused tests for the verified build-manifest import seam.

These exercise the pure planner (identity derivation, basename-only archives,
digest/size verification, placeholder rejection) and the admin multipart import
endpoint, without a database.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from backend.routers.analytics.auth_utils import AuthenticatedUser
from backend.routers.research.agents import import_release
from research.study.agents.enums import QualificationStatus
from research.study.agents.manifest_import import (
    ManifestImportError,
    build_manifest_release,
    manifest_digest,
)

PAYLOAD = b"real-runtime-bytes" * 1024
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()
ARCHIVE = "code4me-agent-macos-arm64.zip"

MANIFEST = {
    "manifest_version": 1,
    "runtime_version": "1.2.3",
    "managed_protocol_version": "1",
    "server_commit": "abc1234",
    "plugin_commit": "def5678",
    "artifacts": [
        {
            "runtime_id": "code4me-agent",
            "version": "1.2.3",
            "platform": "macos",
            "architecture": "arm64",
            "archive": ARCHIVE,
            "sha256": DIGEST,
            "size": len(PAYLOAD),
            "executable": "code4me2-agent",
            "tests": {"self_check": "PASS", "acp_initialize": "PASS", "ran_at": "2026-09-21T00:00:00Z"},
        },
    ],
}


def _admin() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=uuid.uuid4(), is_admin=True, email="admin@example.com", name="Admin"
    )


def _row(release):
    return SimpleNamespace(agent_id=release.agent_id, source_manifest_digest=release.source_manifest_digest, release_json=release.model_dump(mode="json"))


def _archive(tmp_path, payload: bytes = PAYLOAD, name: str = ARCHIVE):
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def _upload(name: str, payload: bytes = PAYLOAD) -> UploadFile:
    return UploadFile(BytesIO(payload), filename=name)


def _clone(manifest: dict) -> dict:
    return json.loads(json.dumps(manifest))


# ---------------------------------------------------------------------------
# Pure planner
# ---------------------------------------------------------------------------


def test_build_manifest_release_is_deterministic_and_maps_artifacts(tmp_path):
    plan = build_manifest_release(MANIFEST, archives={ARCHIVE: _archive(tmp_path)})
    release = plan.release

    assert manifest_digest(MANIFEST).startswith("sha256:")
    assert release.agent_id == "code4me-agent"
    assert release.version == "1.2.3"
    assert release.source_manifest_digest == manifest_digest(MANIFEST)
    # The digest is folded into the release id so the same manifest resolves the
    # same release and a changed one becomes a distinct release.
    assert manifest_digest(MANIFEST)[len("sha256:") :][:12] in release.release_id
    assert release.qualification_status == QualificationStatus.QUALIFIED

    assert len(release.artifacts) == 1
    artifact = release.artifacts[0]
    assert (artifact.os, artifact.arch) == ("macos", "arm64")
    # The release identity stores the archive basename, never a resource prefix.
    assert artifact.path == ARCHIVE
    assert artifact.sha256 == "sha256:" + DIGEST
    assert artifact.size == len(PAYLOAD)
    assert artifact.executable == "code4me2-agent"

    # No adapter is invented when the manifest declares none; the manifest
    # digest must never stand in for an adapter implementation digest.
    assert release.adapter is None

    second = build_manifest_release(MANIFEST, archives={ARCHIVE: _archive(tmp_path)})
    assert second.release.release_id == release.release_id


def test_build_manifest_release_uses_an_explicit_adapter_verbatim(tmp_path):
    manifest = _clone(MANIFEST)
    manifest["adapter"] = {
        "adapter_id": "code4me-acp",
        "version": "1.4.0",
        "digest": "sha256:" + "a" * 64,
    }
    plan = build_manifest_release(manifest, archives={ARCHIVE: _archive(tmp_path)})
    assert plan.release.adapter.digest == "sha256:" + "a" * 64


def test_build_manifest_release_rejects_a_directory_prefixed_archive(tmp_path):
    manifest = _clone(MANIFEST)
    manifest["artifacts"][0]["archive"] = f"code4me-runtime/{ARCHIVE}"
    with pytest.raises(ManifestImportError) as error:
        build_manifest_release(manifest, archives={ARCHIVE: _archive(tmp_path)})
    assert error.value.code == "INVALID_MANIFEST"


def test_build_manifest_release_rejects_a_placeholder_digest(tmp_path):
    manifest = _clone(MANIFEST)
    manifest["artifacts"][0]["sha256"] = "0" * 64
    with pytest.raises(ManifestImportError) as error:
        build_manifest_release(manifest, archives={ARCHIVE: _archive(tmp_path)})
    assert error.value.code == "PLACEHOLDER_DIGEST"


def test_build_manifest_release_requires_every_declared_archive():
    with pytest.raises(ManifestImportError) as error:
        build_manifest_release(MANIFEST)
    assert error.value.code == "ARTIFACT_MISSING"


def test_build_manifest_release_rejects_an_unexpected_upload(tmp_path):
    with pytest.raises(ManifestImportError) as error:
        build_manifest_release(
            MANIFEST,
            archives={
                ARCHIVE: _archive(tmp_path),
                "code4me-agent-linux-x64.zip": _archive(
                    tmp_path, name="code4me-agent-linux-x64.zip"
                ),
            },
        )
    assert error.value.code == "UNEXPECTED_ARCHIVE"


def test_build_manifest_release_rejects_a_digest_mismatch(tmp_path):
    with pytest.raises(ManifestImportError) as error:
        build_manifest_release(MANIFEST, archives={ARCHIVE: _archive(tmp_path, b"nope")})
    assert error.value.code in {"DIGEST_MISMATCH", "SIZE_MISMATCH"}


def test_build_manifest_release_rejects_a_size_mismatch(tmp_path):
    manifest = _clone(MANIFEST)
    manifest["artifacts"][0]["size"] = len(PAYLOAD) + 1
    with pytest.raises(ManifestImportError) as error:
        build_manifest_release(manifest, archives={ARCHIVE: _archive(tmp_path)})
    assert error.value.code == "SIZE_MISMATCH"


def test_build_manifest_release_rejects_a_duplicate_archive(tmp_path):
    manifest = _clone(MANIFEST)
    manifest["artifacts"].append(dict(manifest["artifacts"][0]))
    with pytest.raises(ManifestImportError) as error:
        build_manifest_release(manifest, archives={ARCHIVE: _archive(tmp_path)})
    assert error.value.code == "INVALID_MANIFEST"


def test_build_manifest_release_rejects_an_empty_manifest():
    with pytest.raises(ManifestImportError) as error:
        build_manifest_release({"manifest_version": 1, "artifacts": []})
    assert error.value.code == "INVALID_MANIFEST"


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


def test_import_endpoint_registers_then_is_idempotent(tmp_path):
    app = MagicMock()
    db = MagicMock()
    app.get_db_session.return_value = db
    db.get.return_value = None
    release = build_manifest_release(
        MANIFEST, archives={ARCHIVE: _archive(tmp_path)}
    ).release
    row = _row(release)
    manifest_json = json.dumps(MANIFEST)

    with (
        patch("backend.routers.research.agents.store.list_releases", return_value=[]),
        patch(
            "backend.routers.research.agents.store.upsert_release",
            return_value=row,
        ),
        patch(
            "backend.routers.research.agents.store.release_summary",
            return_value={"release_id": release.release_id, "status": "UNQUALIFIED"},
        ),
    ):
        first = asyncio.run(
            import_release(
                manifest=manifest_json,
                archives=[_upload(ARCHIVE)],
                current_user=_admin(),
                app=app,
            )
        )

    body = json.loads(first.body)
    assert first.status_code == 201
    assert body["created"] is True
    assert body["release"]["release_id"] == release.release_id
    assert [item["archive"] for item in body["verified_artifacts"]] == [ARCHIVE]

    # A second import of the same manifest returns the existing release, not 409.
    with (
        patch(
            "backend.routers.research.agents.store.upsert_release", return_value=row
        ),
        patch(
            "backend.routers.research.agents.store.get_release", return_value=row
        ),
        patch(
            "backend.routers.research.agents.store.release_summary",
            return_value={"release_id": release.release_id, "status": "UNQUALIFIED"},
        ),
    ):
        second = asyncio.run(
            import_release(
                manifest=manifest_json,
                archives=[_upload(ARCHIVE)],
                current_user=_admin(),
                app=app,
            )
        )

    body = json.loads(second.body)
    assert second.status_code == 200
    assert body["created"] is False
    assert body["release"]["release_id"] == release.release_id


def test_import_endpoint_rejects_an_invalid_manifest():
    app = MagicMock()
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            import_release(
                manifest="{ not json",
                archives=[],
                current_user=_admin(),
                app=app,
            )
        )
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "INVALID_MANIFEST"
    app.get_db_session.assert_not_called()


def test_import_endpoint_rejects_an_import_without_bytes():
    app = MagicMock()
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            import_release(
                manifest=json.dumps(MANIFEST),
                archives=[],
                current_user=_admin(),
                app=app,
            )
        )
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "ARTIFACT_MISSING"
    app.get_db_session.assert_not_called()


def test_import_endpoint_rejects_a_duplicate_upload(tmp_path):
    app = MagicMock()
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            import_release(
                manifest=json.dumps(MANIFEST),
                archives=[_upload(ARCHIVE), _upload(ARCHIVE)],
                current_user=_admin(),
                app=app,
            )
        )
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "DUPLICATE_ARCHIVE"
    app.get_db_session.assert_not_called()


def test_import_endpoint_rejects_a_wrong_digest(tmp_path):
    app = MagicMock()
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            import_release(
                manifest=json.dumps(MANIFEST),
                archives=[_upload(ARCHIVE, b"tampered-bytes")],
                current_user=_admin(),
                app=app,
            )
        )
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "DIGEST_MISMATCH"
    app.get_db_session.assert_not_called()


def test_import_endpoint_enforces_the_per_archive_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("RESEARCH_IMPORT_MAX_ARCHIVE_BYTES", "16")
    app = MagicMock()
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            import_release(
                manifest=json.dumps(MANIFEST),
                archives=[_upload(ARCHIVE)],
                current_user=_admin(),
                app=app,
            )
        )
    assert error.value.status_code == 413
    assert error.value.detail["code"] == "ARCHIVE_TOO_LARGE"


@pytest.mark.parametrize("tests", [None, {}, {"self_check": "PASS"}, {"self_check": "FAIL", "acp_initialize": "PASS", "ran_at": "2026-09-21T00:00:00Z"}])
def test_import_rejects_missing_or_failed_platform_tests(tmp_path, tests):
    manifest = _clone(MANIFEST)
    manifest["artifacts"][0]["tests"] = tests
    with pytest.raises(ManifestImportError) as error:
        build_manifest_release(manifest, archives={ARCHIVE: _archive(tmp_path)})
    assert error.value.code == "TESTS_NOT_PASSED"


def test_manifest_binds_byoa_command_version_adapter_and_tests(tmp_path):
    manifest = _clone(MANIFEST)
    manifest["agents"] = [{
        "framework": "codex", "version": "1.2.3", "agent_command": "codex-acp",
        "adapter": {"adapter_id": "codex-acp", "version": "0.1.0"},
        "tests": [dict(MANIFEST["artifacts"][0]["tests"], os="macos", arch="aarch64")],
    }]
    plan = build_manifest_release(manifest, archives={ARCHIVE: _archive(tmp_path)})
    external = plan.byoa_releases[0]
    assert external.is_byoa
    assert external.artifacts == []
    assert external.agent_command == "codex-acp"
    assert external.version == "1.2.3"
    assert external.tests[0].arch == "arm64"
    manifest["agents"][0]["version"] = "1.2.4"
    changed = build_manifest_release(manifest, archives={ARCHIVE: _archive(tmp_path)})
    assert changed.byoa_releases[0].digest_identity != external.digest_identity
    manifest["agents"][0]["tests"] = []
    with pytest.raises(ManifestImportError):
        build_manifest_release(manifest, archives={ARCHIVE: _archive(tmp_path)})


def test_import_rolls_back_all_releases_on_database_failure(tmp_path):
    from sqlalchemy.exc import IntegrityError
    manifest = _clone(MANIFEST)
    manifest["agents"] = [{"framework": "goose", "version": "1.0.0", "agent_command": "goose",
        "adapter": {"adapter_id": "goose", "version": "1"},
        "byoa_config": [
            {"field": "model", "transport": "env", "key": "GOOSE_MODEL"},
            {"field": "max_steps", "transport": "env", "key": "GOOSE_MAX_TURNS"},
            {"field": "approval_policy", "transport": "env", "key": "GOOSE_MODE"},
            {"field": "inference_gateway_host", "transport": "env", "key": "OPENAI_HOST"},
            {"field": "inference_gateway_base_path", "transport": "env", "key": "OPENAI_BASE_PATH"},
            {"field": "inference_gateway_credential", "transport": "env", "key": "OPENAI_API_KEY"},
            {"field": "provider_kind", "transport": "env", "key": "GOOSE_PROVIDER", "value_map": {"openai_compatible": "openai"}},
            {"field": "state_dir", "transport": "env", "key": "GOOSE_PATH_ROOT"},
        ],
        "tests": [dict(MANIFEST["artifacts"][0]["tests"], os="macos", arch="arm64")]}]
    app = MagicMock()
    db = app.get_db_session.return_value
    db.get.return_value = None
    with patch("backend.routers.research.agents.store.upsert_release", side_effect=[MagicMock(), IntegrityError("insert", {}, Exception())]) as upsert:
        with pytest.raises(HTTPException) as error:
            asyncio.run(import_release(json.dumps(manifest), [_upload(ARCHIVE)], _admin(), app))
    assert error.value.status_code == 409
    assert upsert.call_count == 2
    assert all(call.kwargs == {"commit": False} for call in upsert.call_args_list)
    db.commit.assert_not_called()
    db.rollback.assert_called_once()


@pytest.mark.parametrize("failure,code", [(None, None), ("digest", "DIGEST_MISMATCH"), ("redirect", "UNTRUSTED_RELEASE_URL"), ("duplicate", "DUPLICATE_ARCHIVE"), ("size", "IMPORT_TOO_LARGE")])
def test_url_import_uses_the_same_byte_checks(monkeypatch, failure, code):
    import httpx
    from functools import partial
    from backend.routers.research.agents import ReleaseUrlImport, import_release_url
    base = "https://github.com/org/repo/releases/download/v1/"
    seen = []

    def respond(request):
        seen.append(str(request.url))
        if request.url.path.endswith("manifest.json"):
            if failure == "redirect":
                return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
            return httpx.Response(200, json=MANIFEST)
        return httpx.Response(200, content=b"tampered" if failure == "digest" else PAYLOAD)

    if failure == "size":
        monkeypatch.setenv("RESEARCH_IMPORT_MAX_ARCHIVE_BYTES", "16")
    app = MagicMock()
    app.get_db_session.return_value.get.return_value = None
    urls = [base + ARCHIVE] * (2 if failure == "duplicate" else 1)
    with patch("backend.routers.research.agents.httpx.AsyncClient", partial(httpx.AsyncClient, transport=httpx.MockTransport(respond))), patch("backend.routers.research.agents.store.release_summary", return_value={"status": "QUALIFIED"}):
        call = import_release_url(ReleaseUrlImport(manifest_url=base + "manifest.json", archive_urls=urls), _admin(), app)
        if code:
            with pytest.raises(HTTPException) as error:
                asyncio.run(call)
            assert error.value.detail["code"] == code
            app.get_db_session.assert_not_called()
        else:
            result = asyncio.run(call)
            assert result.status_code == 201
            app.get_db_session.return_value.commit.assert_called_once()
    assert all(url.startswith(base) for url in seen)


def test_disable_and_import_are_admin_only():
    from backend.routers.research.agents import disable_release
    user = AuthenticatedUser(user_id=uuid.uuid4(), is_admin=False, email="participant@example.com", name="Participant")
    app = MagicMock()
    with pytest.raises(HTTPException) as error:
        disable_release("release", user, app)
    assert error.value.status_code == 403
    app.get_db_session.assert_not_called()


GOOSE_GATEWAY_BINDINGS = [
    {"field": "model", "transport": "env", "key": "GOOSE_MODEL"},
    {"field": "max_steps", "transport": "env", "key": "GOOSE_MAX_TURNS"},
    {"field": "approval_policy", "transport": "env", "key": "GOOSE_MODE"},
    {"field": "inference_gateway_host", "transport": "env", "key": "OPENAI_HOST"},
    {"field": "inference_gateway_base_path", "transport": "env", "key": "OPENAI_BASE_PATH"},
    {"field": "inference_gateway_credential", "transport": "env", "key": "OPENAI_API_KEY"},
    {"field": "provider_kind", "transport": "env", "key": "GOOSE_PROVIDER", "value_map": {"openai_compatible": "openai"}},
    {"field": "state_dir", "transport": "env", "key": "GOOSE_PATH_ROOT"},
]


def _goose_agent(bindings):
    return {
        "framework": "goose", "version": "1.51.0", "agent_command": "goose",
        "adapter": {"adapter_id": "goose", "version": "1"},
        "byoa_config": bindings,
        "tests": [dict(MANIFEST["artifacts"][0]["tests"], os="macos", arch="arm64")],
    }


def test_import_binds_goose_gateway_bindings(tmp_path):
    manifest = _clone(MANIFEST)
    manifest["agents"] = [_goose_agent(GOOSE_GATEWAY_BINDINGS)]
    plan = build_manifest_release(manifest, archives={ARCHIVE: _archive(tmp_path)})
    goose = plan.byoa_releases[0]
    fields = {binding.field: binding for binding in goose.byoa_config}
    assert fields["inference_gateway_credential"].transport == "env"
    assert fields["provider_kind"].value_map == {"openai_compatible": "openai"}
    assert fields["inference_gateway_host"].key == "OPENAI_HOST"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda bindings: [b for b in bindings if b["field"] != "inference_gateway_credential"],
        lambda bindings: [dict(b, transport="arg") if b["field"] == "inference_gateway_credential" else b for b in bindings],
        lambda bindings: [dict(b, value_map={}) if b["field"] == "provider_kind" else b for b in bindings],
        lambda bindings: [b for b in bindings if b["field"] != "state_dir"],
    ],
)
def test_import_rejects_goose_agent_without_gateway_bindings(tmp_path, mutate):
    manifest = _clone(MANIFEST)
    manifest["agents"] = [_goose_agent(mutate(GOOSE_GATEWAY_BINDINGS))]
    with pytest.raises(ManifestImportError) as error:
        build_manifest_release(manifest, archives={ARCHIVE: _archive(tmp_path)})
    assert error.value.code == "INVALID_MANIFEST"
    assert "inference gateway" in str(error.value) or "credential" in str(error.value)


def test_import_does_not_require_gateway_bindings_for_codex(tmp_path):
    manifest = _clone(MANIFEST)
    manifest["agents"] = [{
        "framework": "codex", "version": "1.2.3", "agent_command": "codex-acp",
        "adapter": {"adapter_id": "codex-acp", "version": "0.1.0"},
        "tests": [dict(MANIFEST["artifacts"][0]["tests"], os="macos", arch="aarch64")],
    }]
    plan = build_manifest_release(manifest, archives={ARCHIVE: _archive(tmp_path)})
    assert plan.byoa_releases[0].agent_id == "codex"
