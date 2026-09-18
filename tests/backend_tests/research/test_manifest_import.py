"""Focused tests for the build-manifest import seam.

These exercise the pure planner (identity derivation, digest verification,
placeholder handling) and the admin import endpoint, without a database.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.routers.analytics.auth_utils import AuthenticatedUser
from backend.routers.research.agents import (
    ReleaseImportRequest,
    import_release,
)
from research.study.agents.enums import QualificationStatus
from research.study.agents.manifest_import import (
    ManifestImportError,
    build_manifest_release,
    manifest_digest,
)

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
            "archive": "code4me-runtime/code4me-agent-macos-arm64.zip",
            "sha256": "a" * 64,
            "executable": "code4me2-agent",
        },
        {
            "runtime_id": "code4me-agent",
            "version": "pending-release",
            "platform": "linux",
            "architecture": "x64",
            "archive": "code4me-runtime/code4me-agent-linux-x64.zip",
            "sha256": "0" * 64,
            "executable": "code4me2-agent",
        },
    ],
}


def _admin() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=uuid.uuid4(), is_admin=True, email="admin@example.com", name="Admin"
    )


def _row(release):
    return SimpleNamespace(release_json=release.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Pure planner
# ---------------------------------------------------------------------------


def test_build_manifest_release_is_deterministic_and_maps_artifacts():
    plan = build_manifest_release(
        MANIFEST,
        artifact_sizes={"code4me-runtime/code4me-agent-macos-arm64.zip": 4242},
    )
    release = plan.release

    assert manifest_digest(MANIFEST).startswith("sha256:")
    assert release.agent_id == "code4me-agent"
    assert release.version == "1.2.3"
    assert release.source_manifest_digest == manifest_digest(MANIFEST)
    # The digest is folded into the release id so the same manifest resolves the
    # same release and a changed one becomes a distinct release.
    assert manifest_digest(MANIFEST)[len("sha256:") :][:12] in release.release_id
    assert release.qualification_status == QualificationStatus.UNQUALIFIED

    assert len(release.artifacts) == 1
    artifact = release.artifacts[0]
    assert (artifact.os, artifact.arch) == ("macos", "arm64")
    assert artifact.path == "code4me-runtime/code4me-agent-macos-arm64.zip"
    assert artifact.sha256 == "sha256:" + "a" * 64
    assert artifact.size == 4242
    assert artifact.executable == "code4me2-agent"

    # The all-zero placeholder platform is excluded (and reported), never
    # imported as a fake digest-pinned artifact.
    assert [item.platform for item in plan.skipped] == ["linux-x64"]

    second = build_manifest_release(
        MANIFEST,
        artifact_sizes={"code4me-runtime/code4me-agent-macos-arm64.zip": 4242},
    )
    assert second.release.release_id == release.release_id


def test_build_manifest_release_verifies_an_available_artifact(tmp_path):
    archive = tmp_path / "code4me-runtime" / "code4me-agent-macos-arm64.zip"
    archive.parent.mkdir(parents=True)
    payload = b"real-runtime-bytes"
    archive.write_bytes(payload)
    real_digest = hashlib.sha256(payload).hexdigest()

    manifest = json.loads(json.dumps(MANIFEST))
    manifest["artifacts"][0]["sha256"] = real_digest
    manifest["artifacts"] = manifest["artifacts"][:1]

    plan = build_manifest_release(manifest, artifact_root=tmp_path)
    artifact = plan.release.artifacts[0]
    assert artifact.sha256 == "sha256:" + real_digest
    assert artifact.size == len(payload)


def test_build_manifest_release_rejects_a_digest_mismatch(tmp_path):
    archive = tmp_path / "code4me-runtime" / "code4me-agent-macos-arm64.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"some-other-bytes")

    manifest = json.loads(json.dumps(MANIFEST))
    manifest["artifacts"] = manifest["artifacts"][:1]  # declared digest is "a"*64

    with pytest.raises(ManifestImportError) as error:
        build_manifest_release(manifest, artifact_root=tmp_path)
    assert error.value.code == "DIGEST_MISMATCH"


def test_build_manifest_release_rejects_a_placeholder_with_a_real_archive(tmp_path):
    archive = tmp_path / "code4me-runtime" / "code4me-agent-linux-x64.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"linux-bytes")

    manifest = json.loads(json.dumps(MANIFEST))
    manifest["artifacts"] = [manifest["artifacts"][1]]

    with pytest.raises(ManifestImportError) as error:
        build_manifest_release(manifest, artifact_root=tmp_path)
    assert error.value.code == "DIGEST_MISMATCH"


def test_build_manifest_release_requires_a_size_when_the_archive_is_missing():
    manifest = json.loads(json.dumps(MANIFEST))
    manifest["artifacts"] = manifest["artifacts"][:1]  # no size anywhere

    with pytest.raises(ManifestImportError) as error:
        build_manifest_release(manifest)
    assert error.value.code == "SIZE_MISSING"


def test_build_manifest_release_rejects_an_empty_manifest():
    with pytest.raises(ManifestImportError) as error:
        build_manifest_release({"manifest_version": 1, "artifacts": []})
    assert error.value.code == "INVALID_MANIFEST"


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


def test_import_endpoint_registers_then_is_idempotent():
    app = MagicMock()
    db = MagicMock()
    app.get_db_session.return_value = db
    payload = ReleaseImportRequest(
        manifest=MANIFEST,
        artifact_sizes={"code4me-runtime/code4me-agent-macos-arm64.zip": 4242},
    )
    release = build_manifest_release(
        MANIFEST,
        artifact_sizes={"code4me-runtime/code4me-agent-macos-arm64.zip": 4242},
    ).release
    row = _row(release)

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
        first = import_release(payload, _admin(), app)

    body = json.loads(first.body)
    assert first.status_code == 201
    assert body["created"] is True
    assert body["release"]["release_id"] == release.release_id
    assert [item["platform"] for item in body["skipped_artifacts"]] == ["linux-x64"]

    # A second import of the same manifest returns the existing release, not 409.
    with (
        patch(
            "backend.routers.research.agents.store.list_releases", return_value=[row]
        ),
        patch(
            "backend.routers.research.agents.store.get_release", return_value=row
        ),
        patch(
            "backend.routers.research.agents.store.release_summary",
            return_value={"release_id": release.release_id, "status": "UNQUALIFIED"},
        ),
    ):
        second = import_release(payload, _admin(), app)

    body = json.loads(second.body)
    assert second.status_code == 200
    assert body["created"] is False
    assert body["release"]["release_id"] == release.release_id


def test_import_endpoint_rejects_an_invalid_manifest():
    app = MagicMock()
    payload = ReleaseImportRequest(manifest={"artifacts": []})

    with pytest.raises(HTTPException) as error:
        import_release(payload, _admin(), app)

    assert error.value.status_code == 422
    assert error.value.detail["code"] == "INVALID_MANIFEST"
    app.get_db_session.assert_not_called()
