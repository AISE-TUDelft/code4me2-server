from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from threading import Event

from code4me2_agent.adapters import (
    CHECKPOINT_PREFIX,
    FakeOpenAICompatibleProvider,
    MemoryWindow,
    OpenAICompatibleReactAdapter,
    ToolRegistry,
)
from code4me2_agent.command_tools import WorkspaceCommandTools
from code4me2_agent.config import (
    AdapterConfig,
    AgentConfig,
    CommandConfig,
    FakeProviderConfig,
    HarnessOptions,
    OpenAICompatibleProviderConfig,
)
from code4me2_agent.events import ApprovalDecision
from code4me2_agent.file_tools import WorkspaceFileTools
from code4me2_agent.session_state import SessionToolState
from code4me2_agent.telemetry import AgentTelemetryRecorder

PYTHON = Path(sys.executable).name


class Sink:
    def __init__(self) -> None:
        self.tool_events = []
        self.texts = []
        self.plans = []

    def tool_call(self, event):
        self.tool_events.append(event)

    def thought(self, event):
        pass

    def assistant_text(self, event):
        self.texts.append(event)

    def plan(self, event):
        self.plans.append(event)

    def usage(self, event):
        pass

    def request_approval(self, tool_call, arguments):
        return ApprovalDecision("accepted", "once")


def _tc(call_id: str, name: str, **arguments) -> dict:
    return {"id": call_id, "name": name, "arguments": arguments}


class Loop:
    """An adapter wired like EchoAgentCore: session state shared by tools and loop."""

    def __init__(
        self,
        tmp_path: Path,
        script: list[dict],
        *,
        harness: HarnessOptions | None = None,
        max_iterations: int = 10,
        blocked: list[str] | None = None,
        model: str = "test-model",
        max_tokens: int = 32000,
        tools: list[str] | None = None,
        autonomous: bool = False,
    ) -> None:
        self.workspace = (tmp_path / "ws").resolve()
        self.workspace.mkdir(exist_ok=True)
        self.config = AgentConfig(
            workspace_root=self.workspace,
            trace_path=tmp_path / "trace.jsonl",
            session_id="session-1",
            tools=tools,
            commands=CommandConfig(blocked_commands=list(blocked or [])),
            adapter=AdapterConfig(
                name="openai_compatible_react",
                max_iterations=max_iterations,
                fake_provider=FakeProviderConfig(enabled=True, script=list(script)),
                provider=OpenAICompatibleProviderConfig(model=model),
            ),
            harness=harness or HarnessOptions(),
            autonomous=autonomous,
        )
        self.events: list[dict] = []

        class Capture:
            def append(inner, event):
                self.events.append(event)

        self.telemetry = AgentTelemetryRecorder(self.config, sinks=[Capture()])
        self.state = SessionToolState()
        self.sink = Sink()
        self.file_tools = WorkspaceFileTools(
            self.config, telemetry=self.telemetry, change_observer=self.state.record_change
        )
        self.command_tools = WorkspaceCommandTools(self.config, telemetry=self.telemetry)
        self.registry = ToolRegistry(
            self.file_tools,
            self.command_tools,
            allowed_tools=frozenset(tools) if tools is not None else None,
            event_sink=self.sink,
            telemetry=self.telemetry,
            workspace_root=self.workspace,
            session_state=self.state,
            harness=self.config.harness,
        )
        self.adapter = OpenAICompatibleReactAdapter(
            self.config,
            telemetry=self.telemetry,
            tool_registry=self.registry,
            event_sink=self.sink,
            session_state=self.state,
        )
        self.provider = FakeOpenAICompatibleProvider(list(script))
        self.adapter._provider = lambda: self.provider
        self.memory = MemoryWindow(strategy="token_window", max_messages=50, max_tokens=max_tokens)
        self.cancel = Event()

    def run(self, prompt: str = "do the thing", run_id: str = "run-1"):
        return self.adapter.handle_prompt(
            prompt=prompt,
            run_id=run_id,
            request_id=f"req-{run_id}",
            message_id=None,
            memory=self.memory,
            cancellation_event=self.cancel,
        )

    def request_text(self, call_index: int) -> str:
        return "\n".join(str(m.get("content", "")) for m in self.provider.calls[call_index]["messages"])

    def last_user_note(self, call_index: int) -> str:
        messages = self.provider.calls[call_index]["messages"]
        return str(messages[-1]["content"]) if messages[-1]["role"] == "user" else ""

    def requested_payloads(self) -> list[dict]:
        return [e["payload"] for e in self.events if e["event_type"] == "agent.model.requested"]

    def tool_results(self, call_index: int) -> list[dict]:
        return [
            json.loads(m["content"])
            for m in self.provider.calls[call_index]["messages"]
            if m["role"] == "tool"
        ]


# ----------------------------------------------------------------- loop guard


