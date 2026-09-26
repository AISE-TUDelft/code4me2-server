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

PASSING_TESTS = {"self_check": "PASS", "acp_initialize": "PASS", "ran_at": "2026-09-21T00:00:00Z"}

# A Goose release is gateway-bound: the recipe must declare how the plugin
# points it at the research inference gateway.
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
                    **({"byoa_config": GOOSE_GATEWAY_BINDINGS} if framework == "goose" else {}),
                }
                for framework in ("code4me2-agent", "goose", "codex")
            ],
        }
    )


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
    # The adapter pin travels per artifact: the plugin checks it there.
    assert all(a["adapter"]["digest"] == "sha256:" + "c" * 64 for a in document["artifacts"])
    # The plugin build reads the same recipe from the resource overlay, with
    # archive paths relative to the resource root.
    runtime_manifest = json.loads(
        (tmp_path / "prepared" / "resources" / "code4me-runtime" / "manifest.json").read_text()
    )
    assert runtime_manifest["runtime_version"] == document["runtime_version"]
    assert runtime_manifest["adapter"] == document["adapter"]
    assert [a["archive"] for a in runtime_manifest["artifacts"]] == [
        f"code4me-runtime/{a['archive']}" for a in document["artifacts"]
    ]
    assert all(a["adapter"] == document["adapter"] for a in runtime_manifest["artifacts"])
    assert written["recipe_digest"].startswith("sha256:")
    # The prepared inputs are re-checkable and the recipe is the single document.
    assert load_prepared(tmp_path / "prepared") == document
    for artifact in document["artifacts"]:
        staged = tmp_path / "prepared" / "resources" / "code4me-runtime" / artifact["archive"]
        assert file_sha256(staged) == artifact["sha256"]


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


def test_recipe_requires_goose_gateway_bindings(tmp_path):
    """A Goose agent without the gateway runtime bindings is refused at recipe time."""
    inputs = tmp_path / "inputs"
    recipe = make_inputs(inputs)
    document = recipe.model_dump(mode="json")
    for agent in document["agents"]:
        if agent["framework"] == "goose":
            agent["byoa_config"] = [b for b in agent["byoa_config"] if b["field"] != "inference_gateway_credential"]
    with pytest.raises(ValueError) as error:
        ParticipantRecipe.model_validate(document)
    assert "inference gateway" in str(error.value)
    for agent in document["agents"]:
        if agent["framework"] == "goose":
            agent["byoa_config"] = GOOSE_GATEWAY_BINDINGS
    ParticipantRecipe.model_validate(document)


def test_recipe_rejects_a_credential_binding_on_argv(tmp_path):
    inputs = tmp_path / "inputs"
    recipe = make_inputs(inputs)
    document = recipe.model_dump(mode="json")
    for agent in document["agents"]:
        if agent["framework"] == "goose":
            agent["byoa_config"] = [
                dict(b, transport="arg") if b["field"] == "inference_gateway_credential" else b
                for b in agent["byoa_config"]
            ]
    with pytest.raises(ValueError) as error:
        ParticipantRecipe.model_validate(document)
    assert "world-readable" in str(error.value) or "env transport" in str(error.value)
