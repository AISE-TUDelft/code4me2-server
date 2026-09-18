"""Import a *build* runtime manifest into a digest-pinned :class:`AgentReleaseV1`.

The packaging pipeline emits a JSON build manifest (``manifest_version``,
``runtime_version``, ``server_commit``/``plugin_commit`` and ``artifacts[]``).
This module turns that document into the registry's own release contract so a
fresh database can be made immediately usable without a human hand-typing a
digest:

* the release identity (``agent_id``/``release_id``/``version``) is derived
  deterministically from the manifest, so re-importing identical bytes resolves
  the *same* release instead of piling up duplicates;
* ``source_manifest_digest`` is the SHA-256 of the canonical manifest bytes;
* each built ``artifacts[]`` entry becomes a :class:`DistributionArtifact` whose
  ``sha256`` is verified against the real archive **when the archive is present
  under ``artifact_root``** -- a mismatch fails loudly rather than silently
  trusting the declared digest;
* an artifact with a placeholder (all-zero) digest is a platform the build did
  not actually produce. It is *excluded* and reported in ``skipped`` rather than
  imported as a fake digest-pinned artifact.

The module is deliberately free of database/App dependencies: the API router and
the local-dev seeder share exactly this planning step.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from .enums import DistributionSourceType
from .models import (
    AdapterRef,
    AgentReleaseV1,
    DistributionArtifact,
    ReleaseDisplay,
)

__all__ = [
    "ManifestImportError",
    "ManifestImportPlan",
    "SkippedArtifact",
    "build_manifest_release",
    "canonical_manifest_bytes",
    "manifest_digest",
    "sha256_prefixed",
]

#: Sentinel digest a build manifest uses for a platform it did not package.
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
class SkippedArtifact:
    """One ``artifacts[]`` entry that was not imported, and why."""

    platform: str
    archive: str
    reason: str


@dataclass(frozen=True)
class ManifestImportPlan:
    """The release a build manifest resolves to, plus what was excluded."""

    release: AgentReleaseV1
    manifest_digest: str
    skipped: list[SkippedArtifact] = field(default_factory=list)


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
    """``sha256:<hex>`` of the canonical manifest bytes."""
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


def _resolve_archive(artifact_root: Optional[Path], archive: str) -> Optional[Path]:
    """The on-disk path of an artifact archive, or ``None`` when unavailable."""
    if artifact_root is None or not archive:
        return None
    candidate = (artifact_root / archive).resolve()
    if candidate.is_file():
        return candidate
    basename = (artifact_root / Path(archive).name).resolve()
    if basename.is_file():
        return basename
    return None


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _size_from_supplied(
    *,
    archive: str,
    platform: str,
    raw_platform: str,
    raw_arch: str,
    sizes: Mapping[str, Any],
) -> Optional[int]:
    """Look up a caller-supplied size by archive path / platform key / basename."""
    for key in (
        archive,
        platform,
        f"{raw_platform}-{raw_arch}",
        Path(archive).name,
    ):
        if key and key in sizes:
            resolved = _as_size(sizes[key])
            if resolved is not None:
                return resolved
    return None


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
    manifest: Mapping[str, Any], *, agent_id: str, version: str, digest: str
) -> AdapterRef:
    """The adapter identity for the release.

    A manifest may carry an explicit ``adapter``/``adapter_ref`` block; when it
    does not, the packaging layer that emitted this manifest is identified by the
    manifest digest itself (no digest is invented from thin air).
    """
    explicit = manifest.get("adapter") or manifest.get("adapter_ref")
    if isinstance(explicit, Mapping):
        return AdapterRef.model_validate(dict(explicit))
    return AdapterRef(
        adapter_id=f"{agent_id}-managed-adapter",
        version=version,
        digest=digest,
    )


def build_manifest_release(
    manifest: Mapping[str, Any],
    *,
    artifact_root: Optional[str | Path] = None,
    artifact_sizes: Optional[Mapping[str, Any]] = None,
) -> ManifestImportPlan:
    """Build the :class:`AgentReleaseV1` a build manifest describes.

    ``artifact_root`` is the directory the manifest's relative ``archive`` paths
    are resolved against. When a listed archive exists there its real digest and
    size are computed and the declared digest is verified; when it does not, the
    declared digest is trusted but an explicit size must be supplied (from the
    manifest itself or ``artifact_sizes``) -- a size is never invented.
    """
    if not isinstance(manifest, Mapping):
        raise ManifestImportError(
            "INVALID_MANIFEST", "manifest must be a JSON object", "manifest"
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
    root = Path(artifact_root) if artifact_root is not None else None
    sizes = dict(artifact_sizes or {})

    artifacts: list[DistributionArtifact] = []
    skipped: list[SkippedArtifact] = []
    seen: set[tuple[str, str]] = set()

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
        if (os_name, arch) in seen:
            raise ManifestImportError(
                "INVALID_MANIFEST",
                f"duplicate artifact for platform {platform!r}",
                f"artifacts[{index}]",
            )
        seen.add((os_name, arch))

        archive = str(raw.get("archive") or "").strip()
        declared = _bare_digest(raw.get("sha256"))
        archive_path = _resolve_archive(root, archive)

        if declared is None or declared == PLACEHOLDER_DIGEST:
            if archive_path is not None:
                raise ManifestImportError(
                    "DIGEST_MISMATCH",
                    (
                        f"artifact {archive!r} exists on disk but the manifest "
                        "declares a placeholder digest; a real artifact must "
                        "carry its real sha256"
                    ),
                    f"artifacts[{index}].sha256",
                )
            # A placeholder digest is a platform the build did not produce. It is
            # excluded explicitly instead of being imported as a fake pin.
            skipped.append(
                SkippedArtifact(
                    platform=platform,
                    archive=archive,
                    reason="manifest digest is a placeholder (platform not built)",
                )
            )
            continue

        if archive_path is not None:
            real = _file_digest(archive_path)
            if real != declared:
                raise ManifestImportError(
                    "DIGEST_MISMATCH",
                    (
                        f"artifact {archive!r} digest {real} does not match the "
                        f"manifest's {declared}"
                    ),
                    f"artifacts[{index}].sha256",
                )
            size = archive_path.stat().st_size
        else:
            size = _as_size(raw.get("size"))
            if size is None:
                size = _size_from_supplied(
                    archive=archive,
                    platform=platform,
                    raw_platform=str(raw_platform or ""),
                    raw_arch=str(raw_arch or ""),
                    sizes=sizes,
                )
            if size is None:
                raise ManifestImportError(
                    "SIZE_MISSING",
                    (
                        f"artifact {archive or platform!r} is not available under "
                        "the artifact root and the manifest/caller supplies no "
                        "size; provide artifact_sizes for it"
                    ),
                    f"artifacts[{index}].size",
                )

        executable = raw.get("executable")
        artifacts.append(
            DistributionArtifact(
                os=os_name,
                arch=arch,
                path=archive or f"code4me-runtime/{platform}",
                sha256="sha256:" + declared,
                size=size,
                executable=str(executable).strip() if executable else None,
            )
        )

    if not artifacts:
        raise ManifestImportError(
            "INCOMPLETE_ARTIFACTS",
            "manifest has no built artifact with a real digest",
            "artifacts",
        )

    release = AgentReleaseV1(
        agent_id=agent_id,
        release_id=release_id,
        version=version,
        display=ReleaseDisplay(
            name=f"Code4Me agent runtime {version}",
            vendor="code4me2",
            description="Imported from the built runtime manifest.",
        ),
        source_type=DistributionSourceType.BUNDLED,
        source_manifest_digest=digest,
        artifacts=artifacts,
        adapter=_derive_adapter(
            manifest, agent_id=agent_id, version=version, digest=digest
        ),
        created_at=datetime.now(timezone.utc),
    )
    return ManifestImportPlan(release=release, manifest_digest=digest, skipped=skipped)
