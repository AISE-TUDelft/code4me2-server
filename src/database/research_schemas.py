"""SQLAlchemy ORM models for the research platform stores.

The research platform keeps its own namespace so operational agent tables stay
independent. Data that is a pure projection of an existing
JSON document lives inside that document rather than in a child table:

* capability evidence is ``acp_capability_receipt.receipt_json.evidence[]``;
* agent artifacts/adapter are ``agent_release.release_json.artifacts[]``/``.adapter``;
* runtime packaging evidence is ``agent_release.release_json.package_json`` and
  its conformance receipts are ``agent_release.release_json.conformance[]``;
* a capability snapshot is ``research_agent_run.snapshot_json``;
* selected profiles are ``study_agent_profile.profile_snapshot_json``;
* study identity/configuration is owned by ``study``;
* draft lifecycle state is ``study.research_status = DRAFT``;
* an emitter cursor is ``max(research_event.emitter_sequence)``;
* rejections are ``telemetry_batch_receipt.receipt_json.rejected[]``;
* session transitions are appended to ``research_session.transitions_json``;
* a withdrawal is ``research_enrollment.withdrawal_json``;
* retention evidence is ``research_retention_job.evidence_json``.

Two cross-cutting concerns get a generic table: ``research_record`` for every
append-only audit/evidence family (kill switch, health, release evidence, pilot
run, export, retention). Coverage/metric projections are derived
purely from canonical events and are not persisted.
"""

from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from .db import Base


# NOTE: ``os`` is used as a column attribute name because it is part of the
# published environment tuple. It shadows the stdlib module only inside this
# class scope, which is safe because the module never imports ``os``.
class AcpCapabilityReceipt(Base):
    """One version-pinned, redacted capability receipt."""

    __tablename__ = "acp_capability_receipt"
    __table_args__ = (
        Index("idx_acp_capability_receipt_agent", "agent_id", "agent_version"),
        Index("idx_acp_capability_receipt_status", "status"),
        Index("idx_acp_capability_receipt_captured_at", "captured_at"),
        {"schema": "public"},
    )

    receipt_id = Column(UUID(as_uuid=True), primary_key=True)
    captured_at = Column(DateTime(timezone=True), nullable=False)
    ide_build = Column(String, nullable=False)
    ai_assistant_build = Column(String, nullable=False)
    plugin_version = Column(String, nullable=False)
    os = Column(String, nullable=False)
    arch = Column(String, nullable=False)
    agent_id = Column(String, nullable=False)
    agent_version = Column(String, nullable=False)
    adapter_version = Column(String, nullable=True)
    protocol_version = Column(String, nullable=False)
    status = Column(String, nullable=False)
    # Unique: a receipt's canonical content is its identity, so storing the same
    # bytes twice is a duplicate rather than a new observation.
    content_hash = Column(String, nullable=False, unique=True)
    receipt_json = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.now)


# ---------------------------------------------------------------------------
# Issue 02: versioned study protocol and publication
# ---------------------------------------------------------------------------


class StudyAgentProfile(Base):
    """One profile selected by a study at creation time.

    The digest and non-secret snapshot keep a DRAFT study stable if the shared
    profile is edited before the study receives its first consent. Assignment
    rows take their own snapshot when a participant is enrolled.
    """

    __tablename__ = "study_agent_profile"
    __table_args__ = (
        Index("idx_study_agent_profile_profile_id", "profile_id"),
        UniqueConstraint(
            "study_id", "profile_id", name="uq_study_agent_profile_selection"
        ),
        {"schema": "public"},
    )

    study_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.study.study_id", ondelete="CASCADE"),
        primary_key=True,
    )
    profile_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.agent_profile.profile_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    profile_digest = Column(String, nullable=False)
    profile_snapshot_json = Column(JSONB, nullable=False)
    selection_order = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.now)


# ---------------------------------------------------------------------------
# Issue 04: agent registry and capability contract
# ---------------------------------------------------------------------------


class AgentRelease(Base):
    """One digest-pinned agent release record.

    Identity is ``(agent_id, release_id, source_manifest_digest)``: a changed
    upstream manifest digest is a distinct release, not an in-place update. The
    unique constraint on ``(agent_id, source_manifest_digest)`` additionally
    makes the same source bytes impossible to register twice.
    """

    __tablename__ = "agent_release"
    __table_args__ = (
        Index("idx_agent_release_agent_id", "agent_id"),
        Index("idx_agent_release_status", "status"),
        UniqueConstraint(
            "agent_id",
            "source_manifest_digest",
            name="uq_agent_release_agent_manifest",
        ),
        {"schema": "public"},
    )

    release_id = Column(String, primary_key=True)
    agent_id = Column(String, nullable=False)
    source_manifest_digest = Column(String, nullable=False)
    status = Column(String, nullable=False)
    release_json = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.now)