def test_identical_calls_are_nudged_at_three_and_stopped_at_five(tmp_path):
    read = lambda n: {"tool_calls": [_tc(f"r{n}", "list_files", path=".")]}  # noqa: E731
    loop = Loop(
        tmp_path,
        [read(1), read(2), read(3), read(4), read(5), {"final_answer": "I am stuck on the listing."}],
        harness=HarnessOptions(instruction_reminders=False),
    )

    result = loop.run()

    assert "identical arguments 3 times" in loop.last_user_note(3)
    assert "identical arguments 4 times" in loop.last_user_note(4)
    final_call = loop.provider.calls[5]
    assert final_call["tool_choice"] == "none"
    assert "Tools are now disabled" in loop.last_user_note(5)
    assert result.stop_reason == "loop_detected"
    assert result.run_status == "completed"
    assert loop.requested_payloads()[-1]["loop_guard"] == "forced_stop"


def test_a_workspace_change_resets_the_loop_guard(tmp_path):
    loop = Loop(
        tmp_path,
        [
            {"tool_calls": [_tc("a", "read_file", path="a.py")]},
            {"tool_calls": [_tc("b", "read_file", path="a.py")]},
            {"tool_calls": [_tc("c", "replace_text", path="a.py", old_text="1", new_text="2")]},
            {"tool_calls": [_tc("d", "read_file", path="a.py")]},
            {"tool_calls": [_tc("e", "read_file", path="a.py")]},
            {"final_answer": "done"},
        ],
        harness=HarnessOptions(instruction_reminders=False, self_review=False, verify_on_stop=False),
    )
    (loop.workspace / "a.py").write_text("x = 1\n")

    loop.run()

    for index in range(len(loop.provider.calls)):
        assert "identical arguments" not in loop.last_user_note(index)


# ------------------------------------------------------------------ reminders


def test_reminders_are_sent_every_four_calls_and_recorded(tmp_path):
    script = [{"tool_calls": [_tc(f"g{n}", "glob_files", pattern=f"*.{n}")]} for n in range(5)]
    loop = Loop(tmp_path, [*script, {"final_answer": "done"}])

    loop.run()

    assert "[Runtime reminder" in loop.last_user_note(4)
    assert "remain for this request" in loop.last_user_note(4)
    for index in (0, 1, 2, 3, 5):
        assert "[Runtime reminder" not in loop.last_user_note(index)
    payloads = loop.requested_payloads()
    assert payloads[4]["reminder"] is True and "reminder" not in payloads[0]


def test_long_sessions_get_a_reminder_on_the_first_call(tmp_path):
    loop = Loop(tmp_path, [{"final_answer": "ok"}])
    for index in range(10):
        loop.memory.append({"role": "user", "content": f"q{index}"})
        loop.memory.append({"role": "assistant", "content": f"a{index}"})

    loop.run()

    assert "[Runtime reminder" in loop.last_user_note(0)


# ----------------------------------------------------- prompt profile, AGENTS


def test_openai_models_get_their_prompt_variant_and_it_is_recorded(tmp_path):
    loop = Loop(tmp_path, [{"final_answer": "hi"}], model="gpt-5.1-codex")
    loop.run("hello")

    system = loop.provider.calls[0]["messages"][0]["content"]
    assert "never write a single-step plan" in system
    assert "Persist until the request is fully handled" in system
    assert "Make edits with apply_patch" in system
    assert loop.requested_payloads()[0]["prompt_profile"] == "openai"


def test_default_models_keep_the_plan_first_prompt(tmp_path):
    loop = Loop(tmp_path, [{"final_answer": "hi"}], model="claude-sonnet-5")
    loop.run("hello")
    system = loop.provider.calls[0]["messages"][0]["content"]
    assert "call update_plan before you start" in system
    assert loop.requested_payloads()[0]["prompt_profile"] == "anthropic"


def test_project_instructions_are_appended_and_recorded(tmp_path):
    loop = Loop(tmp_path, [{"final_answer": "hi"}])
    (loop.workspace / "AGENTS.md").write_text("Always run `make check` before finishing.\n")

    loop.run("hello")

    system = loop.provider.calls[0]["messages"][0]["content"]
    assert "Project instructions (AGENTS.md)" in system
    assert "make check" in system
    payload = loop.requested_payloads()[0]
    assert payload["instruction_files"] == ["AGENTS.md"]


def test_project_instructions_can_be_disabled(tmp_path):
    loop = Loop(tmp_path, [{"final_answer": "hi"}], harness=HarnessOptions(project_instructions=False))
    (loop.workspace / "AGENTS.md").write_text("secret convention\n")
    loop.run("hello")
    assert "secret convention" not in loop.provider.calls[0]["messages"][0]["content"]


# -------------------------------------------------------------------- ask_user


def test_ask_user_ends_the_turn_with_the_question(tmp_path):
    loop = Loop(
        tmp_path,
        [
            {
                "text": "One decision first.",
                "tool_calls": [
                    _tc("q", "ask_user", question="Which database?", options=["Postgres", "SQLite"]),
                    _tc("r", "read_file", path="a.py"),
                ],
            }
        ],
    )

    result = loop.run()

    assert result.final_response == "Which database?\n\n1. Postgres\n2. SQLite"
    assert result.stop_reason == "awaiting_user"
    assert result.run_status == "completed"
    assert loop.sink.texts[-1].final is True
    assert all(event.tool_name != "ask_user" for event in loop.sink.tool_events)
    tool_messages = [m for m in loop.memory.snapshot() if m["role"] == "tool"]
    assert json.loads(tool_messages[1]["content"])["status"] == "skipped"
    assert loop.memory.snapshot()[-1] == {"role": "assistant", "content": result.final_response}


