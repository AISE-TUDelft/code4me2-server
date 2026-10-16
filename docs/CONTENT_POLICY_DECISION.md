# Content-storage policy decision (ISSUE-01)

Recorded: 2026-09-19.

## Authority

Content storage for a research-bound context is decided in exactly one place,
`research/telemetry/content_policy.py`, from:

- the account's **ACTIVE enrollment** for the task's study, with accepted
  consent, and
- the study's **frozen telemetry policy** (`research_config_json.telemetry_policy`)
  resolved through the shared `PrivacyPolicy` engine.

Missing or malformed policy denies. The legacy account preference
(`store_agent_content`) is *not* study consent and is only consulted for
genuinely non-research contexts (no study binding), where there is no study
policy to enforce. Every persistence boundary — managed-agent ingest, the
plugin/proxy task paths, the legacy relay adapter, and the ACP policy endpoints
— resolves through this authority.

## `code_metadata_mode`

The study policy controls how code metadata is persisted:

- **`hash` (default)** — code metadata is stored as a SHA-256 token. This is the
  mode when the policy does not explicitly allow `CODE_METADATA` (including when
  no policy is declared at all for a non-research context).
- **`allow`** — the raw value is stored, only when the frozen policy's
  `allowed_field_classes` explicitly contains `CODE_METADATA`.

The server never silently rewrites already-stored research data. The ingestion
writer validates the canonical event against the policy and **rejects** a
non-compliant event (`PRIVACY_BLOCKED`) instead of editing its bytes, because a
rewrite would invalidate the producer's digest and turn a retry into an
integrity conflict. Producers are expected to filter before sending; the
canonical adapter builds content-bearing fields only when the resolved policy
allows content.
