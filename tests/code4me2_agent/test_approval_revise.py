"""The built-in agent's "Revise…" approval option (ACP elicitation, run 2026-10-03 C1)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from acp.agent.connection import AgentSideConnection
from acp.schema import (
    ClientCapabilities,
    ElicitationCapabilities,
    ElicitationFormCapabilities,
    TextContentBlock,
)

from code4me2_agent.acp_runtime import (
    AcpSessionEventSink,
    _supports_form_elicitation,
    create_acp_agent,
)
from code4me2_agent.acp_updates import REVISE_OPTION_ID, AcpUpdateBuilder
from code4me2_agent.adapters import (
    ToolCall,
    ToolRegistry,
    ToolRegistryError,
    ToolRevisionRequested,
)
from code4me2_agent.async_bridge import EventLoopAsyncRunner
from code4me2_agent.config import (
    AdapterConfig,
    AgentConfig,
    FakeProviderConfig,
    HarnessOptions,
    ServerAgentConfig,
    parse_harness_overrides,
)
from code4me2_agent.events import ApprovalDecision
from code4me2_agent.file_tools import WorkspaceFileTools
from code4me2_agent.hunks import RevisionOffer, apply_hunks, diff_hunks
from code4me2_agent.runtime_auth import AcpRuntimeScope
from code4me2_agent.telemetry import AgentTelemetryRecorder

OLD = "".join(f"line {number}\n" for number in range(1, 41))
# Three hunks: a replacement, an insertion and a deletion.
NEW = (
    OLD.replace("line 3\n", "LINE three\n")
    .replace("line 20\n", "line 20\nadded a\nadded b\n")
    .replace("line 35\n", "")
)
EDITS = [
    {"old_text": "line 3\n", "new_text": "LINE three\n"},
    {"old_text": "line 20\n", "new_text": "line 20\nadded a\nadded b\n"},
    {"old_text": "line 35\n", "new_text": ""},
]
# The options every per-step approval has offered so far, as sent on the wire.
TODAYS_OPTIONS = [
    {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
    {"optionId": "allow_session", "name": "Allow edits for session", "kind": "allow_always"},
    {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
]
REVISE_OPTION = {"optionId": "revise", "name": "Revise…", "kind": "reject_once"}


def _wire(model) -> object:
    return json.loads(json.dumps(model.model_dump(mode="json", by_alias=True, exclude_none=True)))


class _Runner:
    def run(self, awaitable, **_kwargs):
        return asyncio.run(awaitable)


class _Connection:
    """An ACP client double: answers the permission request, then the form."""

    def __init__(self, option_id: str = REVISE_OPTION_ID, form: object = None, form_error=None) -> None:
        self.option_id = option_id
        self.form = form if form is not None else {"action": "decline"}
        self.form_error = form_error
        self.permissions: list[dict] = []
        self.forms: list[dict] = []

    async def request_permission(self, **kwargs):
        self.permissions.append(kwargs)
        return {"outcome": {"outcome": "selected", "optionId": self.option_id}}

    async def create_elicitation(self, **kwargs):
        self.forms.append(kwargs)
        if self.form_error is not None:
            raise self.form_error
        return self.form


def _sink(connection, *, elicitation_form: bool = True, runner=None, cancel_event=None) -> AcpSessionEventSink:
    return AcpSessionEventSink(
        conn=connection,
        session_id="session-1",
        updates=AcpUpdateBuilder(),
        telemetry=object(),
        async_runner=runner or _Runner(),
        cancel_event=cancel_event,
        elicitation_form=elicitation_form,
    )


def _ask(sink, *, revise=None, name="write_file", call_id="tool-1"):
    return sink.request_approval(
        SimpleNamespace(name=name, tool_call_id=call_id),
        {"path": "a.txt", "content": NEW},
        revise=revise,
    )


# ------------------------------------------------------------ options


def test_revise_is_appended_last_and_only_when_asked_for():
    builder = AcpUpdateBuilder()
    request = dict(
        session_id="session-1",
        tool_call_id="tool-1",
        title="Write file: a.txt",
        kind="edit",
        summary="Write file: a.txt",
        raw_input={"path": "a.txt"},
        session_option_name="Allow edits for session",
    )

    assert _wire(builder.permission_request(**request))["options"] == TODAYS_OPTIONS
    assert _wire(builder.permission_request(**request, revise=True))["options"] == [
        *TODAYS_OPTIONS,
        REVISE_OPTION,
    ]
    assert REVISE_OPTION_ID == "revise"


@pytest.mark.parametrize(
    ("elicitation_form", "offer"),
    [(False, RevisionOffer("a.txt", OLD, NEW)), (True, None)],
    ids=["client-without-forms", "switched-off"],
)
def test_without_capability_or_offer_the_request_is_todays(elicitation_form, offer):
    connection = _Connection(option_id="reject_once")
    sink = _sink(connection, elicitation_form=elicitation_form)

    decision = _ask(sink, revise=offer)

    baseline = _Connection(option_id="reject_once")
    _sink(baseline, elicitation_form=False).request_approval(
        SimpleNamespace(name="write_file", tool_call_id="tool-1"), {"path": "a.txt", "content": NEW}
    )
    assert decision == ApprovalDecision("rejected")
    assert [_wire(option) for option in connection.permissions[0]["options"]] == TODAYS_OPTIONS
    assert _wire(connection.permissions[0]["tool_call"]) == _wire(baseline.permissions[0]["tool_call"])
    assert connection.forms == []


@pytest.mark.parametrize(
    ("caps", "expected"),
    [
        ({"elicitation": {"form": {}}}, True),
        ({"elicitation": {"form": {}, "url": {}}}, True),
        ({"elicitation": {"url": {}}}, False),
        ({"elicitation": {}}, False),
        ({"fs": {"readTextFile": True}}, False),
        (None, False),
        (ClientCapabilities(elicitation=ElicitationCapabilities(form=ElicitationFormCapabilities())), True),
        (ClientCapabilities(), False),
    ],
)
def test_form_capability_is_read_from_initialize(caps, expected):
    assert _supports_form_elicitation(caps) is expected


# ---------------------------------------------------------------- form


def test_accepted_form_is_a_revised_decision_with_kept_hunks_and_instructions():
    connection = _Connection(
        form={
            "action": "accept",
            "content": {"keep": ["2", "0", "9", "x", "0"], "instructions": "  Use snake_case.  "},
        }
    )
    offer = RevisionOffer("a.txt", OLD, NEW)

    decision = _ask(_sink(connection), revise=offer)

    assert decision == ApprovalDecision(
        "revised", elicitation_action="accept", kept_hunks=(0, 2), instructions="Use snake_case."
    )
    assert not decision.accepted
    (form,) = connection.forms
    assert form["message"] == "Which parts should be kept, and what should change?"
    mode = form["mode"]
    assert (mode.session_id, mode.tool_call_id) == ("session-1", "tool-1")
    schema = mode.requested_schema
    assert schema.required == ["instructions"]
    keep = schema.properties["keep"]
    assert [(item.const, item.title) for item in keep.items.any_of] == [
        (str(hunk.index), hunk.label) for hunk in diff_hunks(OLD, NEW)
    ]
    assert keep.default == []
    instructions = schema.properties["instructions"]
    assert (instructions.type, instructions.min_length, instructions.max_length) == ("string", 1, 4000)


def test_a_change_that_cannot_be_split_asks_for_instructions_only():
    many_old = "".join(f"line {number}\n" for number in range(1, 300))
    many_new = many_old
    for position in range(21):  # one hunk more than the form lists
        many_new = many_new.replace(f"line {position * 10 + 5}\n", f"changed {position}\n", 1)
    for offer, name in (
        (RevisionOffer(), "run_command"),  # not a file change
        (RevisionOffer("a.txt", many_old, many_new), "write_file"),  # 21 hunks
        (RevisionOffer("a.txt", OLD, OLD.replace("line 1\n", "one\n")), "write_file"),  # one hunk
    ):
        connection = _Connection(form={"action": "accept", "content": {"keep": ["0"], "instructions": "x"}})

        decision = _ask(_sink(connection), revise=offer, name=name)

        (form,) = connection.forms
        assert form["message"] == "What should change?"
        assert list(form["mode"].requested_schema.properties) == ["instructions"]
        assert decision.decision == "revised" and decision.kept_hunks == ()


@pytest.mark.parametrize(
    ("form", "form_error", "expected"),
    [
        ({"action": "decline"}, None, ApprovalDecision("rejected", elicitation_action="decline")),
        ({"action": "cancel"}, None, ApprovalDecision("rejected", elicitation_action="cancel")),
        ({"action": "_vendor"}, None, ApprovalDecision("rejected", elicitation_action="error")),
        (None, RuntimeError("form not supported"), ApprovalDecision("rejected", elicitation_action="error")),
    ],
    ids=["decline", "cancel", "unknown-action", "exception"],
)
def test_a_form_that_is_not_accepted_is_a_rejection(form, form_error, expected):
    connection = _Connection(form=form, form_error=form_error)

    assert _ask(_sink(connection), revise=RevisionOffer("a.txt", OLD, NEW)) == expected


def test_a_cancelled_turn_while_the_form_is_open_is_cancelled():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:

        class Connection(_Connection):
            async def create_elicitation(self, **kwargs):
                self.forms.append(kwargs)
                await asyncio.Event().wait()

        connection = Connection()
        cancel = Event()
        sink = _sink(connection, runner=EventLoopAsyncRunner(loop), cancel_event=cancel)
        threading.Timer(0.3, cancel.set).start()
        started = time.monotonic()

        decision = _ask(sink, revise=RevisionOffer("a.txt", OLD, NEW))

        assert decision == ApprovalDecision("cancelled")
        assert len(connection.forms) == 1
        assert time.monotonic() - started < 3.0
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_a_revision_is_never_remembered_for_the_session():
    connection = _Connection(form={"action": "accept", "content": {"instructions": "x"}})
    sink = _sink(connection)

    _ask(sink, revise=RevisionOffer("a.txt", OLD, NEW), call_id="tool-1")
    _ask(sink, revise=RevisionOffer("a.txt", OLD, NEW), call_id="tool-2")

    assert len(connection.permissions) == 2 and len(connection.forms) == 2


def test_wire_messages_are_camel_case_acp():
    sent: list[tuple[str, dict]] = []

    class Transport:
        async def send_request(self, method, params):
            sent.append((method, params))
            if method == "session/request_permission":
                return {"outcome": {"outcome": "selected", "optionId": "revise"}}
            return {"action": "accept", "content": {"keep": ["1"], "instructions": "Keep it short."}}

    connection = object.__new__(AgentSideConnection)  # the real SDK client surface
    connection._conn = Transport()

    decision = _ask(_sink(connection), revise=RevisionOffer("a.txt", OLD, NEW))

    assert decision.kept_hunks == (1,) and decision.instructions == "Keep it short."
    (permission_method, permission), (form_method, form) = sent
    assert permission_method == "session/request_permission"
    assert permission["options"] == [*TODAYS_OPTIONS, REVISE_OPTION]
    assert form_method == "elicitation/create"
    assert form["sessionId"] == "session-1"
    assert form["toolCallId"] == "tool-1"
    assert form["mode"] == "form"
    schema = form["requestedSchema"]
    assert schema["required"] == ["instructions"]
    assert schema["properties"]["keep"]["type"] == "array"
    assert schema["properties"]["keep"]["items"]["anyOf"][0] == {
        "const": "0",
        "title": diff_hunks(OLD, NEW)[0].label,
    }
    assert schema["properties"]["instructions"] == {
        "type": "string",
        "title": "What should change?",
        "minLength": 1,
        "maxLength": 4000,
    }


# ------------------------------------------------------------ registry


class _DiskAcpBackend:
    """The IDE's fs/read_text_file and fs/write_text_file, backed by the disk."""

    read_text_file_enabled = True
    write_text_file_enabled = True
    session_id = "acp-session"

    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []

    def read_text_file(self, absolute_path: str) -> str:
        return Path(absolute_path).read_bytes().decode("utf-8")

    def write_text_file(self, absolute_path: str, content: str) -> None:
        self.writes.append((absolute_path, content))
        Path(absolute_path).write_bytes(content.encode("utf-8"))


