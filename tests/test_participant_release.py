"""Real archive bytes across preparation, leaf identity and receipt binding (no DB)."""
import json
import hashlib
import stat
import zipfile

import pytest

from research.study.agents.models import AgentReleaseV1, ExecutionFile, PackagedExecution
from research.study.agents.participant_release import (
    PLATFORMS, ParticipantRecipe, extract_execution, file_sha256, prepare, write_json,
    load_prepared,
)
from research.study.agents.registry import qualified_artifact_keys


def make_inputs(root):
    root.mkdir()
    artifacts = []
    for platform in PLATFORMS:
        name = f"code4me-agent-{platform.replace('aarch64', 'arm64')}.zip"
        executable = "code4me2-agent.exe" if platform.startswith("windows") else "code4me2-agent"
        magic = b"MZ00" if platform.startswith("windows") else b"\x7fELF" if platform.startswith("linux") else b"\xcf\xfa\xed\xfe"
        with zipfile.ZipFile(root / name, "w") as bundle:
            bundle.writestr(executable, magic + platform.encode())
            bundle.writestr("_internal/library", b"dependent runtime bytes")
        artifacts.append({"archive": name, "sha256": file_sha256(root / name)})
    write_json(root / "runtime.json", {
        "manifest_version": 1, "runtime_version": "1.2.3", "server_commit": "a" * 40,
        "managed_protocol_version": "1", "artifacts": artifacts,
    })
    return ParticipantRecipe.model_validate({
        "plugin_version": "2.0.0", "plugin_commit": "b" * 40, "server_commit": "a" * 40,
        "runtime": {"manifest": "runtime.json", "sha256": file_sha256(root / "runtime.json")},
        "agents": [
            {"framework": framework, "version": "1.2.3",
             "adapter": {"adapter_id": framework, "version": "1", "digest": "sha256:" + "c" * 64},
             **({"agent_command": framework} if framework != "code4me2-agent" else {})}
            for framework in ("code4me2-agent", "goose", "codex")
        ],
    })


def test_execution_digest_matches_kotlin_utf8_fixture():
    execution = PackagedExecution(
        entrypoint=["agent", "--managed"],
        files=[ExecutionFile(
            path=name, sha256=hashlib.sha256(body.encode()).hexdigest(),
            size=len(body.encode()), executable=name == "agent",
        ) for name, body in [("agent", "agent-binary"), ("library-λ", "library")]],
    )
    assert execution.manifest_digest == "sha256:ce0745a4448a63f335382cb878a015033bc4f916e5170e4604497be6e7c82bc5"


def test_prepare_three_leaves_and_four_platforms_without_source_changes(tmp_path):
    inputs = tmp_path / "inputs"
    recipe = make_inputs(inputs)
    before = (inputs / "runtime.json").read_bytes()
    first = prepare(recipe, inputs, tmp_path / "first")
    second = prepare(recipe, inputs, tmp_path / "second")
    assert first == second
    assert load_prepared(tmp_path / "first") == first
    assert (inputs / "runtime.json").read_bytes() == before
    assert set(first["releases"]) == {"code4me2-agent", "goose", "codex"}
    managed = AgentReleaseV1.model_validate(first["releases"]["code4me2-agent"])
    assert len(managed.artifacts) == 4
    catalog = json.loads((tmp_path / "first/catalog.json").read_text())
    for artifact in managed.artifacts:
        assert artifact.sha256.removeprefix("sha256:") != artifact.execution.executable_sha256
        entry = next(p["agents"][0] for p in catalog["platforms"] if p["os"] == artifact.os and p["arch"] == artifact.arch.replace("arm64", "aarch64"))
        assert entry["release_id"] == managed.release_id
        assert entry["execution_manifest_digest"] == artifact.execution.manifest_digest
        assert entry["artifact_digest"] == artifact.sha256.removeprefix("sha256:")
        for record in entry["files"]:
            assert file_sha256(tmp_path / "first/research-agents" / record["path"]) == record["sha256"]
    for framework in ("goose", "codex"):
        release = first["releases"][framework]
        assert release["distribution_mode"] == "BYOA_EXTERNAL"
        assert release["artifacts"] == []
        assert release["agent_command"] == framework


