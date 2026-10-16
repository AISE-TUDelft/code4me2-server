from __future__ import annotations

import sys

from code4me_swebench import baseline_mini, dataset, evaluate
from code4me_swebench.settings import RunSettings


def test_rebench_rows_get_their_image_and_grader():
    row = dataset.normalize_row({"instance_id": "a__b-1", "image_name": "swerebench/x:latest",
                                 "docker_image": "swerebench/y:latest"})
    assert row["image"] == "swerebench/x:latest"
    verified = dataset.normalize_row({"instance_id": "c__d-2", "image": "swebench/z:latest"})
    assert verified["image"] == "swebench/z:latest"
    assert dataset.default_grader(dataset.REBENCH_DATASET) == "rebench"
    assert dataset.default_grader(dataset.DATASET) == "swebench"


def test_newest_takes_the_latest_created_tasks():
    rows = [{"instance_id": f"r__r-{n}", "created_at": f"2026-0{n}-01"} for n in range(1, 8)]
    assert dataset.newest(rows, 3) == ["r__r-5", "r__r-6", "r__r-7"]


def test_bundled_rebench_subset_is_fifty_ids():
    assert len(dataset.read_subset(dataset.resolve_subset("rebench-2026_03-newest-50"))) == 50


def test_swebench_grader_uses_this_interpreter():
    assert evaluate.grader_command("swebench")[0] == sys.executable


def test_mini_overlay_only_changes_endpoint_platform_and_cost_tracking():
    overlay = baseline_mini.overlay_config(RunSettings(model="deepseek-v4-flash"))
    assert set(overlay) == {"model", "environment"}
    assert overlay["model"]["model_name"] == "openai/deepseek-v4-flash"
    assert overlay["model"]["model_kwargs"]["api_base"] == "https://opencode.ai/zen/go/v1"
    assert overlay["model"]["cost_tracking"] == "ignore_errors"
    # No network in mini's task containers: it calls the model from the host.
    assert overlay["environment"]["run_args"] == ["--rm", "--platform", "linux/amd64", "--network", "none"]
    assert "step_limit" not in str(overlay)
