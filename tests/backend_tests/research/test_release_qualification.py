"""Recipe-derived usability over a disposable PostgreSQL database.

There is no approval or conformance-receipt step: the producer's recipe carries
the agent self-check verdict, and the import records it on the release. A release
is usable exactly when its stored recipe declares ``tests.status == "PASS"``; an
administrator may disable a release one-way.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from database.migration.migration_manager import MigrationManager
from research.study.agents import store as agents_store
from research.study.agents.enums import DistributionMode, QualificationStatus
from research.study.agents.models import (
    AdapterRef,
    AgentConfigBinding,
    AgentReleaseV1,
    DistributionArtifact,
)
from research.study.agents.registry import (
    AgentRegistry,
    artifact_qualified,
    byoa_identity_qualified,
    derive_qualification_status,
    qualified_artifact_keys,
)
from research.study.agents.resolver import RegistryReleaseResolver
from research.study.protocol.enums import ReleaseResolutionStatus

from ._byoa_contract import BYOA_CONFIG_BINDINGS

load_dotenv()
TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/test_db"
)

ARTIFACT_DIGEST = "sha256:" + "a" * 64
ADAPTER_DIGEST = "sha256:" + "d" * 64
MANIFEST_DIGEST = "sha256:" + "1" * 64

PASSING_TESTS = {
    "status": "PASS",
    "approval_options": ["auto", "per_step"],
    "cases": [{"case_id": "acp.initialize", "status": "PASS"}],
}


@pytest.fixture()
def db_sessions():
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
        connection.commit()
    manager = MigrationManager(use_test_db=True)
    manager.init_migrations()
    manager.migrate()
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


def _packaged_release(release_id: str, **overrides) -> AgentReleaseV1:
    data: dict = {
        "agent_id": "qual-agent",
        "release_id": release_id,
        "version": "1.0.0",
        "source_manifest_digest": MANIFEST_DIGEST,
        "distribution_mode": DistributionMode.PACKAGED,
        "artifacts": [
            DistributionArtifact(
                os="macos",
                arch="arm64",
                path="bin/qual-agent.zip",
                sha256=ARTIFACT_DIGEST,
                size=10,
            )
        ],
        "adapter": AdapterRef(
            adapter_id="qual-adapter", version="1.0.0", digest=ADAPTER_DIGEST
        ),
    }
    data.update(overrides)
    return AgentReleaseV1(**data)


def _byoa_release(release_id: str, *, manifest_digest: str = MANIFEST_DIGEST) -> AgentReleaseV1:
    return AgentReleaseV1(
        agent_id="byoa-agent",
        release_id=release_id,
        version="1.0.0",
        source_manifest_digest=manifest_digest,
        distribution_mode=DistributionMode.BYOA_EXTERNAL,
        agent_package="byoa-agent",
        byoa_config=[AgentConfigBinding(**item) for item in BYOA_CONFIG_BINDINGS],
        adapter=AdapterRef(
            adapter_id="byoa-adapter", version="1.0.0", digest=ADAPTER_DIGEST
        ),
    )


def test_packaged_release_is_unusable_without_a_passing_recipe(db_sessions):
    session = db_sessions()
    try:
        row = agents_store.upsert_release(session, _packaged_release("rel-unqualified"))
        assert row.status == QualificationStatus.UNQUALIFIED.value
        assert derive_qualification_status(row.release_json) == QualificationStatus.UNQUALIFIED
        assert qualified_artifact_keys(row.release_json) == set()
        assert (
            artifact_qualified(
                row.release_json,
                os_name="macos",
                arch="arm64",
                digest=ARTIFACT_DIGEST,
            )
            is False
        )
    finally:
        session.close()


def test_packaged_release_is_usable_from_the_recorded_recipe(db_sessions):
    session = db_sessions()
    try:
        row = agents_store.upsert_release(
            session, _packaged_release("rel-qualified"), evidence={"tests": PASSING_TESTS}
        )
        assert row.status == QualificationStatus.QUALIFIED.value
        assert (
            derive_qualification_status(row.release_json)
            == QualificationStatus.QUALIFIED
        )
        reloaded = agents_store.get_release(session, "rel-qualified")
        assert reloaded is not None
        assert reloaded.status == QualificationStatus.QUALIFIED.value
        assert (
            artifact_qualified(
                reloaded.release_json,
                os_name="macos",
                arch="arm64",
                digest=ARTIFACT_DIGEST,
                adapter_digest=ADAPTER_DIGEST,
            )
            is True
        )
        assert (
            artifact_qualified(
                reloaded.release_json,
                os_name="windows",
                arch="x64",
                digest=ARTIFACT_DIGEST,
            )
            is False
        )
    finally:
        session.close()


def test_byoa_release_is_usable_from_its_manifest_identity(db_sessions):
    session = db_sessions()
    try:
        row = agents_store.upsert_release(
            session, _byoa_release("rel-byoa"), evidence={"tests": PASSING_TESTS}
        )
        assert row.status == QualificationStatus.QUALIFIED.value
        assert byoa_identity_qualified(row.release_json) is True
        assert artifact_qualified(
            row.release_json,
            os_name="macos",
            arch="arm64",
            digest=MANIFEST_DIGEST,
        ) is False
    finally:
        session.close()


def test_failing_recipe_is_stored_unqualified(db_sessions):
    session = db_sessions()
    try:
        row = agents_store.upsert_release(
            session,
            _packaged_release("rel-failed-tests"),
            evidence={"tests": {"status": "FAIL", "cases": []}},
        )
        assert row.status == QualificationStatus.UNQUALIFIED.value
    finally:
        session.close()


def test_upsert_never_trusts_a_caller_supplied_status(db_sessions):
    session = db_sessions()
    try:
        release = _packaged_release(
            "rel-caller-status",
            qualification_status=QualificationStatus.QUALIFIED,
        )
        row = agents_store.upsert_release(session, release)
        assert row.status == QualificationStatus.UNQUALIFIED.value
        assert (
            agents_store.row_to_release(row).qualification_status
            == QualificationStatus.UNQUALIFIED
        )
    finally:
        session.close()


def test_disable_is_one_way_and_terminal(db_sessions):
    session = db_sessions()
    try:
        agents_store.upsert_release(
            session, _packaged_release("rel-disable"), evidence={"tests": PASSING_TESTS}
        )
        disabled = agents_store.disable_release(session, "rel-disable")
        assert disabled is not None
        assert disabled.status == QualificationStatus.DISABLED.value

        # Re-importing the same recipe does not re-enable it.
        agents_store.upsert_release(
            session, _packaged_release("rel-disable"), evidence={"tests": PASSING_TESTS}
        )
        reloaded = agents_store.get_release(session, "rel-disable")
        assert reloaded is not None
        assert reloaded.status == QualificationStatus.DISABLED.value
        assert (
            agents_store.row_to_release(reloaded).qualification_status
            == QualificationStatus.DISABLED
        )
        assert (
            derive_qualification_status(reloaded.release_json)
            == QualificationStatus.DISABLED
        )
    finally:
        session.close()


def test_disabled_release_resolves_withdrawn():
    registry = AgentRegistry()
    registry.register_release(_packaged_release("rel-retired", qualification_status=QualificationStatus.RETIRED))
    resolution = RegistryReleaseResolver(registry).resolve(
        "qual-agent", release_id="rel-retired"
    )
    assert resolution.status == ReleaseResolutionStatus.WITHDRAWN

    registry.register_release(
        _packaged_release("rel-disabled", qualification_status=QualificationStatus.DISABLED)
    )
    disabled = RegistryReleaseResolver(registry).resolve(
        "qual-agent", release_id="rel-disabled"
    )
    assert disabled.status == ReleaseResolutionStatus.WITHDRAWN
