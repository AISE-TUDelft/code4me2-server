#!/usr/bin/env python3
"""Seed a synthetic research study for local onboarding.

This is the operator tool the participant runbook uses to mint a study-owned
*join code*. It performs no domain logic of its own: every state transition delegates
to the real research platform services and stores:

* ``research.study.protocol.store`` (``create_study`` / ``get_study`` /
  ``set_study_active`` / ``get_study_by_join_code``);
* ``research.study.lifecycle`` (``allocate_join_code`` for the study-owned join
  code, ``open_study_enrollment`` for the atomic web-consent enrollment);
* ``research.study.agents.store`` (``upsert_release``) with
  ``research.study.agents.models.AgentReleaseV1`` (qualification is derived from
  the release's producer tests, not supplied by the seeder);
* ``database.crud`` for the login account.

Everything is digest-pinned and deterministic so the script is safe to re-run:
the same arguments resolve the same account, release, study and enrollment
instead of piling up duplicates. Nothing secret is printed except
a password the operator explicitly supplied with ``--create-account``.

Usage (from ``code4me2-server/``, development/test only)::

    CODE4ME_DEV_SEED=1 PYTHONPATH=src python scripts/dev/seed_research_study.py \\
        --account-email participant@example.com \\
        --account-password 'Password123' --create-account

``TEST_MODE=true`` is accepted in place of ``CODE4ME_DEV_SEED=1``. Without one of
those guards the script exits before touching a database, so it can never seed a
production deployment by accident.

The printed ``join_code`` is the study-owned code the participant redeems
through web consent; enrollment itself happens there, never in the plugin.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional, Sequence
from uuid import UUID

from research.study import lifecycle as study_lifecycle
from research.study.agents.enums import (
    DistributionMode,
    QualificationStatus,
)
from research.study.agents.manifest_import import build_manifest_release
from research.study.agents.registry import AgentRegistry
from research.study.protocol import store as protocol_store

if TYPE_CHECKING:
    from research.study.agents.models import AgentReleaseV1

# The fixed window is deliberately deterministic (not "now + N days") so the
# seeded study schedule is identical on every run. ``end_at`` stays far enough
# in the future for the synthetic study to remain open.
FIXED_SCHEDULE_START = datetime(2024, 1, 1, tzinfo=timezone.utc)
FIXED_SCHEDULE_END = datetime(2099, 1, 1, tzinfo=timezone.utc)

DEFAULT_STUDY_NAME = "Synthetic Onboarding Study"
DEFAULT_AGENT_ID = "code4me-synthetic-agent"
DEFAULT_RELEASE_VERSION = "1.0.0"
DEFAULT_ARTIFACT_SIZE = 1024

#: The fresh-DB seeder pins the *built-in* Code4Me runtime distribution and
#: leaves the participant-installed runtimes as BYOA identities.
DEFAULT_BUILTIN_STUDY_NAME = "Code4Me2 Built-in Agent Study"
BUILTIN_DISTRIBUTION_NAME = "default-code4me2-agent"
#: Development-only account created by ``--fresh-db`` seeding. Guarded by the
#: CODE4ME_DEV_SEED environment variable in ``scripts/dev/seed_local_dev.sh``
#: and by the ``--fresh-db`` flag; never a production credential.
DEFAULT_DEV_OWNER_EMAIL = "research-owner@local.dev"
DEFAULT_DEV_PASSWORD = "Code4me-dev1"
#: ``(profile name, discovery package, command)`` for the BYOA profiles.
BYOA_PROFILE_IDENTITIES = (
    ("default-goose", "goose", "goose"),
    ("default-codex", "codex", "codex"),
)
#: Development-only provider connection created by ``--fresh-db`` seeding.
#: The research UI only lists admin-created connections, so without this a fresh
#: database can never author a profile from the UI. Only the secret's env-var
#: name is stored; the value is resolved from the environment at inference time.
DEFAULT_DEV_CONNECTION_LABEL = "local-dev-openrouter"
DEFAULT_DEV_CONNECTION_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_DEV_CONNECTION_SECRET_REF = "OPENROUTER_API_KEY"
DEFAULT_DEV_CONNECTION_MODELS: tuple[str, ...] = (
    "cohere/north-mini-code:free",
    "openai/gpt-4o-mini",
)


class SeedError(RuntimeError):
    """A typed, operator-facing failure (no partial policy is silently kept)."""


@dataclass(frozen=True)
class SeedRequest:
    """The fully resolved inputs of one seed run."""

    account_email: str
    account_password: Optional[str]
    account_name: str
    create_account: bool
    config_id: Optional[int]
    study_name: str
    agent_id: str
    release_id: str
    release_version: str
    artifact_digest: str
    artifact_path: str
    artifact_size: int
    os_name: str
    arch: str
    actor: str
    require_capabilities: bool = False
    # Distribution mode: PACKAGED (default, digest-pinned artifact) or
    # BYOA_EXTERNAL (participant-installed Goose/Codex/...; command/package
    # identity instead of an artifact digest).
    distribution_mode: str = DistributionMode.PACKAGED.value
    agent_command: Optional[str] = None
    agent_command_args: tuple[str, ...] = ()
    agent_package: Optional[str] = None
    # Optional link to an existing, server-side AgentProfile (provider/model/
    # tools/policy catalogue). Only the opaque id is pinned into the protocol;
    # the profile's provider/base_url/api_key_ref never enter the document.
    agent_profile_id: Optional[UUID] = None

    @property
    def is_byoa(self) -> bool:
        return self.distribution_mode.strip().upper() == DistributionMode.BYOA_EXTERNAL.value

    @property
    def study_id(self) -> UUID:
        """Deterministic study identity derived from the study name."""
        return uuid.uuid5(
            uuid.NAMESPACE_URL, f"code4me2://research/study/{self.study_name}"
        )

    @property
    def distribution_id(self) -> UUID:
        """Deterministic distribution (AgentProfile) identity.

        The distribution is the single thing a researcher selects; the seed mints
        a deterministic one per (study, release, mode) so re-runs are idempotent.
        """
        return uuid.uuid5(
            uuid.NAMESPACE_URL,
            "code4me2://research/distribution/"
            f"{self.study_name}/{self.release_id}/{self.distribution_mode}",
        )


@dataclass(frozen=True)
class FreshDbRequest:
    """The resolved inputs of a "fresh DB immediately usable" seed run.

    It imports the built runtime manifest (never a synthetic digest), approves
    the release for the built platforms so it is QUALIFIED, pins the built-in
    distribution to that release, marks the participant-installed profiles as
    BYOA, and creates one live study with a working session policy.
    """

    manifest: dict[str, Any]
    #: Directory holding the manifest's declared archives (basenames). The
    #: ``code4me-runtime/`` resource subdirectory is also searched.
    archives_dir: Optional[str] = None
    study_name: str = DEFAULT_BUILTIN_STUDY_NAME
    actor: str = "seed-script"
    require_capabilities: bool = False
    # Development owner account that owns the seeded profile and study. It is
    # created enabled-for-research when missing so a fresh database is usable
    # without any per-participant SQL or seed migration.
    owner_email: str = DEFAULT_DEV_OWNER_EMAIL
    owner_password: str = DEFAULT_DEV_PASSWORD
    owner_name: str = "Research Owner"
    # Development provider connection the built-in profile is wired to.
    connection_label: str = DEFAULT_DEV_CONNECTION_LABEL
    connection_base_url: str = DEFAULT_DEV_CONNECTION_BASE_URL
    connection_secret_ref: str = DEFAULT_DEV_CONNECTION_SECRET_REF
    connection_models: tuple[str, ...] = DEFAULT_DEV_CONNECTION_MODELS
    profile_model: str = DEFAULT_DEV_CONNECTION_MODELS[0]

    @property
    def study_id(self) -> UUID:
        """Deterministic study identity derived from the study name."""
        return uuid.uuid5(
            uuid.NAMESPACE_URL, f"code4me2://research/study/{self.study_name}"
        )


@dataclass(frozen=True)
class FreshDbSummary:
    """The result of a fresh-DB seed run (non-secret, copy-pasteable)."""

    agent_id: str
    release_id: str
    release_version: str
    qualification: str
    distribution_id: str
    distribution_verified: bool
    supported_platforms: list[dict[str, str]]
    study_id: str
    join_code: str
    session_policy: dict[str, Any]
    verified_artifacts: list[dict[str, Any]]
    provider_connection_label: str = ""
    provider_connection_models: tuple[str, ...] = ()
    profile_model: str = ""


# ---------------------------------------------------------------------------
# Pure planning helpers
# ---------------------------------------------------------------------------


def detect_os() -> str:
    """Return the Gradle/plugin platform name for the current host."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def detect_arch() -> str:
    """Return the Gradle/plugin architecture name for the current host."""
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return "aarch64"
    return "x64"


