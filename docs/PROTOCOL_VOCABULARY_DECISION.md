# Protocol vocabulary decision: `condition_id` and the revision-era validators

- Status: recorded 2026-09-19 (ISSUE-19 item 1; review §12.3(6)/§14.3).
- Owner: `code4me2-server` server/protocol maintainers.
- Review trigger: the next protocol cleanup after ISSUE-19. Every allowlisted
  symbol must then be deleted, or re-justified with a fresh owner and trigger.

## Decision

The remaining `condition_id` protocol-authoring vocabulary
(`research.study.protocol.models.StudyCondition`,
`research.study.protocol.validation` condition uniqueness/weights, and
`research.study.protocol.canonical` condition ordering) has **no execution
consumer** in the revision-free study lifecycle.

It is kept (not deleted) and stays on the forbidden-symbol allowlist as a
temporary retirement record, with an owner and a planned-removal review trigger
per entry (`scripts/dev/forbidden_symbols.txt`). The validation module is not
deleted: it still backs the authoring-only protocol contract, and removing it
before the current profile/study invariants were demonstrated independently
would have been unjustified. The scan keeps the vocabulary from spreading into
new code: any occurrence outside the three allowlisted files fails closed.

## Current invariants are enforced independently

The current `create_study()` / clone / profile invariants do not pass through the
revision-era validators:

- profile owner, active state, release presence, released status
  (`RETIRED`/`BLOCKED` withdrawn, `QUALIFIED` required) and executable
  framework/mode/tools/approval-evidence binding:
  `research.study.protocol.store.build_profile_selections` (shared by study
  create and stopped-study clone) and
  `research.study.agents.distributions.validate_profile_configuration`.
- qualification is derived from verified conformance evidence bound to the exact
  artifact/platform: `research.study.agents.registry.derive_qualification_status`
  and `qualified_artifact_keys`, plus the bootstrap artifact-selection gate.

## Test coverage (ISSUE-03 / ISSUE-10 / ISSUE-17)

- ISSUE-03:
  `tests/backend_tests/research/test_distributions.py::test_profile_configuration_matrix`,
  `::test_profile_configuration_enforces_approval_option_evidence`,
  `tests/backend_tests/research/test_profile_release_contract.py::test_create_profile_rejects_a_framework_release_mode_mismatch`,
  `::test_create_profile_accepts_a_qualified_packaged_release`,
  `::test_create_profile_accepts_a_qualified_byoa_release`.
- ISSUE-10:
  `tests/backend_tests/research/test_runtime.py::test_qualification_is_derived_from_passing_conformance_evidence`,
  `::test_cross_artifact_receipt_does_not_qualify_either_artifact`,
  `::test_qualified_artifact_keys_bind_only_the_receipts_own_platform`,
  `::test_bootstrap_selects_only_the_artifact_the_evidence_qualifies`,
  `::test_bootstrap_qualifies_a_byoa_release_bound_to_its_manifest_digest`.
- ISSUE-17:
  `tests/backend_tests/research/test_distributions.py::test_release_only_byoa_distribution_derives_mode_and_identity_from_release`,
  `::test_release_only_packaged_distribution_is_not_reported_as_byoa`.
- Study-creation freeze:
  `tests/database_tests/test_research_schema_lifecycle.py::test_study_creation_freezes_owned_profiles_and_rejects_foreign_profiles`.

## Tested boundary: qualification vs verification vs readiness (ISSUE-004)

These three concepts stay distinct in code and docs; no blanket restriction
was added:

- **Qualification** (release-level, receipt-derived): a BYOA release is
  `QUALIFIED` exactly when a passing conformance receipt binds its own
  `source_manifest_digest` plus the adapter digest
  (`research.study.agents.registry.qualified_artifact_keys` /
  `byoa_identity_qualified`; ISSUE-003 evidence, read-only here).
- **Verification** (distribution-level, publication-time): a `BYOA_EXTERNAL`
  distribution is always unverified — `resolve_distribution_view` reports
  `verified=False` — so publication emits `DISTRIBUTION_UNVERIFIED` as an
  admin `WARNING` (publishable) and a non-admin `ERROR` (blocked). The
  admin/non-admin paths are preserved, not weakened.
- **Readiness** (participant-host, resolution-time): a host without the
  installed agent blocks with `AGENT_NOT_FOUND`; the plugin resolves a
  present agent through the documented discovery order (configured command,
  release command, `PATH`, known locations).

Role-severity proof (ISSUE-004, resolver-level, no database):
`tests/backend_tests/research/test_profile_release_contract.py::test_byoa_resolver_resolves_receipt_qualified_identity_but_not_unqualified`,
`::test_byoa_publication_severity_is_role_specific`.
Participant-flow proof (DB-backed HTTP):
`tests/backend_tests/research/test_study_lifecycle_bootstrap_session_telemetry_http.py::test_http_codex_byoa_adapter_normalization_and_terminal_closure`
(qualified BYOA release → study create → bootstrap manifest with the BYOA
identity).
Plugin discovery proof:
`code4me2/src/test/kotlin/me/code4me/research/proxy/ProxyTest.kt::ByoaAgentResolverTest`.
The `MANAGED_RUNTIME_FRAMEWORK` note in
`research.study.agents.enums` records the same boundary as a comment only;
the vocabulary itself is unchanged (additive-only).

## Allowlist rules

- Each entry must name an owner and a planned-removal review trigger. An entry
  without both is a review failure.
- The scan must report `0 forbidden`; a new hit outside the allowlist fails the
  build. Only a per-symbol entry is acceptable for a multi-symbol file.
- Deleting the symbol is preferred over extending the allowlist. When the
  authoring module's last consumer disappears, the entry and the code go
  together.
