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

from .models import AdapterRef, AgentConfigBinding, ReleaseTests, normalize_platform

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
    tests: list[ReleaseTests] = Field(default_factory=list)

    @model_validator(mode="after")
    def explicit_identity(self):
        if self.version.lower() == "latest" or "pending" in self.version.lower():
            raise ValueError("agent versions must be explicit")
        if self.framework != "code4me2-agent":
            if not self.agent_command:
                raise ValueError("installed agents need an explicit ACP command")
            from .distributions import missing_gateway_bindings, requires_inference_gateway

            if requires_inference_gateway(self.framework):
                missing = missing_gateway_bindings(
                    {binding.field: binding.model_dump(mode="json") for binding in self.byoa_config}
                )
                if missing:
                    raise ValueError(
                        f"{self.framework} releases must bind the research inference gateway: "
                        + ", ".join(missing)
                    )
        elif self.agent_command or self.agent_command_args or self.byoa_config:
            raise ValueError("the managed runtime uses its packaged --managed entrypoint")
        if len({(test.os, test.arch) for test in self.tests}) != len(self.tests):
            raise ValueError("duplicate test platform")
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


def prepare(
    recipe: ParticipantRecipe,
    inputs: Path,
    output: Path,
    platforms: Sequence[str] = PLATFORMS,
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
            # The plugin verifies the bootstrap's adapter pin per artifact, so
            # a recipe-built plugin must declare it there, not only top-level.
            "adapter": managed.adapter.model_dump(mode="json"),
            "tests": projected.get("tests"),
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
            "tests": [t.model_dump(mode="json") for t in agent.tests],
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
        "artifacts": artifacts,
        "agents": agents,
        "profiles": profiles,
    }
    from .manifest_import import build_manifest_release

    build_manifest_release(document, archives={a["archive"]: resources / a["archive"] for a in artifacts})
    document["recipe_digest"] = "sha256:" + hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    write_json(output / "recipe.json", document)
    # The participant plugin build copies ``resources/code4me-runtime`` into the
    # plugin and its runtime installer reads ``code4me-runtime/manifest.json``
    # with archive paths relative to the resource root: the same recipe, in the
    # plugin's spelling.
    write_json(resources / "manifest.json", runtime_manifest_for_plugin(document))
    write_json(output / "prepared-inputs.json", {
        file.relative_to(output).as_posix(): file_sha256(file)
        for file in sorted(output.rglob("*")) if file.is_file()
    })
    return document


def runtime_manifest_for_plugin(document: dict) -> dict:
    """The prepared recipe as the plugin's bundled ``code4me-runtime/manifest.json``."""
    return {
        "manifest_version": 1,
        "runtime_version": document["runtime_version"],
        "managed_protocol_version": document["managed_protocol_version"],
        "server_commit": document.get("server_commit"),
        "plugin_commit": document.get("plugin_commit"),
        "adapter": document.get("adapter"),
        "artifacts": [
            {**artifact, "archive": f"code4me-runtime/{artifact['archive']}"}
            for artifact in document["artifacts"]
        ],
    }


def load_prepared(output: Path) -> dict:
    """Catch changed/missing prepared inputs before building or any API write."""
    hashes = json.loads((output / "prepared-inputs.json").read_text())
    if "recipe.json" not in hashes:
        raise ValueError("incomplete preparation input inventory")
    for name, expected in hashes.items():
        if file_sha256(input_path(output, name)) != expected:
            raise ValueError(f"prepared input changed: {name}; prepare into a new directory")
    return json.loads((output / "recipe.json").read_text())


