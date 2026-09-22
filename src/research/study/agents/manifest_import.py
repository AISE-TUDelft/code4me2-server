"""Import a *build* runtime manifest plus its archive bytes into a release.

The packaging pipeline emits a JSON build manifest (``manifest_version``,
``runtime_version``, ``server_commit``/``plugin_commit`` and ``artifacts[]``)
whose ``archive`` fields are **basenames**. This module turns that document into
the registry's own release contract so a fresh database can be made immediately
usable without a human hand-typing a digest:

* the release identity (``agent_id``/``release_id``/``version``) is derived
  deterministically from the manifest, so re-importing identical bytes resolves
  the *same* release instead of piling up duplicates;
* ``source_manifest_digest`` is the SHA-256 of the canonical manifest bytes and
  identifies the **release record**;
* each built ``artifacts[]`` entry is bound to the **exact uploaded archive**
  whose digest and size are recomputed here -- a mismatch or a placeholder
  (all-zero) digest rejects the *entire* import and no release row is created.

Only bytes are accepted: there is no artifact-root lookup and no caller-supplied
size. A manifest that declares an archive the caller did not upload (or an upload
the manifest does not declare) is rejected; a platform the build did not produce
is *omitted from the manifest entirely*, never represented by a placeholder.

The module is deliberately free of database/App dependencies: the API router and
any local-dev seeding share exactly this planning step.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from pydantic import ValidationError

from .enums import DistributionMode, DistributionSourceType, QualificationStatus
from .models import (
    AdapterRef,
    AgentReleaseV1,
    DistributionArtifact,
    ReleaseDisplay,
    ReleaseTests,
)

__all__ = [
    "PLACEHOLDER_DIGEST",
    "ManifestImportError",
    "ManifestImportPlan",
    "build_manifest_release",
    "canonical_manifest_bytes",
    "manifest_digest",
    "sha256_prefixed",
]

#: Sentinel digest a build manifest must never carry for a shipped archive.
PLACEHOLDER_DIGEST = "0" * 64


class ManifestImportError(ValueError):
    """A typed, operator-facing reason a build manifest cannot be imported.

    ``code``/``field`` mirror the registry's typed issue shape so the endpoint
    can return a machine-readable rejection.
    """

    def __init__(self, code: str, message: str, field: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


@dataclass(frozen=True)
class ManifestImportPlan:
    """The release a build manifest + its archives resolve to."""

    release: AgentReleaseV1
    manifest_digest: str
    verified_artifacts: list[dict[str, Any]] = field(default_factory=list)
    byoa_releases: list[AgentReleaseV1] = field(default_factory=list)


def canonical_manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    """Canonical UTF-8 bytes of a manifest mapping.

    Keys are sorted and separators are compact so the digest is stable for the
    same semantic document regardless of formatting/whitespace: a re-import of
    the "same" manifest is idempotent and never a new release.
    """
    return json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    """``sha256:<hex>`` of the canonical manifest bytes (release identity)."""
    return sha256_prefixed(canonical_manifest_bytes(manifest))


def sha256_prefixed(data: bytes) -> str:
    """``sha256:<hex>`` for ``data`` (the repo-wide digest convention)."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _bare_digest(value: Any) -> Optional[str]:
    """Return the bare lowercase hex of a digest, or ``None`` if malformed."""
    text = str(value or "").strip().lower()
    if text.startswith("sha256:"):
        text = text[len("sha256:") :]
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        return None
    return text


def _normalize_os(value: Any) -> str:
    """Map a manifest platform name onto the repo's ``macos|linux|windows``."""
    text = str(value or "").strip().lower()
    if text in {"macos", "darwin", "mac", "macosx"}:
        return "macos"
    if text.startswith("win"):
        return "windows"
    if text.startswith("linux"):
        return "linux"
    raise ManifestImportError(
        "UNSUPPORTED_PLATFORM", f"unknown platform {value!r}", "platform"
    )


def _normalize_arch(value: Any) -> str:
    """Map a manifest architecture onto the repo's ``arm64|x64`` vocabulary."""
    text = str(value or "").strip().lower()
    if text in {"arm64", "aarch64"}:
        return "arm64"
    if text in {"x64", "x86_64", "amd64"}:
        return "x64"
    raise ManifestImportError(
        "UNSUPPORTED_PLATFORM", f"unknown architecture {value!r}", "architecture"
    )


