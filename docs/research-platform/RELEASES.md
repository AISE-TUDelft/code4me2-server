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

## Participant inference budgets (shared provider key)

Goose and built-in (`code4me2-agent`) study arms now spend from the study's
server-held provider key through a metered relay, and every enrollment carries
a USD budget. The consolidated revision creates the tables and columns below, so
a fresh database gets them; a database that is already at that revision does
not. Before deploying to an existing database, run:

```sql
CREATE TABLE IF NOT EXISTS public.provider_model_price (
    connection_id UUID NOT NULL REFERENCES public.provider_connection(connection_id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    input_usd_per_million NUMERIC(14,6) NOT NULL CHECK (input_usd_per_million >= 0),
    output_usd_per_million NUMERIC(14,6) NOT NULL CHECK (output_usd_per_million >= 0),
    cached_input_usd_per_million NUMERIC(14,6) NULL CHECK (cached_input_usd_per_million IS NULL OR cached_input_usd_per_million >= 0),
    updated_at TIMESTAMPTZ NOT NULL,
    updated_by TEXT NULL,
    PRIMARY KEY (connection_id, model)
);
CREATE INDEX IF NOT EXISTS idx_provider_model_price_connection ON public.provider_model_price (connection_id);

ALTER TABLE public.study
    ADD COLUMN IF NOT EXISTS inference_budget_default_micro_usd BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS inference_budget_warning_fraction NUMERIC(4,3) NOT NULL DEFAULT 0.800,
    ADD COLUMN IF NOT EXISTS inference_budget_updated_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS inference_budget_updated_by TEXT NULL;

CREATE TABLE IF NOT EXISTS public.enrollment_inference_balance (
    enrollment_id UUID PRIMARY KEY REFERENCES public.research_enrollment(enrollment_id) ON DELETE CASCADE,
    study_id UUID NOT NULL REFERENCES public.study(study_id) ON DELETE CASCADE,
    unit TEXT NOT NULL DEFAULT 'micro_usd',
    limit_micro_usd BIGINT NOT NULL CHECK (limit_micro_usd >= 0),
    settled_micro_usd BIGINT NOT NULL DEFAULT 0 CHECK (settled_micro_usd >= 0),
    reserved_micro_usd BIGINT NOT NULL DEFAULT 0 CHECK (reserved_micro_usd >= 0),
    settled_prompt_tokens BIGINT NOT NULL DEFAULT 0,
    settled_completion_tokens BIGINT NOT NULL DEFAULT 0,
    call_count INTEGER NOT NULL DEFAULT 0,
    refused_count INTEGER NOT NULL DEFAULT 0,
    last_call_at TIMESTAMPTZ NULL,
    limit_source TEXT NOT NULL DEFAULT 'STUDY_DEFAULT',
    exhausted_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_enrollment_inference_balance_study_id ON public.enrollment_inference_balance (study_id);

CREATE TABLE IF NOT EXISTS public.inference_reservation (
    reservation_id UUID PRIMARY KEY,
    enrollment_id UUID NOT NULL REFERENCES public.enrollment_inference_balance(enrollment_id) ON DELETE CASCADE,
    study_id UUID NOT NULL,
    connection_id UUID NULL REFERENCES public.provider_connection(connection_id) ON DELETE SET NULL,
    model TEXT NOT NULL,
    entry_point TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    research_session_id UUID NULL,
    state TEXT NOT NULL CHECK (state IN ('RESERVED', 'SETTLED', 'FORFEITED', 'VOIDED', 'EXPIRED')),
    hold_micro_usd BIGINT NOT NULL CHECK (hold_micro_usd >= 0),
    estimated_prompt_tokens INTEGER NOT NULL,
    output_cap_tokens INTEGER NOT NULL,
    charged_micro_usd BIGINT NULL CHECK (charged_micro_usd IS NULL OR charged_micro_usd >= 0),
    prompt_tokens INTEGER NULL,
    completion_tokens INTEGER NULL,
    cached_prompt_tokens INTEGER NULL,
    usage_source TEXT NULL,
    resolution_reason TEXT NULL,
    upstream_status INTEGER NULL,
    finish_reason TEXT NULL,
    reserved_at TIMESTAMPTZ NOT NULL,
    deadline_at TIMESTAMPTZ NOT NULL,
    resolved_at TIMESTAMPTZ NULL
);
CREATE INDEX IF NOT EXISTS idx_inference_reservation_enrollment_state ON public.inference_reservation (enrollment_id, state);
CREATE INDEX IF NOT EXISTS idx_inference_reservation_open_deadline ON public.inference_reservation (deadline_at) WHERE state = 'RESERVED';
CREATE INDEX IF NOT EXISTS idx_inference_reservation_study_reserved_at ON public.inference_reservation (study_id, reserved_at);

CREATE TABLE IF NOT EXISTS public.inference_budget_adjustment (
    adjustment_id UUID PRIMARY KEY,
    enrollment_id UUID NOT NULL REFERENCES public.enrollment_inference_balance(enrollment_id) ON DELETE CASCADE,
    study_id UUID NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('TOP_UP', 'SET_LIMIT', 'APPLY_DEFAULT', 'BACKFILL')),
    delta_micro_usd BIGINT NOT NULL,
    limit_before_micro_usd BIGINT NOT NULL,
    limit_after_micro_usd BIGINT NOT NULL,
    in_flight_micro_usd BIGINT NOT NULL DEFAULT 0,
    reason TEXT NULL,
    actor TEXT NULL,
    idempotency_key TEXT NULL,
    request_digest TEXT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    UNIQUE (study_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_inference_budget_adjustment_enrollment ON public.inference_budget_adjustment (enrollment_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_inference_budget_adjustment_study ON public.inference_budget_adjustment (study_id, occurred_at);

-- Give every existing enrollment a balance row at its study's default.
INSERT INTO public.enrollment_inference_balance (enrollment_id, study_id, limit_micro_usd, limit_source, created_at, updated_at)
SELECT e.enrollment_id, e.study_id, s.inference_budget_default_micro_usd, 'BACKFILL', now(), now()
FROM public.research_enrollment e
JOIN public.study s ON s.study_id = e.study_id
ON CONFLICT (enrollment_id) DO NOTHING;
```

This is fail-closed on purpose. A study's default budget is 0 until a
researcher sets it (study Settings → Participant budgets, then "Apply new
default" for the participants already enrolled), and a model without a price
row on its provider connection refuses every call with `503 price_missing`.
Until both are configured, Goose and built-in arms receive `402 quota_exhausted`
or `503 price_missing` and spend nothing. Codex arms are unaffected: they sign in
with ChatGPT and are not metered.
