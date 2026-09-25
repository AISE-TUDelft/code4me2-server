# Tested release import

> From scratch setup: [`FROM_SCRATCH_SETUP.md`](FROM_SCRATCH_SETUP.md).

The producer owns the test results. The server verifies archive bytes and makes
passing releases usable immediately. No host approval, conformance receipt, or
“Mark tested” action is needed. `DISABLED` permanently prevents a release from
being selected for a new bootstrap; reimport cannot enable it. Existing running
sessions are not terminated by this endpoint (use the runtime kill switch).

## Local native build

Install the existing locked build/runtime/test dependencies as described in
`packaging/README.md`, then run **from the server repository** (the module lives
in `code4me2-server/src/research`, so `PYTHONPATH=src` only resolves there):

```bash
cd code4me2-server

PYTHONPATH=src python -m research.study.agents.participant_release native \
  --version 1.2.3 --platform macos-arm64 \
  --server-commit "$(git rev-parse HEAD)" --output dist/release-1.2.3
```

This stamps the version, runs the runtime tests, builds with PyInstaller, archives
it, extracts that archive, and checks the extracted executable's version,
`--self-check` and ACP `initialize` response. It writes the ZIP and one
`native-macos-arm64.json`, and prints that manifest JSON on stdout. A failed
check produces no output release directory. Use a new output directory/version;
`--skip-build` is for an already built/signed native bundle, and still tests the
archived executable — with `--skip-build` the `--version` **must equal the
version already embedded in that bundle** (run without it to stamp a new
version), otherwise the producer refuses with "packaged executable version does
not match the release". Local release coverage is explicitly one native platform.
The version stamp updates `src/code4me2_agent/_build_version.py`.

`arm64` is canonical; `aarch64` is accepted at input boundaries. The producer
requires a matching native host. Build artifacts do not imply IntelliJ UI,
permission enforcement, inference, or tool-call end-to-end acceptance.

## GitHub release

Run `.github/workflows/build-managed-runtime.yml` manually with a version and
`publishRelease=true`. Four native jobs build and test signed binaries. macOS
archives also require successful notarization. The publishing job calls the same
module's `merge` command, validates every ZIP, requires all four platforms, and
publishes only the combined JSON plus ZIPs under `runtime-v<version>`. Existing
tags/releases cannot be overwritten. Signing secrets remain necessary.

## Admin import

Dashboard → Agent Catalogue accepts a JSON file (or pasted JSON) plus exactly
its declared ZIPs. Alternatively select “Import from release URLs”, enter a
manifest URL and one archive URL per line. Both paths perform the same checks.

- `POST /api/research/agents/releases/import`: multipart `manifest` JSON text,
  repeated `archives` file fields.
- `POST /api/research/agents/releases/import-url`: JSON `manifest_url` and
  `archive_urls` string array.
- `POST /api/research/agents/releases/{release_id}/disable`: terminal disable.

HTTPS download hosts default to `github.com`, `release-assets.githubusercontent.com`
and `objects.githubusercontent.com`. Operators can explicitly configure trusted
hosts with `RESEARCH_RELEASE_HOSTS`; every redirect is checked. Manifest downloads
are limited to 1 MiB; `RESEARCH_IMPORT_MAX_ARCHIVE_BYTES` and
`RESEARCH_IMPORT_MAX_TOTAL_BYTES` control archive/import limits. Downloads are
streamed to temporary files and removed after validation. Imported archives are
not a download CDN: participant distribution is still the published plugin/agent.

The archive declaration is:

```json
{
  "runtime_id": "code4me-agent",
  "version": "1.2.3",
  "platform": "macos",
  "architecture": "arm64",
  "archive": "code4me-agent-macos-arm64.zip",
  "sha256": "<64 hex characters>",
  "size": 12345,
  "executable": "code4me2-agent",
  "tests": {
    "self_check": "PASS",
    "acp_initialize": "PASS",
    "ran_at": "2026-09-21T12:00:00Z"
  }
}
```

Each declared archive needs its own passing results. Missing/extra/duplicate
archives, digest/size mismatches, missing or failed tests reject the import.
Every managed and BYOA row is written in one database transaction. Identical
imports are idempotent. The canonical manifest digest pins release identity;
archive SHA-256 pins distributed bytes. No extracted-file inventory is stored.

Admin import is the trust boundary for the producer's result declaration. Hashing
bytes detects tampering; it does not independently prove tests were executed or
prove all IDE behavior. Only import output from the controlled producer/CI.

## BYOA and plugin boundary

The optional `agents` array carries external `framework`, explicit `version`,
`agent_command`, optional `agent_command_args`/`byoa_config`, `adapter`, and
`tests` (an array of the results above plus `os`/`arch`). External agents require
their own results; managed-runtime results never qualify Goose or Codex. Server
import never runs an uploaded command. The manifest digest binds these identities.

The plugin is the IDE integration; the proxy observes/transports ACP; the agent
executes tasks. Managed agents ship inside the plugin; BYOA agents are installed
separately. The existing signed bootstrap still pins release/version/adapter and
command/package identity. Local executable identity enforcement belongs to the
plugin and is not demonstrated by server tests.

Only the server repository changed. The plugin's participant-release CLI must
adopt per-artifact `tests` and per-agent `tests`, read/stage the producer manifest,
retain its archive consistency build gate, and present platform installation
instructions. Its prior global `tests.status` and approval endpoints are no longer
supported. Plugin ZIP publication, native host permission/tool behavior and all
four CI jobs require their own verification. Server protocol restrictions on
participant-ready runtimes are unchanged.

## Existing data

This repository uses a consolidated fresh schema. The obsolete
`approved_hosts_json` column is removed from that schema and ORM. An existing
physical column may remain inert until the normal schema replacement process;
this change does not reset the application database. Historical releases without
platform results remain unqualified; import a newly tested manifest rather than
converting old manual approvals into passing tests. Legacy aggregate test verdicts
also stay unqualified. Existing `DISABLED`, `BLOCKED`, and `RETIRED` states remain
terminal on reads.

Agent profiles now store an optional `system_prompt` (packaged releases only). The
column is part of the consolidated revision, so a fresh database gets it, but a
database that is already at that revision does not: every profile query fails
with an undefined-column error until you add it. Before deploying to an existing
database, run:

```sql
ALTER TABLE public.agent_profile ADD COLUMN IF NOT EXISTS system_prompt TEXT;
```

The column is nullable and the previous code ignores it, so it is safe to add
while the old backend is still running. Existing profiles, their configuration
digests and frozen studies are unchanged: a prompt only enters a digest when set.
