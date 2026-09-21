"""Import a producer *recipe* plus its verified archive bytes into release(s).

The single producer (the ``scripts/participant-release.py`` CLI, run locally or
by CI) emits one recipe JSON document. It declares, for every supported
platform, the exact archive **basename**, ``sha256`` and ``size`` of the runtime
ZIP, the adapter identity, the BYOA agent declarations, and the result of the
agent self-check (``tests``). The server turns that document into the registry's
release contract:

* the release identity is derived deterministically from the recipe, so
  re-importing identical bytes resolves the *same* release instead of piling up
  duplicates;
* ``source_manifest_digest`` is the SHA-256 of the canonical recipe bytes;
* every declared archive is bound to the **exact bytes** the caller supplied --
  locally as multipart uploads, or deployed from ``archive_urls`` that the server
  downloads and hashes itself. A digest or size mismatch, a missing, extra or
  duplicated archive, or a recipe that requires bytes it did not receive rejects
  the *entire* import and creates no release row;
* a recipe whose self-check did not pass is rejected: "tests passed" is the only
  thing that makes a release usable.

There is no extracted-file inventory: a runtime's identity is its ZIP
fingerprint plus the adapter and executable names. There is no artifact-root
lookup and no caller-supplied size.

The module is deliberately free of database/App dependencies: the API router and
any local-dev seeding share exactly this planning step.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .enums import DistributionMode, DistributionSourceType
from .models import (
    AdapterRef,
    AgentConfigBinding,
    AgentReleaseV1,
    DistributionArtifact,
    ReleaseDisplay,
)

__all__ = [
    "PLACEHOLDER_DIGEST",
    "ManifestImportError",
    "ManifestImportPlan",
    "build_manifest_releases",
    "canonical_manifest_bytes",
    "manifest_digest",
    "sha256_prefixed",
]

#: Sentinel digest a recipe must never carry for a shipped archive.
PLACEHOLDER_DIGEST = "0" * 64

#: One verified archive: ``basename -> (bare sha256 hex, size)``.
VerifiedArchives = Mapping[str, tuple[str, int]]


class ManifestImportError(ValueError):
    """A typed, operator-facing reason a recipe cannot be imported.

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
    """The release(s) a recipe + its archives resolve to."""

    releases: list[AgentReleaseV1] = field(default_factory=list)
    manifest_digest: str = ""
    verified_artifacts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def release(self) -> AgentReleaseV1:
        """The managed (PACKAGED) release this recipe describes."""
        for release in self.releases:
            if release.distribution_mode == DistributionMode.PACKAGED:
                return release
        raise LookupError("the recipe declares no packaged release")


def canonical_manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    """Canonical UTF-8 bytes of a recipe mapping.

    Keys are sorted and separators are compact so the digest is stable for the
    same semantic document regardless of formatting/whitespace: a re-import of
    the "same" recipe is idempotent and never a new release.
    """
    return json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    """``sha256:<hex>`` of the canonical recipe bytes (release identity)."""
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
    """Map a recipe platform name onto the repo's ``macos|linux|windows``."""
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
    """Map a recipe architecture onto the repo's canonical ``arm64|x64``."""
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


def _archive_basename(value: Any, field_name: str) -> str:
    """A declared archive must be a bare basename (no directory component)."""
    text = str(value or "").strip()
    if not text:
        raise ManifestImportError(
            "INVALID_MANIFEST", "artifact declares no archive name", field_name
        )
    if text != Path(text).name or "/" in text or "\\" in text:
        raise ManifestImportError(
            "INVALID_MANIFEST",
            (
                f"artifact archive {text!r} must be a bare archive basename; "
                "the recipe never carries a directory prefix"
            ),
            field_name,
        )
    return text


def _derive_identity(
    manifest: Mapping[str, Any], digest: str
) -> tuple[str, str, str]:
    """Deterministically derive ``(agent_id, release_id, version)``."""
    artifacts = [
        item for item in manifest.get("artifacts") or [] if isinstance(item, Mapping)
    ]
    runtime_ids = sorted(
        {str(item.get("runtime_id")).strip() for item in artifacts if item.get("runtime_id")}
    )
    if len(runtime_ids) > 1:
        raise ManifestImportError(
            "INVALID_MANIFEST",
            "recipe artifacts disagree on runtime_id: " + ", ".join(runtime_ids),
            "artifacts.runtime_id",
        )
    agent_id = runtime_ids[0] if runtime_ids else "code4me-agent"

    version = str(
        manifest.get("runtime_version") or manifest.get("version") or ""
    ).strip()
    if not version:
        raise ManifestImportError(
            "INVALID_MANIFEST",
            "recipe declares no runtime_version",
            "runtime_version",
        )

    digest_hex = digest[len("sha256:") :]
    release_id = f"{agent_id}-{version}-{digest_hex[:12]}"
    return agent_id, release_id, version


