# Research analytics scope

This note records the scope boundaries of the research analytics read paths and
the assignment-model protocol decisions (ISSUE-13, then the 2026-10 assignment
policy).

## Assignment model (protocol decision)

Assignment is **enrollment-owned, equal-probability, sticky random**, and every
study freezes how it draws in its configuration
(`research_config_json["assignment"]`, part of the configuration digest):

- The unit of randomization is the `enrollment_id` (never account, device, task
  or process); it is a server-generated UUID created when the participant
  consents, so nobody can choose or re-roll it.
- **Studies created since the 2026-10 decision** draw with
  `DETERMINISTIC_HASH` (`research.runtime.assignment.hashing`). With `s` the
  study id, `e` the randomization epoch (0), `u` the enrollment id and the
  study's K arms ordered by `selection_order`:

      h(u) = first 64 bits (big-endian) of SHA-256("s:e:u")
      arm(u) = arm k with k = floor(K * h(u) / 2^64)

  UUIDs are lowercase and hyphenated. The draw is reproducible from exported
  data, P(arm k) = 1/K up to an error below 2^-64, and the study id salts it, so
  studies are independent. Golden vector:
  `00000000-0000-0000-0000-000000000001:0:00000000-0000-0000-0000-000000000002`
  gives h = 0x39673deb8a861ef7, so k = 0 (K = 2 or 3) and k = 1 (K = 5).
- **Studies created earlier** carry no `assignment` block and keep
  `RANDOM_EQUAL`: an equal-probability draw from the system CSPRNG
  (`secrets.choice` at join; `SystemRandom` in the bootstrap fallback).
- Both are **simple randomization**: arm sizes are Multinomial(N, 1/K), so small
  studies are often unbalanced (two arms: P(larger arm >= 60%) is .50 at N = 20,
  .31 at N = 24, .27 at N = 40 and .06 at N = 100). There is no blocking,
  stratification or minimization.
- **Manual override (opt-in per study).** A study created with
  `allow_manual_assignment` lets its owner set a participant's arm by hand
  (`PUT /api/research/studies/{id}/enrollments/{enrollment_id}/assignment`), only
  while the enrollment is active and before any research session, event,
  metered inference reservation or agent task exists for it. The consent notice
  of such a study says participants are assigned "at random or by the research
  team". The row keeps its `randomization_epoch` and becomes `strategy = MANUAL`,
  so the randomized arm stays recomputable (as-randomized vs. as-assigned
  analyses); a `STUDY_LIFECYCLE` record (`ASSIGNMENT_OVERRIDDEN`) names the old
  and new arm, and the session bootstrap reads the assignment under a share lock
  so an override and a first session serialize.
- Otherwise the assignment is immutable and sticky: a repeated bootstrap/join
  returns the existing assignment and never re-randomizes it
  (`StudyAssignment`, unique `enrollment_id`).
- `AssignmentStrategy.STRATIFIED` / `WEIGHTED_RANDOM` remain additive protocol
  vocabulary; `allocate` refuses any strategy other than `RANDOM_EQUAL` and
  `DETERMINISTIC_HASH`.

Changing how a study draws is a protocol decision recorded here, never a silent
change to `allocate` or the join path.

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
