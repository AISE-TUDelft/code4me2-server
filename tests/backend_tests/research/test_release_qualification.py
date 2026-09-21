"""Receipt-derived qualification evidence (R1, R4, R9).

Qualification is derived, never transitioned: a release is QUALIFIED exactly
when a passing conformance receipt binds digest, platform and adapter to the
same declared component (PACKAGED) or to the release manifest digest (BYOA).
These tests drive the real persistence path -- agents ``upsert_release`` plus
packaging ``insert_receipt`` over a disposable database -- and the in-memory
RETIRED -> WITHDRAWN resolver mapping.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

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
from research.study.packaging import store as packaging_store
from research.study.packaging.enums import ConformanceStatus
from research.study.packaging.models import (
    ConformanceCaseResultV1,
    ConformanceReceiptV1,
    PlatformTriple,
)
from research.study.protocol.enums import ReleaseResolutionStatus

from ._byoa_contract import BYOA_CONFIG_BINDINGS

load_dotenv()
TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/test_db"
)

ARTIFACT_DIGEST = "sha256:" + "a" * 64
ADAPTER_DIGEST = "sha256:" + "d" * 64
MANIFEST_DIGEST = "sha256:" + "1" * 64


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


def _packaged_release(
    release_id: str,
    *,
    agent_id: str = "qual-agent",
    artifact_digest: str = ARTIFACT_DIGEST,
    adapter_digest: str = ADAPTER_DIGEST,
    **overrides,
) -> AgentReleaseV1:
    data: dict = {
        "agent_id": agent_id,
        "release_id": release_id,
        "version": "1.0.0",
        "source_manifest_digest": MANIFEST_DIGEST,
        "distribution_mode": DistributionMode.PACKAGED,
        "artifacts": [
            DistributionArtifact(
                os="macos",
                arch="arm64",
                path="bin/qual-agent",
                sha256=artifact_digest,
                size=10,
            )
        ],
        "adapter": AdapterRef(
            adapter_id="qual-adapter", version="1.0.0", digest=adapter_digest
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


def _receipt(
    *,
    artifact_digest: str,
    adapter_digest: str = ADAPTER_DIGEST,
    os_name: str = "macos",
    arch: str = "arm64",
    status: ConformanceStatus = ConformanceStatus.PASS,
    case_status: ConformanceStatus = ConformanceStatus.PASS,
) -> ConformanceReceiptV1:
    return ConformanceReceiptV1(
        receipt_id=uuid.uuid4(),
        artifact_digest=artifact_digest,
        adapter_digest=adapter_digest,
        host=PlatformTriple(os=os_name, arch=arch),
        plugin_version="2026.1",
        protocol_version="1",
        fixture_digests={},
        case_results=[
            ConformanceCaseResultV1(case_id="c1", status=case_status)
        ],
        status=status,
        created_at=datetime.now(timezone.utc),
    )


def test_packaged_release_unqualified_without_bound_receipt(db_sessions):
    session = db_sessions()
    try:
        row = agents_store.upsert_release(
            session, _packaged_release("rel-qual-unbound")
        )
        assert row.status == QualificationStatus.UNQUALIFIED.value
        assert (
            derive_qualification_status(row.release_json)
            == QualificationStatus.UNQUALIFIED
        )
        assert qualified_artifact_keys(row.release_json) == set()
        assert (
            artifact_qualified(
                row.release_json,
                os_name="macos",
                arch="arm64",
                digest=ARTIFACT_DIGEST,
                adapter_digest=ADAPTER_DIGEST,
            )
            is False
        )
    finally:
        session.close()


def test_packaged_receipt_bound_to_declared_component_qualifies(db_sessions):
    session = db_sessions()
    try:
        row = agents_store.upsert_release(
            session, _packaged_release("rel-qual-bound")
        )
        assert row.status == QualificationStatus.UNQUALIFIED.value

        packaging_store.insert_receipt(
            session,
            _receipt(
                artifact_digest=ARTIFACT_DIGEST, adapter_digest=ADAPTER_DIGEST
            ),
        )
        stored = agents_store.get_release(session, "rel-qual-bound")
        assert stored is not None
        assert stored.status == QualificationStatus.QUALIFIED.value
        assert (
            derive_qualification_status(stored.release_json)
            == QualificationStatus.QUALIFIED
        )
        assert qualified_artifact_keys(stored.release_json) != set()
        assert (
            artifact_qualified(
                stored.release_json,
                os_name="macos",
                arch="arm64",
                digest=ARTIFACT_DIGEST,
                adapter_digest=ADAPTER_DIGEST,
            )
            is True
        )
    finally:
        session.close()


def test_byoa_receipt_bound_to_manifest_digest_qualifies_identity(db_sessions):
    session = db_sessions()
    try:
        row = agents_store.upsert_release(session, _byoa_release("rel-qual-byoa"))
        assert row.status == QualificationStatus.UNQUALIFIED.value

        # The receipt host is irrelevant for BYOA: the identity is the
        # manifest digest, not a platform pin.
        packaging_store.insert_receipt(
            session,
            _receipt(
                artifact_digest=MANIFEST_DIGEST,
                adapter_digest=ADAPTER_DIGEST,
                os_name="windows",
                arch="x64",
            ),
        )
        stored = agents_store.get_release(session, "rel-qual-byoa")
        assert stored is not None
        assert stored.status == QualificationStatus.QUALIFIED.value
        assert byoa_identity_qualified(stored.release_json) is True
        assert (
            artifact_qualified(
                stored.release_json,
                os_name="macos",
                arch="arm64",
                digest=MANIFEST_DIGEST,
                adapter_digest=ADAPTER_DIGEST,
            )
            is False
        )
    finally:
        session.close()


def test_mismatched_digest_and_unbound_receipts_stay_unqualified(db_sessions):
    session = db_sessions()
    try:
        agents_store.upsert_release(session, _packaged_release("rel-qual-mismatch"))

        # Same artifact but a foreign adapter: the release is found, yet the
        # receipt binds to no declared identity.
        packaging_store.insert_receipt(
            session,
            _receipt(
                artifact_digest=ARTIFACT_DIGEST,
                adapter_digest="sha256:" + "e" * 64,
            ),
        )
        stored = agents_store.get_release(session, "rel-qual-mismatch")
        assert stored is not None
        assert stored.status == QualificationStatus.UNQUALIFIED.value
        assert (
            derive_qualification_status(stored.release_json)
            == QualificationStatus.UNQUALIFIED
        )

        # A FAIL receipt bound to the right digests is not evidence either.
        packaging_store.insert_receipt(
            session,
            _receipt(
                artifact_digest=ARTIFACT_DIGEST,
                adapter_digest=ADAPTER_DIGEST,
                status=ConformanceStatus.FAIL,
                case_status=ConformanceStatus.FAIL,
            ),
        )
        stored = agents_store.get_release(session, "rel-qual-mismatch")
        assert stored is not None
        assert stored.status == QualificationStatus.UNQUALIFIED.value

        # A digest no release owns cannot bind anywhere.
        with pytest.raises(LookupError):
            packaging_store.insert_receipt(
                session, _receipt(artifact_digest="sha256:" + "9" * 64)
            )
        stored = agents_store.get_release(session, "rel-qual-mismatch")
        assert stored is not None
        assert stored.status == QualificationStatus.UNQUALIFIED.value
    finally:
        session.close()


def test_withdrawn_scoping_retired_resolves_withdrawn_but_derives_unqualified(
    db_sessions,
):
    # In-memory registry: a RETIRED release resolves WITHDRAWN, while derive
    # on its evidence-free document stays UNQUALIFIED.
    registry = AgentRegistry()
    retired = _packaged_release(
        "rel-qual-retired",
        qualification_status=QualificationStatus.RETIRED,
    )
    assert registry.register_release(retired).accepted is True
    resolution = RegistryReleaseResolver(registry).resolve(
        retired.agent_id, release_id=retired.release_id
    )
    assert resolution.status == ReleaseResolutionStatus.WITHDRAWN
    assert (
        derive_qualification_status(retired.model_dump(mode="json"))
        == QualificationStatus.UNQUALIFIED
    )

    # Persisted path: a stored RETIRED release with no bound receipt derives
    # UNQUALIFIED through the real row (row_to_release re-derives; it never
    # preserves the caller-supplied RETIRED value).
    session = db_sessions()
    try:
        row = agents_store.upsert_release(session, retired)
        assert row.status == QualificationStatus.UNQUALIFIED.value
        assert (
            derive_qualification_status(row.release_json)
            == QualificationStatus.UNQUALIFIED
        )
        assert (
            agents_store.row_to_release(row).qualification_status
            == QualificationStatus.UNQUALIFIED
        )
    finally:
        session.close()
