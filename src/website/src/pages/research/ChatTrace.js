import React, { useCallback, useEffect, useState } from "react";
import Icon from "../../components/common/Icon";
import { Alert, Badge, Card, Loading } from "../../components/common/ui";
import { getStudyChatTrace } from "../../utils/api";
import { formatDateTime, formatDuration, formatNumber, humanize } from "../../utils/format";
import { CHAT_END_LABELS, DECISION_LABELS, STOP_REASON_LABELS, labelFor } from "./studyMetrics";

const PAGE_TURNS = 20;

const DECISION_TONES = { allow: "success", reject: "danger", revise: "warning", cancelled: "neutral" };

const CHAT_START_LABELS = {
  "session/new": "Chat opened",
  "session/fork": "Chat forked",
  "session/load": "Chat reopened",
  "session/resume": "Chat resumed",
  "session/close": "Chat closed",
  "session/delete": "Chat deleted",
};

const ATTACHMENT_KINDS = { resource_link: "link", resource: "contents" };

/** A file the IDE sent with a prompt (the open file, an attached file or image), by name. */
const attachmentLabel = (attachment) =>
  ATTACHMENT_KINDS[attachment.type] ? `${attachment.name} (${ATTACHMENT_KINDS[attachment.type]})` : attachment.name;

const offset = (ms) => (ms === null || ms === undefined ? "" : `+${(ms / 1000).toFixed(1)}s`);

/** Stored text, or why there is none. */
const TraceText = ({ field, emptyLabel = "No text." }) => {
  if (!field) return <p className="ui-hint">{emptyLabel}</p>;
  if (field.redacted && !field.text) {
    return (
      <Badge tone="neutral" title="This study's telemetry policy did not keep this text">
        Not captured
      </Badge>
    );
  }
  if (!field.text) return <p className="ui-hint">{emptyLabel}</p>;
  return (
    <>
      <div className="trace-text">{field.text}</div>
      {field.truncated ? <p className="ui-hint">Shortened for display.</p> : null}
    </>
  );
};

const Permission = ({ permission }) => {
  if (!permission) return null;
  const decision = permission.decision;
  return (
    <span className="ui-row">
      <Badge tone={DECISION_TONES[decision] || "neutral"}>
        {decision ? labelFor(DECISION_LABELS, decision) : "Awaiting approval"}
      </Badge>
      {permission.wait_ms !== null && permission.wait_ms !== undefined ? (
        <span className="ui-subtle">after {formatDuration(permission.wait_ms / 1000)}</span>
      ) : null}
    </span>
  );
};

const REVISION_FORM_LABELS = {
  accept: "Form sent",
  decline: "Form declined (counted as a rejection)",
  cancel: "Form dismissed (counted as a rejection)",
  error: "Form could not be shown (counted as a rejection)",
};

const REVISION_STATUS_LABELS = {
  applied: "kept parts written",
  instructions_only: "nothing kept, instructions only",
  file_changed: "file changed meanwhile, kept parts not written",
  write_failed: "writing the kept parts failed",
};

/** What the participant answered in the built-in agent's "Revise…" form. */
const RevisionDetails = ({ revision }) => {
  if (!revision) return null;
  const parts = [labelFor(REVISION_FORM_LABELS, revision.form)];
  if (revision.status) parts.push(labelFor(REVISION_STATUS_LABELS, revision.status));
  // Kept parts only mean something for a form that was sent.
  if (revision.form === "accept" && revision.hunks) {
    parts.push(`kept ${formatNumber(revision.kept_hunks ?? 0)} of ${formatNumber(revision.hunks)} change${revision.hunks === 1 ? "" : "s"}`);
  }
  return (
    <div className="trace-revision">
      <p className="ui-hint">Revise… · {parts.join(" · ")}</p>
      {revision.instructions ? (
        <details className="research-advanced" open>
          <summary>Participant's instructions</summary>
          <TraceText field={revision.instructions} />
        </details>
      ) : null}
    </div>
  );
};

