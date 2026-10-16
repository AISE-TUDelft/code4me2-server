"""Package verification and path containment (Issue 11).

``verify_package`` is default-deny: it enforces relative safe paths that stay
under the package root, SHA-256 and size integrity, executability expectations,
no undeclared executables, no secret files, an optional HMAC over the manifest,
and an optional self-check. Every failure is typed; a package that cannot be
fully verified is never considered usable.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .enums import PackageReasonCode
from .models import (
    PackageIssue,
    PackageVerificationResult,
    RuntimeManifestV2,
    normalize_sha256,
)

__all__ = [
    "EXECUTABLE_SUFFIXES",
    "SECRET_FILE_PATTERNS",
    "PathContainment",
    "hash_file",
    "resolve_under_root",
    "scan_secret_files",
    "scan_undeclared_executables",
    "verify_package",
]

#: File suffixes treated as executables even without a POSIX execute bit.
EXECUTABLE_SUFFIXES = frozenset({".exe", ".bat", ".cmd", ".com", ".sh", ".ps1", ".command"})

#: Name components that must never appear in a participant package.
SECRET_FILE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"(^|[._-])\.env(\.[a-z0-9]+)?$",
        r"(^|[._-])id_(rsa|dsa|ecdsa|ed25519)($|[._-])",
        r"(^|[._-])(credentials|credential|secret|secrets|token|tokens)($|[._-])",
        r"(^|[._-])\.(npmrc|netrc|pypirc|git-credentials)$",
        r"\.(pem|key|p12|pfx|jks|keystore)$",
    )
)

#: High-confidence secret *content* patterns for small text files.
SECRET_CONTENT_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"\bsk-[A-Za-z0-9]{16,}",
        r"\bghp_[A-Za-z0-9]{20,}",
        r"\bxox[baprs]-[A-Za-z0-9-]{10,}",
        r"AWS_SECRET_ACCESS_KEY\s*=",
    )
)

_TEXT_SUFFIXES = frozenset(
    {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".txt", ".md", ".env", ".sh"}
)
_MAX_CONTENT_SCAN_BYTES = 256 * 1024

_SIGNATURE_PREFIXES = ("hmac-sha256:", "sha256:")


@dataclass
class PathContainment:
    """Result of resolving a declared relative path under a trusted root."""

    path: Optional[Path] = None
    issue: Optional[PackageIssue] = None
    errors: list[PackageIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether the path is contained and safe."""
        return self.path is not None and self.issue is None


def _issue(code: PackageReasonCode, message: str, field_name: str = "") -> PackageIssue:
    return PackageIssue(code=code, message=message, field=field_name)


def resolve_under_root(root: Path, relative_path: str) -> PathContainment:
    """Resolve ``relative_path`` under ``root``, rejecting escapes and symlink escapes."""
    if not relative_path or not relative_path.strip():
        return PathContainment(issue=_issue(PackageReasonCode.PATH_UNSAFE, "path must not be blank", relative_path))
    if "\x00" in relative_path:
        return PathContainment(issue=_issue(PackageReasonCode.PATH_UNSAFE, "path must not contain NUL", relative_path))
    candidate = Path(relative_path)
    if candidate.is_absolute():
        return PathContainment(issue=_issue(PackageReasonCode.PATH_ESCAPE, f"absolute path '{relative_path}' is not allowed", relative_path))
    if relative_path.startswith("~"):
        return PathContainment(issue=_issue(PackageReasonCode.PATH_ESCAPE, f"home-relative path '{relative_path}' is not allowed", relative_path))
    if any(part == ".." for part in candidate.parts):
        return PathContainment(issue=_issue(PackageReasonCode.PATH_ESCAPE, f"path '{relative_path}' contains '..'", relative_path))

    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        return PathContainment(issue=_issue(PackageReasonCode.PATH_ESCAPE, f"path '{relative_path}' escapes the package root", relative_path))
    if resolved.exists() and resolved.is_symlink():
        real = resolved.resolve()
        if real != resolved_root and resolved_root not in real.parents:
            return PathContainment(issue=_issue(PackageReasonCode.PATH_ESCAPE, f"path '{relative_path}' escapes the package root via a link", relative_path))
    return PathContainment(path=resolved)