# ------------------------------------------------------------ read before edit


def test_edits_need_a_prior_read_unless_forced(tmp_path):
    loop = Loop(
        tmp_path,
        [
            {"tool_calls": [_tc("e1", "replace_text", path="a.py", old_text="1", new_text="2")]},
            {"tool_calls": [_tc("w1", "write_file", path="b.py", content="new\n")]},
            {"tool_calls": [_tc("r1", "read_file", path="a.py")]},
            {"tool_calls": [_tc("e2", "replace_text", path="a.py", old_text="1", new_text="2")]},
            {"tool_calls": [_tc("w2", "write_file", path="b.py", content="forced\n", force=True)]},
            {"tool_calls": [_tc("c1", "create_file", path="c.py", content="x\n")]},
            {"final_answer": "done"},
        ],
        harness=HarnessOptions(self_review=False, verify_on_stop=False),
    )
    (loop.workspace / "a.py").write_text("x = 1\n")
    (loop.workspace / "b.py").write_text("old\n")

    loop.run()

    first = loop.tool_results(1)[0]
    assert first["status"] == "error" and first["error_code"] == "read_before_edit"
    assert "read_file" in first["error"]
    assert loop.tool_results(2)[1]["error_code"] == "read_before_edit"
    assert (loop.workspace / "a.py").read_text() == "x = 2\n"
    assert (loop.workspace / "b.py").read_text() == "forced\n"
    assert (loop.workspace / "c.py").read_text() == "x\n"


def test_grep_hits_count_as_seen(tmp_path):
    loop = Loop(
        tmp_path,
        [
            {"tool_calls": [_tc("g", "grep_files", pattern="value")]},
            {"tool_calls": [_tc("e", "replace_text", path="a.py", old_text="value = 1", new_text="value = 2")]},
            {"final_answer": "done"},
        ],
        harness=HarnessOptions(self_review=False, verify_on_stop=False),
    )
    (loop.workspace / "a.py").write_text("value = 1\n")
    loop.run()
    assert (loop.workspace / "a.py").read_text() == "value = 2\n"


# ------------------------------------------------------------------ apply_patch


PATCH = """*** Begin Patch
*** Update File: src/app.py
@@ def handler(event):
-    return None
+    return event
*** Add File: src/new.py
+print("hello")
*** Delete File: old.txt
*** Update File: notes.md
*** Move to: docs/notes.md
@@
 title
-draft
+final
*** End Patch"""


def test_apply_patch_changes_several_files_atomically_in_one_card(tmp_path):
    loop = Loop(
        tmp_path,
        [
            {"tool_calls": [_tc("r", "read_file", path="src/app.py"), _tc("r2", "read_file", path="notes.md"), _tc("r3", "read_file", path="old.txt")]},
            {"tool_calls": [_tc("p", "apply_patch", patch=PATCH)]},
            {"final_answer": "done"},
        ],
        harness=HarnessOptions(self_review=False, verify_on_stop=False),
    )
    (loop.workspace / "src").mkdir()
    # Exact bytes: text mode would write CRLF on Windows, which the patch keeps.
    (loop.workspace / "src/app.py").write_bytes(b"def handler(event):\n    return None\n")
    (loop.workspace / "notes.md").write_bytes(b"title\ndraft\n")
    (loop.workspace / "old.txt").write_bytes(b"bye\n")

    loop.run()

    result = loop.tool_results(2)[-1]
    assert result["status"] == "ok" and result["file_count"] == 4
    assert (loop.workspace / "src/app.py").read_text() == "def handler(event):\n    return event\n"
    assert (loop.workspace / "src/new.py").read_text() == 'print("hello")\n'
    assert not (loop.workspace / "old.txt").exists()
    assert not (loop.workspace / "notes.md").exists()
    assert (loop.workspace / "docs/notes.md").read_text() == "title\nfinal\n"
    completed = [e for e in loop.sink.tool_events if e.tool_name == "apply_patch" and e.phase == "completed"][0]
    assert completed.kind == "edit"
    assert completed.diff_new_text == "def handler(event):\n    return event\n"
    assert len(completed.extra_diffs) == 3
    assert len(completed.locations) == 4
    # One turn checkpoint covers every file the patch touched.
    assert set(loop.state.last_turn().files) == {"src/app.py", "src/new.py", "old.txt", "notes.md", "docs/notes.md"}