def build_research_config(
    request: SeedRequest,
    *,
    profile_id: UUID,
) -> dict[str, Any]:
    """The deterministic ``research_config_json`` this script stores on the study.

    Mirrors the ``POST /api/research/studies`` shape: a telemetry policy, a
    fully populated session policy (so the sessions endpoints can open a
    session instead of returning ``POLICY_MISSING``), and the selected
    agent profile ids.
    """
    return {
        "telemetry_policy": {"metadata_only": True},
        "session_policy": {
            "idle_timeout_seconds": 900,
            "resume_grace_seconds": 300,
            "heartbeat_seconds": 30,
        },
        "profile_ids": [str(profile_id)],
    }


# ---------------------------------------------------------------------------
# Account resolution
# ---------------------------------------------------------------------------


def _registry_from_db(session: Any) -> AgentRegistry:
    from research.study.agents import store as agents_store

    registry = AgentRegistry()
    for row in agents_store.list_releases(session):
        registry.register_release(agents_store.row_to_release(row))
    return registry


def ensure_study(
    session: Any,
    request: SeedRequest,
    research_config: dict[str, Any],
    *,
    owner_user_id: Any = None,
    profile_id: UUID,
) -> Any:
    """Create the deterministic study identity if it does not exist yet.

    The study owns its join code and selects the seeded agent profile, exactly
    like ``POST /api/research/studies``. Safe to re-run: an existing study is
    returned untouched.
    """
    row = protocol_store.get_study(session, request.study_id)
    if row is None:
        row = protocol_store.create_study(
            session,
            study_id=request.study_id,
            name=request.study_name,
            description="Synthetic study seeded for local onboarding.",
            created_by=owner_user_id,
            starts_at=FIXED_SCHEDULE_START,
            ends_at=FIXED_SCHEDULE_END,
            is_research=True,
            research_config_json=research_config,
            join_code=study_lifecycle.allocate_join_code(session),
            profile_ids=[profile_id],
        )
        return row
    if getattr(row, "research_status", None) != "STUDY_STOPPED":
        return row
    # A stopped study is terminal (``set_study_active`` refuses it), but a
    # re-seed must still yield a live study. Reuse an earlier replacement when it
    # is still open, otherwise create one, so re-running stays idempotent.
    replacement_name = f"{request.study_name} (reseeded)"
    existing = _study_by_name(session, replacement_name)
    if existing is not None and getattr(existing, "research_status", None) != "STUDY_STOPPED":
        view = protocol_store.get_study(session, existing.study_id)
        if view is not None:
            return view
    return protocol_store.create_study(
        session,
        study_id=uuid.uuid4(),
        name=replacement_name,
        description="Synthetic study seeded for local onboarding (replaces a stopped study).",
        created_by=owner_user_id,
        starts_at=FIXED_SCHEDULE_START,
        ends_at=FIXED_SCHEDULE_END,
        is_research=True,
        research_config_json=research_config,
        join_code=study_lifecycle.allocate_join_code(session),
        profile_ids=[profile_id],
    )


