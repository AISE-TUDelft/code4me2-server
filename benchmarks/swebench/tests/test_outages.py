from __future__ import annotations

import json

from code4me_swebench import runner


def _task(run, iid, *, result, events=(), telemetry=None):
    directory = run / "instances" / iid
    directory.mkdir(parents=True)
    (directory / "result.json").write_text(json.dumps(result))
    (directory / "trace.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    (directory / "prediction.json").write_text(json.dumps({"instance_id": iid, "model_patch": ""}))
    (directory / "eval.json").write_text(json.dumps({"resolved": False, "status": "empty_patch"}))
    if telemetry is not None:
        (directory / "telemetry.json").write_text(json.dumps(telemetry))
    return directory


def test_provider_outage_is_told_apart_from_an_agent_failure(tmp_path):
    rate_limited = _task(tmp_path, "a__a-1", result={"run_status": "failed", "stop_reason": "error",
                                                      "final_response": "HTTP 429 after 3 attempts"},
                         events=[{"event_type": "agent.adapter.loop_failed",
                                  "payload": {"failure_reason": "provider_request_failed",
                                              "error_type": "ProviderRequestFailed"},
                                  "metrics": {"status_code": 429, "attempts": 3}}])
    finished = _task(tmp_path, "b__b-2", result={"run_status": "completed", "stop_reason": "end_turn"})
    gave_up = _task(tmp_path, "c__c-3", result={"run_status": "failed", "stop_reason": "max_iterations"})

    assert runner.provider_outage(rate_limited)["status_code"] == 429
    assert runner.provider_outage(finished) is None
    assert runner.provider_outage(gave_up) is None


def test_requeue_archives_outage_attempts_and_drops_their_predictions(tmp_path):
    run = tmp_path / "run"
    ids = ["a__a-1", "b__b-2"]
    run.mkdir()
    (run / "manifest.json").write_text(json.dumps({"instance_ids": ids, "settings": {}}))
    _task(run, "a__a-1", result={"run_status": "failed", "stop_reason": "error"})
    _task(run, "b__b-2", result={"run_status": "completed", "stop_reason": "end_turn"})
    stale_report = run / "logs" / "run_evaluation" / "run" / "m" / "a__a-1"
    stale_report.mkdir(parents=True)

    assert runner.requeue_outages(run) == ["a__a-1"]

    a = run / "instances" / "a__a-1"
    assert not (a / "prediction.json").exists() and (a / "attempts" / "1" / "trace.jsonl").exists()
    assert not stale_report.exists()
    assert (run / "instances" / "b__b-2" / "prediction.json").exists()
    assert [json.loads(line)["instance_id"] for line in (run / "predictions.jsonl").read_text().splitlines()] == ["b__b-2"]


def test_requeue_uses_mini_exit_statuses_for_baseline_runs(tmp_path):
    run = tmp_path / "mini"
    run.mkdir()
    (run / "manifest.json").write_text(json.dumps({"instance_ids": ["a__a-1", "b__b-2"], "baseline": {}}))
    _task(run, "a__a-1", result={"status": "completed"}, telemetry={"exit_status": "RateLimitError"})
    _task(run, "b__b-2", result={"status": "completed"}, telemetry={"exit_status": "Submitted"})

    assert runner.requeue_outages(run) == ["a__a-1"]


def test_a_rejected_request_is_the_agents_failure_not_an_outage(tmp_path):
    bad_request = _task(tmp_path, "d__d-4", result={"run_status": "failed", "stop_reason": "error"},
                        events=[{"event_type": "agent.adapter.loop_failed",
                                 "payload": {"failure_reason": "provider_request_failed"},
                                 "metrics": {"status_code": 400}}])
    assert runner.provider_outage(bad_request) is None


def test_requeue_finalizes_a_mislabelled_rejection_for_grading(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "manifest.json").write_text(json.dumps({"instance_ids": ["d__d-4"], "settings": {"model": "m"}}))
    directory = _task(run, "d__d-4", result={"run_status": "failed", "stop_reason": "error"},
                      events=[{"event_type": "agent.adapter.loop_failed",
                               "payload": {"failure_reason": "provider_request_failed"},
                               "metrics": {"status_code": 400}}])
    (directory / "prediction.json").unlink()
    (directory / "eval.json").unlink()
    (directory / "patch.diff").write_text("diff --git a/x b/x\n")
    (directory / "infra_error.json").write_text("{}")

    assert runner.requeue_outages(run) == []
    assert json.loads((directory / "prediction.json").read_text())["model_patch"].startswith("diff --git")
    assert not (directory / "infra_error.json").exists()