def test_a_patch_that_does_not_apply_changes_nothing(tmp_path):
    bad = PATCH.replace("-    return None", "-    return 42")
    loop = Loop(
        tmp_path,
        [
            {"tool_calls": [_tc("r", "read_file", path="src/app.py"), _tc("r2", "read_file", path="notes.md"), _tc("r3", "read_file", path="old.txt")]},
            {"tool_calls": [_tc("p", "apply_patch", patch=bad)]},
            {"final_answer": "done"},
        ],
        harness=HarnessOptions(self_review=False, verify_on_stop=False),
    )
    (loop.workspace / "src").mkdir()
    (loop.workspace / "src/app.py").write_text("def handler(event):\n    return None\n")
    (loop.workspace / "notes.md").write_text("title\ndraft\n")
    (loop.workspace / "old.txt").write_text("bye\n")

    loop.run()

    result = loop.tool_results(2)[-1]
    assert result["status"] == "error" and result["error_code"] == "patch_does_not_apply"
    assert (loop.workspace / "old.txt").exists()
    assert not (loop.workspace / "src/new.py").exists()
    assert (loop.workspace / "src/app.py").read_text() == "def handler(event):\n    return None\n"


def test_apply_patch_updates_need_a_prior_read(tmp_path):
    loop = Loop(
        tmp_path,
        [{"tool_calls": [_tc("p", "apply_patch", patch=PATCH)]}, {"final_answer": "done"}],
        harness=HarnessOptions(self_review=False, verify_on_stop=False),
    )
    (loop.workspace / "src").mkdir()
    (loop.workspace / "src/app.py").write_text("def handler(event):\n    return None\n")
    (loop.workspace / "notes.md").write_text("title\ndraft\n")
    (loop.workspace / "old.txt").write_text("bye\n")
    loop.run()
    assert loop.tool_results(1)[0]["error_code"] == "read_before_edit"


# -------------------------------------------------------------- self-review


def _edit_script(final: str = "Changed a.py.") -> list[dict]:
    return [
        {"tool_calls": [_tc("r", "read_file", path="a.py")]},
        {"tool_calls": [_tc("e", "replace_text", path="a.py", old_text="x = 1", new_text="x = 2")]},
        {"final_answer": final},
    ]


def test_self_review_findings_lead_to_one_fix_iteration(tmp_path):
    script = [
        *_edit_script(),
        {"final_answer": "- a.py line 1: the request asked for x = 3, not 2."},
        {"tool_calls": [_tc("f", "replace_text", path="a.py", old_text="x = 2", new_text="x = 3")]},
        {"final_answer": "Set x to 3."},
    ]
    loop = Loop(tmp_path, script, harness=HarnessOptions(verify_on_stop=False, instruction_reminders=False))
    (loop.workspace / "a.py").write_text("x = 1\n")

    result = loop.run("set x to 3")

    review_call = loop.provider.calls[3]
    assert review_call["include_tools"] is False
    assert review_call["messages"][0]["role"] == "system"
    assert "+x = 2" in review_call["messages"][1]["content"]
    assert "Self-review by the Code4Me runtime" in loop.request_text(4)
    assert result.final_response == "Set x to 3."
    assert (loop.workspace / "a.py").read_text() == "x = 3\n"
    purposes = [payload["call_purpose"] for payload in loop.requested_payloads()]
    assert purposes == ["turn", "turn", "turn", "self_review", "turn", "turn"]
    assert len(loop.provider.calls) == 6  # one review, never a second one
    progress = [event.text for event in loop.sink.texts if not event.final]
    assert any("Self-review of the changes raised possible issues" in text for text in progress)


def test_no_issues_finishes_with_the_original_answer(tmp_path):
    loop = Loop(
        tmp_path,
        [*_edit_script(), {"final_answer": "NO_ISSUES"}],
        harness=HarnessOptions(verify_on_stop=False),
    )
    (loop.workspace / "a.py").write_text("x = 1\n")
    result = loop.run()
    assert result.final_response == "Changed a.py."
    assert len(loop.provider.calls) == 4


def test_each_model_message_keeps_its_own_reasoning_and_runtime_notes_get_none(tmp_path):
    script = [
        {"tool_calls": [_tc("r", "read_file", path="a.py")], "reasoning_fields": {"reasoning_content": "plan A"}},
        {"tool_calls": [_tc("e", "replace_text", path="a.py", old_text="x = 1", new_text="x = 2")],
         "reasoning_fields": {"reasoning_content": "plan B"}},
        {"final_answer": "Changed a.py.", "reasoning_fields": {"reasoning_content": "plan C"}},
        # The clean-context review (a side call: it must not consume the turn's reasoning).
        {"final_answer": "- a.py line 1: the request asked for x = 3."},
        {"tool_calls": [_tc("f", "replace_text", path="a.py", old_text="x = 2", new_text="x = 3")],
         "reasoning_fields": {"reasoning_content": "plan D"}},
        {"final_answer": "Set x to 3.", "reasoning_fields": {"reasoning_content": "plan E"}},
    ]
    loop = Loop(tmp_path, script, harness=HarnessOptions(verify_on_stop=False, instruction_reminders=False))
    (loop.workspace / "a.py").write_text("x = 1\n")

    result = loop.run("set x to 3")

    assert result.final_response == "Set x to 3."
    last_request = loop.provider.calls[-1]["messages"]
    carried = [(m.get("provider_reasoning") or {}).get("reasoning_content") for m in last_request
               if m.get("role") == "assistant"]
    # A, B: tool calls; C: the provisional answer kept before the review note; D: the fix.
    assert carried == ["plan A", "plan B", "plan C", "plan D"]
    runtime_notes = [m for m in last_request if m.get("role") == "user" and m.get("code4me_runtime")]
    assert runtime_notes and all("provider_reasoning" not in m for m in runtime_notes)
    review_call = loop.provider.calls[3]["messages"]
    assert all("provider_reasoning" not in m for m in review_call)  # side calls start clean
    assert loop.memory.snapshot()[-1]["provider_reasoning"] == {"reasoning_content": "plan E"}