class _RevisingSink:
    def __init__(self, decision: ApprovalDecision, *, while_asking=None) -> None:
        self.decision = decision
        self.while_asking = while_asking
        self.offers: list[RevisionOffer | None] = []
        self.events = []

    def tool_call(self, event):
        self.events.append(event)

    def request_approval(self, tool_call, arguments, *, revise=None):
        self.offers.append(revise)
        if self.while_asking is not None:
            self.while_asking()
        return self.decision


class _Workspace:
    def __init__(self, tmp_path: Path, sink, *, store_agent_content=True, harness=None, policy="per_step"):
        self.root = (tmp_path / "ws").resolve()
        self.root.mkdir(exist_ok=True)
        self.file = self.root / "a.txt"
        self.file.write_bytes(OLD.encode("utf-8"))
        config = AgentConfig(
            workspace_root=self.root,
            trace_path=tmp_path / "trace.jsonl",
            session_id="session-1",
            approval_policy=policy,
            store_agent_content=store_agent_content,
            harness=harness or HarnessOptions(),
        )
        self.events: list[dict] = []

        class Capture:
            def append(inner, event):
                self.events.append(event)

        telemetry = AgentTelemetryRecorder(config, sinks=[Capture()])
        self.backend = _DiskAcpBackend()
        self.registry = ToolRegistry(
            WorkspaceFileTools(config, acp_backend=self.backend, telemetry=telemetry),
            MagicMock(),
            event_sink=sink,
            approval_policy=policy,
            telemetry=telemetry,
            workspace_root=self.root,
            harness=config.harness,
        )

    def edit(self, call_id: str = "edit-1"):
        return self.registry.execute(
            ToolCall(call_id, "edit_file", {"path": "a.txt", "edits": EDITS}),
            run_id="run-1",
            request_id="request-1",
        )

    def decided(self) -> dict:
        (event,) = [e for e in self.events if e["event_type"] == "agent.permission.decided"]
        return event["payload"]