# ---------------------------------------------------------------------------
# Issue 03: participant identity, consent and withdrawal
# ---------------------------------------------------------------------------


class ResearchParticipant(Base):
    """Private account-to-participant mapping.

    This is the only research table that links a login account to a study
    participant. It is privileged and is never returned to researchers or
    embedded in an export.
    """

    __tablename__ = "research_participant"
    __table_args__ = (
        Index("idx_research_participant_account_id", "account_id"),
        {"schema": "public"},
    )

    participant_id = Column(UUID(as_uuid=True), primary_key=True)
    account_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.user.user_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.now)


class ResearchEnrollment(Base):
    """One participant's study enrollment and consent identity."""

    __tablename__ = "research_enrollment"
    __table_args__ = (
        Index("idx_research_enrollment_participant_id", "participant_id"),
        Index("idx_research_enrollment_study_id", "study_id"),
        Index("idx_research_enrollment_participant_code", "participant_code"),
        Index("idx_research_enrollment_status", "status"),
        UniqueConstraint(
            "participant_id",
            "study_id",
            name="uq_research_enrollment_participant_study",
        ),
        # One active enrollment per account across the whole platform. ACTIVE is
        # the only state that holds the slot; a completed study frees it.
        Index(
            "uq_research_enrollment_active_participant",
            "participant_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        {"schema": "public"},
    )

    enrollment_id = Column(UUID(as_uuid=True), primary_key=True)
    participant_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.research_participant.participant_id", ondelete="CASCADE"),
        nullable=False,
    )
    study_id = Column(UUID(as_uuid=True), nullable=False)
    # Random study-local pseudonym. Never derived from the account id.
    participant_code = Column(String, nullable=False)
    status = Column(String, nullable=False)
    revocation_epoch = Column(Integer, nullable=False, server_default="0", default=0)
    eligibility_json = Column(JSONB, nullable=False)
    enrolled_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    # The single consent acceptance: set once when the participant joins. There
    # is no document identity, re-consent or withdrawal.
    consent_accepted_at = Column(DateTime(timezone=True), nullable=True)
    retention_action = Column(String, nullable=False)


# ---------------------------------------------------------------------------
# Issue 05: enrollment assignment and bootstrap capabilities
# ---------------------------------------------------------------------------


class StudyAssignment(Base):
    """The immutable, sticky agent profile assigned to one enrollment."""

    __tablename__ = "study_assignment"
    __table_args__ = (
        Index("idx_study_assignment_enrollment_id", "enrollment_id"),
        Index("idx_study_assignment_study_id", "study_id"),
        Index("idx_study_assignment_agent_profile_id", "agent_profile_id"),
        UniqueConstraint("enrollment_id", name="uq_study_assignment_enrollment"),
        {"schema": "public"},
    )

    assignment_id = Column(UUID(as_uuid=True), primary_key=True)
    enrollment_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.research_enrollment.enrollment_id", ondelete="CASCADE"),
        nullable=False,
    )
    study_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.study.study_id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_profile_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.agent_profile.profile_id", ondelete="RESTRICT"),
        nullable=False,
    )
    strategy = Column(String, nullable=False, default="RANDOM_EQUAL")
    randomization_epoch = Column(Integer, nullable=False, server_default="0", default=0)
    profile_digest = Column(String, nullable=False)
    profile_snapshot_json = Column(JSONB, nullable=False)
    status = Column(String, nullable=False, default="ACTIVE")
    assigned_at = Column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# Issue 07: research session lifecycle and agent runs
# ---------------------------------------------------------------------------


class ResearchSessionV1(Base):
    """One research session: the authoritative participant-activity boundary.

    Idle/resume timing lives in the study configuration, never in these columns.

    ``context_id`` is an opaque, client-scoped execution-context identifier (a
    project/window instance). The partial unique index
    ``uq_research_session_active_context`` enforces at most one non-terminal
    session per ``(enrollment_id, context_id)``, so creation is idempotent for a
    context while different windows/projects get distinct sessions. It is never a
    filesystem path or an account identifier.
    """

    __tablename__ = "research_session"
    __table_args__ = (
        Index("idx_research_session_enrollment_id", "enrollment_id"),
        Index("idx_research_session_study_id", "study_id"),
        Index("idx_research_session_state", "state"),
        Index("idx_research_session_context_id", "context_id"),
        Index(
            "uq_research_session_active_context",
            "enrollment_id",
            "context_id",
            unique=True,
            postgresql_where=text("state NOT IN ('ended', 'revoked')"),
        ),
        {"schema": "public"},
    )

    session_id = Column(UUID(as_uuid=True), primary_key=True)
    enrollment_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.research_enrollment.enrollment_id", ondelete="CASCADE"),
        nullable=False,
    )
    study_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.study.study_id", ondelete="CASCADE"),
        nullable=False,
    )
    # Opaque execution-context id (project/window instance), never a path.
    context_id = Column(String, nullable=False)
    state = Column(String, nullable=False)
    opened_at = Column(DateTime(timezone=True), nullable=True)
    # Liveness marker from heartbeats; never used to refresh activity/expiry.
    last_heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    last_activity_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    close_reason = Column(String, nullable=True)
    resume_generation = Column(Integer, nullable=False, server_default="0", default=0)
    manifest_digest = Column(String, nullable=False)
    environment_json = Column(JSONB, nullable=False, default=dict)
    # Append-only transition log (one serialized transition per entry). It lives
    # on the session row because every transition belongs to exactly one session.
    transitions_json = Column(JSONB, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.now)


