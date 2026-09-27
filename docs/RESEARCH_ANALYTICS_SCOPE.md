# Research analytics scope

This note records the scope boundaries of the research analytics read paths and
the assignment-model protocol decision (ISSUE-13). It is documentation only: no
randomization algorithm changed.

## Assignment model (protocol decision)

Assignment is **enrollment-owned, equal-probability, sticky random**:

- The unit of randomization is the `enrollment_id` (never account, device, task
  or process).
- Each enrollment receives one profile drawn with equal probability from the
  study's frozen, digest-pinned profile selections
  (`research.runtime.assignment.service.allocate`, strategy `RANDOM_EQUAL`).
- The assignment is immutable and sticky: a repeated bootstrap/join returns the
  existing assignment and never re-randomizes it
  (`StudyAssignment`, unique `enrollment_id`; `randomization_epoch = 0`).
- There is **no balancing, stratification, minimization, block randomization or
  manual assignment** in V1. `AssignmentStrategy.STRATIFIED` /
  `WEIGHTED_RANDOM` / `DETERMINISTIC_HASH` exist only as additive protocol
  vocabulary; the V1 join path always allocates `RANDOM_EQUAL`.

This is a deliberate study-protocol decision, not a randomization bug. If a
study protocol requires balancing or stratification, that is a new protocol
version and a separate issue; it must not be added by silently changing
`allocate` or the join path.

## Personal agent analytics (unchanged)

`research.analysis.read_models.dashboard` (the Dashboard → Agents tab) is scoped
to the caller's **own** agent tasks: a non-administrator only ever aggregates
rows where `agent_task.owner_user_id = current_user.user_id`; an administrator
may optionally scope to one user. It is personal run analytics, not participant
results, and this slice does not change it.

## Study-owner participant analytics

Study-owner-scoped participant coverage lives in:

- Endpoint: `GET /api/research/operations/participants/coverage?study_id=<uuid>`
  (`src/backend/routers/research/read_models.py`).
- Read model: `StudyParticipantCoverageV1` / `ParticipantCoverageRowV1`
  (`src/research/analysis/read_models/models.py`), built by
  `build_participant_coverage` (`.../service.py`) from study-scoped domain
  records loaded by `list_coverage_inputs` (`.../store.py`).

Each row is keyed by the study-local `enrollment_id` / `participant_code` and
contains:

- enrollment status and timestamps;
- frozen assignment facts (profile id, strategy, randomization epoch, profile
  digest, status) — never the profile snapshot or any secret;
- session counts (total/active/terminal) plus `last_activity_at` /
  `last_heartbeat_at`;
- canonical event counts total and grouped by event type and source, plus the
  last event timestamp.

Access control: only the study owner (`study.created_by`) or an administrator
may read it (`require_study_owner`), matching the existing enrollment/telemetry
coverage endpoints. Rows are always study-scoped, so no cross-study personal
timeline is ever exposed; login/account/participant identity is never joined
into the payload, and retention tombstones (`retention_state = "DELETED"`) are
excluded.

## Participant budgets and metered spend

Goose and built-in arms spend from the study's provider connection (the shared,
server-held key). Every enrollment owns one balance row (`limit`, `settled`,
`reserved`, micro-USD) and every metered call one reservation-ledger row. The
participants table carries each row's `budget` (limit, consumed, reserved,
remaining, exhausted) and the study summary's `totals` carry
`metered_spend_micro_usd` / `metered_calls` computed from the ledger (exact,
all-time, not windowed) — these are separate from the telemetry-derived
`usage_tokens`, which stay agent-reported. Participants see only their own
arm-blind numbers (`budget` on *My studies*): never a model, price or profile.
Codex arms are unmetered (`budget: null`).
