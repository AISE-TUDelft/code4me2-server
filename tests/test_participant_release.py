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
import subprocess
import sys
import zipfile
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from pathlib import Path

PASSING_TESTS = {"self_check": "PASS", "acp_initialize": "PASS", "ran_at": "2026-09-21T00:00:00Z"}


@pytest.mark.skipif(sys.platform == "win32", reason="Windows CI does not create symlinks")
def test_runtime_archive_materializes_external_framework_links(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "code4me2-agent").write_bytes(b"agent")
    framework = tmp_path / "Python.framework"
    (framework / "Versions" / "A").mkdir(parents=True)
    (framework / "Versions" / "A" / "Python").write_bytes(b"framework binary")
    (bundle / "Python.framework").symlink_to(framework, target_is_directory=True)
    archive = tmp_path / "agent.zip"

    subprocess.run(
        [sys.executable, "packaging/archive_runtime.py", "--root", str(bundle),
         "--platform", "macos-x64", "--output", str(archive)],
        check=True,
    )

    with zipfile.ZipFile(archive) as zipped:
        assert zipped.read("Python.framework/Versions/A/Python") == b"framework binary"


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
                "tests": PASSING_TESTS,
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
                    "tests": [dict(PASSING_TESTS, os="macos", arch="arm64")],
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


def test_native_ci_manifests_merge_into_one_admin_import(tmp_path):
    inputs = tmp_path / "inputs"
    make_inputs(inputs)
    expected = json.loads((inputs / "runtime.json").read_text())
    for artifact in expected["artifacts"]:
        platform = f"{artifact['platform']}-{artifact['architecture']}"
        write_json(inputs / f"native-{platform}.json", dict(expected, artifacts=[artifact]))

    output = inputs / "code4me-managed-runtime-release.json"
    subprocess.run(
        [sys.executable, "-m", "research.study.agents.participant_release", "merge",
         "--directory", str(inputs), "--output", str(output)],
        check=True,
    )
    actual = json.loads(output.read_text())["artifacts"]
    assert sorted(actual, key=lambda item: item["archive"]) == sorted(
        expected["artifacts"], key=lambda item: item["archive"]
    )


def test_managed_only_recipe_rejects_profile_for_missing_agent(tmp_path):
    recipe = make_inputs(tmp_path / "inputs").model_dump()
    recipe["agents"] = recipe["agents"][:1]
    recipe["profiles"] = [{
        "name": "unavailable-agent",
        "model": "example",
        "framework_version": "goose",
        "connection_id": "00000000-0000-0000-0000-000000000000",
        "approval_policy": "auto",
        "max_steps": 1,
    }]
    with pytest.raises(ValueError, match="profile frameworks must match the recipe"):
        ParticipantRecipe.model_validate(recipe)


def test_prepare_emits_one_recipe_with_verified_archives(tmp_path):
    inputs = tmp_path / "inputs"
    recipe = make_inputs(inputs)
    document = prepare(recipe, inputs, tmp_path / "prepared")

    assert document["manifest_version"] == 1
    assert document["runtime_version"] == "1.2.3"
    assert document["managed_protocol_version"] == "1"
    assert all(a["tests"]["self_check"] == "PASS" for a in document["artifacts"])
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
    runtime_manifest = json.loads(
        (tmp_path / "prepared" / "resources" / "code4me-runtime" / "manifest.json").read_text()
    )
    assert runtime_manifest == dict(
        document,
        artifacts=[
            dict(artifact, archive=f"code4me-runtime/{artifact['archive']}")
            for artifact in document["artifacts"]
        ],
    )
    assert written["recipe_digest"].startswith("sha256:")
    # The prepared inputs are re-checkable and the recipe is the single document.
    assert load_prepared(tmp_path / "prepared") == document
    for artifact in document["artifacts"]:
        staged = tmp_path / "prepared" / "resources" / "code4me-runtime" / artifact["archive"]
        assert file_sha256(staged) == artifact["sha256"]


def test_prepare_accepts_one_managed_release_without_external_agents(tmp_path):
    inputs = tmp_path / "inputs"
    recipe = make_inputs(inputs).model_dump(mode="json")
    recipe["agents"] = [agent for agent in recipe["agents"] if agent["framework"] == "code4me2-agent"]

    prepared = prepare(ParticipantRecipe.model_validate(recipe), inputs, tmp_path / "prepared")

    assert len(prepared["artifacts"]) == 4
    assert prepared["agents"] == []


def test_prepare_does_not_invent_a_managed_adapter(tmp_path):
    inputs = tmp_path / "inputs"
    recipe = make_inputs(inputs).model_dump(mode="json")
    recipe["agents"] = [agent for agent in recipe["agents"] if agent["framework"] == "code4me2-agent"]
    recipe["agents"][0].pop("adapter")

    prepared = prepare(ParticipantRecipe.model_validate(recipe), inputs, tmp_path / "prepared")

    assert "adapter" not in prepared


def test_prepare_requires_passing_platform_tests(tmp_path):
    inputs = tmp_path / "inputs"
    recipe = make_inputs(inputs)
    path = inputs / "runtime.json"
    document = json.loads(path.read_text())
    document["artifacts"][0]["tests"]["self_check"] = "FAIL"
    write_json(path, document)
    recipe.runtime.sha256 = file_sha256(path)
    with pytest.raises(ValueError, match="passing self_check"):
        prepare(recipe, inputs, tmp_path / "failed-tests")
    assert not (tmp_path / "failed-tests" / "recipe.json").exists()


def test_prepare_rejects_a_tampered_archive(tmp_path):
    inputs = tmp_path / "inputs"
    recipe = make_inputs(inputs)
    (inputs / f"code4me-agent-{PLATFORMS[0]}.zip").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        prepare(recipe, inputs, tmp_path / "out")


def test_prepare_rejects_a_declared_size_mismatch(tmp_path):
    inputs = tmp_path / "inputs"
    recipe = make_inputs(inputs)
    manifest_path = inputs / "runtime.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["artifacts"][0]["size"] = manifest["artifacts"][0]["size"] + 1
    write_json(manifest_path, manifest)
    recipe.runtime.sha256 = file_sha256(manifest_path)
    with pytest.raises(ValueError, match="size"):
        prepare(recipe, inputs, tmp_path / "out")


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
            )
    with pytest.raises(ValueError, match="supported native platform"):
        prepare(
            recipe,
            inputs,
            tmp_path / "bad",
            platforms=("solaris-sparc",),
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
        prepare(recipe, inputs, tmp_path / "out")


def test_changed_prepared_input_cannot_be_loaded(tmp_path):
    inputs = tmp_path / "inputs"
    recipe = make_inputs(inputs)
    prepare(recipe, inputs, tmp_path / "prepared")
    staged = next((tmp_path / "prepared" / "resources" / "code4me-runtime").iterdir())
    staged.write_bytes(b"changed after preparation")
    with pytest.raises(ValueError, match="prepared input changed"):
        load_prepared(tmp_path / "prepared")
