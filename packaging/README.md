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
release tag and plugin manifest. The workflow performs this stamp automatically;
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
copy must still pass `--self-check`. The plugin release manifest supplies its
version, target and SHA-256 digest and embeds one archive per supported
target. The `build-managed-runtime` workflow implements this layout; do not
revert to archiving the parent folder.

macOS builds must be signed and notarized after collection. Windows builds
must be Authenticode-signed after collection. Perform both operations in their
native release jobs and test the final signed archive rather than this raw
directory. Publishing from the workflow requires the `CODE4ME_MACOS_*`,
`CODE4ME_APPLE_*`, and `CODE4ME_WINDOWS_*` signing secrets named in the
workflow; missing secrets fail the release rather than publishing unsigned
participant binaries.

## Deferred managed runtimes

- TODO: package, register, repair and certify Goose without PATH discovery.
- TODO: package, register, repair and certify Codex without Node/npm or a source checkout.
- TODO: add both runtimes to shared onboarding, policy enforcement and telemetry tests.

Existing Goose and Codex developer integrations remain supported while these
participant distribution tasks are pending.

See [release import and plugin integration](../docs/research-platform/RELEASES.md).