def hash_file(path: Path) -> str:
    """Stream a file and return its ``sha256:<hex>`` digest."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _is_executable_file(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.suffix.lower() in EXECUTABLE_SUFFIXES:
        return True
    return hasattr(os, "X_OK") and os.access(path, os.X_OK)


def scan_undeclared_executables(root: Path, declared: set[str]) -> list[str]:
    """Return executable-looking files under ``root`` that no component declares."""
    declared_normalized = {_normalize_relative(value) for value in declared}
    undeclared: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or not _is_executable_file(path):
            continue
        relative = _normalize_relative(str(path.relative_to(root)))
        if relative not in declared_normalized:
            undeclared.append(relative)
    return undeclared


def scan_secret_files(root: Path) -> list[str]:
    """Return files under ``root`` whose name or content looks like a secret."""
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = str(path.relative_to(root))
        if any(part.startswith((".aws", ".ssh", ".gnupg")) for part in path.parts):
            findings.append(relative)
            continue
        if any(pattern.search(path.name.lower()) for pattern in SECRET_FILE_PATTERNS):
            findings.append(relative)
            continue
        if path.suffix.lower() in _TEXT_SUFFIXES and path.stat().st_size <= _MAX_CONTENT_SCAN_BYTES:
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if any(pattern.search(content) for pattern in SECRET_CONTENT_PATTERNS):
                findings.append(relative)
    return findings


def _normalize_relative(value: str) -> str:
    return value.replace("\\", "/").lstrip("./")


def _signature_payload(manifest: RuntimeManifestV2) -> bytes:
    from research.canonical import canonical_bytes

    return canonical_bytes(manifest.canonical_payload())


def _verify_signature(
    manifest: RuntimeManifestV2, secret: Optional[str]
) -> Optional[PackageIssue]:
    if not manifest.signature:
        return None  # a signature is optional; when absent nothing is claimed
    if not secret:
        return _issue(
            PackageReasonCode.SIGNATURE_MISMATCH,
            "manifest claims a signature but no verification secret was supplied",
            "signature",
        )
    expected = hmac.new(secret.encode("utf-8"), _signature_payload(manifest), hashlib.sha256).hexdigest()
    claimed = manifest.signature.strip().lower()
    for prefix in _SIGNATURE_PREFIXES:
        if claimed.startswith(prefix):
            claimed = claimed[len(prefix) :]
            break
    if not hmac.compare_digest(claimed, expected):
        return _issue(
            PackageReasonCode.SIGNATURE_MISMATCH,
            "manifest signature does not match the canonical payload",
            "signature",
        )
    return None


def sign_manifest(manifest: RuntimeManifestV2, secret: str) -> str:
    """Return the HMAC signature over the manifest's canonical payload."""
    return "hmac-sha256:" + hmac.new(
        secret.encode("utf-8"), _signature_payload(manifest), hashlib.sha256
    ).hexdigest()


