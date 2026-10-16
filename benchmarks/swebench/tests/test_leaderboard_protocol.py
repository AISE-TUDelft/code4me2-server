from __future__ import annotations

import json
import math
from statistics import stdev

import pytest
from code4me_swebench import cli, dataset, report, runner
from code4me_swebench.settings import RunSettings


def test_windows_are_end_exclusive_by_creation_date():
    rows = [{"instance_id": f"t-{day}", "created_at": f"2026-04-{day:02d}T10:00:00Z"} for day in (1, 14, 15, 30)]
    assert dataset.in_window(rows, "2026-04-01", "2026-04-15") == ["t-1", "t-14"]
    assert dataset.in_window(rows, "2026-04-15", "2026-05-01") == ["t-15", "t-30"]
    assert dataset.default_revision(dataset.REBENCH_DATASET) == dataset.REBENCH_REVISION
    assert dataset.default_revision(dataset.DATASET) is None


def test_bundled_windows_match_the_leaderboard_task_counts():
    counts = {name: len(dataset.read_subset(dataset.resolve_subset(name)))
              for name in ("rebench-2026_03-0301-0515", "rebench-2026_03-0301-0401", "rebench-2026_03-0401-0515",
                           "rebench-2026_03-0415-0515", "rebench-2026_03-0501-0515")}
    assert list(counts.values()) == [110, 46, 64, 39, 21]


def test_defaults_follow_the_leaderboard_protocol():
    settings = RunSettings()
    assert (settings.max_iterations, settings.context_tokens) == (500, 128_000)
    assert (settings.command_timeout_s, settings.max_command_timeout_s) == (300.0, 300.0)
    args = cli.build_parser().parse_args(
        ["run", "--run-name", "x", "--base-url", "https://openrouter.ai/api/v1",
         "--model", "deepseek/deepseek-v4-flash", "--openrouter-upstream", "deepinfra"])
    routed = cli._settings(args)
    assert routed.temperature == 1.0
    assert routed.extra_body["top_p"] == 1.0 and routed.extra_body["reasoning"] == {"effort": "high"}
    assert cli._settings(cli.build_parser().parse_args(["run", "--run-name", "x"])).temperature is None
    assert cli.repeat_names("base", 1) == ["base"]
    assert cli.repeat_names("base", 3) == ["base-r1", "base-r2", "base-r3"]


def test_repeats_run_task_by_task_and_pull_each_image_once(tmp_path, monkeypatch):
    calls, removed = [], []
    monkeypatch.setenv("TEST_KEY", "x")
    monkeypatch.setattr(runner.sandbox, "ensure", lambda base_url: "model.test")
    monkeypatch.setattr(runner.docker_cli, "remove_image", removed.append)

    def fake_run_instance(row, *, run_dir, run_name, settings, runtime, options):
        assert options.remove_images is False  # images go once, after the last attempt
        calls.append((row["instance_id"], run_name))
        return {"instance_id": row["instance_id"], "agent_status": "completed"}

    monkeypatch.setattr(runner, "run_instance", fake_run_instance)
    rows = [{"instance_id": "a", "image": "img-a"}, {"instance_id": "b", "image": "img-b"}]
    runs = [(tmp_path / "r1", "r1"), (tmp_path / "r2", "r2")]
    records = runner.run_repeated(rows, runs=runs, settings=RunSettings(api_key_env="TEST_KEY"),
                                  runtime=None, options=runner.RunOptions(remove_images=True), workers=1)

    assert calls == [("a", "r1"), ("a", "r2"), ("b", "r1"), ("b", "r2")]
    assert removed == ["img-a", "img-b"]
    assert {record["run"] for record in records} == {"r1", "r2"}