class ResearchAgentRun(Base):
    """One agent process (or one qualification run) against a release.

    A capability snapshot belongs to the run that captured it, so the snapshot
    JSON and its capture time live on this row. A qualification snapshot may be
    uploaded before any participant session exists, so ``research_session_id``
    is nullable: a run with no session is a detached qualification run.
    """

    __tablename__ = "research_agent_run"
    __table_args__ = (
        Index("idx_research_agent_run_session_id", "research_session_id"),
        {"schema": "public"},
    )

    agent_run_id = Column(UUID(as_uuid=True), primary_key=True)
    research_session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.research_session.session_id", ondelete="CASCADE"),
        nullable=True,
    )
    agent_release_id = Column(String, nullable=True)
    # Immutable assignment/profile identity for participant runs. Nullable for
    # detached qualification runs and legacy rows.
    assignment_id = Column(UUID(as_uuid=True), nullable=True)
    agent_profile_id = Column(UUID(as_uuid=True), nullable=True)
    profile_digest = Column(String, nullable=True)
    profile_snapshot_json = Column(JSONB, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    outcome = Column(String, nullable=True)
    # The full ``CapabilitySnapshotV1`` JSON (declared-vs-observed) captured by
    # this run, and when it was captured (nullable: not every run snapshots).
    snapshot_json = Column(JSONB, nullable=True)
    snapshot_captured_at = Column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# Issue 09: idempotent telemetry ingestion and research persistence
# ---------------------------------------------------------------------------


class ResearchEvent(Base):
    """One immutable accepted canonical telemetry fact.

    ``event_id`` is globally unique; identity is additionally constrained by
    ``(research_session_id, emitter_id, emitter_sequence)`` where a session
    applies, so per-emitter ordering is preserved. Accepted payload bytes and
    provenance are never updated in place.
    """

    __tablename__ = "research_event"
    __table_args__ = (
        Index("idx_research_event_enrollment_id", "enrollment_id"),
        Index("idx_research_event_session_id", "research_session_id"),
        Index("idx_research_event_emitter_id", "emitter_id"),
        Index("idx_research_event_occurred_at", "occurred_at"),
        Index("idx_research_event_event_type", "event_type"),
        Index("idx_research_event_digest", "digest"),
        UniqueConstraint(
            "research_session_id",
            "emitter_id",
            "emitter_sequence",
            name="uq_research_event_session_emitter_sequence",
        ),
        {"schema": "public"},
    )

    event_id = Column(UUID(as_uuid=True), primary_key=True)
    schema_version = Column(String, nullable=False)
    event_type = Column(String, nullable=False)
    source = Column(String, nullable=False)
    # SET NULL: telemetry is retained/deleted by the enrollment's retention
    # policy, so a removed session/enrollment row must not cascade into the
    # events. `agent_run_id` stays a plain string: it is an external run
    # identifier, not `research_agent_run.agent_run_id` (a UUID).
    study_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.study.study_id", ondelete="SET NULL"),
        nullable=True,
    )
    enrollment_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.research_enrollment.enrollment_id", ondelete="SET NULL"),
        nullable=True,
    )
    research_session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.research_session.session_id", ondelete="SET NULL"),
        nullable=True,
    )
    agent_run_id = Column(String, nullable=True)
    emitter_id = Column(String, nullable=False)
    emitter_sequence = Column(Integer, nullable=False)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    # The complete validated CanonicalEventV1 envelope (the event authority).
    # The scalar columns are searchable projections derived from it at insert
    # time (identity, ordering and joins); content lives only in the envelope.
    envelope_json = Column(JSONB, nullable=False, default=dict)
    digest = Column(String, nullable=False)
    accepted_at = Column(DateTime(timezone=True), nullable=False)
    retention_state = Column(String, nullable=False, server_default="RETAINED")
    # When a retention action anonymized or tombstoned this row (additive).
    anonymized_at = Column(DateTime(timezone=True), nullable=True)