def _study_by_name(session: Any, name: str) -> Any:
    """Return the study row with ``name``, or ``None`` (seed helper)."""
    from sqlalchemy import select

    from database.db_schemas import Study as StudyRow

    return session.execute(select(StudyRow).where(StudyRow.name == name)).scalars().first()


def activate_study(session: Any, study_id: UUID) -> None:
    """Mark the seeded research study live, as the study routes do.

    Activation reserves the owner's single live-study slot (``is_active``);
    without it the assignment path refuses the study as not live. Idempotent,
    so re-running the seed is safe.
    """
    row = protocol_store.set_study_active(session, study_id, True)
    if row is None:
        raise SeedError(f"seeded study {study_id} has no study row")


def resolve_declared_archives(
    manifest: dict[str, Any], archives_dir: Optional[str]
) -> dict[str, Any]:
    """Map every archive the manifest declares to its on-disk bytes.

    ``archive`` fields are basenames; the seeder resolves each against the
    supplied directory (and the ``code4me-runtime/`` resource subdirectory, so
    the checked-in plugin manifest works too). A declared archive that cannot be
    found is left out: the planner then rejects the import, exactly like the
    HTTP route does.
    """
    from pathlib import Path

    roots: list[Path] = []
    if archives_dir:
        roots.append(Path(archives_dir))
        roots.append(Path(archives_dir) / "code4me-runtime")
    found: dict[str, Path] = {}
    for raw in manifest.get("artifacts") or []:
        if not isinstance(raw, dict):
            continue
        name = Path(str(raw.get("archive") or "")).name
        if not name or name in found:
            continue
        for root in roots:
            candidate = root / name
            if candidate.is_file():
                found[name] = candidate
                break
    return found


