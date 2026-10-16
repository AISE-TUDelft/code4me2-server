"""Producer test qualification and irreversible disable, without approval state."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from research.study.agents import store
from research.study.agents.enums import QualificationStatus
from research.study.agents.models import AgentReleaseV1
from research.study.agents.registry import AgentRegistry, derive_qualification_status
from research.study.agents.resolver import RegistryReleaseResolver
from research.study.protocol.enums import ReleaseResolutionStatus

TEST = {"os": "macos", "arch": "arm64", "self_check": "PASS", "acp_initialize": "PASS", "ran_at": "2026-09-21T00:00:00Z"}


def release(**changes):
    return AgentReleaseV1.model_validate(dict(
        agent_id="agent", release_id="agent-1", version="1.0.0",
        source_manifest_digest="sha256:" + "a" * 64,
        artifacts=[{"os": "macos", "arch": "arm64", "path": "agent.zip", "size": 10, "sha256": "sha256:" + "b" * 64}],
        tests=[TEST], **changes,
    ))


@pytest.mark.parametrize("tests", [None, [], {}, [{"status": "PASS"}], [dict(TEST, self_check="FAIL")], [dict(TEST, arch="x64")], [TEST, TEST]])
def test_incomplete_or_wrong_platform_results_do_not_qualify(tests):
    payload = release().model_dump(mode="json")
    payload["tests"] = tests
    assert derive_qualification_status(payload) == QualificationStatus.UNQUALIFIED


def test_results_qualify_and_arch_aliases_match():
    payload = release().model_dump(mode="json")
    payload["tests"][0]["arch"] = "aarch64"
    assert derive_qualification_status(payload) == QualificationStatus.QUALIFIED


def test_disabled_release_cannot_be_reenabled_by_reimport():
    model = release()
    row = SimpleNamespace(release_id=model.release_id, agent_id=model.agent_id,
                          source_manifest_digest=model.source_manifest_digest,
                          status="QUALIFIED", release_json=model.model_dump(mode="json"))
    session = MagicMock()
    session.get.return_value = row
    store.disable_release(session, model.release_id)
    assert store.upsert_release(session, model).status == "DISABLED"
    restored = store.row_to_release(row)
    assert restored.qualification_status == QualificationStatus.DISABLED
    registry = AgentRegistry()
    registry.register_release(restored)
    assert RegistryReleaseResolver(registry).resolve(model.agent_id, release_id=model.release_id).status == ReleaseResolutionStatus.WITHDRAWN


def test_existing_release_bytes_are_immutable():
    model = release()
    session = MagicMock()
    session.get.return_value = SimpleNamespace(agent_id=model.agent_id, source_manifest_digest="other")
    with pytest.raises(ValueError, match="immutable"):
        store.upsert_release(session, model)
    session.commit.assert_not_called()


def test_unknown_release_cannot_be_disabled():
    session = MagicMock()
    session.get.return_value = None
    assert store.disable_release(session, "missing") is None