def test_a_failed_review_is_recorded_and_the_turn_finishes(tmp_path):
    # The script has no answer for the review call, so the provider raises.
    loop = Loop(tmp_path, _edit_script(), harness=HarnessOptions(verify_on_stop=False))
    (loop.workspace / "a.py").write_text("x = 1\n")

    result = loop.run()

    assert result.final_response == "Changed a.py."
    # No event of its own (step counts are event counts): the turn reports it.
    assert [failure["call_purpose"] for failure in result.side_call_failures] == ["self_review"]
    assert result.side_call_failures[0]["error_type"]
    kinds = {event["event_type"] for event in loop.events}
    assert "agent.model.failed" not in kinds


def test_a_provider_failure_in_the_main_loop_closes_the_request(tmp_path):
    from code4me2_agent.adapters import ProviderRequestFailed

    class RateLimited:
        def generate(self, *_args, **_kwargs):
            raise ProviderRequestFailed("Go usage limit exceeded", status_code=429, attempts=3)

    loop = Loop(tmp_path, [], harness=HarnessOptions(verify_on_stop=False))
    loop.adapter._provider = lambda: RateLimited()

    result = loop.run()

    assert result.run_status == "failed"
    kinds = [event["event_type"] for event in loop.events]
    assert "agent.model.failed" not in kinds
    failed = next(event for event in loop.events if event["event_type"] == "agent.adapter.loop_failed")
    assert failed["payload"]["failure_reason"] == "provider_request_failed"
    assert (failed["payload"]["call_purpose"], failed["payload"]["error_type"]) == ("turn", "ProviderRequestFailed")
    # Numbers are metrics: the server keeps them as structural data without content consent.
    assert (failed["metrics"]["status_code"], failed["metrics"]["attempts"]) == (429, 3)


def test_review_is_skipped_without_budget_for_a_fix(tmp_path):
    loop = Loop(tmp_path, _edit_script(), harness=HarnessOptions(verify_on_stop=False), max_iterations=4)
    (loop.workspace / "a.py").write_text("x = 1\n")
    result = loop.run()
    assert result.final_response == "Changed a.py."
    assert len(loop.provider.calls) == 3


def test_turns_without_changes_are_not_reviewed(tmp_path):
    loop = Loop(tmp_path, [{"final_answer": "Just an answer."}])
    loop.run("explain the code")
    assert len(loop.provider.calls) == 1


# ------------------------------------------------------------- verify on stop


def test_configured_verification_failure_sends_the_model_back(tmp_path):
    check = "import sys; sys.exit(0 if open('a.py').read() == 'x = 3\\n' else 1)"
    script = [
        *_edit_script("Done."),
        {"tool_calls": [_tc("f", "replace_text", path="a.py", old_text="x = 2", new_text="x = 3")]},
        {"final_answer": "Fixed."},
    ]
    loop = Loop(
        tmp_path,
        script,
        harness=HarnessOptions(
            self_review=False, verify_command=(PYTHON, "-c", check), instruction_reminders=False
        ),
    )
    (loop.workspace / "a.py").write_text("x = 1\n")

    result = loop.run()

    assert "verification command" in loop.request_text(3) and "failed (exit code 1)" in loop.request_text(3)
    assert result.final_response.startswith("Fixed.")
    assert f"Verified by the runtime: `{PYTHON} -c" in result.final_response
    verify_cards = [e for e in loop.sink.tool_events if e.tool_call_id.startswith("verify-")]
    assert {e.phase for e in verify_cards} >= {"started", "completed"}
    runtime_calls = [
        m for m in loop.memory.snapshot() if m.get("code4me_runtime") == "verify" and m["role"] == "assistant"
    ]
    assert len(runtime_calls) == 2


def test_without_a_command_the_model_is_nudged_once_to_verify(tmp_path):
    loop = Loop(
        tmp_path,
        [*_edit_script("Done."), {"final_answer": "Done; run pytest to check."}],
        harness=HarnessOptions(self_review=False, instruction_reminders=False),
    )
    (loop.workspace / "a.py").write_text("x = 1\n")

    result = loop.run()

    assert "ran no command since your last change" in loop.request_text(3)
    assert "If a test, build or lint command applies, run it now" in loop.request_text(3)
    assert result.final_response == "Done; run pytest to check."
    assert len(loop.provider.calls) == 4