def _revised(*kept: int, instructions: str = "Name it added_a.") -> ApprovalDecision:
    return ApprovalDecision("revised", elicitation_action="accept", kept_hunks=kept, instructions=instructions)


def test_kept_hunks_are_written_once_through_the_acp_write_path(tmp_path):
    sink = _RevisingSink(_revised(1))
    workspace = _Workspace(tmp_path, sink)

    with pytest.raises(ToolRevisionRequested) as raised:
        workspace.edit()

    merged = apply_hunks(OLD, NEW, [1])
    assert [(Path(path), content) for path, content in workspace.backend.writes] == [(workspace.file, merged)]
    assert workspace.file.read_bytes().decode("utf-8") == merged
    offer = sink.offers[0]
    assert (offer.path, offer.old_text, offer.new_text) == ("a.txt", OLD, NEW)
    labels = [hunk.label for hunk in diff_hunks(OLD, NEW)]
    result = raised.value.result
    assert raised.value.failure_reason == "approval_revised"
    assert {key: result[key] for key in ("status", "kept_hunks", "total_hunks", "applied_path")} == {
        "status": "revise",
        "kept_hunks": [labels[1]],
        "total_hunks": 3,
        "applied_path": "a.txt",
    }
    assert result["user_instructions"] == "Name it added_a."
    assert "already written to a.txt" in result["message"] and "approval again" in result["message"]
    # The card closes with the diff that was actually applied.
    assert [(event.phase, event.status) for event in sink.events] == [
        ("started", "pending"),
        ("completed", "completed"),
    ]
    assert (sink.events[-1].diff_old_text, sink.events[-1].diff_new_text) == (OLD, merged)
    assert workspace.decided() == {
        "tool_name": "edit_file",
        "tool_call_id": "edit-1",
        "kind": "edit",
        "decision": "revised",
        "decision_scope": "none",
        "elicitation_action": "accept",
        "hunk_count": 3,
        "kept_hunk_count": 1,
        "revise_status": "applied",
        "text": "Name it added_a.",
    }
    # The write is reported under the call that proposed it.
    (write,) = [e for e in workspace.events if e["event_type"] == "agent.tool.completed"]
    assert (write["payload"]["tool_name"], write["payload"]["tool_call_id"]) == ("edit_file", "edit-1")