def test_repeats_stop_after_three_outages_in_a_row(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("TEST_KEY", "x")
    monkeypatch.setattr(runner.sandbox, "ensure", lambda base_url: "model.test")
    monkeypatch.setattr(runner.docker_cli, "remove_image", lambda image: None)

    def spent_key(row, *, run_dir, run_name, settings, runtime, options):
        calls.append((row["instance_id"], run_name))
        return {"instance_id": row["instance_id"], "agent_status": "infra_error", "status_code": 402}

    monkeypatch.setattr(runner, "run_instance", spent_key)
    rows = [{"instance_id": i, "image": f"img-{i}"} for i in ("a", "b", "c")]
    runs = [(tmp_path / f"r{n}", f"r{n}") for n in (1, 2)]
    runner.run_repeated(rows, runs=runs, settings=RunSettings(api_key_env="TEST_KEY"),
                        runtime=None, options=runner.RunOptions(), workers=1)
    assert len(calls) == 3  # a/r1, a/r2, b/r1: then the run stops


def _run_dir(base, name, resolved: dict, *, created: dict, tokens=(1000, 900, 100), cost=0.01):
    run_dir = base / name
    ids = list(resolved)
    (run_dir / "instances").mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({"instance_ids": ids, "settings": {"model": "m"},
                                                       "dataset": "d", "split": "s"}))
    (run_dir / "dataset.json").write_text(json.dumps([{"instance_id": i, "created_at": created[i]} for i in ids]))
    for instance_id, ok in resolved.items():
        directory = run_dir / "instances" / instance_id
        directory.mkdir()
        (directory / "prediction.json").write_text("{}")
        (directory / "result.json").write_text(json.dumps({"status": "completed"}))
        (directory / "eval.json").write_text(json.dumps({"resolved": ok, "status": "resolved" if ok else "failed"}))
        prompt, cached, out = tokens
        (directory / "telemetry.json").write_text(json.dumps({
            "model_calls": 10, "prompt_tokens": prompt, "cached_prompt_tokens": cached,
            "completion_tokens": out, "cost_usd": cost}))
    return run_dir


def test_aggregate_uses_the_leaderboard_formulas(tmp_path):
    created = {"a": "2026-03-10", "b": "2026-04-20", "c": "2026-05-05", "d": "2026-05-10"}
    outcomes = [
        {"a": True, "b": False, "c": True, "d": False},
        {"a": True, "b": True, "c": False, "d": False},
        {"a": False, "b": False, "c": True, "d": False},
    ]
    dirs = [_run_dir(tmp_path, f"x-r{n}", o, created=created) for n, o in enumerate(outcomes, start=1)]

    result = report.aggregate_runs(dirs, windows=[("2026-05-01", "2026-05-15")])

    rates = [50.0, 50.0, 25.0]
    overall = result["overall"]
    assert overall["resolved_rate"] == round(sum(rates) / 3, 2)
    assert overall["sem"] == round(stdev(rates) / math.sqrt(3), 2)
    assert overall["pass_at_3"] == 75.0  # a, b, c solved at least once; d never
    assert overall["tokens_per_problem"] == 1100 and overall["cached_share"] == 90.0
    assert overall["cost_per_problem"] == 0.01
    may = result["windows"]["2026-05-01..2026-05-15"]
    assert (may["tasks"], may["per_run_rates"]) == (2, [50.0, 0.0, 50.0])


def test_aggregate_compares_only_the_same_task_window_with_a_reference(tmp_path, monkeypatch):
    refs = {"references": {"ref": {"label": "R", "windows": {
        "2026-05-01..2026-05-15": {"tasks": 2, "resolved_rate": 40.0, "sem": 3.0},
        "2026-04-01..2026-05-15": {"tasks": 64, "resolved_rate": 40.0, "sem": 2.0}}}}}
    path = tmp_path / "references.json"
    path.write_text(json.dumps(refs))
    monkeypatch.setattr(report, "REFERENCES", path)
    created = {"c": "2026-05-05", "d": "2026-05-10"}
    dirs = [_run_dir(tmp_path, f"y-r{n}", {"c": n == 1, "d": False}, created=created) for n in (1, 2)]

    result = report.aggregate_runs(dirs, windows=[("2026-05-01", "2026-05-15"), ("2026-04-01", "2026-05-15")],
                                   reference="ref")

    may = result["windows"]["2026-05-01..2026-05-15"]
    assert may["reference"]["difference"] == round(may["resolved_rate"] - 40.0, 2)
    assert "reference" not in result["windows"]["2026-04-01..2026-05-15"]  # 2 tasks here, 64 there
    with pytest.raises(SystemExit):
        report.aggregate_runs(dirs, reference="missing")
