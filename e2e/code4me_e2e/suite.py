"""One-command regression gate for backend, plugin, real IDE, website and ACP."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import browser, native_agents, prereqs, report, stack, ui, workflow
from .steps import STEP_ORDER, StepResult

#: Layers a full gate attempts, in execution order. ``plugin`` and ``backend``
#: depend on the IDE layer; ``browser`` is independent and is always attempted,
#: so an IDE failure can never hide a website regression.
ALL_LAYERS: Tuple[str, ...] = ("ui", "plugin", "backend", "agents", "browser")

#: Layers that are skipped when an earlier planned layer did not pass.
DEPENDENT_LAYERS = frozenset({"plugin", "backend"})

#: The report step id each layer records, when it has one.
LAYER_STEP_IDS = {"ui": "ui_test", "plugin": "plugin_test", "agents": "agents_test", "browser": browser.BROWSER_STEP_ID}


def plan_layers(layer: str) -> Tuple[str, ...]:
    """The layers one invocation attempts, in execution order."""
    if layer == "all":
        return ALL_LAYERS
    if layer == "plugin":
        # ``plugin`` keeps its historical meaning: plugin test plus the backend
        # workflow it depends on (the IDE layer is only part of ``all``).
        return ("plugin", "backend")
    if layer in ("backend", "browser", "agents"):
        return (layer,)
    raise ValueError(f"unknown layer {layer!r}")


@dataclass
class LayerResult:
    layer: str
    status: str = "NOT_ATTEMPTED"
    reason: str = ""

    @property
    def attempted(self) -> bool:
        return self.status != "NOT_ATTEMPTED"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "layer": self.layer,
            "status": self.status,
            "attempted": self.attempted,
            "reason": self.reason,
        }


def _read_report(run_path: Path) -> Dict[str, Any]:
    try:
        built = json.loads((run_path / "report.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return built if isinstance(built, dict) else {}


def layer_outcome(run_path: Path, *, step_id: Optional[str], rc: int) -> Tuple[str, str]:
    """Map one layer's exit code (plus its recorded step) to a gate status.

    A layer that recorded a BLOCKED step is reported as BLOCKED with its
    reason; a non-zero exit is never a pass.
    """
    built = _read_report(run_path)
    if step_id:
        step = next(
            (item for item in built.get("steps", []) if item.get("id") == step_id),
            None,
        )
        if step and step.get("status") == "BLOCKED":
            details = step.get("details") or {}
            reason = str(step.get("fix_hint") or details.get("error") or details.get("message") or "")
            return "BLOCKED", reason
        if rc == 0 and step and step.get("status") == "PASS":
            return "PASS", ""
        return "FAIL", ""
    # The HTTP workflow layer has no single step: judge it by the workflow's own
    # steps only, so another layer's blocked prerequisite cannot relabel it.
    statuses = {item.get("status") for item in built.get("steps", []) if item.get("id") in STEP_ORDER}
    if rc == 0 and statuses == {"PASS"}:
        return "PASS", ""
    if "FAIL" in statuses:
        return "FAIL", ""
    if "BLOCKED" in statuses:
        return "BLOCKED", ""
    return "FAIL", ""


#: Report step recording the prerequisite checks of this gate.
PREREQUISITES_STEP_ID = "prerequisites"


def _run_layer(name: str, scenario, run_path: Path,
               executables: Optional[Dict[str, str]] = None) -> int:
    if name == "ui":
        return ui.run_ui_test(scenario, run_dir=str(run_path), keep_stack=True)
    if name == "plugin":
        print("Testing live Kotlin plugin workflow", file=sys.stderr)
        return workflow.run_plugin_test(scenario, run_dir=str(run_path), keep_stack=True)
    if name == "backend":
        print("Testing HTTP inference, telemetry and enrollment revocation", file=sys.stderr)
        return workflow.run_workflow(scenario, from_step="bootstrap", run_dir=str(run_path), keep_stack=True)
    if name == "browser":
        return browser.run(scenario, run_path)
    if name == "agents":
        return native_agents.run(scenario, run_path, executables=executables)
    raise ValueError(f"unknown layer {name!r}")


def _final_report(run_path: Path, scenario, layers: Sequence[LayerResult],
                  prerequisites: Optional[prereqs.Prerequisites] = None) -> Dict[str, Any]:
    path = run_path / "report.json"
    if path.is_file():
        try:
            built = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            built = None
    else:
        built = None
    if not isinstance(built, dict):
        built = report.build_report(
            run_id=run_path.name,
            started_at=report.utc_now_iso(),
            finished_at=report.utc_now_iso(),
            scenario=scenario,
            steps=[],
            findings=[],
        )
    built["report_path"] = str(path)
    built["layers"] = [item.to_dict() for item in layers]
    if prerequisites is not None:
        built["prerequisites"] = prerequisites.to_dict()
    return built


def _record_prerequisites(run_path: Path, scenario, prerequisites: prereqs.Prerequisites,
                          duration_ms: int) -> None:
    """One report step for the host checks; BLOCKED names every unusable one."""
    unusable = [check.to_dict() for check in prerequisites.checks.values() if not check.usable]
    status = "BLOCKED" if unusable else "PASS"
    fix_hint = "; ".join(f"{item['id']}: {item.get('remediation') or item['detail']}" for item in unusable)
    workflow._record_plugin_step(run_path, scenario, StepResult(
        PREREQUISITES_STEP_ID, status, duration_ms,
        {"checks": [check.to_dict() for check in prerequisites.checks.values()]}, fix_hint,
    ))


def run(scenario, *, run_dir=None, keep_stack=False, as_json=False, layer="all",
        provision: bool = True) -> int:
    """Run every planned layer; missing prerequisites are provisioned first.

    With ``provision=False`` nothing is installed or started and a missing
    prerequisite blocks its layers exactly like one that cannot be provided.
    """
    run_path = Path(run_dir).resolve() if run_dir else report.new_run_dir(workflow.E2E_DIR)
    run_path.mkdir(parents=True, exist_ok=True)
    layers: Dict[str, LayerResult] = {name: LayerResult(name) for name in plan_layers(layer)}
    prerequisites: Optional[prereqs.Prerequisites] = None
    rc = 1
    started = time.monotonic()
    try:
        print(f"E2E artifacts: {run_path}", file=sys.stderr)
        print("Checking prerequisites" + (" (provisioning what is missing)" if provision else ""),
              file=sys.stderr)
        prerequisites = prereqs.prepare(scenario, tuple(layers), provision=provision)
        prereqs.print_table(prerequisites)
        _record_prerequisites(run_path, scenario, prerequisites, int((time.monotonic() - started) * 1000))
        with contextlib.redirect_stdout(io.StringIO()):
            prior: List[LayerResult] = []
            for name in layers:
                result = layers[name]
                if name in DEPENDENT_LAYERS and any(item.status != "PASS" for item in prior):
                    result.reason = "not attempted: an earlier dependent layer did not pass"
                    prior.append(result)
                    continue
                blocked = prerequisites.blocked_reason(name)
                if blocked:
                    # A missing prerequisite is reported, never skipped silently.
                    result.status, result.reason = "BLOCKED", blocked
                    prior.append(result)
                    continue
                try:
                    if layer == "backend":
                        layer_rc = workflow.run_workflow(scenario, run_dir=str(run_path), keep_stack=True)
                    else:
                        layer_rc = _run_layer(name, scenario, run_path, prerequisites.executables)
                except Exception as error:
                    result.status = "FAIL"
                    result.reason = str(error)
                    workflow._record_plugin_step(run_path, scenario, StepResult(
                        "suite", "FAIL", int((time.monotonic() - started) * 1000),
                        {"layer": name, "error": str(error)}, f"Inspect {run_path}",
                    ))
                    report.capture_backend_logs(scenario, run_path)
                    prior.append(result)
                    continue
                result.status, result.reason = layer_outcome(
                    run_path, step_id=LAYER_STEP_IDS.get(name), rc=layer_rc
                )
                prior.append(result)
            rc = 0 if all(item.status == "PASS" for item in layers.values()) else 1
        print("Layers: " + ", ".join(f"{item.layer}={item.status}" for item in layers.values()), file=sys.stderr)
    except (Exception, KeyboardInterrupt) as error:
        rc = 1
        result = StepResult("suite", "FAIL", int((time.monotonic() - started) * 1000),
                            {"error": str(error) or "interrupted", "attempted_layers": [item.to_dict() for item in layers.values()]},
                            f"Inspect {run_path}")
        workflow._record_plugin_step(run_path, scenario, result)
        report.capture_backend_logs(scenario, run_path)
    finally:
        if rc == 0 and not keep_stack and layer != "agents":
            try:
                stack.down(scenario)
            except Exception as error:
                rc = 1
                workflow._record_plugin_step(run_path, scenario,
                    StepResult("cleanup", "FAIL", 0, {"error": str(error)}, "Run stack down"))
        built = _final_report(run_path, scenario, list(layers.values()), prerequisites)
        report.write_report(run_path, built)
        report.print_summary(built, as_json)
    return rc