def test_nothing_is_written_when_the_file_changed_since_the_preview(tmp_path):
    def user_edits_the_file():
        workspace.file.write_bytes(OLD.replace("line 1\n", "edited by the user\n").encode("utf-8"))

    sink = _RevisingSink(_revised(0, 2), while_asking=user_edits_the_file)
    workspace = _Workspace(tmp_path, sink)

    with pytest.raises(ToolRevisionRequested) as raised:
        workspace.edit()

    assert workspace.backend.writes == []
    assert workspace.file.read_bytes().decode("utf-8").startswith("edited by the user\n")
    result = raised.value.result
    assert result["applied_path"] is None and len(result["kept_hunks"]) == 2
    assert "changed after the change was proposed, so nothing was written" in result["message"]
    assert workspace.decided()["revise_status"] == "file_changed"
    assert workspace.decided()["kept_hunk_count"] == 2
    assert sink.events[-1].status == "failed"
    assert "the file changed" in sink.events[-1].content_text


def test_keeping_nothing_only_passes_the_instructions_on(tmp_path):
    sink = _RevisingSink(_revised())
    workspace = _Workspace(tmp_path, sink)

    with pytest.raises(ToolRevisionRequested) as raised:
        workspace.edit()

    assert workspace.backend.writes == [] and workspace.file.read_bytes().decode("utf-8") == OLD
    assert raised.value.result["kept_hunks"] == [] and raised.value.result["total_hunks"] == 3
    assert "nothing was applied" in raised.value.result["message"]
    assert workspace.decided()["revise_status"] == "instructions_only"
    assert sink.events[-1].content_text == "Not run: revision requested."