def import_manifest_release(
    session: Any, request: FreshDbRequest
) -> tuple[AgentReleaseV1, list[dict[str, Any]]]:
    """Register the release the build manifest + archives describe (idempotently).

    The manifest is mapped through the same pure planner the import endpoint
    uses, so the seeder and CI resolve the exact same release identity. Every
    declared archive must be present and match its digest and size; a mismatch
    raises rather than silently trusting the manifest.
    """
    from research.study.agents import store as agents_store

    archives = resolve_declared_archives(request.manifest, request.archives_dir)
    try:
        plan = build_manifest_release(request.manifest, archives=archives)
    except Exception as error:  # noqa: BLE001 - re-raise as an operator-facing error
        raise SeedError(f"build manifest could not be imported: {error}") from error

    desired = plan.release
    registry = _registry_from_db(session)
    existing = registry.get_release(desired.agent_id, desired.release_id)
    if existing is None:
        registration = registry.register_release(desired)
        if not registration.accepted:
            raise SeedError(
                "release could not be registered: "
                f"{registration.issue.message if registration.issue else 'unknown'}"
            )
        agents_store.upsert_release(session, desired)
    elif existing.digest_identity != desired.digest_identity:
        raise SeedError(
            f"release {desired.release_id!r} already exists with a different digest "
            "identity; the manifest is immutable -- remove the stale release first."
        )

    row = agents_store.get_release(session, desired.release_id)
    if row is None:  # pragma: no cover - defensive
        raise SeedError(f"release {desired.release_id!r} vanished after registration")
    return agents_store.row_to_release(row), plan.verified_artifacts


def ensure_dev_provider_connection(
    session: Any,
    *,
    label: str = DEFAULT_DEV_CONNECTION_LABEL,
    base_url: str = DEFAULT_DEV_CONNECTION_BASE_URL,
    secret_ref: str = DEFAULT_DEV_CONNECTION_SECRET_REF,
    models: tuple[str, ...] = DEFAULT_DEV_CONNECTION_MODELS,
) -> Any:
    """Return the development provider connection, creating it when missing.

    A fresh database has none, and the research UI only lists admin-created
    connections, so a seeded profile could never be authored from the UI. The
    row stores the secret's environment-variable name only; the value is resolved
    from the environment at inference time and is never persisted here.
    """
    from database import crud

    if not models:
        raise SeedError("a development provider connection needs at least one model")
    existing = crud.get_provider_connection_by_label(session, label)
    if existing is not None:
        return existing
    return crud.create_provider_connection(
        session,
        label=label,
        base_url=base_url,
        secret_ref=secret_ref,
        models_json=json.dumps(list(models)),
        is_active=True,
    )


def ensure_dev_owner(
    session: Any,
    email: str,
    *,
    password: str = DEFAULT_DEV_PASSWORD,
    name: str = "Research Owner",
) -> Any:
    """Return the development owner account, creating/enabling it if needed.

    The account is administrator-independent: it is created *enabled for
    research* (``can_research``) so a fresh database can author its first study
    without a per-study role grant. Existing accounts are left alone except for
    the enable flag.
    """
    from database import crud

    user = crud.get_user_by_email(session, email)
    if user is None:
        configs = crud.get_all_configs(session)
        if not configs:
            raise SeedError(
                "no configuration row exists; run init.sql/migrations before "
                "seeding a development owner account."
            )
        import Queries

        user = crud.create_user(
            session,
            Queries.CreateUser(
                email=email,
                name=name,
                password=password,
                config_id=int(configs[0].config_id),
            ),
        )
    if not user.can_research or not user.verified:
        user.can_research = True
        user.verified = True
        session.add(user)
        session.commit()
        session.refresh(user)
    return user


