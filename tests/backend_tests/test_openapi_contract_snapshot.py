"""The tracked curated OpenAPI contract must stay reproducible.

`openapi.json` is the curated 18-path plugin-client contract. The exporter is the
committed generation invocation, so CI can prove the tracked snapshot matches the
live routers instead of trusting a hand-edited file.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPORTER = REPO_ROOT / "scripts" / "dev" / "export_openapi.py"
SNAPSHOT = REPO_ROOT / "openapi.json"


def _exporter_module():
    spec = importlib.util.spec_from_file_location("export_openapi", EXPORTER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_tracked_snapshot_is_reproducible():
    result = subprocess.run(
        [sys.executable, str(EXPORTER), "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_the_snapshot_is_exactly_the_curated_client_contract():
    module = _exporter_module()
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    assert set(snapshot["paths"]) == set(module.CURATED_PATHS)
    # Every curated path must still exist on the live routers (a removed client
    # path is a breaking contract change, never a silent snapshot edit).
    live = module.curated_spec(SNAPSHOT)
    assert set(live["paths"]) == set(module.CURATED_PATHS)
