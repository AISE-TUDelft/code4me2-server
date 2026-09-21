"""The one participant-release recipe: build inputs in, a single recipe out.

No database, no downloads and no agent installation. The producer (the
``scripts/participant-release.py`` CLI, run locally or by CI) builds the native
runtime ZIPs, runs the agent self-check, and calls :func:`prepare` to fold
everything into **one recipe document**:

* the runtime build manifest plus the exact ZIP ``sha256``/``size`` per platform;
* the adapter identity;
* the BYOA agent declarations (Goose, Codex, ...);
* the self-check verdict (``tests``).

The recipe is the single source of truth: the plugin embeds it as its runtime
manifest and the server imports it, verifying the recipe's bytes against the
archives. There is no extracted-file inventory and no separate manifest copy to
keep in sync. A self-check that did not pass produces no recipe.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any, Literal, Optional, Sequence
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import AdapterRef, AgentConfigBinding
from .models import normalize_platform

#: Canonical native platform ids. ``arm64`` is the single canonical spelling for
#: the 64-bit ARM architecture (``aarch64`` is normalised to it at the boundary).
PLATFORMS = ("macos-arm64", "macos-x64", "linux-x64", "windows-x64")
FRAMEWORKS = ("code4me2-agent", "goose", "codex")
SEMVER = r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"

#: The one architecture spelling accepted in a recipe.
_CANONICAL_ARCH = {"arm64", "x64"}

#: Every accepted architecture spelling, normalised to the single canonical one.
_ARCH_ALIASES = {
    "arm64": "arm64",
    "aarch64": "arm64",
    "x64": "x64",
    "x86_64": "x64",
    "amd64": "x64",
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def canonical_platform(value: str) -> str:
    """Normalise ``os-arch`` to the single canonical spelling (``arm64``/``x64``).

    ``aarch64``/``arm64`` (and ``x86_64``/``amd64``/``x64``) are all accepted at
    the boundary and folded to the one canonical spelling, so a recipe never
    carries two names for the same platform.
    """
    if "-" not in value:
        raise ValueError(f"platform {value!r} must be 'os-arch'")
    os_name, arch = value.split("-", 1)
    canonical_os, _ = normalize_platform(os_name, arch)
    canonical_arch = _ARCH_ALIASES.get(arch.strip().lower())
    if canonical_os not in {"macos", "linux", "windows"} or canonical_arch not in _CANONICAL_ARCH:
        raise ValueError(f"platform {value!r} is not a supported native platform")
    return f"{canonical_os}-{canonical_arch}"


def input_path(root: Path, value: str) -> Path:
    # Recipes are portable: machine paths are command options, never recipe data.
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
        if self.framework != "code4me2-agent":
            if not self.agent_command:
                raise ValueError("installed agents need an explicit ACP command")
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


def _normalize_tests(tests: Optional[dict[str, Any]]) -> dict[str, Any]:
    """A recipe is only produced from a passing self-check."""
    if not isinstance(tests, dict):
        raise ValueError("a recipe requires the agent self-check result")
    if str(tests.get("status", "")).strip().upper() != "PASS":
        raise ValueError("the agent self-check did not pass; no recipe is produced")
    cases = tests.get("cases")
    if cases is not None and (
        not isinstance(cases, list)
        or not all(isinstance(case, dict) for case in cases)
    ):
        raise ValueError("self-check cases must be an array of objects")
    approval = tests.get("approval_options")
    if approval is not None and (
        not isinstance(approval, list) or not all(isinstance(o, str) for o in approval)
    ):
        raise ValueError("self-check approval_options must be an array of strings")
    return dict(tests)


def prepare(
    recipe: ParticipantRecipe,
    inputs: Path,
    output: Path,
    platforms: Sequence[str] = PLATFORMS,
    tests: Optional[dict[str, Any]] = None,
) -> dict:
    """Prepare one recipe document in a new directory.

    ``platforms`` defaults to every supported native platform. A caller may
    request an explicit subset for a local development release; the emitted
    recipe then declares exactly that subset, so a partial preparation is
    visible in every downstream artifact.
    """
    requested = tuple(canonical_platform(p) for p in platforms)
    if not requested or len(set(requested)) != len(requested):
        raise ValueError("platforms must be a non-empty unique subset of the supported native platforms")
    verified_tests = _normalize_tests(tests)

    runtime_manifest = input_path(inputs, recipe.runtime.manifest)
    if file_sha256(runtime_manifest) != recipe.runtime.sha256:
        raise ValueError("runtime release manifest checksum mismatch")
    runtime = json.loads(runtime_manifest.read_text(encoding="utf-8"))
    managed = next(a for a in recipe.agents if a.framework == "code4me2-agent")
    if runtime.get("server_commit") != recipe.server_commit:
        raise ValueError("runtime server commit does not match the recipe")
    if runtime.get("runtime_version") != managed.version or runtime.get("managed_protocol_version") != "1":
        raise ValueError("runtime version/protocol does not match the managed agent")

    expected: dict[str, str] = {}
    for projected in runtime.get("artifacts", []):
        platform = canonical_platform(f"{projected.get('platform')}-{projected.get('architecture')}")
        expected[str(projected.get("archive"))] = platform
    if len(expected) != len(requested) or set(expected.values()) != set(requested):
        if set(requested) == set(PLATFORMS):
            raise ValueError("runtime release must cover all four supported native platforms")
        raise ValueError(
            "runtime release must cover exactly the requested native platforms: "
            + ", ".join(requested)
        )

    artifacts: list[dict[str, Any]] = []
    resources = output / "resources" / "code4me-runtime"
    resources.mkdir(parents=True)
    for projected in runtime.get("artifacts", []):
        archive = str(projected.get("archive"))
        source = input_path(runtime_manifest.parent, archive)
        digest = file_sha256(source)
        if digest != projected.get("sha256"):
            raise ValueError(f"runtime archive checksum mismatch: {archive}")
        size = source.stat().st_size
        if int(projected.get("size") or size) != size:
            raise ValueError(f"runtime archive size mismatch: {archive}")
        os_name, arch = expected[archive].split("-")
        shutil.copyfile(source, resources / Path(archive).name)
        artifacts.append({
            "runtime_id": str(projected.get("runtime_id") or "code4me-agent"),
            "version": managed.version,
            "platform": os_name,
            "architecture": arch,
            "archive": Path(archive).name,
            "sha256": digest,
            "size": size,
            "executable": str(
                projected.get("executable")
                or ("code4me2-agent.exe" if os_name == "windows" else "code4me2-agent")
            ),
            "managed_protocol": "1",
        })

    agents: list[dict[str, Any]] = []
    for agent in recipe.agents:
        if agent.framework == "code4me2-agent":
            continue
        agents.append({
            "framework": agent.framework,
            "version": agent.version,
            "adapter": agent.adapter.model_dump(mode="json"),
            "agent_command": agent.agent_command,
            "agent_command_args": agent.agent_command_args,
            "byoa_config": [b.model_dump(mode="json") for b in agent.byoa_config],
        })

    profiles = []
    for profile in recipe.profiles:
        profiles.append(dict(
            {"tools_json": "[]", "is_active": True, "temperature": None, "max_context_tokens": None},
            **profile,
        ))

    document = {
        "manifest_version": 1,
        "schema_version": "1",
        "runtime_version": managed.version,
        "managed_protocol_version": "1",
        "plugin_version": recipe.plugin_version,
        "plugin_commit": recipe.plugin_commit,
        "server_commit": recipe.server_commit,
        "adapter": managed.adapter.model_dump(mode="json"),
        "tests": verified_tests,
        "artifacts": artifacts,
        "agents": agents,
        "profiles": profiles,
    }
    document["recipe_digest"] = "sha256:" + hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    write_json(output / "recipe.json", document)
    write_json(output / "prepared-inputs.json", {
        file.relative_to(output).as_posix(): file_sha256(file)
        for file in sorted(output.rglob("*")) if file.is_file()
    })
    return document


def load_prepared(output: Path) -> dict:
    """Catch changed/missing prepared inputs before building or any API write."""
    hashes = json.loads((output / "prepared-inputs.json").read_text())
    if "recipe.json" not in hashes:
        raise ValueError("incomplete preparation input inventory")
    for name, expected in hashes.items():
        if file_sha256(input_path(output, name)) != expected:
            raise ValueError(f"prepared input changed: {name}; prepare into a new directory")
    return json.loads((output / "recipe.json").read_text())
