"""Offline preparation of one participant distribution over immutable leaf releases.

No database, qualification claims, source stamping, downloads or agent installation.
The output is a build input, never an assignment or persisted parent authority.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
import zipfile
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .manifest_import import build_manifest_release, manifest_digest
from .models import AdapterRef, AgentConfigBinding, AgentReleaseV1, ExecutionFile, PackagedExecution
from .registry import AgentRegistry

PLATFORMS = ("macos-aarch64", "macos-x64", "linux-x64", "windows-x64")
FRAMEWORKS = ("code4me2-agent", "goose", "codex")
SEMVER = r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def input_path(root: Path, value: str) -> Path:
    # Recipes are portable: machine paths are command options, never recipe data.
    ExecutionFile.safe_path(value)
    result = (root / value).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError("input escapes the recipe input directory")
    return result


class AgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    framework: Literal["code4me2-agent", "goose", "codex"]
    version: str = Field(min_length=1)
    adapter: AdapterRef
    agent_command: str | None = None
    agent_command_args: list[str] = Field(default_factory=list)
    byoa_config: list[AgentConfigBinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def explicit_identity(self):
        if self.version.lower() == "latest" or "pending" in self.version.lower():
            raise ValueError("agent versions must be explicit")
        if (not self.adapter.digest or not re.fullmatch(r"(sha256:)?[0-9a-f]{64}", self.adapter.digest)
                or self.adapter.digest.removeprefix("sha256:") == "0" * 64):
            raise ValueError("the qualified adapter must have an explicit SHA-256")
        if self.framework != "code4me2-agent":
            if not self.agent_command:
                raise ValueError("installed agents need an explicit ACP command")
            ExecutionFile.safe_path(self.agent_command)
        elif self.agent_command or self.agent_command_args or self.byoa_config:
            raise ValueError("the managed runtime uses its packaged --managed entrypoint")
        return self


class RuntimeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ParticipantRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1"] = "1"
    plugin_version: str = Field(pattern=SEMVER)
    plugin_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    server_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    runtime: RuntimeInput
    agents: list[AgentInput]
    # Existing profile request fields only; populated with derived release pins.
    profiles: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def three_agents(self):
        if sorted(a.framework for a in self.agents) != sorted(FRAMEWORKS):
            raise ValueError("the recipe must declare Code4Me, Goose and Codex exactly once")
        names = set()
        allowed = {"name", "model", "framework_version", "connection_id", "tools_json",
                   "approval_policy", "max_steps", "is_active", "temperature", "max_context_tokens"}
        required = {"name", "model", "framework_version", "connection_id", "approval_policy", "max_steps"}
        for profile in self.profiles:
            if set(profile) - allowed or required - set(profile):
                raise ValueError("profile templates must use explicit non-secret profile request fields")
            if profile["framework_version"] not in FRAMEWORKS or not profile["name"] or profile["name"] in names:
                raise ValueError("profile frameworks must match the recipe and names must be unique")
            names.add(profile["name"])
            UUID(str(profile["connection_id"]))
            if not isinstance(profile["max_steps"], int) or isinstance(profile["max_steps"], bool) or profile["max_steps"] < 1:
                raise ValueError("profile max_steps must be a positive integer")
            if profile["approval_policy"] not in {"auto", "per_step", "suggestion_only"}:
                raise ValueError("profile approval_policy is unsupported")
            if not isinstance(profile["model"], str) or not profile["model"].strip():
                raise ValueError("profile model must be explicit")
            tools = json.loads(profile.get("tools_json", "[]"))
            if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
                raise ValueError("profile tools_json must be a string array")
            framework = profile["framework_version"]
            if framework != "code4me2-agent":
                agent = next(a for a in self.agents if a.framework == framework)
                required_bindings = {"model", "max_steps", "approval_policy"}
                if tools:
                    required_bindings.add("tools")
                if profile.get("temperature") is not None:
                    required_bindings.add("temperature")
                if required_bindings - {b.field for b in agent.byoa_config}:
                    raise ValueError("installed-agent profile requires missing configuration bindings")
        return self


def extract_execution(archive: Path, destination: Path, executable: str) -> PackagedExecution:
    """Validate the *whole* archive before writing any member; no symlinks/devices."""
    with zipfile.ZipFile(archive) as bundle:
        files = []
        seen = set()
        total = 0
        for member in bundle.infolist():
            name = member.filename.rstrip("/") if member.is_dir() else member.filename
            ExecutionFile.safe_path(name)
            folded = name.casefold()
            if folded in seen:
                raise ValueError("archive contains duplicate or case-colliding paths")
            seen.add(folded)
            mode = member.external_attr >> 16
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError("archive contains a symlink or special file")
            if member.flag_bits & 1:
                raise ValueError("encrypted runtime archives are unsupported")
            total += member.file_size
            if total > 2 * 1024**3:
                raise ValueError("runtime archive exceeds 2 GiB unpacked limit")
            if not member.is_dir():
                files.append(member)
        if executable not in {member.filename for member in files}:
            raise ValueError("archive is missing its managed executable")
        # Reject file/directory overlaps before extraction.
        paths = {m.filename for m in files}
        for name in paths:
            if any(str(p) in paths for p in Path(name).parents if str(p) != "."):
                raise ValueError("archive file overlaps a directory")
        records = []
        for member in sorted(files, key=lambda m: m.filename):
            target = destination / member.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, target.open("xb") as sink:
                shutil.copyfileobj(source, sink)
            executable_member = member.filename == executable or bool((member.external_attr >> 16) & 0o111)
            target.chmod(0o755 if executable_member else 0o644)
            records.append(ExecutionFile(
                path=member.filename, sha256=file_sha256(target),
                size=target.stat().st_size, executable=executable_member,
            ))
    return PackagedExecution(entrypoint=[executable, "--managed"], files=records)


def prepare(recipe: ParticipantRecipe, inputs: Path, output: Path) -> dict:
    """Prepare into a new directory; callers can atomically publish it on success."""
    runtime_manifest = input_path(inputs, recipe.runtime.manifest)
    if file_sha256(runtime_manifest) != recipe.runtime.sha256:
        raise ValueError("runtime release manifest checksum mismatch")
    runtime = json.loads(runtime_manifest.read_text(encoding="utf-8"))
    managed = next(a for a in recipe.agents if a.framework == "code4me2-agent")
    if runtime.get("server_commit") != recipe.server_commit:
        raise ValueError("runtime server commit does not match the recipe")
    if runtime.get("runtime_version") != managed.version or runtime.get("managed_protocol_version") != "1":
        raise ValueError("runtime version/protocol does not match the managed agent")
    expected = {
        f"code4me-agent-{p.replace('aarch64', 'arm64')}.zip": p for p in PLATFORMS
    }
    raw_artifacts = runtime.get("artifacts", [])
    names = [a.get("archive") for a in raw_artifacts]
    if len(names) != len(expected) or set(names) != set(expected):
        raise ValueError("runtime release must cover all four supported native platforms")
    # Validate all transport hashes before extraction.
    for raw in raw_artifacts:
        archive = input_path(runtime_manifest.parent, raw["archive"])
        if file_sha256(archive) != raw.get("sha256"):
            raise ValueError(f"runtime archive checksum mismatch: {raw['archive']}")
    augmented = dict(runtime, adapter=managed.adapter.model_dump(mode="json"), artifacts=[])
    executions = {}
    for raw in raw_artifacts:
        platform = expected[raw["archive"]]
        os_name, arch = platform.split("-")
        executable = "code4me2-agent.exe" if os_name == "windows" else "code4me2-agent"
        archive = input_path(runtime_manifest.parent, raw["archive"])
        extraction = output / "extracted" / platform
        execution = extract_execution(archive, extraction, executable)
        # Check native format as well as the transport hash.
        with (extraction / executable).open("rb") as binary:
            magic = binary.read(4)
        native = {
            "linux": magic == b"\x7fELF", "windows": magic.startswith(b"MZ"),
            "macos": magic in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe",
                              b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"),
        }
        if not native[os_name]:
            raise ValueError(f"managed executable is not native to {platform}")
        executions[platform] = execution
        augmented["artifacts"].append(dict(
            raw, runtime_id="code4me-agent", platform=os_name, architecture=arch,
            executable=executable, size=archive.stat().st_size,
            execution=execution.model_dump(mode="json"),
        ))
    # New manifest => new leaf identity; old manifests/records are never changed.
    managed_release = build_manifest_release(augmented).release
    releases = {"code4me2-agent": managed_release}
    for agent in recipe.agents:
        if agent.framework == "code4me2-agent":
            continue
        source = agent.model_dump(mode="json")
        digest = manifest_digest(source)
        releases[agent.framework] = AgentReleaseV1(
            agent_id=agent.framework, version=agent.version,
            release_id=f"{agent.framework}-{agent.version}-{digest[7:19]}",
            source_manifest_digest=digest, distribution_mode="BYOA_EXTERNAL",
            agent_package=agent.framework, agent_command=agent.agent_command,
            agent_command_args=agent.agent_command_args,
            byoa_config=agent.byoa_config, adapter=agent.adapter,
        )
    registry = AgentRegistry()
    for release in releases.values():
        result = registry.register_release(release)
        if not result.accepted:
            raise ValueError(str(result.issue))

    catalog = {"schema_version": "1", "platforms": []}
    resources = output / "resources" / "code4me-runtime"
    resources.mkdir(parents=True)
    managed_artifacts = []
    for raw in augmented["artifacts"]:
        platform = expected[raw["archive"]]
        execution = executions[platform]
        prefix = f"agents/{managed_release.release_id}/{platform}"
        target = output / "research-agents" / prefix
        target.parent.mkdir(parents=True, exist_ok=True)
        (output / "extracted" / platform).rename(target)
        shutil.copyfile(input_path(runtime_manifest.parent, raw["archive"]), resources / raw["archive"])
        files = [dict(f.model_dump(), path=f"{prefix}/{f.path}") for f in execution.files]
        catalog["platforms"].append({
            "os": raw["platform"], "arch": platform.split("-")[1],
            "agents": [{
                "release_id": managed_release.release_id,
                "artifact_digest": raw["sha256"],
                "archive_sha256": raw["sha256"],
                "adapter_digest": managed.adapter.digest,
                "digest": execution.executable_sha256,
                "execution_manifest_digest": execution.manifest_digest,
                "execution": execution.model_dump(mode="json"),
                "entrypoint": [f"{prefix}/{execution.entrypoint[0]}", *execution.entrypoint[1:]],
                "files": files,
            }],
        })
        managed_artifacts.append({
            "runtime_id": "code4me-agent", "version": managed.version,
            "platform": raw["platform"], "architecture": raw["architecture"].replace("aarch64", "arm64"),
            "archive": "code4me-runtime/" + raw["archive"], "sha256": raw["sha256"],
            "executable": raw["executable"], "managed_protocol": "1",
        })
    (output / "extracted").rmdir()
    write_json(resources / "manifest.json", {
        "manifest_version": 1, "runtime_version": managed.version, "managed_protocol_version": "1",
        "server_commit": recipe.server_commit, "plugin_commit": recipe.plugin_commit,
        "artifacts": managed_artifacts,
    })
    declarations = {framework: release.model_dump(mode="json", exclude={"created_at", "qualification_status"})
                    for framework, release in releases.items()}
    inventory = {
        "schema_version": "1", "recipe_digest": manifest_digest(recipe.model_dump(mode="json")),
        "plugin_version": recipe.plugin_version, "plugin_commit": recipe.plugin_commit,
        "server_commit": recipe.server_commit, "platforms": list(PLATFORMS),
        "releases": [{"framework": framework, "release_id": r.release_id,
                      "source_manifest_digest": r.source_manifest_digest,
                      "distribution_mode": r.distribution_mode.value,
                      "adapter": r.adapter.model_dump(mode="json"),
                      "version": r.version}
                     for framework, r in releases.items()],
    }
    catalog["participant_release"] = inventory
    profiles = []
    for profile in recipe.profiles:
        framework = profile["framework_version"]
        profiles.append(dict(
            {"tools_json": "[]", "is_active": True, "temperature": None, "max_context_tokens": None},
            **profile, release_id=releases[framework].release_id,
        ))
    plan = {"inventory": inventory, "releases": declarations, "profiles": profiles}
    write_json(output / "catalog.json", catalog)
    write_json(output / "registration.json", plan)
    write_json(output / "recipe.json", recipe.model_dump(mode="json"))
    write_json(output / "prepared-inputs.json", {
        file.relative_to(output).as_posix(): file_sha256(file)
        for file in sorted(output.rglob("*")) if file.is_file()
    })
    return plan


def load_prepared(output: Path) -> dict:
    """Catch changed/missing prepared inputs before building or any API write."""
    hashes = json.loads((output / "prepared-inputs.json").read_text())
    if not {"recipe.json", "catalog.json", "registration.json", "resources/code4me-runtime/manifest.json"} <= hashes.keys():
        raise ValueError("incomplete preparation input inventory")
    for name, expected in hashes.items():
        if file_sha256(input_path(output, name)) != expected:
            raise ValueError(f"prepared input changed: {name}; prepare into a new directory")
    plan = json.loads((output / "registration.json").read_text())
    catalog = json.loads((output / "catalog.json").read_text())
    recipe = ParticipantRecipe.model_validate_json((output / "recipe.json").read_text())
    if (plan["inventory"] != catalog["participant_release"] or
            plan["inventory"]["recipe_digest"] != manifest_digest(recipe.model_dump(mode="json"))):
        raise ValueError("prepared recipe, registration and catalog do not agree")
    return plan