def test_status_says_whether_the_runtime_will_run_the_verify_command(tmp_path):
    (tmp_path / "blocked").mkdir()
    (tmp_path / "runnable").mkdir()
    blocked = Loop(tmp_path / "blocked", [], harness=HarnessOptions(verify_command=("pytest", "-q")), blocked=["PyTest"])
    status = blocked.run("/status").final_response
    assert "- Commands: any installed program except PyTest" in status
    assert "- Verification: the agent is asked to verify after changes (pytest -q cannot run in this session)" in status

    runnable = Loop(tmp_path / "runnable", [], harness=HarnessOptions(verify_command=(PYTHON, "-c", "pass")))
    assert f"- Verification: the runtime runs {PYTHON} -c pass after changes" in runnable.run("/status").final_response

    (tmp_path / "no-commands").mkdir()
    no_commands = Loop(
        tmp_path / "no-commands", [], harness=HarnessOptions(verify_command=(PYTHON, "-c", "pass")),
        tools=["read_file", "replace_text"],
    )
    status = no_commands.run("/status").final_response
    assert "- Commands: none" in status
    assert "- Verification: off: commands cannot run in this session" in status


def test_a_blocked_verify_command_falls_back_to_the_nudge(tmp_path):
    """A config row may block the frozen verify command's program: the runtime skips it."""
    loop = Loop(
        tmp_path,
        [*_edit_script("Done."), {"final_answer": "Done; run the check yourself."}],
        harness=HarnessOptions(
            self_review=False, verify_command=("bash", "-c", f"{PYTHON} -c 1"), instruction_reminders=False
        ),
        blocked=[PYTHON.upper()],
    )
    (loop.workspace / "a.py").write_text("x = 1\n")

    result = loop.run()

    assert not [e for e in loop.sink.tool_events if e.tool_call_id.startswith("verify-")]
    assert "ran no command since your last change" in loop.request_text(3)
    assert result.final_response == "Done; run the check yourself."


def test_only_commands_that_run_the_code_count_as_verification(tmp_path):
    from code4me2_agent.adapters import ToolCall, _is_verification_command, _TurnState

    for argv in (["git", "status"], ["git", "diff"], ["ls", "-la"], ["cat", "a.py"], ["grep", "-n", "x", "a.py"],
                 ["sed", "-n", "1,20p", "a.py"], ["bash", "-c", "git diff | head -50"],
                 ["bash", "-lc", "cd src && ls"], ["pip", "install", "-e", "."], ["python", "-m", "pip", "install", "x"],
                 ["uv", "add", "requests"], ["npm", "install"], ["bash", "-c", "pip install -q pytest"]):
        assert not _is_verification_command(argv), argv
    for argv in (["pytest", "-q"], ["python", "-m", "pytest", "tests"], ["python", "repro.py"],
                 ["bash", "-c", "cd src && pytest -q | tail -20"], ["make", "test"], ["npm", "test"],
                 ["bash", "-c", "FOO=1 python check.py"], ["bash", "-c", "unbalanced 'quote"],
                 ["uv", "run", "pytest"], ["npm", "run", "test"], ["bash", "-c", "pip install -q . && pytest"]):
        assert _is_verification_command(argv), argv

    loop = Loop(tmp_path, [])
    state = _TurnState()
    loop.adapter._note_step(state, ToolCall("e", "replace_text", {}), {"status": "ok"})
    loop.adapter._note_step(state, ToolCall("g", "run_command", {"argv": ["git", "status"]}), {"status": "ok"})
    assert state.last_change_step > state.last_command_step  # inspection: still unverified
    loop.adapter._note_step(state, ToolCall("t", "run_command", {"argv": ["pytest", "-q"]}), {"status": "error"})
    assert state.last_change_step > state.last_command_step  # never ran
    loop.adapter._note_step(state, ToolCall("t", "run_command", {"argv": ["pytest", "-q"]}), {"status": "ok"})
    assert state.last_command_step > state.last_change_step  # ran the tests, failing or not
    assert state.commands_run == ["git status", "pytest -q", "pytest -q"]


def _announcing_script(*finals: str) -> list[dict]:
    script: list[dict] = []
    for index, text in enumerate(finals):
        script.append({"tool_calls": [_tc(f"r{index}", "read_file", path="a.py")]})
        script.append({"final_answer": text})
    return script


def test_ide_sessions_never_continue_a_final_message(tmp_path):
    loop = Loop(tmp_path, _announcing_script("The bug is in a.py. Let me also check the other module…"),
                harness=HarnessOptions(self_review=False, verify_on_stop=False, instruction_reminders=False))
    (loop.workspace / "a.py").write_text("x = 1\n")
    assert loop.run().final_response.endswith("Let me also check the other module…")
    assert len(loop.provider.calls) == 2


