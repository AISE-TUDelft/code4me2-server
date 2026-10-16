from __future__ import annotations

import json
from collections import Counter

import pytest
from code4me_swebench import dataset, report
from code4me_swebench.prompt import task_prompt


def _rows() -> list[dict]:
    rows = []
    for repo, difficulty, count in [
        ("django/django", "<15 min fix", 40), ("django/django", "15 min - 1 hour", 30),
        ("sympy/sympy", "15 min - 1 hour", 20), ("psf/requests", "1-4 hours", 7),
        ("pallets/flask", "<15 min fix", 3),
    ]:
        for index in range(count):
            rows.append({"instance_id": f"{repo.split('/')[1]}-{difficulty[:2]}-{index}",
                         "repo": repo, "difficulty": difficulty})
    return rows


def test_stratified_sample_is_exact_proportional_and_deterministic():
    rows = _rows()
    first = dataset.stratified_sample(rows, 20, seed=3)
    assert len(first) == len(set(first)) == 20
    assert first == dataset.stratified_sample(rows, 20, seed=3)
    assert first != dataset.stratified_sample(rows, 20, seed=4)
    by_repo = Counter(iid.split("-")[0] for iid in first)
    # Quotas of 20: django 8 + 6, sympy 4, requests 1.4, flask 0.6. The last
    # seat goes to the largest remainder (flask 0.6 beats requests 0.4).
    assert by_repo == {"django": 14, "sympy": 4, "requests": 1, "flask": 1}


def test_read_subset_ignores_comments_and_rejects_duplicates(tmp_path):
    path = tmp_path / "s.txt"
    path.write_text("# header\na__a-1\n\nb__b-2  # note\n")
    assert dataset.read_subset(path) == ["a__a-1", "b__b-2"]
    path.write_text("a__a-1\na__a-1\n")
    with pytest.raises(ValueError):
        dataset.read_subset(path)


def test_select_rows_keeps_order_and_reports_missing_ids():
    rows = [{"instance_id": "x"}, {"instance_id": "y"}]
    assert [row["instance_id"] for row in dataset.select_rows(rows, ["y", "x"])] == ["y", "x"]
    with pytest.raises(KeyError):
        dataset.select_rows(rows, ["z"])


def test_bundled_subsets_have_fifty_distinct_ids():
    for name in ("verified-mini", "verified-stratified-50"):
        ids = dataset.read_subset(dataset.resolve_subset(name))
        assert len(ids) == 50 and all("__" in iid for iid in ids)


def test_wilson_interval_matches_reference_values():
    low, high = report.wilson_interval(25, 50)
    assert (round(low, 3), round(high, 3)) == (0.366, 0.634)
    assert report.wilson_interval(0, 0) == (0.0, 0.0)


def test_report_counts_ungraded_tasks_as_unresolved(tmp_path):
    run = tmp_path / "run"
    ids = ["a__a-1", "b__b-2", "c__c-3"]
    (run / "instances").mkdir(parents=True)
    (run / "manifest.json").write_text(json.dumps(
        {"instance_ids": ids, "settings": {"model": "m"}, "subset": "s"}))
    for iid, resolved in (("a__a-1", True), ("b__b-2", False)):
        directory = run / "instances" / iid
        directory.mkdir()
        (directory / "prediction.json").write_text("{}")
        (directory / "result.json").write_text(json.dumps({"status": "completed"}))
        (directory / "eval.json").write_text(json.dumps(
            {"resolved": resolved, "status": "resolved" if resolved else "unresolved"}))
        (directory / "telemetry.json").write_text(json.dumps(
            {"model_calls": 4, "tool_calls": {"read_file": 2}}))
        (directory / "patch.diff").write_text("--- a/x\n+++ b/x\n-old\n+new\n")

    data = report.write_report(run)

    assert (data["instances"], data["graded"], data["resolved"]) == (3, 2, 1)
    assert data["resolve_rate"] == round(1 / 3, 4)
    assert data["agent_status"] == {"completed": 2, "not_run": 1}
    assert data["model_calls"] == {"total": 8, "median": 4}
    table = (run / "instances.csv").read_text().splitlines()
    assert len(table) == 4 and "patch_lines" in table[0]


def test_task_prompt_carries_the_issue_but_no_hints():
    text = task_prompt("  The widget crashes.\n", workspace="/testbed")
    assert "<issue>\nThe widget crashes.\n</issue>" in text
    assert "/testbed" in text
    assert "hint" not in text.lower()


def test_mcnemar_exact_p_values():
    assert report.mcnemar_exact_p(0, 0) == 1.0
    assert report.mcnemar_exact_p(3, 3) == 1.0
    # 8 vs 1 discordant pairs: 2 * P(X <= 1), X ~ Bin(9, 0.5) = 2 * 10/512.
    assert round(report.mcnemar_exact_p(8, 1), 4) == round(20 / 512, 4)


def test_no_self_review_flag_is_explicit_in_the_settings():
    from code4me_swebench import cli

    def settings(*flags):
        return cli._settings(cli.build_parser().parse_args(["run", "--run-name", "x", *flags]))

    assert settings().harness_options == {}
    assert settings("--no-self-review").harness_options == {"self_review": False}
    assert settings("--no-self-review").as_dict()["harness_options"] == {"self_review": False}