def verify_package(
    root: Path,
    manifest: RuntimeManifestV2,
    secret: Optional[str] = None,
    now: Optional[object] = None,
    *,
    self_check_runner: Optional[Callable[[RuntimeManifestV2, Path], int]] = None,
) -> PackageVerificationResult:
    """Verify a package directory against ``manifest``. Default-deny."""
    del now  # validation is time-independent; kept for a stable call signature
    errors: list[PackageIssue] = []

    if manifest.schema_version != "2":
        errors.append(
            _issue(
                PackageReasonCode.SCHEMA_VERSION_UNSUPPORTED,
                f"manifest schema_version '{manifest.schema_version}' is not supported",
                "schema_version",
            )
        )
    if not manifest.digest_matches():
        errors.append(
            _issue(PackageReasonCode.DIGEST_MISMATCH, "manifest content does not match manifest_digest", "manifest_digest")
        )
    if not manifest.components:
        errors.append(_issue(PackageReasonCode.EMPTY_COMPONENTS, "manifest declares no components", "components"))

    signature_issue = _verify_signature(manifest, secret)
    if signature_issue is not None:
        errors.append(signature_issue)

    if not root.is_dir():
        errors.append(_issue(PackageReasonCode.ARTIFACT_MISSING, f"package root '{root}' is not a directory", "root"))
        return PackageVerificationResult(valid=False, manifest_digest=manifest.manifest_digest, errors=errors)

    verified = 0
    declared_paths: set[str] = set()
    for component in manifest.components:
        containment = resolve_under_root(root, component.relative_path)
        if not containment.ok:
            errors.append(
                containment.issue
                or _issue(PackageReasonCode.PATH_ESCAPE, "component path is not contained", component.name)
            )
            continue
        declared_paths.add(_normalize_relative(component.relative_path))
        path = containment.path
        assert path is not None
        if not path.is_file():
            errors.append(
                _issue(PackageReasonCode.ARTIFACT_MISSING, f"component '{component.name}' file is missing", component.relative_path)
            )
            continue
        actual_size = path.stat().st_size
        if actual_size != component.size:
            errors.append(
                _issue(
                    PackageReasonCode.SIZE_MISMATCH,
                    f"component '{component.name}' size {actual_size} != declared {component.size}",
                    component.relative_path,
                )
            )
            continue
        if normalize_sha256(hash_file(path)) != normalize_sha256(component.sha256):
            errors.append(
                _issue(PackageReasonCode.DIGEST_MISMATCH, f"component '{component.name}' digest mismatch", component.relative_path)
            )
            continue
        if component.executable and not _is_executable_file(path):
            errors.append(
                _issue(PackageReasonCode.NOT_EXECUTABLE, f"component '{component.name}' is declared executable but is not", component.relative_path)
            )
            continue
        verified += 1

    for relative in scan_undeclared_executables(root, declared_paths):
        errors.append(
            _issue(PackageReasonCode.UNDECLARED_EXECUTABLE, f"executable '{relative}' is not declared by any component", relative)
        )
    for relative in scan_secret_files(root):
        errors.append(
            _issue(PackageReasonCode.SECRET_FILE_PRESENT, f"secret-shaped file '{relative}' must not be packaged", relative)
        )

    if manifest.self_check is not None:
        errors.extend(_verify_self_check(root, manifest, self_check_runner))

    return PackageVerificationResult(
        valid=not errors,
        manifest_digest=manifest.manifest_digest,
        verified_components=verified,
        errors=errors,
    )


def _verify_self_check(
    root: Path,
    manifest: RuntimeManifestV2,
    runner: Optional[Callable[[RuntimeManifestV2, Path], int]],
) -> list[PackageIssue]:
    spec = manifest.self_check
    assert spec is not None
    if not spec.command:
        return [_issue(PackageReasonCode.SELF_CHECK_FAILED, "self-check declares an empty command", "self_check.command")]
    command_path = resolve_under_root(root, spec.command[0])
    if not command_path.ok or command_path.path is None or not command_path.path.exists():
        return [
            _issue(
                PackageReasonCode.SELF_CHECK_FAILED,
                f"self-check executable '{spec.command[0]}' is not present under the package root",
                "self_check.command",
            )
        ]
    if runner is None:
        return []
    try:
        exit_code = runner(manifest, command_path.path)
    except Exception as error:  # noqa: BLE001 - a throwing self-check is a failure
        return [_issue(PackageReasonCode.SELF_CHECK_FAILED, f"self-check raised: {error}", "self_check")]
    if exit_code != spec.expected_exit_code:
        return [
            _issue(
                PackageReasonCode.SELF_CHECK_FAILED,
                f"self-check exited {exit_code}, expected {spec.expected_exit_code}",
                "self_check",
            )
        ]
    return []