def test_headless_turns_announcing_more_work_are_continued_at_most_twice(tmp_path):
    loop = Loop(
        tmp_path,
        _announcing_script("Let me check b.py…", "Next, I'll check c.py.", "Let me check d.py:"),
        harness=HarnessOptions(self_review=False, verify_on_stop=False, instruction_reminders=False),
        autonomous=True,
    )
    (loop.workspace / "a.py").write_text("x = 1\n")

    result = loop.run()

    assert result.final_response == "Let me check d.py:"  # the third announcement ends the turn
    assert len(loop.provider.calls) == 6
    flags = [payload.get("continuation_nudge") for payload in loop.requested_payloads()]
    assert flags == [None, None, True, None, True, None]
    assert "announced more work but called no tool" in loop.request_text(2)


def test_headless_needs_a_tool_run_between_continuations(tmp_path):
    loop = Loop(
        tmp_path,
        [{"tool_calls": [_tc("r", "read_file", path="a.py")]},
         {"final_answer": "Let me also check the other module…"},
         {"final_answer": "Let me look at b.py too."}],  # no tool since the nudge: accepted
        harness=HarnessOptions(self_review=False, verify_on_stop=False, instruction_reminders=False),
        autonomous=True,
    )
    (loop.workspace / "a.py").write_text("x = 1\n")
    assert loop.run().final_response == "Let me look at b.py too."
    assert len(loop.provider.calls) == 3


def test_headless_continues_an_answer_cut_off_by_the_output_limit(tmp_path):
    loop = Loop(
        tmp_path,
        [{"tool_calls": [_tc("r", "read_file", path="a.py")]},
         {"final_answer": "The fix changes x to", "finish_reason": "length"},
         {"final_answer": "The fix changes x to 2."}],
        harness=HarnessOptions(self_review=False, verify_on_stop=False, instruction_reminders=False),
        autonomous=True,
    )
    (loop.workspace / "a.py").write_text("x = 1\n")
    assert loop.run().final_response == "The fix changes x to 2."
    assert "cut off by the output limit" in loop.request_text(2)


def test_polite_closings_and_tool_less_answers_are_not_continued(tmp_path):
    from code4me2_agent.adapters import _announces_more_work

    assert _announces_more_work("Fixed. Let me also run the tests…")
    assert _announces_more_work("Next, I'll update the docs.")
    assert _announces_more_work("Here is what I will check next:")
    assert not _announces_more_work("Fixed the bug. Let me know if you need anything else.")
    assert not _announces_more_work("I changed a.py so that x is 2.")
    # A plain Q&A turn (no tools used) is never continued, even headless.
    loop = Loop(tmp_path, [{"final_answer": "Let me explain: x holds the count."}],
                harness=HarnessOptions(self_review=False, verify_on_stop=False), autonomous=True)
    assert loop.run().final_response == "Let me explain: x holds the count."
    assert len(loop.provider.calls) == 1


def test_no_nudge_when_commands_cannot_run(tmp_path):
    loop = Loop(
        tmp_path,
        _edit_script("Done."),
        harness=HarnessOptions(self_review=False),
        tools=["read_file", "replace_text"],
    )
    (loop.workspace / "a.py").write_text("x = 1\n")
    loop.run()
    assert len(loop.provider.calls) == 3


# ------------------------------------------------------------- summarisation


def test_old_context_is_summarised_into_a_checkpoint_without_using_the_budget(tmp_path):
    loop = Loop(
        tmp_path,
        [{"final_answer": "Goal: refactor. Done so far: many reads."}, {"final_answer": "ok"}],
        max_tokens=9000,
        max_iterations=1,
        harness=HarnessOptions(instruction_reminders=False),
    )
    for index in range(12):
        loop.memory.append({"role": "user", "content": f"question {index} " + "q" * 800})
        loop.memory.append({"role": "assistant", "content": f"answer {index} " + "a" * 800})

    result = loop.run("next question")

    summary_call = loop.provider.calls[0]
    assert summary_call["include_tools"] is False
    assert "Session excerpt to compress" in summary_call["messages"][1]["content"]
    snapshot = loop.memory.snapshot()
    assert snapshot[1]["content"].startswith(CHECKPOINT_PREFIX)
    assert snapshot[1]["code4me_runtime"] == "checkpoint"
    assert result.final_response == "ok"  # max_iterations=1 still allowed the real call
    payloads = loop.requested_payloads()
    assert payloads[0]["call_purpose"] == "summarize" and payloads[0]["compacted_units"] > 0
    assert "Context checkpoint" in loop.request_text(1)


