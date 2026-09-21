"""The single producer recipe: build inputs in, one verified recipe out (no DB).

The producer is the ``scripts/participant-release.py`` CLI plus the agent
self-check. :func:`prepare` folds the runtime build manifest, the exact archive
``sha256``/``size`` per platform, the BYOA agent declarations and the self-check
verdict into **one** recipe document. There is no extracted-file inventory and no
separate catalog/manifest copy to keep in sync, and a self-check that did not
pass produces no recipe.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from research.study.agents.participant_release import (
    PLATFORMS,
    ParticipantRecipe,
    canonical_platform,
    file_sha256,
    load_prepared,
    prepare,
    write_json,
)

PASSING_TESTS = {
    "status": "PASS",
    "approval_options": ["auto", "per_step"],
    "cases": [{"case_id": "acp.initialize", "status": "PASS"}],
}


def _make_archive(path: Path, platform: str, *, executable: str) -> None:
    magic = {
        "windows": b"MZ00",
        "linux": b"\x7fELF",
    }.get(platform.split("-")[0], b"\xcf\xfa\xed\xfe")
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr(executable, magic + platform.encode())
        bundle.writestr("_internal/library", b"dependent runtime bytes")


def make_inputs(root: Path) -> ParticipantRecipe:
    """Build a portable input directory and the recipe that describes it."""
    root.mkdir()
    artifacts = []
    for platform in PLATFORMS:
        name = f"code4me-agent-{platform}.zip"
        executable = (
            "code4me2-agent.exe" if platform.startswith("windows") else "code4me2-agent"
        )
        archive = root / name
        _make_archive(archive, platform, executable=executable)
        os_name, arch = platform.split("-")
        artifacts.append(
            {
                "runtime_id": "code4me-agent",
                "version": "1.2.3",
                "platform": os_name,
                "architecture": arch,
                "archive": name,
                "sha256": file_sha256(archive),
                "size": archive.stat().st_size,
                "executable": executable,
                "managed_protocol": "1",
            }
        )
    write_json(
        root / "runtime.json",
        {
            "manifest_version": 1,
            "runtime_version": "1.2.3",
            "server_commit": "a" * 40,
            "managed_protocol_version": "1",
            "artifacts": artifacts,
        },
    )
    return ParticipantRecipe.model_validate(
        {
            "plugin_version": "2.0.0",
            "plugin_commit": "b" * 40,
            "server_commit": "a" * 40,
            "runtime": {
                "manifest": "runtime.json",
                "sha256": file_sha256(root / "runtime.json"),
            },
            "agents": [
                {
                    "framework": framework,
                    "version": "1.2.3",
                    "adapter": {
                        "adapter_id": framework,
                        "version": "1",
                        "digest": "sha256:" + "c" * 64,
                    },
                    **({"agent_command": framework} if framework != "code4me2-agent" else {}),
                }
                for framework in ("code4me2-agent", "goose", "codex")
            ],
        }
    )


def test_prepare_emits_one_recipe_with_verified_archives(tmp_path):
    inputs = tmp_path / "inputs"
    recipe = make_inputs(inputs)
    document = prepare(recipe, inputs, tmp_path / "prepared", tests=PASSING_TESTS)

    assert document["manifest_version"] == 1
    assert document["runtime_version"] == "1.2.3"
    assert document["managed_protocol_version"] == "1"
    assert document["tests"]["status"] == "PASS"
    assert len(document["artifacts"]) == len(PLATFORMS)
    for artifact in document["artifacts"]:
        assert artifact["platform"] in {"macos", "linux", "windows"}
        # The single canonical architecture spelling: never ``aarch64``.
        assert artifact["architecture"] in {"arm64", "x64"}
        assert len(artifact["sha256"]) == 64
        assert artifact["size"] > 0

    frameworks = sorted(agent["framework"] for agent in document["agents"])
    assert frameworks == ["codex", "goose"]

    written = json.loads((tmp_path / "prepared" / "recipe.json").read_text())
    assert written == document
    assert written["recipe_digest"].startswith("sha256:")
    # The prepared inputs are re-checkable and the recipe is the single document.
    assert load_prepared(tmp_path / "prepared") == document
    for artifact in document["artifacts"]:
        staged = tmp_path / "prepared" / "resources" / "code4me-runtime" / artifact["archive"]
        assert file_sha256(staged) == artifact["sha256"]


def test_prepare_requires_a_passing_self_check(tmp_path):
    inputs = tmp_path / "inputs"
    recipe = make_inputs(inputs)
    with pytest.raises(ValueError, match="self-check"):
        prepare(recipe, inputs, tmp_path / "no-tests")
    with pytest.raises(ValueError, match="self-check"):
        prepare(
            recipe,
            inputs,
            tmp_path / "failed-tests",
            tests={"status": "FAIL", "cases": []},
        )
    assert not (tmp_path / "no-tests").exists()


def test_prepare_rejects_a_tampered_archive(tmp_path):
    inputs = tmp_path / "inputs"
    recipe = make_inputs(inputs)
    (inputs / f"code4me-agent-{PLATFORMS[0]}.zip").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        prepare(recipe, inputs, tmp_path / "out", tests=PASSING_TESTS)


def test_prepare_rejects_a_declared_size_mismatch(tmp_path):
    inputs = tmp_path / "inputs"
    recipe = make_inputs(inputs)
    manifest_path = inputs / "runtime.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["artifacts"][0]["size"] = manifest["artifacts"][0]["size"] + 1
    write_json(manifest_path, manifest)
    recipe.runtime.sha256 = file_sha256(manifest_path)
    with pytest.raises(ValueError, match="size"):
        prepare(recipe, inputs, tmp_path / "out", tests=PASSING_TESTS)


def test_local_subset_declares_exactly_its_platforms(tmp_path):
    inputs = tmp_path / "inputs"
    recipe = make_inputs(inputs)
    manifest_path = inputs / "runtime.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["artifacts"] = [
        artifact
        for artifact in manifest["artifacts"]
        if artifact["archive"] == "code4me-agent-macos-arm64.zip"
    ]
    write_json(manifest_path, manifest)
    recipe.runtime.sha256 = file_sha256(manifest_path)

    # ``aarch64`` is normalised to the single canonical ``arm64`` at the boundary.
    assert canonical_platform("macos-aarch64") == "macos-arm64"
    document = prepare(
        recipe,
        inputs,
        tmp_path / "prepared",
        platforms=("macos-aarch64",),
        tests=PASSING_TESTS,
    )
    assert [
        f"{artifact['platform']}-{artifact['architecture']}"
        for artifact in document["artifacts"]
    ] == ["macos-arm64"]

    with pytest.raises(ValueError, match="requested native platforms"):
        prepare(
            recipe,
            inputs,
            tmp_path / "other",
            platforms=("macos-arm64", "linux-x64"),
            tests=PASSING_TESTS,
        )
    with pytest.raises(ValueError, match="supported native platform"):
        prepare(
            recipe,
            inputs,
            tmp_path / "bad",
            platforms=("solaris-sparc",),
            tests=PASSING_TESTS,
        )


def test_prepare_rejects_a_missing_platform(tmp_path):
    inputs = tmp_path / "inputs"
    recipe = make_inputs(inputs)
    manifest_path = inputs / "runtime.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["artifacts"].pop()
    write_json(manifest_path, manifest)
    recipe.runtime.sha256 = file_sha256(manifest_path)
    with pytest.raises(ValueError, match="four"):
        prepare(recipe, inputs, tmp_path / "out", tests=PASSING_TESTS)


def test_changed_prepared_input_cannot_be_loaded(tmp_path):
    inputs = tmp_path / "inputs"
    recipe = make_inputs(inputs)
    prepare(recipe, inputs, tmp_path / "prepared", tests=PASSING_TESTS)
    staged = next((tmp_path / "prepared" / "resources" / "code4me-runtime").iterdir())
    staged.write_bytes(b"changed after preparation")
    with pytest.raises(ValueError, match="prepared input changed"):
        load_prepared(tmp_path / "prepared")