def pin_builtin_distribution(
    session: Any,
    release: AgentReleaseV1,
    *,
    name: str = BUILTIN_DISTRIBUTION_NAME,
    owner_user_id: Any = None,
    connection_id: Any = None,
    model: str = DEFAULT_DEV_CONNECTION_MODELS[0],
) -> Any:
    """Pin the built-in Code4Me profile to ``release`` (idempotently).

    Profiles are now researcher-owned and are never seeded by a migration, so a
    fresh database has no built-in profile. This creates one owned by the
    development owner account when it is missing; a profile that already exists
    only gets its release pin updated (and its provider wiring repaired when it
    still carries the old placeholder model/connection).
    """
    from sqlalchemy import select

    from database import db_schemas

    row = (
        session.execute(
            select(db_schemas.AgentProfile).where(
                db_schemas.AgentProfile.owner_user_id == owner_user_id,
                db_schemas.AgentProfile.name == name,
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        if owner_user_id is None:
            raise SeedError(
                f"cannot create built-in agent profile {name!r} without an owner "
                "account (fresh-db seeding creates one automatically)."
            )
        row = db_schemas.AgentProfile(
            profile_id=uuid.uuid4(),
            owner_user_id=owner_user_id,
            name=name,
            model=model,
            framework_version="code4me2-agent",
            tools_json="[]",
            # Verified by the synthetic permission/edit cases the dev seed records.
            approval_policy="suggestion_only",
            max_steps=1,
            is_active=True,
            release_id=release.release_id,
            connection_id=connection_id,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row
    row.release_id = release.release_id
    # Repair a profile seeded before connections existed (placeholder model and
    # no connection), so an existing dev database becomes usable on a re-run.
    if row.connection_id is None and connection_id is not None:
        row.connection_id = connection_id
    if (row.model or "").strip() in ("", "seed-model") and model:
        row.model = model
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def seed_fresh_database(session: Any, request: FreshDbRequest) -> FreshDbSummary:
    """Make a fresh database immediately usable for research.

    Registers the shipped runtime release from its producer manifest (the
    manifest's passing platform tests make it usable),
    pins ``default-code4me2-agent`` to it (Goose/Codex become BYOA),
    and creates one live study whose ``session_policy`` is fully populated
    so the sessions endpoints can open a session instead of returning
    ``POLICY_MISSING``. Safe to re-run: every step resolves the existing row.
    """
    from research.study.agents.distributions import (
        distribution_supported_platforms,
    )

    release, verified = import_manifest_release(session, request)

    owner = ensure_dev_owner(session, request.owner_email)
    connection = ensure_dev_provider_connection(
        session,
        label=request.connection_label,
        base_url=request.connection_base_url,
        secret_ref=request.connection_secret_ref,
        models=tuple(request.connection_models),
    )
    profile = pin_builtin_distribution(
        session,
        release,
        owner_user_id=owner.user_id,
        connection_id=connection.connection_id,
        model=request.profile_model or request.connection_models[0],
    )

    # Re-read the imported test-derived qualification.
    from research.study.agents import store as agents_store

    release_row = agents_store.get_release(session, release.release_id)
    if release_row is None:  # pragma: no cover - defensive
        raise SeedError(f"release {release.release_id!r} vanished")
    release = agents_store.row_to_release(release_row)

    seed_request = SeedRequest(
        account_email="",
        account_password=None,
        account_name="",
        create_account=False,
        config_id=None,
        study_name=request.study_name,
        agent_id=release.agent_id,
        release_id=release.release_id,
        release_version=release.version,
        artifact_digest="",
        artifact_path="",
        artifact_size=0,
        os_name="",
        arch="",
        actor=request.actor,
        require_capabilities=request.require_capabilities,
    )
    research_config = build_research_config(seed_request, profile_id=profile.profile_id)
    study = ensure_study(
        session,
        seed_request,
        research_config,
        owner_user_id=profile.owner_user_id,
        profile_id=profile.profile_id,
    )
    # Also covers the idempotent path where the study already existed.
    activate_study(session, study.study_id)

    live = protocol_store.get_study(session, study.study_id)
    join_code = live.join_code if live is not None else None
    return FreshDbSummary(
        agent_id=release.agent_id,
        release_id=release.release_id,
        release_version=release.version,
        qualification=release.qualification_status.value,
        distribution_id=str(profile.profile_id),
        distribution_verified=release.qualification_status == QualificationStatus.QUALIFIED,
        supported_platforms=distribution_supported_platforms(release),
        study_id=str(study.study_id),
        join_code=str(join_code or ""),
        session_policy=dict(research_config["session_policy"]),
        verified_artifacts=verified,
        provider_connection_label=connection.label,
        provider_connection_models=tuple(json.loads(connection.models_json or "[]")),
        profile_model=profile.model,
    )


def format_fresh_db_summary(summary: FreshDbSummary) -> str:
    """Render the fresh-DB seed result (no secrets)."""
    lines = [
        "Research database seeded from the built runtime manifest.",
        f"  agent_id:               {summary.agent_id}",
        f"  release_id:             {summary.release_id}",
        f"  release_version:        {summary.release_version}",
        f"  qualification:          {summary.qualification}",
        f"  distribution_id:        {summary.distribution_id}",
        f"  distribution_verified:  {summary.distribution_verified}",
        f"  supported_platforms:    {summary.supported_platforms}",
        f"  study_id:               {summary.study_id}",
        f"  join_code:              {summary.join_code}",
        f"  session_policy:         {summary.session_policy}",
        f"  provider_connection:    {summary.provider_connection_label} {list(summary.provider_connection_models)}",
        f"  profile_model:          {summary.profile_model}",
        f"  verified_artifacts:     {summary.verified_artifacts}",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Seed research onboarding state. Imports the built runtime manifest "
            "with its verified archive bytes, qualifies its release, pins the "
            "built-in distribution and creates a live study. Safe to re-run; no "
            "secrets are printed except a password supplied with "
            "--account-password."
        )
    )
    parser.add_argument(
        "--account-email",
        default=None,
        help="participant login email (informational; the study is seeded without it)",
    )
    parser.add_argument(
        "--fresh-db",
        action="store_true",
        help=(
            "accepted for backwards-compatible invocation; the manifest import is "
            "the only seed path"
        ),
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help=(
            "path to the producer runtime manifest JSON (use '-' to read it from "
            "stdin); basename archives must be present for a successful import"
        ),
    )
    parser.add_argument(
        "--archives-dir",
        default=None,
        help=(
            "directory holding the manifest's declared archives (basenames); the "
            "code4me-runtime/ resource subdirectory is also searched"
        ),
    )
    parser.add_argument(
        "--account-password",
        default=None,
        help="participant password (required with --create-account)",
    )
    parser.add_argument(
        "--account-name",
        default="Synthetic Participant",
        help="display name used when creating the account",
    )
    parser.add_argument(
        "--create-account",
        action="store_true",
        help="create the login account if it does not already exist",
    )
    parser.add_argument(
        "--config-id",
        type=int,
        default=None,
        help="completion config id for a new account (default: first available)",
    )
    parser.add_argument("--study-name", default=DEFAULT_STUDY_NAME)
    parser.add_argument("--agent-id", default=DEFAULT_AGENT_ID)
    parser.add_argument("--release-id", default=None)
    parser.add_argument("--release-version", default=DEFAULT_RELEASE_VERSION)
    parser.add_argument(
        "--artifact-digest",
        default=None,
        help="sha256:<64 hex> digest of the synthetic distribution artifact",
    )
    parser.add_argument(
        "--artifact-path",
        default=None,
        help="relative artifact path recorded on the release",
    )
    parser.add_argument("--artifact-size", type=int, default=DEFAULT_ARTIFACT_SIZE)
    parser.add_argument("--os", dest="os_name", default=None, help="artifact os")
    parser.add_argument("--arch", default=None, help="artifact architecture")
    parser.add_argument(
        "--distribution-mode",
        choices=[DistributionMode.PACKAGED.value, DistributionMode.BYOA_EXTERNAL.value],
        default=DistributionMode.PACKAGED.value,
        help=(
            "PACKAGED (default) pins a digest-pinned artifact; BYOA_EXTERNAL pins "
            "a participant-installed agent command/package"
        ),
    )
    parser.add_argument(
        "--agent-command",
        default=None,
        help="BYOA only: participant-installed agent command (for example goose)",
    )
    parser.add_argument(
        "--agent-command-args",
        default=None,
        help="BYOA only: space-separated args appended after the agent command",
    )
    parser.add_argument(
        "--agent-package",
        default=None,
        help="BYOA only: logical package to discover when no command is given (goose/codex)",
    )
    parser.add_argument(
        "--profile-id",
        "--agent-profile",
        dest="profile_id",
        type=UUID,
        default=None,
        help=(
            "UUID of an existing AgentProfile to select for the study "
            "(provider/model/tools/policy catalogue)"
        ),
    )
    parser.add_argument("--actor", default="seed-script", help="audit actor label")
    parser.add_argument(
        "--builtin-study-name",
        default=DEFAULT_BUILTIN_STUDY_NAME,
        help="study name used by --fresh-db",
    )
    parser.add_argument(
        "--connection-label",
        default=DEFAULT_DEV_CONNECTION_LABEL,
        help="--fresh-db: label of the development provider connection",
    )
    parser.add_argument(
        "--connection-base-url",
        default=DEFAULT_DEV_CONNECTION_BASE_URL,
        help="--fresh-db: upstream base URL for the development provider connection",
    )
    parser.add_argument(
        "--connection-secret-ref",
        default=DEFAULT_DEV_CONNECTION_SECRET_REF,
        help="--fresh-db: name of the deployment env var holding the provider key",
    )
    parser.add_argument(
        "--connection-model",
        action="append",
        default=[],
        metavar="MODEL",
        help=(
            "--fresh-db: allowed model on the development provider connection "
            "(repeatable; defaults to the built-in OpenRouter models)"
        ),
    )
    parser.add_argument(
        "--profile-model",
        default=None,
        help="--fresh-db: model the built-in profile selects (default: first connection model)",
    )
    parser.add_argument(
        "--capability-requirements",
        dest="require_capabilities",
        action="store_true",
        default=False,
        help=(
            "require host capability evidence (the compatibility gate). Off by "
            "default: the plugin half of the gate is not implemented, so a study "
            "that demands it cannot be joined."
        ),
    )
    return parser


def _load_manifest(source: str) -> dict[str, Any]:
    """Load a manifest JSON document from a path or ``-`` (stdin)."""
    if source == "-":
        payload = json.load(sys.stdin)
    else:
        with open(source, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    if not isinstance(payload, dict):
        raise SystemExit("manifest must be a JSON object")
    return payload


def fresh_db_request_from_args(args: argparse.Namespace) -> FreshDbRequest:
    """Build the fresh-DB seed request from parsed CLI arguments."""
    if not args.manifest:
        raise SystemExit("--fresh-db requires --manifest PATH (or --manifest -).")
    return FreshDbRequest(
        manifest=_load_manifest(args.manifest),
        archives_dir=args.archives_dir,
        study_name=args.builtin_study_name,
        actor=args.actor,
        require_capabilities=args.require_capabilities,
        connection_label=args.connection_label,
        connection_base_url=args.connection_base_url,
        connection_secret_ref=args.connection_secret_ref,
        connection_models=tuple(args.connection_model) or DEFAULT_DEV_CONNECTION_MODELS,
        profile_model=args.profile_model
        or (tuple(args.connection_model) or DEFAULT_DEV_CONNECTION_MODELS)[0],
    )


def require_dev_guard() -> None:
    """Fail fast unless this development-only seeder is explicitly enabled.

    The script mutates a real account/study/enrollment database, so it refuses
    to run unless ``CODE4ME_DEV_SEED=1`` or ``TEST_MODE=true`` is set. This
    makes it impossible to seed a production deployment by accident.
    """
    if os.environ.get("CODE4ME_DEV_SEED") == "1":
        return
    if os.environ.get("TEST_MODE", "").strip().lower() in {"1", "true", "yes"}:
        return
    raise SystemExit(
        "seed_research_study.py refuses to run: it is a development-only tool "
        "that mutates a real database. Set CODE4ME_DEV_SEED=1 (or TEST_MODE=true) "
        "to confirm you are seeding a local/dev/test database."
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    # Parse first so `--help` never requires the dev guard (or the app stack).
    parser = build_parser()
    args = parser.parse_args(argv)
    require_dev_guard()

    from dotenv import load_dotenv

    load_dotenv()

    # Imported lazily so importing this module (for tests or --help) never
    # pulls in the heavy application singleton / model stack.
    from App import App

    app = App.get_instance()
    session = app.get_db_session()
    try:
        # There is exactly one seed path: the built runtime manifest + its
        # verified archive bytes. A synthetic release with an invented digest is
        # no longer representable.
        fresh_request = fresh_db_request_from_args(args)
        summary = seed_fresh_database(session, fresh_request)
        print(format_fresh_db_summary(summary))
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