const toolStatus = (block) =>
  // A tool answered with a sent "Revise…" form never ran as proposed: the
  // agent reports it as failed, but the participant revised it.
  block.status === "failed" && block.permission?.revision?.form === "accept" ? "revised" : block.status;

const ToolBlock = ({ block }) => {
  const status = toolStatus(block);
  return (
  <div className="trace-block trace-tool">
    <div className="trace-block-head">
      <Icon name="wrench" size={14} />
      <strong>{block.title || "Tool call"}</strong>
      {block.tool_kind ? <Badge tone="info">{humanize(block.tool_kind)}</Badge> : null}
      {status ? <Badge tone={status === "failed" ? "danger" : status === "revised" ? "warning" : "neutral"}>{humanize(status)}</Badge> : null}
      {block.duration_ms !== null && block.duration_ms !== undefined ? (
        <span className="ui-subtle">{formatDuration(block.duration_ms / 1000)}</span>
      ) : null}
      <Permission permission={block.permission} />
      <span className="ui-subtle trace-offset">{offset(block.offset_ms)}</span>
    </div>
    <RevisionDetails revision={block.permission?.revision} />
    {block.arguments ? (
      <details className="research-advanced">
        <summary>Arguments</summary>
        <TraceText field={block.arguments} />
      </details>
    ) : null}
    {block.result && (block.result.text || block.result.redacted) ? (
      <details className="research-advanced">
        <summary>Result</summary>
        <TraceText field={block.result} />
      </details>
    ) : null}
    {(block.diffs || []).map((diff, index) => (
      <details key={`${diff.path}-${index}`} className="research-advanced">
        <summary>Diff · {diff.path || "file"}</summary>
        {diff.redacted ? <Badge tone="neutral">Not captured</Badge> : <div className="trace-text trace-diff">{diff.diff}</div>}
      </details>
    ))}
  </div>
  );
};

const Block = ({ block }) => {
  switch (block.type) {
    case "prompt":
      return (
        <div className="trace-block trace-prompt">
          <div className="trace-block-head">
            <Icon name="user" size={14} />
            <strong>Prompt</strong>
            <span className="ui-subtle trace-offset">{offset(block.offset_ms)}</span>
          </div>
          <TraceText
            field={block}
            emptyLabel={block.captured ? "Empty prompt." : "Prompt text was not recorded by this plugin version."}
          />
          {block.attachments?.length ? (
            <p className="ui-hint">Sent along by the IDE: {block.attachments.map(attachmentLabel).join(", ")}</p>
          ) : null}
        </div>
      );
    case "thought":
      return (
        <details className="research-advanced trace-block trace-thought" open={Boolean(block.text)}>
          <summary>
            Thinking <span className="ui-subtle">{offset(block.offset_ms)}</span>
          </summary>
          <TraceText field={block} />
        </details>
      );
    case "message":
      return (
        <div className="trace-block trace-message">
          <div className="trace-block-head">
            <Icon name="message" size={14} />
            <strong>Agent</strong>
            <span className="ui-subtle trace-offset">{offset(block.offset_ms)}</span>
          </div>
          <TraceText field={block} />
        </div>
      );
    case "tool":
      return <ToolBlock block={block} />;
    case "permission":
      return (
        <div className="trace-block trace-row">
          <strong>Approval</strong> <Permission permission={block.permission} />
        </div>
      );
    case "plan":
      return (
        <div className="trace-block trace-row ui-subtle">
          Plan updated: {formatNumber(block.completed)} of {formatNumber(block.plan_size)} steps done{" "}
          {offset(block.offset_ms)}
        </div>
      );
    case "cancel":
      return (
        <div className="trace-block trace-row">
          <Badge tone="warning">Interrupted by the participant</Badge> <span className="ui-subtle">{offset(block.offset_ms)}</span>
        </div>
      );
    case "lifecycle":
      return (
        <div className="trace-block trace-row ui-subtle">
          {block.end_reason
            ? `Chat ended: ${CHAT_END_LABELS[block.end_reason] || humanize(block.end_reason)}`
            : CHAT_START_LABELS[block.acp_method] || humanize(block.acp_method || "chat event")}{" "}
          {offset(block.offset_ms)}
        </div>
      );
    case "error":
      return (
        <div className="trace-block trace-row">
          <Badge tone="danger">Error{block.error_code ? ` · ${block.error_code}` : ""}</Badge>
          {block.message ? <span className="ui-subtle"> {block.message}</span> : null}
        </div>
      );
    default:
      return <div className="trace-block trace-row ui-subtle">{humanize(block.event_type || block.type)}</div>;
  }
};

