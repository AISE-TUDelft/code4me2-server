"""Synthetic package assembly helper (Issue 11).

``build_package`` writes a directory of files and returns a fully-populated,
digest-correct :class:`RuntimeManifestV2`. It exists so tests (and a future CI
builder) can assemble a package without any network or host state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping, Optional, Sequence

from .models import (
    ComponentEntry,
    PlatformTriple,
    ProtocolCompatibility,
    RuntimeManifestV2,
    SelfCheckSpec,
    sha256_of_bytes,
)
from .verifier import sign_manifest

if TYPE_CHECKING:
    from pathlib import Path

    from research.study.agents.models import AdapterRef

__all__ = ["build_package", "write_manifest"]


def build_package(
    root: Path,
    *,
    release_id: str,
    agent_id: str,
    adapter_ref: AdapterRef,
    os: str,
    arch: str,
    files: Mapping[str, bytes],
    executable: Optional[str] = None,
    component_name: str = "runtime",
    args_template: Optional[Sequence[str]] = None,
    licenses: Optional[Sequence[str]] = None,
    compatibility: Optional[ProtocolCompatibility] = None,
    self_check: Optional[SelfCheckSpec] = None,
    sign_secret: Optional[str] = None,
) -> RuntimeManifestV2:
    """Write ``files`` under ``root`` and return a digest-correct manifest."""
    root.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        if relative == executable:
            target.chmod(target.stat().st_mode | 0o111)

    if executable is not None and executable not in files:
        raise ValueError(f"executable '{executable}' is not one of the package files")

    # One component per payload file keeps every declared path exact.
    components = [
        ComponentEntry(
            name=component_name if relative == (executable or "") else f"{component_name}:{relative}",
            os=os,
            arch=arch,
            relative_path=relative,
            sha256=sha256_of_bytes(content),
            size=len(content),
            executable=(relative == executable),
        )
        for relative, content in files.items()
    ]

    manifest = RuntimeManifestV2(
        schema_version="2",
        release_id=release_id,
        agent_id=agent_id,
        adapter_ref=adapter_ref,
        components=components,
        supported_platforms=[PlatformTriple(os=os, arch=arch)],
        args_template={f"{os}-{arch}": list(args_template or [])},
        licenses=list(licenses or []),
        compatibility=compatibility or ProtocolCompatibility(),
        self_check=self_check,
    )
    if sign_secret:
        # Sign the unsigned payload, then digest the signed manifest.
        manifest = manifest.model_copy(update={"signature": sign_manifest(manifest, sign_secret)})
    return manifest.with_digest()


def write_manifest(path: Path, manifest: RuntimeManifestV2) -> Path:
    """Write a manifest as canonical JSON to ``path``."""
    from research.canonical import canonical_json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(manifest.model_dump(mode="json")), encoding="utf-8")
    return path