def test_a_long_turn_keeps_a_stable_request_prefix_for_prompt_caching(tmp_path):
    reads = [{"tool_calls": [_tc(f"r{i}", "read_file", path=f"f{i}.txt")]} for i in range(10)]
    loop = Loop(
        tmp_path,
        [*reads, {"final_answer": "done"}],
        max_tokens=6000,
        harness=HarnessOptions(instruction_reminders=False, self_review=False, verify_on_stop=False),
    )
    for i in range(10):
        (loop.workspace / f"f{i}.txt").write_text(f"line {i}\n" * 400)

    loop.run("read everything")

    requests = [call["messages"] for call in loop.provider.calls]
    breaks = sum(
        1 for before, after in zip(requests, requests[1:]) if after[: len(before)] != before
    )
    compactions = [
        e for e in loop.events
        if e["event_type"] == "agent.model.requested" and (e.get("metrics") or {}).get("context_elided_units")
    ]
    assert compactions, "the turn should have outgrown the budget"
    assert all(e["metrics"]["context_tokens_after"] < e["metrics"]["context_tokens_before"] for e in compactions)
    assert all(e["metrics"]["token_scale_milli"] >= 1000 for e in compactions)
    assert "agent.context.compacted" not in {e["event_type"] for e in loop.events}
    # Each compaction rewrites the prefix once; between them every request
    # extends the previous one (a sliding trim would break it on every call).
    assert breaks <= len(compactions)
    assert breaks < len(requests) - 2


def test_summarisation_can_be_disabled(tmp_path):
    loop = Loop(
        tmp_path,
        [{"final_answer": "ok"}],
        max_tokens=3000,
        harness=HarnessOptions(context_summarization=False),
    )
    for index in range(12):
        loop.memory.append({"role": "user", "content": "q" * 800})
        loop.memory.append({"role": "assistant", "content": "a" * 800})
    loop.run("next")
    assert len(loop.provider.calls) == 1


# ------------------------------------------------------------- parallel reads


def test_read_only_batches_run_concurrently_and_keep_order(tmp_path):
    loop = Loop(
        tmp_path,
        [
            {"tool_calls": [_tc(f"r{n}", "read_file", path=f"f{n}.txt") for n in range(4)]},
            {"final_answer": "read them"},
        ],
    )
    for n in range(4):
        (loop.workspace / f"f{n}.txt").write_text(f"file {n}\n")
    original = loop.file_tools.read_file

    def slow_read(*args, **kwargs):
        time.sleep(0.4)
        return original(*args, **kwargs)

    loop.file_tools.read_file = slow_read
    started = time.monotonic()
    loop.run()
    elapsed = time.monotonic() - started

    assert elapsed < 1.2  # four 0.4 s reads in parallel, not 1.6 s in sequence
    contents = [result["content"] for result in loop.tool_results(1)]
    assert contents == [f"1|file {n}" for n in range(4)]
    sequences = [e["sequence"] for e in loop.events if e["run_id"] == "run-1"]
    assert sequences == list(range(1, len(sequences) + 1))


# ------------------------------------------------------------ streamed output


def test_running_command_output_streams_into_its_card(tmp_path):
    code = "import time\nfor i in range(3):\n    print('tick', i, flush=True)\n    time.sleep(0.7)\n"
    loop = Loop(
        tmp_path,
        [{"tool_calls": [_tc("c", "run_command", argv=[PYTHON, "-c", code])]}, {"final_answer": "ran"}],
    )
    loop.run()
    progress = [e for e in loop.sink.tool_events if e.phase == "progress"]
    assert progress and all(e.status == "in_progress" and "tick" in e.content_text for e in progress)


# ------------------------------------------------------------- slash commands


def test_status_and_undo_commands_need_no_model_call(tmp_path):
    loop = Loop(tmp_path, [*_edit_script()], harness=HarnessOptions(self_review=False, verify_on_stop=False))
    (loop.workspace / "a.py").write_text("x = 1\n")
    loop.run("change it")
    calls_after_turn = len(loop.provider.calls)

    status = loop.run("/status", run_id="run-2")
    undo = loop.run("/undo", run_id="run-3")
    nothing = loop.run("/undo", run_id="run-4")

    assert len(loop.provider.calls) == calls_after_turn
    assert "Undo checkpoints: 1" in status.final_response
    assert "Restored: a.py" in undo.final_response
    assert (loop.workspace / "a.py").read_text() == "x = 1\n"
    assert "no file changes" in nothing.final_response
    snapshot = loop.memory.snapshot()
    undo_at = next(i for i, m in enumerate(snapshot) if m == {"role": "user", "content": "/undo"})
    assert snapshot[undo_at + 1]["role"] == "assistant" and "Restored: a.py" in snapshot[undo_at + 1]["content"]


def test_review_command_reviews_the_whole_session(tmp_path):
    loop = Loop(
        tmp_path,
        [*_edit_script(), {"final_answer": "NO_ISSUES"}, {"final_answer": "- a.py: x should stay an int"}],
        harness=HarnessOptions(verify_on_stop=False),
    )
    (loop.workspace / "a.py").write_text("x = 1\n")
    loop.run("change it")

    review = loop.run("/review error handling", run_id="run-2")

    assert "found possible issues" in review.final_response
    assert "Focus: error handling" in loop.provider.calls[-1]["messages"][1]["content"]
    assert loop.provider.calls[-1]["include_tools"] is False


def test_unknown_slash_text_is_an_ordinary_prompt(tmp_path):
    loop = Loop(tmp_path, [{"final_answer": "sure"}])
    result = loop.run("/usr/bin is a path")
    assert result.final_response == "sure"