const turnTitle = (turn) => {
  if (turn.kind === "preamble") return "Chat opening";
  if (turn.kind === "continued") return `Turn ${turn.index} (continued)`;
  return `Turn ${turn.index}`;
};

/**
 * One chat, turn by turn: the participant's prompts, the agent's reasoning
 * and messages, and its tool calls with their approvals. Text appears only
 * where the study captured content; everything is plain text.
 */
const ChatTrace = ({ studyId, enrollmentId, chat, onBack }) => {
  const [state, setState] = useState({ isLoading: true, error: "", turns: [], next: null, contentCapture: null });

  const load = useCallback(
    async (cursor) => {
      setState((current) => ({ ...current, isLoading: true, error: "" }));
      const result = await getStudyChatTrace(studyId, enrollmentId, chat.chat_id, { cursor, limit: PAGE_TURNS });
      if (result && result.ok) {
        const data = result.data || {};
        setState((current) => ({
          isLoading: false,
          error: "",
          turns: cursor ? [...current.turns, ...(data.turns || [])] : data.turns || [],
          next: data.next_cursor || null,
          contentCapture: data.content_capture_enabled === true,
        }));
      } else {
        setState((current) => ({ ...current, isLoading: false, error: (result && result.error) || "The chat trace could not be loaded." }));
      }
    },
    [studyId, enrollmentId, chat.chat_id],
  );

  useEffect(() => {
    load(null);
  }, [load]);

  return (
    <div className="ui-stack chat-trace">
      <div className="ui-row-between">
        <button type="button" className="ghost-button button-sm" onClick={onBack}>
          <Icon name="chevronLeft" size={14} />
          Back to the dashboard
        </button>
        <span className="ui-subtle">
          Chat #{chat.ordinal} · {formatDateTime(chat.started_at)} · {CHAT_END_LABELS[chat.end_reason] || humanize(chat.end_reason || "")}
        </span>
      </div>
      {state.contentCapture === false ? (
        <Alert tone="info" live={false}>
          This study does not capture content, so prompts, reasoning and tool data show as “Not captured”. The order,
          timing and outcomes of every step are still shown.
        </Alert>
      ) : null}
      {state.error ? (
        <p className="research-error" role="alert">
          {state.error}
        </p>
      ) : null}
      {state.turns.map((turn) => (
        <Card
          key={`${turn.kind}-${turn.index}-${turn.started_at}`}
          title={turnTitle(turn)}
          subtitle={[
            formatDateTime(turn.started_at),
            turn.duration_ms !== null && turn.duration_ms !== undefined ? formatDuration(turn.duration_ms / 1000) : null,
            turn.stop_reason ? labelFor(STOP_REASON_LABELS, turn.stop_reason) : null,
            turn.usage_tokens ? `${formatNumber(turn.usage_tokens)} tokens` : null,
          ]
            .filter(Boolean)
            .join(" · ")}
        >
          <div className="ui-stack-sm">
            {turn.blocks.map((block, index) => (
              <Block key={`${block.type}-${block.at}-${index}`} block={block} />
            ))}
            {turn.continues ? <p className="ui-hint">This turn continues on the next page.</p> : null}
          </div>
        </Card>
      ))}
      {state.isLoading ? <Loading label="Loading the chat…" /> : null}
      {!state.isLoading && state.next ? (
        <button type="button" className="secondary-button" onClick={() => load(state.next)}>
          Load more turns
        </button>
      ) : null}
    </div>
  );
};

export default ChatTrace;