def test_execution_receipts_cannot_be_reused_from_old_archive_leaf(tmp_path):
    recipe = make_inputs(tmp_path / "inputs")
    plan = prepare(recipe, tmp_path / "inputs", tmp_path / "prepared")
    release = plan["releases"]["code4me2-agent"]
    artifact = AgentReleaseV1.model_validate(release).artifacts[0]
    receipt = {
        "artifact_digest": artifact.sha256, "adapter_digest": release["adapter"]["digest"],
        "host": {"os": artifact.os, "arch": artifact.arch},
        "status": "PASS", "case_results": [{"case_id": "real-case", "status": "PASS"}],
    }
    document = dict(release, conformance=[receipt])
    assert qualified_artifact_keys(document) == set()
    receipt["release_id"] = release["release_id"]
    receipt["execution_manifest_digest"] = artifact.execution.manifest_digest
    assert len(qualified_artifact_keys(document)) == 1
    receipt["release_id"] = "historical-release"
    assert qualified_artifact_keys(document) == set()
    receipt["release_id"] = release["release_id"]
    receipt["execution_manifest_digest"] = "sha256:" + "0" * 64
    assert qualified_artifact_keys(document) == set()


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/outside", "a\\b"])
def test_archive_escape_rejected_before_any_extraction(tmp_path, name):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("code4me2-agent", b"executable")
        bundle.writestr(name, b"unsafe")
    with pytest.raises(ValueError):
        extract_execution(archive, tmp_path / "out", "code4me2-agent")
    assert not (tmp_path / "out").exists()


def test_symlink_rejected_before_extraction(tmp_path):
    archive = tmp_path / "link.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("code4me2-agent", b"executable")
        link = zipfile.ZipInfo("link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        bundle.writestr(link, "../outside")
    with pytest.raises(ValueError, match="symlink"):
        extract_execution(archive, tmp_path / "out", "code4me2-agent")


def test_transport_tampering_rejected_before_extraction(tmp_path):
    recipe = make_inputs(tmp_path / "inputs")
    (tmp_path / "inputs/code4me-agent-linux-x64.zip").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        prepare(recipe, tmp_path / "inputs", tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_missing_platform_is_not_silently_dropped(tmp_path):
    recipe = make_inputs(tmp_path / "inputs")
    path = tmp_path / "inputs/runtime.json"
    manifest = json.loads(path.read_text())
    manifest["artifacts"].pop()
    write_json(path, manifest)
    recipe.runtime.sha256 = file_sha256(path)
    with pytest.raises(ValueError, match="four"):
        prepare(recipe, tmp_path / "inputs", tmp_path / "out")


def test_changed_dependency_gets_new_leaf_without_rewriting_history(tmp_path):
    recipe = make_inputs(tmp_path / "inputs")
    first = prepare(recipe, tmp_path / "inputs", tmp_path / "first")
    archive = tmp_path / "inputs/code4me-agent-linux-x64.zip"
    with zipfile.ZipFile(archive, "a") as bundle:
        bundle.writestr("_internal/new-library", b"new dependency")
    path = tmp_path / "inputs/runtime.json"
    manifest = json.loads(path.read_text())
    next(a for a in manifest["artifacts"] if a["archive"] == archive.name)["sha256"] = file_sha256(archive)
    write_json(path, manifest)
    recipe.runtime.sha256 = file_sha256(path)
    second = prepare(recipe, tmp_path / "inputs", tmp_path / "second")
    assert first["releases"]["code4me2-agent"]["release_id"] != second["releases"]["code4me2-agent"]["release_id"]
    assert first["releases"]["goose"] == second["releases"]["goose"]
    assert first == json.loads((tmp_path / "first/registration.json").read_text())


def test_changed_preparation_cannot_be_built_or_applied(tmp_path):
    recipe = make_inputs(tmp_path / "inputs")
    prepare(recipe, tmp_path / "inputs", tmp_path / "prepared")
    registration = tmp_path / "prepared/registration.json"
    plan = json.loads(registration.read_text())
    plan["releases"]["codex"]["agent_command"] = "another-agent"
    write_json(registration, plan)
    with pytest.raises(ValueError, match="prepared input changed"):
        load_prepared(tmp_path / "prepared")