class TelemetryBatchReceipt(Base):
    """An immutable, retry-safe batch acknowledgement record."""

    __tablename__ = "telemetry_batch_receipt"
    __table_args__ = (
        Index("idx_telemetry_batch_receipt_session_id", "research_session_id"),
        {"schema": "public"},
    )

    receipt_id = Column(UUID(as_uuid=True), primary_key=True)
    batch_id = Column(String, nullable=False, unique=True)
    enrollment_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.research_enrollment.enrollment_id", ondelete="SET NULL"),
        nullable=True,
    )
    research_session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.research_session.session_id", ondelete="SET NULL"),
        nullable=True,
    )
    accepted_at = Column(DateTime(timezone=True), nullable=False)
    receipt_json = Column(JSONB, nullable=False)


# ---------------------------------------------------------------------------
# Issue 12: researcher control plane, coverage and export
#
# Researcher enablement is a single ``user.can_research`` flag (P3); there is no
# per-study ``research_researcher_role`` table.
# ---------------------------------------------------------------------------


#: ``kind`` values for :class:`ResearchRecord`.
RECORD_KIND_KILL_SWITCH = "KILL_SWITCH"
RECORD_KIND_HEALTH = "HEALTH"
RECORD_KIND_RELEASE_EVIDENCE = "RELEASE_EVIDENCE"
RECORD_KIND_PILOT_RUN = "PILOT_RUN"
RECORD_KIND_STUDY_PUBLICATION = "STUDY_PUBLICATION"
RECORD_KIND_STUDY_LIFECYCLE = "STUDY_LIFECYCLE"
RECORD_KIND_RETENTION_EVIDENCE = "RETENTION_EVIDENCE"


class ResearchRecord(Base):
    """One generic append-only research record.

    Consolidates every cross-cutting evidence record into a single table:
    kill-switch engagements, operational health, release evidence, pilot runs,
    study publication/lifecycle audits, export audits and retention evidence
    (deletion ledger + deletion-drill verification). ``kind`` selects the
    family, ``scope_type``/``scope_id`` name the scoped subject (study,
    revision, enrollment, export) and the full original record lives in
    ``payload_json``. ``study_id``/``actor`` are nullable because export and
    retention evidence is not always study- or actor-scoped.
    """

    __tablename__ = "research_record"
    __table_args__ = (
        Index("idx_research_record_kind", "kind"),
        Index("idx_research_record_scope", "scope_type", "scope_id"),
        Index("idx_research_record_study_id", "study_id"),
        Index("idx_research_record_occurred_at", "occurred_at"),
        {"schema": "public"},
    )

    record_id = Column(UUID(as_uuid=True), primary_key=True)
    kind = Column(String, nullable=False)
    scope_type = Column(String, nullable=True)
    scope_id = Column(UUID(as_uuid=True), nullable=True)
    study_id = Column(UUID(as_uuid=True), nullable=True)
    actor = Column(String, nullable=True)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    payload_json = Column(JSONB, nullable=False, default=dict)


# ---------------------------------------------------------------------------
# Issue 13: pilot, operations, retention and the release gate
# ---------------------------------------------------------------------------


class ResearchRetentionJob(Base):
    """One idempotent retention-execution job over an enrollment's stored data.

    The unique ``(enrollment_id, action)`` constraint makes withdrawal enqueue
    idempotent: re-withdrawing never creates a second job. ``enrollment_id`` is
    intentionally not a foreign key so the evidence survives even if an
    enrollment row is later removed.
    """

    __tablename__ = "research_retention_job"
    __table_args__ = (
        Index("idx_research_retention_job_enrollment", "enrollment_id"),
        Index("idx_research_retention_job_state", "state"),
        UniqueConstraint(
            "enrollment_id",
            "action",
            name="uq_research_retention_job_enrollment_action",
        ),
        {"schema": "public"},
    )

    job_id = Column(UUID(as_uuid=True), primary_key=True)
    enrollment_id = Column(UUID(as_uuid=True), nullable=False)
    action = Column(String, nullable=False)
    state = Column(String, nullable=False)
    attempts = Column(Integer, nullable=False, server_default="0", default=0)
    created_at = Column(DateTime(timezone=True), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(String, nullable=True)
    evidence_digest = Column(String, nullable=True)
    affected_events = Column(Integer, nullable=False, server_default="0", default=0)
    # Retained ledger/verification evidence (and its digest) for this job. It
    # lives here so the evidence survives the enrollment row it describes.
    evidence_json = Column(JSONB, nullable=False, default=dict)


# ---------------------------------------------------------------------------
# Issue 11: runtime packaging and agent conformance
#
# Packaging evidence is not a table: a release's ``RuntimeManifestV2`` lives in
# ``agent_release.release_json.package_json`` and its immutable conformance
# receipts live in ``agent_release.release_json.conformance[]``.
# ---------------------------------------------------------------------------