def _derive_adapter(manifest: Mapping[str, Any]) -> Optional[AdapterRef]:
    """The adapter identity the recipe declares, or ``None``.

    A recipe that carries an explicit ``adapter``/``adapter_ref`` block keeps it
    as-is. Otherwise the release declares **no** adapter: the server never
    invents an adapter identity or reuses the recipe digest as one.
    """
    explicit = manifest.get("adapter") or manifest.get("adapter_ref")
    if isinstance(explicit, Mapping):
        return AdapterRef.model_validate(dict(explicit))
    return None


def recipe_tests(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """The self-check block a usable recipe must carry and pass."""
    tests = manifest.get("tests")
    if not isinstance(tests, Mapping):
        raise ManifestImportError(
            "RECIPE_TESTS_FAILED",
            "the recipe declares no agent self-check result",
            "tests",
        )
    if str(tests.get("status", "")).strip().upper() != "PASS":
        raise ManifestImportError(
            "RECIPE_TESTS_FAILED",
            "the recipe's agent self-check did not pass; a release is only usable when tests pass",
            "tests.status",
        )
    approval = tests.get("approval_options")
    if approval is not None and (
        not isinstance(approval, list)
        or not all(isinstance(item, str) for item in approval)
    ):
        raise ManifestImportError(
            "INVALID_MANIFEST",
            "tests.approval_options must be an array of strings",
            "tests.approval_options",
        )
    return dict(tests)


def _verify_archives(
    artifacts: Sequence[DistributionArtifact],
    verified: VerifiedArchives,
) -> list[dict[str, Any]]:
    """Bind each declared artifact to exactly one supplied, matching archive.

    Extra, missing, duplicated or mismatched archives reject the whole import.
    """
    declared_names = [artifact.path for artifact in artifacts]
    supplied = dict(verified)
    for name in supplied:
        candidate = _archive_basename(name, "archives")
        if candidate != name:
            raise ManifestImportError(
                "INVALID_UPLOAD",
                f"uploaded archive {name!r} must be a bare archive basename",
                "archives",
            )
    unexpected = sorted(set(supplied) - set(declared_names))
    if unexpected:
        raise ManifestImportError(
            "UNEXPECTED_ARCHIVE",
            "supplied archive(s) are not declared by the recipe: " + ", ".join(unexpected),
            "archives",
        )
    missing = sorted(set(declared_names) - set(supplied))
    if missing:
        raise ManifestImportError(
            "ARTIFACT_MISSING",
            "declared archive(s) were not supplied: " + ", ".join(missing),
            "archives",
        )
    verified_artifacts: list[dict[str, Any]] = []
    for artifact in artifacts:
        digest_hex, size = supplied[artifact.path]
        expected = _bare_digest(artifact.sha256)
        if expected is None or _bare_digest(digest_hex) != expected:
            raise ManifestImportError(
                "DIGEST_MISMATCH",
                (
                    f"archive {artifact.path!r} digest {digest_hex} does not match "
                    f"the recipe's {expected}"
                ),
                "archives",
            )
        if size != artifact.size:
            raise ManifestImportError(
                "SIZE_MISMATCH",
                (
                    f"archive {artifact.path!r} size {size} does not match the "
                    f"recipe's {artifact.size}"
                ),
                "archives",
            )
        verified_artifacts.append(
            {
                "archive": artifact.path,
                "platform": f"{artifact.os}-{artifact.arch}",
                "sha256": "sha256:" + expected,
                "size": size,
                "verified": True,
            }
        )
    return verified_artifacts


def _byoa_release(raw: Mapping[str, Any], digest: str) -> Optional[AgentReleaseV1]:
    """Build one participant-installed (BYOA) release from a recipe declaration."""
    framework = str(raw.get("framework") or raw.get("agent_id") or "").strip().lower()
    if not framework or framework == "code4me2-agent":
        return None
    version = str(raw.get("version") or "").strip()
    if not version:
        raise ManifestImportError(
            "INVALID_MANIFEST",
            f"agent {framework!r} declares no version",
            "agents.version",
        )
    try:
        adapter = (
            AdapterRef.model_validate(dict(raw["adapter"]))
            if isinstance(raw.get("adapter"), Mapping)
            else None
        )
        bindings = [
            AgentConfigBinding.model_validate(dict(item))
            for item in (raw.get("byoa_config") or [])
        ]
        command_args = [str(item) for item in (raw.get("agent_command_args") or [])]
    except (TypeError, ValueError) as error:
        raise ManifestImportError(
            "INVALID_MANIFEST",
            f"agent {framework!r} declaration is invalid: {error}",
            "agents",
        ) from error
    declaration_digest = manifest_digest(dict(raw))
    return AgentReleaseV1(
        agent_id=framework,
        release_id=f"{framework}-{version}-{declaration_digest[7:19]}",
        version=version,
        display=ReleaseDisplay(name=f"{framework} {version}", vendor=framework),
        source_type=DistributionSourceType.BUNDLED,
        source_manifest_digest=declaration_digest,
        distribution_mode=DistributionMode.BYOA_EXTERNAL,
        agent_package=framework,
        agent_command=str(raw.get("agent_command") or "").strip() or None,
        agent_command_args=command_args,
        byoa_config=bindings,
        adapter=adapter,
        created_at=datetime.now(timezone.utc),
    )


def build_manifest_releases(
    manifest: Mapping[str, Any],
    *,
    verified: Optional[VerifiedArchives] = None,
) -> ManifestImportPlan:
    """Build the release(s) a recipe plus its verified archive bytes describe.

    ``verified`` maps each declared archive **basename** to ``(sha256, size)``
    that the caller computed from the real bytes (an upload or a download). Every
    declared archive must be present exactly once, every supplied archive must be
    declared, and each must match the recipe. Any failure rejects the whole import.
    """
    if not isinstance(manifest, Mapping):
        raise ManifestImportError(
            "INVALID_MANIFEST", "recipe must be a JSON object", "manifest"
        )
    if manifest.get("manifest_version") not in (None, 1, "1"):
        raise ManifestImportError(
            "INVALID_MANIFEST", "unsupported manifest_version", "manifest_version"
        )
    tests = recipe_tests(manifest)

    raw_artifacts = manifest.get("artifacts")
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        raise ManifestImportError(
            "INVALID_MANIFEST", "recipe declares no artifacts", "artifacts"
        )

    digest = manifest_digest(manifest)
    agent_id, release_id, version = _derive_identity(manifest, digest)

    artifacts: list[DistributionArtifact] = []
    declared_names: set[str] = set()
    seen_platforms: set[tuple[str, str]] = set()
    for index, raw in enumerate(raw_artifacts):
        if not isinstance(raw, Mapping):
            raise ManifestImportError(
                "INVALID_MANIFEST",
                "each artifacts[] entry must be an object",
                f"artifacts[{index}]",
            )
        os_name = _normalize_os(raw.get("platform"))
        arch = _normalize_arch(raw.get("architecture"))
        platform = f"{os_name}-{arch}"
        if (os_name, arch) in seen_platforms:
            raise ManifestImportError(
                "INVALID_MANIFEST",
                f"duplicate artifact for platform {platform!r}",
                f"artifacts[{index}]",
            )
        seen_platforms.add((os_name, arch))

        archive = _archive_basename(raw.get("archive"), f"artifacts[{index}].archive")
        if archive in declared_names:
            raise ManifestImportError(
                "DUPLICATE_ARCHIVE",
                f"recipe declares archive {archive!r} more than once",
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
                    str(raw.get("executable")).strip() if raw.get("executable") else None
                ),
            )
        )

    verified_artifacts = _verify_archives(artifacts, verified or {})

    managed = AgentReleaseV1(
        agent_id=agent_id,
        release_id=release_id,
        version=version,
        display=ReleaseDisplay(
            name=f"Code4Me agent runtime {version}",
            vendor="code4me2",
            description="Imported from the produced recipe and its verified archives.",
        ),
        source_type=DistributionSourceType.BUNDLED,
        source_manifest_digest=digest,
        artifacts=artifacts,
        adapter=_derive_adapter(manifest),
        created_at=datetime.now(timezone.utc),
    )
    releases = [managed]
    for raw in manifest.get("agents") or []:
        if not isinstance(raw, Mapping):
            raise ManifestImportError(
                "INVALID_MANIFEST", "each agents[] entry must be an object", "agents"
            )
        byoa = _byoa_release(raw, digest)
        if byoa is not None:
            releases.append(byoa)

    return ManifestImportPlan(
        releases=releases,
        manifest_digest=digest,
        verified_artifacts=verified_artifacts,
    )