def test_instructions_are_dropped_when_content_is_not_stored(tmp_path):
    workspace = _Workspace(tmp_path, _RevisingSink(_revised(0)), store_agent_content=False)

    with pytest.raises(ToolRevisionRequested):
        workspace.edit()

    decided = workspace.decided()
    assert "text" not in decided
    assert (decided["decision"], decided["revise_status"], decided["kept_hunk_count"]) == ("revised", "applied", 1)


def test_a_declined_form_is_a_plain_rejection_with_the_form_outcome(tmp_path):
    sink = _RevisingSink(ApprovalDecision("rejected", elicitation_action="decline"))
    workspace = _Workspace(tmp_path, sink)

    with pytest.raises(ToolRegistryError) as raised:
        workspace.edit()

    assert not isinstance(raised.value, ToolRevisionRequested)
    assert raised.value.failure_reason == "approval_rejected"
    assert workspace.backend.writes == []
    decided = workspace.decided()
    assert (decided["decision"], decided["elicitation_action"], decided["hunk_count"]) == ("rejected", "decline", 3)
    assert "revise_status" not in decided and "kept_hunk_count" not in decided


def test_switched_off_revise_calls_the_sink_as_before(tmp_path):
    sink = _RevisingSink(ApprovalDecision("accepted", "once"))
    workspace = _Workspace(tmp_path, sink, harness=HarnessOptions(approval_revise=False))

    workspace.edit()

    assert sink.offers == [None]
    assert workspace.file.read_bytes().decode("utf-8") == NEW
    assert set(workspace.decided()) == {"tool_name", "tool_call_id", "kind", "decision", "decision_scope"}


def test_two_argument_approval_doubles_keep_working(tmp_path):
    class TwoArgumentSink:
        def __init__(self):
            self.events = []

        def tool_call(self, event):
            self.events.append(event)

        def request_approval(self, tool_call, arguments):
            return ApprovalDecision("accepted", "once")

    workspace = _Workspace(tmp_path, TwoArgumentSink())

    workspace.edit()

    assert workspace.file.read_bytes().decode("utf-8") == NEW


def test_revise_is_never_offered_without_per_step_approval(tmp_path):
    sink = _RevisingSink(_revised(0))
    workspace = _Workspace(tmp_path, sink, policy="auto")

    workspace.edit()

    assert sink.offers == []
    assert workspace.file.read_bytes().decode("utf-8") == NEW