def main() -> None:
    """The native producer and CI aggregation share the server import contract."""
    import argparse
    import platform
    import subprocess
    import sys
    import tempfile
    import zipfile
    from datetime import datetime, timezone

    from .manifest_import import build_manifest_release

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    native = commands.add_parser("native", help="build, test and emit one native manifest")
    native.add_argument("--version", required=True)
    native.add_argument("--platform", required=True)
    native.add_argument("--server-commit", required=True)
    native.add_argument("--bundle", type=Path, default=Path("dist/code4me2-agent"))
    native.add_argument("--output", type=Path, required=True)
    native.add_argument("--skip-build", action="store_true", help="use an already built/signed bundle")
    merge = commands.add_parser("merge", help="verify and combine native CI artifacts")
    merge.add_argument("--directory", type=Path, required=True)
    merge.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "merge":
        documents = [json.loads(path.read_text()) for path in sorted(args.directory.glob("native-*.json"))]
        if not documents:
            raise ValueError("no native manifests")
        document = dict(documents[0])
        for item in documents:
            if any(item[key] != document[key] for key in ("runtime_version", "server_commit", "managed_protocol_version")):
                raise ValueError("native manifests describe different builds")
        document["artifacts"] = [artifact for item in documents for artifact in item["artifacts"]]
        if {f"{a['platform']}-{a['architecture']}" for a in document["artifacts"]} != set(PLATFORMS):
            raise ValueError("published runtime must cover all four native platforms")
        archives = {a["archive"]: input_path(args.directory, a["archive"]) for a in document["artifacts"]}
        if set(archives) != {p.name for p in args.directory.glob("*.zip")}:
            raise ValueError("archives must exactly match the manifest")
        build_manifest_release(document, archives=archives)
        write_json(args.output, document)
    else:
        target = canonical_platform(args.platform)
        host = canonical_platform(f"{platform.system()}-{platform.machine()}")
        if target != host:
            raise ValueError(f"native tests require {target}, current host is {host}")
        if not re.fullmatch(SEMVER, args.version) or not re.fullmatch(r"[0-9a-f]{40}", args.server_commit):
            raise ValueError("explicit semantic version and full server commit are required")
        if args.output.exists():
            raise ValueError("use a new output directory for an immutable release")
        if not args.skip_build:
            subprocess.run([sys.executable, "packaging/stamp_runtime_version.py", "--version", args.version], check=True, stdout=sys.stderr)
            subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/code4me2_agent"], check=True, stdout=sys.stderr)
            subprocess.run([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", "packaging/code4me2-agent.spec"], check=True, stdout=sys.stderr)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="code4me-native-", dir=args.output.parent) as temporary:
            stage = Path(temporary)
            archive = stage / f"code4me-agent-{target}.zip"
            subprocess.run([sys.executable, "packaging/archive_runtime.py", "--root", str(args.bundle), "--platform", target, "--output", str(archive)], check=True, stdout=sys.stderr)
            extracted = stage / "runtime"
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(extracted)
            executable = "code4me2-agent.exe" if target.startswith("windows") else "code4me2-agent"
            binary = (extracted / executable).resolve()
            binary.chmod(0o755)
            version = subprocess.run([str(binary), "--version"], check=True, capture_output=True, text=True, timeout=60).stdout.strip()
            if version != args.version:
                raise ValueError("packaged executable version does not match the release")
            checked = subprocess.run([str(binary), "--self-check"], check=True, capture_output=True, text=True, timeout=60)
            if json.loads(checked.stdout).get("status") != "ok":
                raise ValueError("packaged self-check failed")
            request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1, "clientCapabilities": {}, "clientInfo": {"name": "code4me-release-test", "version": "1"}}}) + "\n"
            process = subprocess.Popen([str(binary), "--managed"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=extracted)
            try:
                stdout, stderr = process.communicate(request, timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise ValueError("packaged ACP initialize timed out") from None
            responses = [json.loads(line) for line in stdout.splitlines() if line.strip()]
            response = next((item for item in responses if item.get("id") == 1), {})
            if process.returncode or response.get("result", {}).get("protocolVersion") != 1 or "error" in response:
                raise ValueError("packaged ACP initialize failed")
            os_name, arch = target.split("-")
            document = {
                "manifest_version": 1,
                "runtime_version": args.version,
                "managed_protocol_version": "1",
                "server_commit": args.server_commit,
                "artifacts": [{
                    "runtime_id": "code4me-agent", "version": args.version,
                    "platform": os_name, "architecture": arch,
                    "archive": archive.name, "sha256": file_sha256(archive),
                    "size": archive.stat().st_size, "executable": executable,
                    "tests": {"self_check": "PASS", "acp_initialize": "PASS", "ran_at": datetime.now(timezone.utc).isoformat()},
                }],
            }
            build_manifest_release(document, archives={archive.name: archive})
            write_json(stage / f"native-{target}.json", document)
            shutil.rmtree(extracted)
            stage.rename(args.output)
    print(json.dumps(document))


if __name__ == "__main__":
    main()
