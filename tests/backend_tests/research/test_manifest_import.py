"""Focused tests for the recipe import seam.

These exercise the pure planner (identity derivation, byte verification, recipe
self-check) and both admin import endpoints (local multipart and deployed URLs),
without a database.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.routers.analytics.auth_utils import AuthenticatedUser
from backend.routers.research import agents as agents_router
from research.study.agents.enums import DistributionMode, QualificationStatus
from research.study.agents.manifest_import import (
    ManifestImportError,
    build_manifest_releases,
    manifest_digest,
)

MACOS_BYTES = b"macos-runtime-zip-bytes"
LINUX_BYTES = b"linux-runtime-zip-bytes"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _recipe() -> dict:
    return {
        "manifest_version": 1,
        "runtime_version": "1.2.3",
        "managed_protocol_version": "1",
        "plugin_version": "2.0.0",
        "plugin_commit": "b" * 40,
        "server_commit": "a" * 40,
        "adapter": {
            "adapter_id": "code4me-acp",
            "version": "1.0.0",
            "digest": "sha256:" + "d" * 64,
        },
        "tests": {
            "status": "PASS",
            "approval_options": ["auto", "per_step"],
            "cases": [{"case_id": "acp.initialize", "status": "PASS"}],
        },
        "artifacts": [
            {
                "runtime_id": "code4me-agent",
                "version": "1.2.3",
                "platform": "macos",
                "architecture": "arm64",
                "archive": "code4me-agent-macos-arm64.zip",
                "sha256": _digest(MACOS_BYTES),
                "size": len(MACOS_BYTES),
                "executable": "code4me2-agent",
            },
            {
                "runtime_id": "code4me-agent",
                "version": "1.2.3",
                "platform": "linux",
                "architecture": "x64",
                "archive": "code4me-agent-linux-x64.zip",
                "sha256": _digest(LINUX_BYTES),
                "size": len(LINUX_BYTES),
                "executable": "code4me2-agent",
            },
        ],
        "agents": [
            {
                "framework": "goose",
                "version": "1.0.0",
                "agent_command": "goose",
                "adapter": {
                    "adapter_id": "goose-adapter",
                    "version": "1.0.0",
                    "digest": "sha256:" + "e" * 64,
                },
            }
        ],
    }


def _verified() -> dict[str, tuple[str, int]]:
    return {
        "code4me-agent-macos-arm64.zip": (_digest(MACOS_BYTES), len(MACOS_BYTES)),
        "code4me-agent-linux-x64.zip": (_digest(LINUX_BYTES), len(LINUX_BYTES)),
    }


def _admin() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=uuid.uuid4(), is_admin=True, email="admin@example.com", name="Admin"
    )


def _row(release):
    return SimpleNamespace(
        release_id=release.release_id,
        agent_id=release.agent_id,
        source_manifest_digest=release.source_manifest_digest,
        status=release.qualification_status.value,
        release_json=release.model_dump(mode="json"),
        created_at=None,
    )


class _Upload:
    """A minimal async UploadFile stand-in."""

    def __init__(self, filename: str, data: bytes):
        self.filename = filename
        self._data = data

    async def read(self, _size: int = -1) -> bytes:
        data, self._data = self._data, b""
        return data


# ---------------------------------------------------------------------------
# Pure planner
# ---------------------------------------------------------------------------


def test_build_manifest_releases_is_deterministic_and_verifies_bytes():
    recipe = _recipe()
    plan = build_manifest_releases(recipe, verified=_verified())

    assert manifest_digest(recipe).startswith("sha256:")
    managed = plan.release
    assert managed.agent_id == "code4me-agent"
    assert managed.version == "1.2.3"
    assert managed.source_manifest_digest == manifest_digest(recipe)
    assert manifest_digest(recipe)[len("sha256:") :][:12] in managed.release_id
    assert len(managed.artifacts) == 2
    assert [a.path for a in managed.artifacts] == [
        "code4me-agent-macos-arm64.zip",
        "code4me-agent-linux-x64.zip",
    ]
    assert managed.artifacts[0].sha256 == "sha256:" + _digest(MACOS_BYTES)
    assert managed.artifacts[0].executable == "code4me2-agent"

    # The BYOA declaration becomes its own release.
    assert len(plan.releases) == 2
    byoa = next(r for r in plan.releases if r.distribution_mode == DistributionMode.BYOA_EXTERNAL)
    assert byoa.agent_id == "goose"
    assert byoa.agent_command == "goose"

    second = build_manifest_releases(recipe, verified=_verified())
    assert [r.release_id for r in second.releases] == [r.release_id for r in plan.releases]
    assert second.manifest_digest == plan.manifest_digest


def test_build_manifest_releases_rejects_missing_archive():
    verified = _verified()
    verified.pop("code4me-agent-linux-x64.zip")
    with pytest.raises(ManifestImportError) as error:
        build_manifest_releases(_recipe(), verified=verified)
    assert error.value.code == "ARTIFACT_MISSING"


def test_build_manifest_releases_rejects_an_extra_archive():
    verified = _verified()
    verified["code4me-agent-windows-x64.zip"] = (_digest(b"x"), 1)
    with pytest.raises(ManifestImportError) as error:
        build_manifest_releases(_recipe(), verified=verified)
    assert error.value.code == "UNEXPECTED_ARCHIVE"


def test_build_manifest_releases_rejects_a_digest_mismatch():
    verified = _verified()
    verified["code4me-agent-macos-arm64.zip"] = (_digest(b"tampered"), len(MACOS_BYTES))
    with pytest.raises(ManifestImportError) as error:
        build_manifest_releases(_recipe(), verified=verified)
    assert error.value.code == "DIGEST_MISMATCH"


def test_build_manifest_releases_rejects_a_size_mismatch():
    verified = _verified()
    verified["code4me-agent-macos-arm64.zip"] = (_digest(MACOS_BYTES), 999)
    with pytest.raises(ManifestImportError) as error:
        build_manifest_releases(_recipe(), verified=verified)
    assert error.value.code == "SIZE_MISMATCH"


def test_build_manifest_releases_rejects_a_duplicate_archive_declaration():
    recipe = _recipe()
    recipe["artifacts"][1]["archive"] = recipe["artifacts"][0]["archive"]
    with pytest.raises(ManifestImportError) as error:
        build_manifest_releases(recipe, verified=_verified())
    assert error.value.code == "DUPLICATE_ARCHIVE"


def test_build_manifest_releases_rejects_a_failing_self_check():
    recipe = _recipe()
    recipe["tests"] = {"status": "FAIL"}
    with pytest.raises(ManifestImportError) as error:
        build_manifest_releases(recipe, verified=_verified())
    assert error.value.code == "RECIPE_TESTS_FAILED"


def test_build_manifest_releases_rejects_a_recipe_without_a_self_check():
    recipe = _recipe()
    recipe.pop("tests")
    with pytest.raises(ManifestImportError) as error:
        build_manifest_releases(recipe, verified=_verified())
    assert error.value.code == "RECIPE_TESTS_FAILED"


def test_build_manifest_releases_rejects_a_placeholder_digest():
    recipe = _recipe()
    recipe["artifacts"][0]["sha256"] = "0" * 64
    with pytest.raises(ManifestImportError) as error:
        build_manifest_releases(recipe, verified=_verified())
    assert error.value.code == "PLACEHOLDER_DIGEST"


def test_build_manifest_releases_requires_bytes_for_every_declared_archive():
    with pytest.raises(ManifestImportError) as error:
        build_manifest_releases(_recipe(), verified={})
    assert error.value.code == "ARTIFACT_MISSING"


# ---------------------------------------------------------------------------
# Local multipart endpoint
# ---------------------------------------------------------------------------


def _archives() -> list[_Upload]:
    return [
        _Upload("code4me-agent-macos-arm64.zip", MACOS_BYTES),
        _Upload("code4me-agent-linux-x64.zip", LINUX_BYTES),
    ]


def _run_local(recipe, uploads, app):
    return asyncio.run(
        agents_router.import_release(
            recipe=json.dumps(recipe),
            archives=uploads,
            current_user=_admin(),
            app=app,
        )
    )


def test_import_endpoint_registers_then_is_idempotent():
    app = MagicMock()
    app.get_db_session.return_value = MagicMock()
    recipe = _recipe()
    plan = build_manifest_releases(recipe, verified=_verified())
    rows = [_row(release) for release in plan.releases]

    with patch(
        "backend.routers.research.agents.store.list_releases", return_value=[]
    ), patch(
        "backend.routers.research.agents.store.upsert_release",
        side_effect=rows,
    ) as upsert, patch(
        "backend.routers.research.agents.store.release_summary",
        side_effect=lambda row: {"release_id": row.release_id, "status": "QUALIFIED"},
    ):
        first = _run_local(recipe, _archives(), app)

    body = json.loads(first.body)
    assert first.status_code == 201
    assert body["created"] is True
    assert {item["archive"] for item in body["verified_artifacts"]} == {
        "code4me-agent-macos-arm64.zip",
        "code4me-agent-linux-x64.zip",
    }
    assert upsert.call_count == 2
    # The recorded evidence is the recipe's self-check verdict.
    assert upsert.call_args.kwargs["evidence"]["tests"]["status"] == "PASS"

    # A second import of the same recipe returns the existing rows.
    existing_rows = [_row(release) for release in plan.releases]
    with patch(
        "backend.routers.research.agents.store.list_releases",
        return_value=existing_rows,
    ), patch(
        "backend.routers.research.agents.store.get_release",
        side_effect=lambda _db, release_id: next(
            (r for r in existing_rows if r.release_id == release_id), None
        ),
    ), patch(
        "backend.routers.research.agents.store.release_summary",
        side_effect=lambda row: {"release_id": row.release_id, "status": "QUALIFIED"},
    ):
        second = _run_local(recipe, _archives(), app)

    assert second.status_code == 200
    assert json.loads(second.body)["created"] is False


def test_import_endpoint_rejects_a_wrong_zip():
    app = MagicMock()
    recipe = _recipe()
    uploads = [
        _Upload("code4me-agent-macos-arm64.zip", b"wrong-bytes"),
        _Upload("code4me-agent-linux-x64.zip", LINUX_BYTES),
    ]
    with pytest.raises(HTTPException) as error:
        _run_local(recipe, uploads, app)
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "DIGEST_MISMATCH"
    app.get_db_session.assert_not_called()


def test_import_endpoint_rejects_a_missing_upload():
    app = MagicMock()
    with pytest.raises(HTTPException) as error:
        _run_local(_recipe(), [_Upload("code4me-agent-macos-arm64.zip", MACOS_BYTES)], app)
    assert error.value.detail["code"] == "ARTIFACT_MISSING"


def test_import_endpoint_rejects_an_extra_upload():
    app = MagicMock()
    uploads = [
        _Upload("code4me-agent-macos-arm64.zip", MACOS_BYTES),
        _Upload("code4me-agent-linux-x64.zip", LINUX_BYTES),
        _Upload("code4me-agent-windows-x64.zip", b"extra"),
    ]
    with pytest.raises(HTTPException) as error:
        _run_local(_recipe(), uploads, app)
    assert error.value.detail["code"] == "UNEXPECTED_ARCHIVE"


def test_import_endpoint_rejects_a_duplicate_upload():
    app = MagicMock()
    uploads = [
        _Upload("code4me-agent-macos-arm64.zip", MACOS_BYTES),
        _Upload("code4me-agent-macos-arm64.zip", MACOS_BYTES),
        _Upload("code4me-agent-linux-x64.zip", LINUX_BYTES),
    ]
    with pytest.raises(HTTPException) as error:
        _run_local(_recipe(), uploads, app)
    assert error.value.detail["code"] == "DUPLICATE_ARCHIVE"


def test_import_endpoint_rejects_an_import_without_bytes():
    app = MagicMock()
    with pytest.raises(HTTPException) as error:
        _run_local(_recipe(), [], app)
    assert error.value.detail["code"] == "ARTIFACT_MISSING"


def test_import_endpoint_rejects_a_failing_recipe():
    app = MagicMock()
    recipe = _recipe()
    recipe["tests"] = {"status": "FAIL"}
    uploads = [
        _Upload("code4me-agent-macos-arm64.zip", MACOS_BYTES),
        _Upload("code4me-agent-linux-x64.zip", LINUX_BYTES),
    ]
    with pytest.raises(HTTPException) as error:
        _run_local(recipe, uploads, app)
    assert error.value.detail["code"] == "RECIPE_TESTS_FAILED"


def test_import_endpoint_rejects_an_invalid_recipe():
    app = MagicMock()
    with pytest.raises(HTTPException) as error:
        _run_local({"artifacts": []}, [_Upload("x.zip", b"x")], app)
    assert error.value.status_code == 422
    app.get_db_session.assert_not_called()


# ---------------------------------------------------------------------------
# Deployed endpoint
# ---------------------------------------------------------------------------


def test_deployed_import_hashes_downloaded_bytes():
    app = MagicMock()
    db = MagicMock()
    app.get_db_session.return_value = db
    recipe = _recipe()
    payloads = {
        "https://example.invalid/recipe.json": json.dumps(recipe).encode(),
        "https://example.invalid/code4me-agent-macos-arm64.zip": MACOS_BYTES,
        "https://example.invalid/code4me-agent-linux-x64.zip": LINUX_BYTES,
    }

    def fake_download(url, destination, max_bytes):
        data = payloads[url]
        destination.write_bytes(data)
        return _digest(data), len(data)

    plan = build_manifest_releases(recipe, verified=_verified())
    rows = [_row(release) for release in plan.releases]

    with patch.object(agents_router, "_download", side_effect=fake_download), patch(
        "backend.routers.research.agents.store.list_releases", return_value=[]
    ), patch(
        "backend.routers.research.agents.store.upsert_release", side_effect=rows
    ), patch(
        "backend.routers.research.agents.store.release_summary",
        side_effect=lambda row: {"release_id": row.release_id, "status": "QUALIFIED"},
    ):
        response = agents_router.import_release_deployed(
            agents_router.DeployedImportRequest(
                manifest_url="https://example.invalid/recipe.json",
                archive_urls=[
                    "https://example.invalid/code4me-agent-macos-arm64.zip",
                    "https://example.invalid/code4me-agent-linux-x64.zip",
                ],
            ),
            _admin(),
            app,
        )

    body = json.loads(response.body)
    assert response.status_code == 201
    assert body["created"] is True


def test_deployed_import_rejects_a_mismatched_archive():
    app = MagicMock()
    recipe = _recipe()

    def fake_download(url, destination, max_bytes):
        if url.endswith("recipe.json"):
            data = json.dumps(recipe).encode()
        elif url.endswith("code4me-agent-macos-arm64.zip"):
            data = b"tampered"
        else:
            data = LINUX_BYTES
        destination.write_bytes(data)
        return _digest(data), len(data)

    with patch.object(agents_router, "_download", side_effect=fake_download), pytest.raises(
        HTTPException
    ) as error:
        agents_router.import_release_deployed(
            agents_router.DeployedImportRequest(
                manifest_url="https://example.invalid/recipe.json",
                archive_urls=[
                    "https://example.invalid/code4me-agent-macos-arm64.zip",
                    "https://example.invalid/code4me-agent-linux-x64.zip",
                ],
            ),
            _admin(),
            app,
        )
    assert error.value.detail["code"] == "DIGEST_MISMATCH"
    app.get_db_session.assert_not_called()


def test_deployed_import_rejects_plain_http_off_loopback():
    with pytest.raises(HTTPException) as error:
        agents_router._safe_origin("http://example.invalid/recipe.json")
    assert error.value.detail["code"] == "INVALID_URL"