def test_only_single_file_text_changes_offer_hunks(tmp_path):
    sink = _RevisingSink(ApprovalDecision("rejected"))
    workspace = _Workspace(tmp_path, sink)
    (workspace.root / "b.txt").write_bytes(b"b\n")
    update = "*** Begin Patch\n*** Update File: a.txt\n@@\n-line 3\n+LINE three\n*** End Patch"
    calls = [
        ("write_file", {"path": "a.txt", "content": NEW}, "a.txt"),
        ("replace_text", {"path": "a.txt", "old_text": "line 3\n", "new_text": "x\n"}, "a.txt"),
        ("create_file", {"path": "new.txt", "content": "x\n"}, "new.txt"),
        ("apply_patch", {"patch": update}, "a.txt"),
        ("apply_patch", {"patch": update.replace("*** End Patch", "*** Delete File: b.txt\n*** End Patch")}, None),
        ("apply_patch", {"patch": update.replace("@@", "*** Move to: c.txt\n@@")}, None),
        ("delete_file", {"path": "b.txt"}, None),
        ("move_file", {"source_path": "b.txt", "destination_path": "d.txt"}, None),
    ]
    for index, (name, arguments, path) in enumerate(calls):
        with pytest.raises(ToolRegistryError, match="approval rejected"):
            workspace.registry.execute(ToolCall(f"call-{index}", name, arguments), run_id="r", request_id="q")
        assert len(sink.offers) == index + 1, name  # the call reached the approval
        assert sink.offers[-1].path == path, name
    assert sink.offers[2].old_text is None and len(sink.offers[2].hunks) == 1


# ------------------------------------------------------------ harness switch


def test_harness_switch_defaults_on_and_parses_strictly(tmp_path):
    assert HarnessOptions().approval_revise is True
    assert parse_harness_overrides({"approval_revise": False}, strict=True) == {"approval_revise": False}
    assert parse_harness_overrides({"approval_revise": "off"}, strict=False) == {}
    with pytest.raises(ValueError, match="approval_revise"):
        parse_harness_overrides({"approval_revise": "off"}, strict=True)
    policy = ServerAgentConfig.from_managed_payload(
        {
            "version": "1",
            "transport": "managed_backend",
            "agent_profile": "arm-a",
            "framework_version": "code4me2-agent",
            "model": "m",
            "tools": ["edit_file"],
            "commands_allowlist": [],
            "max_iterations": 8,
            "max_context_tokens": 32000,
            "approval_policy": "per_step",
            "temperature": None,
            "store_agent_content": False,
            "harness_options": {"approval_revise": False},
        }
    )
    base = AgentConfig(workspace_root=tmp_path, trace_path=tmp_path / "t.jsonl", session_id="s")
    assert base.with_server_overrides(policy).harness.approval_revise is False


# ------------------------------------------------------------ over ACP


class _Authorization:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.is_authenticated = True
        self.server_agent_config = None
        self.backend_url = None

    def authenticate(self) -> AcpRuntimeScope:
        return AcpRuntimeScope(project_id="project-1", workspace=str(self.workspace))

    def validate(self) -> AcpRuntimeScope:
        return self.authenticate()

    def telemetry_headers(self) -> dict[str, str]:
        return {}

    def authorized_json_request(self, method, path, payload):
        return {"messages": []} if method == "GET" else {}


class _Ide:
    """An IDE that owns the files, answers approvals from a script and fills in forms."""

    def __init__(self, answers: list[str], form: dict) -> None:
        self.answers = list(answers)
        self.form = form
        self.updates: list[object] = []
        self.permissions: list[list[str]] = []
        self.forms: list[tuple[str, object]] = []
        self.writes: list[tuple[str, str]] = []

    async def session_update(self, *, session_id, update, **kwargs):
        self.updates.append(update)

    async def request_permission(self, *, session_id, tool_call, options, **kwargs):
        self.permissions.append([option.option_id for option in options])
        return {"outcome": {"outcome": "selected", "optionId": self.answers.pop(0)}}

    async def create_elicitation(self, *, message, mode, **kwargs):
        self.forms.append((message, mode))
        return self.form

    async def read_text_file(self, *, path, session_id, **kwargs):
        return {"content": Path(path).read_bytes().decode("utf-8")}

    async def write_text_file(self, *, content, path, session_id, **kwargs):
        self.writes.append((path, content))
        Path(path).write_bytes(content.encode("utf-8"))


def _tc(call_id: str, name: str, **arguments) -> dict:
    return {"id": call_id, "name": name, "arguments": arguments}


