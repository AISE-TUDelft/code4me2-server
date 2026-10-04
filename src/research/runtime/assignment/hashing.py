"""Salted-hash arm assignment and the study's assignment policy.

A study created with ``research_config_json["assignment"]["strategy"] ==
"DETERMINISTIC_HASH"`` gives each enrollment the arm with index

    k = floor(K * h / 2**64)
    h = first 8 bytes (big-endian) of
        SHA-256("{study_id}:{randomization_epoch}:{enrollment_id}")

over its K arms ordered by ``selection_order`` (UUIDs in lowercase hyphenated
form). The draw is reproducible from exported data, and the study id salts it,
so assignments in different studies are independent. Studies created before
this protocol decision carry no ``assignment`` block and keep the
``RANDOM_EQUAL`` draw. See ``docs/RESEARCH_ANALYTICS_SCOPE.md``.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, TypeVar

from research.study.protocol.enums import AssignmentStrategy

__all__ = [
    "assignment_policy",
    "assignment_strategy",
    "hash_arm_index",
    "hashed_profile",
    "manual_override_enabled",
    "new_study_assignment_policy",
]

_Profile = TypeVar("_Profile")


def hash_arm_index(
    study_id: uuid.UUID | str,
    randomization_epoch: int,
    enrollment_id: uuid.UUID | str,
    arm_count: int,
) -> int:
    """The arm index in ``range(arm_count)`` for one enrollment."""
    if arm_count < 1:
        raise ValueError("a study needs at least one arm")
    material = f"{uuid.UUID(str(study_id))}:{int(randomization_epoch)}:{uuid.UUID(str(enrollment_id))}"
    digest = hashlib.sha256(material.encode("ascii")).digest()
    return (arm_count * int.from_bytes(digest[:8], "big")) >> 64


def _selection_key(profile: Any) -> tuple[int, str]:
    profile_id = getattr(profile, "agent_profile_id", None) or getattr(profile, "profile_id", "")
    return (int(getattr(profile, "selection_order", 0) or 0), str(profile_id))


def hashed_profile(
    profiles: Sequence[_Profile],
    *,
    study_id: uuid.UUID | str,
    enrollment_id: uuid.UUID | str,
    randomization_epoch: int = 0,
) -> _Profile:
    """Pick the study arm the hash assigns (arms ordered by ``selection_order``)."""
    ordered = sorted(profiles, key=_selection_key)
    index = hash_arm_index(study_id, randomization_epoch, enrollment_id, len(ordered))
    return ordered[index]


def assignment_policy(config: Any) -> Mapping[str, Any]:
    """The frozen ``assignment`` block of a study config (empty for legacy studies)."""
    policy = config.get("assignment") if isinstance(config, Mapping) else None
    return policy if isinstance(policy, Mapping) else {}


def assignment_strategy(config: Any) -> str:
    """The study's allocation strategy; studies without a policy draw ``RANDOM_EQUAL``."""
    strategy = assignment_policy(config).get("strategy")
    return str(strategy) if strategy else AssignmentStrategy.RANDOM_EQUAL.value


def manual_override_enabled(config: Any) -> bool:
    """Whether the study's protocol allows the owner to change an arm by hand."""
    return assignment_policy(config).get("manual_override") is True


def new_study_assignment_policy(*, manual_override: bool) -> dict[str, Any]:
    """The assignment block every newly created study freezes into its config."""
    return {
        "strategy": AssignmentStrategy.DETERMINISTIC_HASH.value,
        "manual_override": bool(manual_override),
    }