def _as_size(value: Any) -> Optional[int]:
    """Coerce a size-like value to a positive int, or ``None``."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        size = int(value)
    except (TypeError, ValueError):
        return None
    return size if size > 0 else None


def _file_digest_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _archive_basename(value: Any, field: str) -> str:
    """A declared archive must be a bare basename (no directory component)."""
    text = str(value or "").strip()
    if not text:
        raise ManifestImportError(
            "INVALID_MANIFEST", "artifact declares no archive name", field
        )
    if text in {".", ".."} or text != Path(text).name or "/" in text or "\\" in text:
        raise ManifestImportError(
            "INVALID_MANIFEST",
            (
                f"artifact archive {text!r} must be a bare archive basename; "
                "the producer manifest never carries a resource directory prefix"
            ),
            field,
        )
    return text


def _derive_identity(
    manifest: Mapping[str, Any], digest: str
) -> tuple[str, str, str]:
    """Deterministically derive ``(agent_id, release_id, version)``."""
    artifacts = [item for item in manifest.get("artifacts") or [] if isinstance(item, Mapping)]
    runtime_ids = sorted(
        {str(item.get("runtime_id")).strip() for item in artifacts if item.get("runtime_id")}
    )
    if len(runtime_ids) > 1:
        raise ManifestImportError(
            "INVALID_MANIFEST",
            "manifest artifacts disagree on runtime_id: "
            + ", ".join(runtime_ids),
            "artifacts.runtime_id",
        )
    agent_id = runtime_ids[0] if runtime_ids else "code4me-agent"

    version = str(
        manifest.get("runtime_version") or manifest.get("version") or ""
    ).strip()
    if not version:
        raise ManifestImportError(
            "INVALID_MANIFEST",
            "manifest declares no runtime_version",
            "runtime_version",
        )

    # The digest is folded into the release id so a changed manifest is a
    # *distinct* release, exactly as the registry's digest identity requires.
    digest_hex = digest[len("sha256:") :]
    release_id = f"{agent_id}-{version}-{digest_hex[:12]}"
    return agent_id, release_id, version


def _derive_adapter(
    manifest: Mapping[str, Any], *, agent_id: str, version: str
) -> Optional[AdapterRef]:
    """The adapter identity the manifest declares, or ``None``.

    A manifest that carries an explicit ``adapter``/``adapter_ref`` block keeps
    it as-is (including its own ``digest``). Otherwise the release declares **no
    adapter**: the server never invents an adapter identity or reuses the
    manifest/release digest as one, and nothing gates on the adapter anyway.
    """
    explicit = manifest.get("adapter") or manifest.get("adapter_ref")
    if isinstance(explicit, Mapping):
        return AdapterRef.model_validate(dict(explicit))
    return None


def build_manifest_release(
    manifest: Mapping[str, Any],
    *,
    archives: Optional[Mapping[str, Path]] = None,
) -> ManifestImportPlan:
    """Build the :class:`AgentReleaseV1` a build manifest + archives describe.

    ``archives`` maps each declared archive **basename** to the on-disk file the
    caller uploaded. Every declared archive must be present exactly once, every
    uploaded file must be declared, and each file's size and SHA-256 are
    recomputed and must match the manifest. Any failure rejects the whole import.
    """
    if not isinstance(manifest, Mapping):
        raise ManifestImportError(
            "INVALID_MANIFEST", "manifest must be a JSON object", "manifest"
        )
    if manifest.get("manifest_version") not in (None, 1, "1"):
        raise ManifestImportError(
            "INVALID_MANIFEST", "unsupported manifest_version", "manifest_version"
        )
    raw_artifacts = manifest.get("artifacts")
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        raise ManifestImportError(
            "INVALID_MANIFEST",
            "manifest declares no artifacts",
            "artifacts",
        )

    digest = manifest_digest(manifest)
    agent_id, release_id, version = _derive_identity(manifest, digest)
    supplied = dict(archives or {})

    artifacts: list[DistributionArtifact] = []
    tests: list[ReleaseTests] = []
    verified: list[dict[str, Any]] = []
    declared_names: set[str] = set()
    seen_platforms: set[tuple[str, str]] = set()

    for index, raw in enumerate(raw_artifacts):
        if not isinstance(raw, Mapping):
            raise ManifestImportError(
                "INVALID_MANIFEST",
                "each artifacts[] entry must be an object",
                f"artifacts[{index}]",
            )
        raw_platform = raw.get("platform")
        raw_arch = raw.get("architecture")
        os_name = _normalize_os(raw_platform)
        arch = _normalize_arch(raw_arch)
        platform = f"{os_name}-{arch}"
        if (os_name, arch) in seen_platforms:
            raise ManifestImportError(
                "INVALID_MANIFEST",
                f"duplicate artifact for platform {platform!r}",
                f"artifacts[{index}]",
            )
        seen_platforms.add((os_name, arch))
        try:
            tests.append(ReleaseTests.model_validate(dict(raw.get("tests") or {}, os=os_name, arch=arch)))
        except (ValidationError, TypeError, ValueError) as error:
            raise ManifestImportError(
                "TESTS_NOT_PASSED", "each platform requires passing self_check and acp_initialize with ran_at",
                f"artifacts[{index}].tests",
            ) from error


        archive = _archive_basename(raw.get("archive"), f"artifacts[{index}].archive")
        if archive in declared_names:
            raise ManifestImportError(
                "DUPLICATE_ARCHIVE",
                f"manifest declares archive {archive!r} more than once",
                f"artifacts[{index}].archive",
            )
        declared_names.add(archive)

        declared = _bare_digest(raw.get("sha256"))
        if declared is None or declared == PLACEHOLDER_DIGEST:
            raise ManifestImportError(
                "PLACEHOLDER_DIGEST",
                (
                    f"artifact {archive!r} declares a placeholder or malformed "
                    "sha256; a shipped archive must carry its real digest"
                ),
                f"artifacts[{index}].sha256",
            )
        declared_size = _as_size(raw.get("size"))
        if declared_size is None:
            raise ManifestImportError(
                "INVALID_MANIFEST",
                f"artifact {archive!r} declares no positive size",
                f"artifacts[{index}].size",
            )
        artifacts.append(
            DistributionArtifact(
                os=os_name,
                arch=arch,
                path=archive,
                sha256="sha256:" + declared,
                size=declared_size,
                executable=(
                    str(raw.get("executable")).strip()
                    if raw.get("executable")
                    else None
                ),
            )
        )

    unexpected = sorted(set(supplied) - declared_names)
    if unexpected:
        raise ManifestImportError(
            "UNEXPECTED_ARCHIVE",
            "uploaded archive(s) are not declared by the manifest: "
            + ", ".join(unexpected),
            "archives",
        )
    missing = sorted(declared_names - set(supplied))
    if missing:
        raise ManifestImportError(
            "ARTIFACT_MISSING",
            "declared archive(s) were not uploaded: " + ", ".join(missing),
            "archives",
        )

    for artifact in artifacts:
        source = supplied[artifact.path]
        if not Path(source).is_file():
            raise ManifestImportError(
                "ARTIFACT_MISSING",
                f"uploaded archive {artifact.path!r} is not readable",
                "archives",
            )
        actual, size = _file_digest_and_size(Path(source))
        expected = _bare_digest(artifact.sha256)
        if actual != expected:
            raise ManifestImportError(
                "DIGEST_MISMATCH",
                (
                    f"archive {artifact.path!r} digest {actual} does not match the "
                    f"manifest's {expected}"
                ),
                "archives",
            )
        if size != artifact.size:
            raise ManifestImportError(
                "SIZE_MISMATCH",
                (
                    f"archive {artifact.path!r} size {size} does not match the "
                    f"manifest's {artifact.size}"
                ),
                "archives",
            )
        verified.append(
            {
                "archive": artifact.path,
                "platform": f"{artifact.os}-{artifact.arch}",
                "sha256": "sha256:" + actual,
                "size": size,
                "verified": True,
            }
        )

    release = AgentReleaseV1(
        agent_id=agent_id,
        release_id=release_id,
        version=version,
        display=ReleaseDisplay(
            name=f"Code4Me agent runtime {version}",
            vendor="code4me2",
            description="Imported from the built runtime manifest and verified archives.",
        ),
        source_type=DistributionSourceType.BUNDLED,
        source_manifest_digest=digest,
        artifacts=artifacts,
        tests=tests,
        qualification_status=QualificationStatus.QUALIFIED,
        adapter=_derive_adapter(manifest, agent_id=agent_id, version=version),
        created_at=datetime.now(timezone.utc),
    )
    byoa_releases = []
    seen_agents = {agent_id}
    raw_agents = manifest.get("agents", [])
    if not isinstance(raw_agents, list):
        raise ManifestImportError("INVALID_MANIFEST", "agents must be an array", "agents")
    for index, raw in enumerate(raw_agents):
        try:
            from .participant_release import AgentInput

            agent = AgentInput.model_validate(raw)
            if agent.framework == "code4me2-agent" or agent.framework in seen_agents:
                raise ValueError("duplicate or managed agent in external declarations")
            seen_agents.add(agent.framework)
            if not agent.tests:
                raise ValueError("external agents need their own passing platform tests")
            byoa_releases.append(AgentReleaseV1(
                agent_id=agent.framework,
                release_id=f"{agent.framework}-{agent.version}-{digest.removeprefix('sha256:')[:12]}",
                version=agent.version,
                source_manifest_digest=digest,
                distribution_mode=DistributionMode.BYOA_EXTERNAL,
                agent_command=agent.agent_command,
                agent_command_args=agent.agent_command_args,
                agent_package=agent.framework,
                adapter=agent.adapter,
                byoa_config=agent.byoa_config,
                tests=agent.tests,
                qualification_status=QualificationStatus.QUALIFIED,
                created_at=release.created_at,
            ))
        except (ValidationError, TypeError, ValueError) as error:
            raise ManifestImportError("INVALID_MANIFEST", str(error), f"agents[{index}]") from error
    return ManifestImportPlan(
        byoa_releases=byoa_releases,
        release=release,
        manifest_digest=digest,
        verified_artifacts=verified,
    )