def _run_over_acp(tmp_path: Path, ide: _Ide, script: list[dict], capabilities: dict):
    workspace = (tmp_path / "ws").resolve()
    workspace.mkdir()
    (workspace / "a.py").write_bytes(OLD.encode("utf-8"))
    agent = create_acp_agent(
        AgentConfig(
            workspace_root=workspace,
            trace_path=tmp_path / "trace.jsonl",
            session_id="bootstrap",
            tools=["read_file", "write_file", "replace_text"],
            approval_policy="per_step",
            harness=HarnessOptions(self_review=False, verify_on_stop=False),
            adapter=AdapterConfig(
                name="openai_compatible_react",
                fake_provider=FakeProviderConfig(enabled=True, script=script),
            ),
        ),
        authorization=_Authorization(workspace),
    )
    agent.on_connect(ide)

    async def scenario():
        await agent.initialize(protocol_version=1, client_capabilities=capabilities)
        session = await agent.new_session(cwd=str(workspace))
        response = await agent.prompt(
            prompt=[TextContentBlock(type="text", text="tidy a.py")],
            session_id=session.session_id,
        )
        return session.session_id, response

    session_id, response = asyncio.run(scenario())
    provider = agent._sessions[session_id].core._adapter._provider_instance
    return workspace / "a.py", session_id, response, provider


FS = {"fs": {"readTextFile": True, "writeTextFile": True}}


def test_revise_then_a_second_edit_in_the_same_turn_over_acp(tmp_path):
    ide = _Ide(
        answers=["revise", "allow_once"],
        form={"action": "accept", "content": {"keep": ["0"], "instructions": "Call the new lines added_a."}},
    )
    script = [
        {"tool_calls": [_tc("read-1", "read_file", path="a.py")]},
        {"tool_calls": [_tc("edit-1", "write_file", path="a.py", content=NEW)]},
        {
            "tool_calls": [
                _tc("edit-2", "replace_text", path="a.py", old_text="line 20\n", new_text="line 20\nadded_a\n")
            ]
        },
        {"final_answer": "Kept your part and renamed the rest."},
    ]

    target, session_id, response, provider = _run_over_acp(
        tmp_path, ide, script, {**FS, "elicitation": {"form": {}}}
    )

    assert response.stop_reason == "end_turn"
    assert ide.permissions == [["allow_once", "allow_session", "reject_once", "revise"]] * 2
    ((message, mode),) = ide.forms
    assert message == "Which parts should be kept, and what should change?"
    assert (mode.session_id, mode.tool_call_id) == (session_id, "edit-1")
    labels = [hunk.label for hunk in diff_hunks(OLD, NEW)]
    assert [item.title for item in mode.requested_schema.properties["keep"].items.any_of] == labels
    merged = apply_hunks(OLD, NEW, [0])
    final = merged.replace("line 20\n", "line 20\nadded_a\n")
    assert [(Path(path), content) for path, content in ide.writes] == [(target, merged), (target, final)]
    assert target.read_bytes().decode("utf-8") == final
    # The model saw the revision as the tool result and continued the turn.
    revise_result = json.loads(provider.calls[2]["messages"][-1]["content"])
    assert revise_result["status"] == "revise"
    assert revise_result["kept_hunks"] == [labels[0]]
    assert revise_result["applied_path"] == "a.py"
    assert revise_result["user_instructions"] == "Call the new lines added_a."
    second_result = json.loads(provider.calls[3]["messages"][-1]["content"])
    assert second_result["status"] == "ok"
    # The revised card closed as completed with the diff that was applied.
    closed = [
        update
        for update in ide.updates
        if getattr(update, "tool_call_id", None) == "edit-1" and getattr(update, "status", None) == "completed"
    ]
    assert len(closed) == 1 and closed[0].content[0].new_text == merged


def test_without_form_support_the_ide_sees_todays_three_options(tmp_path):
    ide = _Ide(answers=["reject_once"], form={"action": "accept"})
    script = [
        {"tool_calls": [_tc("read-1", "read_file", path="a.py")]},
        {"tool_calls": [_tc("edit-1", "write_file", path="a.py", content=NEW)]},
        {"final_answer": "No change was made."},
    ]

    target, _session_id, response, provider = _run_over_acp(tmp_path, ide, script, FS)

    assert response.stop_reason == "end_turn"
    assert ide.permissions == [["allow_once", "allow_session", "reject_once"]]
    assert ide.forms == [] and ide.writes == []
    assert target.read_bytes().decode("utf-8") == OLD
    rejected = json.loads(provider.calls[2]["messages"][-1]["content"])
    assert (rejected["status"], rejected["reason"]) == ("rejected", "user_rejected")
