"""Study export building blocks (pure)."""

from __future__ import annotations

import csv
import io
import json
import zipfile

from research.analysis.study_export.build import (
    Column,
    ExportWriter,
    csv_cell,
    event_csv_row,
    export_envelope,
)


def test_csv_cells_never_start_a_spreadsheet_formula():
    assert csv_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert csv_cell("+1") == "'+1"
    assert csv_cell("-cmd") == "'-cmd"
    assert csv_cell("@sum") == "'@sum"
    assert csv_cell("plain") == "plain"
    # Numbers are numbers, negative or not.
    assert csv_cell(-32603) == "-32603"
    assert csv_cell(1.5) == "1.5"
    assert csv_cell(None) == ""
    assert csv_cell(True) == "true"
    assert csv_cell({"b": 1, "a": "=x"}) == '{"a": "=x", "b": 1}'


def test_envelopes_lose_account_links_always_and_content_unless_asked():
    envelope = {
        "event_type": "agent.message.started",
        "payload": {"message_kind": "user", "prompt": [{"type": "text", "text": "secret plan"}], "account_id": "a", "email": "x@y.z"},
        "provenance": {"source": "acp", "session_token": "t"},
    }
    without = export_envelope(envelope, include_content=False)
    assert without["payload"]["prompt"] == "[REDACTED]"
    assert without["payload"]["message_kind"] == "user"
    assert "account_id" not in without["payload"] and "email" not in without["payload"]
    assert "session_token" not in without["provenance"]
    with_content = export_envelope(envelope, include_content=True)
    assert with_content["payload"]["prompt"] == [{"type": "text", "text": "secret plan"}]
    assert "email" not in with_content["payload"]
    # The stored envelope itself is untouched.
    assert envelope["payload"]["email"] == "x@y.z"


def test_event_csv_rows_are_metadata_only_and_read_revise():
    row = event_csv_row(
        {
            "event_type": "permission.decided",
            "source": "acp",
            "lifecycle_state": None,
            "payload": {"session_id": "chat-1", "decision": "reject", "selected_option_id": "revise", "content": "x"},
            "correlations": {"permission_id": "7"},
            "metrics": {"latency_ms": 12},
        }
    )
    assert row["decision"] == "revise"
    assert row["chat_id"] == "chat-1"
    assert row["permission_id"] == "7"
    assert "content" not in row


def test_writer_streams_csv_and_jsonl_into_one_zip():
    columns = (Column("a", "first"), Column("b", "second"))
    for export_format in ("csv", "jsonl"):
        writer = ExportWriter(export_format)
        assert writer.write_rows("data", columns, iter([{"a": "=1", "b": 2}, {"a": None, "b": "x"}])) == 2
        writer.write_json("manifest.json", {"files": writer.counts})
        archive = zipfile.ZipFile(writer.finish())
        assert set(archive.namelist()) == {f"data.{export_format}", "manifest.json"}
        body = archive.read(f"data.{export_format}").decode("utf-8")
        if export_format == "csv":
            assert list(csv.reader(io.StringIO(body))) == [["a", "b"], ["'=1", "2"], ["", "x"]]
        else:
            assert [json.loads(line) for line in body.splitlines()] == [{"a": "=1", "b": 2}, {"a": None, "b": "x"}]
        assert json.loads(archive.read("manifest.json")) == {"files": {f"data.{export_format}": 2}}
