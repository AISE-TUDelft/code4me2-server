"""Shared unit-test isolation: suite tests must never touch the real host."""
from __future__ import annotations

from unittest.mock import patch

from code4me_e2e import prereqs, suite


def ready_prerequisites(scenario, layers, *, provision=True):
    """Every prerequisite of ``layers`` READY, without running a single check."""
    result = prereqs.Prerequisites(tuple(layers))
    for layer in layers:
        for name in prereqs.LAYER_REQUIREMENTS.get(layer, ()):
            result.checks.setdefault(name, prereqs.Check(name, prereqs.READY, "unit-test fake"))
    return result


def isolate_prerequisites(test_case) -> None:
    """Patch the gate's host checks for the duration of ``test_case``."""
    patcher = patch.object(suite.prereqs, "prepare", side_effect=ready_prerequisites)
    patcher.start()
    test_case.addCleanup(patcher.stop)
