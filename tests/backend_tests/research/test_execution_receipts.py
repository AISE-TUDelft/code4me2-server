"""New execution leaves can reuse transport bytes without reusing old evidence."""
import pytest

from research.study.agents import store as agents_store
from research.study.agents.models import ExecutionFile, PackagedExecution
from research.study.packaging import store as packaging_store
from .test_release_qualification import (
    ADAPTER_DIGEST, ARTIFACT_DIGEST, _packaged_release, _receipt, db_sessions,
)


def test_receipt_targets_new_leaf_with_same_archive_and_is_immutable(db_sessions):
    with db_sessions() as session:
        old = _packaged_release("historical")
        new = _packaged_release("execution", source_manifest_digest="sha256:" + "2" * 64)
        new.artifacts[0].execution = PackagedExecution(
            entrypoint=["agent"], files=[ExecutionFile(
                path="agent", sha256="b" * 64, size=10, executable=True,
            )],
        )
        agents_store.upsert_release(session, old)
        agents_store.upsert_release(session, new)
        with pytest.raises(ValueError, match="multiple releases"):
            packaging_store.insert_receipt(
                session, _receipt(artifact_digest=ARTIFACT_DIGEST, adapter_digest=ADAPTER_DIGEST),
            )
        receipt = _receipt(artifact_digest=ARTIFACT_DIGEST, adapter_digest=ADAPTER_DIGEST).model_copy(
            update={"release_id": new.release_id,
                    "execution_manifest_digest": new.artifacts[0].execution.manifest_digest},
        )
        packaging_store.insert_receipt(session, receipt)
        assert agents_store.get_release(session, old.release_id).status == "UNQUALIFIED"
        assert agents_store.get_release(session, new.release_id).status == "QUALIFIED"
        packaging_store.insert_receipt(session, receipt)
        assert len(agents_store.get_release(session, new.release_id).release_json["conformance"]) == 1
        with pytest.raises(ValueError, match="immutable"):
            packaging_store.insert_receipt(session, receipt.model_copy(update={"plugin_version": "other"}))
        with pytest.raises(ValueError, match="execution manifest"):
            packaging_store.insert_receipt(session, receipt.model_copy(update={"execution_manifest_digest": "sha256:" + "0" * 64}))
        with pytest.raises(ValueError, match="platform archive"):
            packaging_store.insert_receipt(session, receipt.model_copy(update={"artifact_digest": "sha256:" + "0" * 64}))
