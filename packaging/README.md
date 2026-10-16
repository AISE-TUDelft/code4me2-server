# Managed runtime packaging

Build on each target operating system and architecture; PyInstaller does not
cross-compile. From the repository root:

```bash
python -m pip install -r packaging/requirements-runtime.lock -r packaging/requirements-build.lock
python -m pip install -r packaging/requirements-test.lock
python -m pip install --no-deps .
PYTHONPATH=src python -m research.study.agents.participant_release native \
  --version 1.2.0 --platform macos-arm64 \
  --server-commit "$(git rev-parse HEAD)" --output dist/release-1.2.0
```

The version is embedded in the executable and must match the immutable runtime
release tag. The workflow performs this stamp automatically;
reset `_build_version.py` to its source-development value after a manual build.

`requirements-runtime.lock` pins the complete participant dependency graph.
Regenerate it with the command recorded in the lock file and review all changes
before publishing a study release.

The one-folder artifact is `dist/code4me2-agent/` (the executable is suffixed
`.exe` on Windows). Archive the directory *contents* as a flat zip: the
executable must sit at the archive root (`code4me2-agent` /
`code4me2-agent.exe`), with `_internal/` beside it. Materialize macOS
framework symlinks as real files under their link paths (the plugin installer
extracts with `ZipInputStream` and does not restore symlinks); the extracted
copy must still pass `--self-check`. The runtime release manifest
(`code4me-managed-runtime-release.json`) supplies each archive's version, target,
SHA-256 digest, size and download URL; the plugin downloads the archive a study
pins and verifies it. The `build-managed-runtime` workflow implements this layout;
do not revert to archiving the parent folder.

Code signing is optional. The plugin downloads the archive itself, verifies the
SHA-256 pinned by the study server and launches the executable directly, so
macOS Gatekeeper (which only assesses quarantined downloads) and Windows
SmartScreen (which only checks Mark-of-the-Web files) never evaluate it. The
pin, not a signature, is the integrity control. Signing mainly helps Windows
hosts with Smart App Control or strict antivirus. To sign, dispatch the
`build-managed-runtime` workflow with `sign: true`: macOS binaries are signed
and the archive notarized, Windows binaries Authenticode-signed, in their native
jobs before the archive is hashed and tested. This needs the `CODE4ME_MACOS_*`,
`CODE4ME_APPLE_*` and `CODE4ME_WINDOWS_*` secrets named in the workflow; a
requested signing run fails when they are missing. Publicly trusted Windows
code-signing keys must live in hardware since June 2023, so the PFX secret only
works for a legacy exportable certificate. Signing changes the archive bytes, so
a signed build is a new runtime version with its own SHA-256.

## Deferred managed runtimes

- TODO: package, register, repair and certify Goose without PATH discovery.
- TODO: package, register, repair and certify Codex without Node/npm or a source checkout.
- TODO: add both runtimes to shared onboarding, policy enforcement and telemetry tests.

Existing Goose and Codex developer integrations remain supported while these
participant distribution tasks are pending.

See [release import and plugin integration](../docs/research-platform/RELEASES.md).
