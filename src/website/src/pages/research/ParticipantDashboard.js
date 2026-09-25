import React, { useEffect, useState } from "react";
import { getStudyParticipantDashboard } from "../../utils/api";
import { BarList, CapBullet, ChartCard, DailyColumns, DataTable } from "../../components/charts/Charts";
import { Badge, Card, EmptyState, KpiTile, Loading } from "../../components/common/ui";
import {
  formatCompact,
  formatDate,
  formatDateTime,
  formatDuration,
  formatNumber,
  formatPercent,
  humanize,
} from "../../utils/format";
import {
  DECISION_LABELS,
  DECISION_ORDER,
  STOP_REASON_LABELS,
  STOP_REASON_ORDER,
  TOOL_KIND_ORDER,
  formatMetric,
  labelFor,
  orderedKeys,
} from "./studyMetrics";
import { ENROLLMENT_STATUS, HEALTH, ToolName } from "./studyUtils";

const TILE_METRICS = [
  "prompts",
  "active_days",
  "session_hours",
  "tool_calls_per_prompt",
  "tool_failure_rate",
  "median_turn_seconds",
  "cancel_rate",
  "permission_denial_rate",
  "tokens_per_prompt",
  "seconds_to_first_agent_edit",
];

const TILE_LABELS = {
  prompts: "Prompts",
  active_days: "Active days",
  session_hours: "Session time",
  tool_calls_per_prompt: "Tool calls / prompt",
  tool_failure_rate: "Tool failure rate",
  median_turn_seconds: "Median turn",
  cancel_rate: "Interrupted turns",
  permission_denial_rate: "Approvals denied",
  tokens_per_prompt: "Tokens / prompt",
  seconds_to_first_agent_edit: "Time to first edit",
};

const EVENT_LABELS = {
  "agent.message.started": "Prompt sent",
  "agent.message.completed": "Agent replied",
  "interaction.started": "Session opened",
  "interaction.completed": "Turn cancelled",
  "tool.created": "Tool call",
  "tool.started": "Tool started",
  "tool.completed": "Tool finished",
  "tool.failed": "Tool failed",
  "permission.requested": "Approval requested",
  "permission.decided": "Approval decided",
  "plan.updated": "Plan updated",
  "usage.updated": "Usage reported",
  "ide.document.changed": "Developer edited a file",
  "ide.file.opened": "File opened",
  "ide.file.saved": "File saved",
  "ide.file.closed": "File closed",
  "ide.run.executed": "Run executed",
  "agent.error": "Agent error",
  "system.agent.crashed": "Agent crashed",
  "system.proxy.error": "Proxy error",
};

const eventTone = (event) => {
  const type = String(event.event_type || "");
  if (type.includes("error") || type.includes("crashed") || type === "tool.failed" || event.status === "failed") return "danger";
  if (type.startsWith("permission")) return event.decision === "reject" ? "warning" : "violet";
  if (type.startsWith("tool")) return "info";
  if (type.startsWith("agent.message")) return "primary";
  if (type.startsWith("ide")) return "neutral";
  return "neutral";
};

const shortDay = (date) => {
  const parsed = new Date(`${date}T00:00:00Z`);
  return Number.isNaN(parsed.getTime())
    ? date
    : parsed.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
};

const eventDetail = (event) =>
  [
    event.tool_name,
    event.tool_kind ? `(${event.tool_kind})` : null,
    event.status && !["completed"].includes(event.status) ? event.status : null,
    event.decision ? labelFor(DECISION_LABELS, event.decision) : null,
    event.stop_reason ? labelFor(STOP_REASON_LABELS, event.stop_reason) : null,
    event.error_code ? `error ${event.error_code}` : null,
  ]
    .filter(Boolean)
    .join(" ");

/** Per-participant telemetry dashboard shown in the study drawer. */
const ParticipantDashboard = ({ studyId, participant, color }) => {
  const [state, setState] = useState({ isLoading: true, error: "", data: null });
  const [showAllTurns, setShowAllTurns] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setState({ isLoading: true, error: "", data: null });
    getStudyParticipantDashboard(studyId, participant.enrollment_id).then((result) => {
      if (cancelled) return;
      if (result && result.ok) setState({ isLoading: false, error: "", data: result.data });
      else setState({ isLoading: false, error: (result && result.error) || "The participant dashboard could not be loaded.", data: null });
    });
    return () => {
      cancelled = true;
    };
  }, [studyId, participant.enrollment_id]);

  if (state.isLoading) return <Loading label="Loading participant telemetry…" />;
  if (state.error) {
    return (
      <p className="research-error" role="alert">
        {state.error}
      </p>
    );
  }

  const data = state.data || {};
  const metrics = data.metrics || {};
  const enrollment = ENROLLMENT_STATUS[data.status || participant.status] || { label: data.status || "Unknown", tone: "neutral" };
  const health = HEALTH[data.health || participant.health];
  const daily = Array.isArray(data.daily) ? data.daily : [];
  const turns = Array.isArray(data.turns) ? data.turns : [];
  const sessions = Array.isArray(data.sessions_list) ? data.sessions_list : [];
  const timeline = Array.isArray(data.timeline) ? data.timeline : [];
  const toolKinds = Array.isArray(data.tool_kinds) ? data.tool_kinds : [];
  const tools = Array.isArray(data.tools) ? data.tools : [];
  const stopReasons = Array.isArray(data.stop_reasons) ? data.stop_reasons : [];
  const decisions = Array.isArray(data.permission_decisions) ? data.permission_decisions : [];
  const context = data.context || null;
  const hasTelemetry = (data.activity?.prompts || 0) > 0 || timeline.length > 0;

  const days = daily.map((day) => ({
    key: day.date,
    label: shortDay(day.date),
    fullLabel: formatDate(day.date),
    values: { prompts: Number(day.prompts) || 0 },
  }));

  const kindItems = orderedKeys(toolKinds.map((item) => item.tool_kind || "other"), TOOL_KIND_ORDER).map((kind) => {
    const item = toolKinds.find((entry) => (entry.tool_kind || "other") === kind) || {};
    return {
      key: kind,
      label: humanize(kind),
      value: item.calls || 0,
      detail: item.failures ? `· ${formatNumber(item.failures)} failed` : null,
    };
  });
  const stopItems = orderedKeys(stopReasons.map((item) => item.stop_reason), STOP_REASON_ORDER).map((reason) => ({
    key: reason,
    label: labelFor(STOP_REASON_LABELS, reason),
    value: (stopReasons.find((item) => item.stop_reason === reason) || {}).count || 0,
  }));
  const decisionItems = orderedKeys(decisions.map((item) => item.decision), DECISION_ORDER).map((decision) => ({
    key: decision,
    label: labelFor(DECISION_LABELS, decision),
    value: (decisions.find((item) => item.decision === decision) || {}).count || 0,
  }));
  const visibleTurns = showAllTurns ? turns : turns.slice(0, 15);

  return (
    <div className="ui-stack participant-dashboard">
      <div className="ui-row">
        <Badge tone={enrollment.tone}>{enrollment.label}</Badge>
        {health ? <Badge tone={health.tone}>{health.label}</Badge> : null}
        <span className="ui-subtle">
          Enrolled {formatDateTime(data.enrolled_at || participant.enrolled_at)}
          {data.consent_accepted_at ? ` · consent ${formatDateTime(data.consent_accepted_at)}` : ""}
        </span>
      </div>

      {!hasTelemetry ? (
        <Card>
          <EmptyState icon="activity" title="No telemetry from this participant yet">
            The participant has consented, but no research session has reported agent activity. They may still need
            to install the plugin or sign in inside the IDE.
          </EmptyState>
        </Card>
      ) : null}

      <div className="ui-kpis is-compact">
        {TILE_METRICS.map((key) => (
          <KpiTile key={key} label={TILE_LABELS[key]} value={formatMetric(key, metrics[key])} />
        ))}
      </div>

      <ChartCard
        title="Prompts per day"
        subtitle={days.length ? `${days.length} active day${days.length === 1 ? "" : "s"}` : "No prompts yet"}
        table={
          <DataTable
            caption="Daily activity"
            columns={[
              { key: "date", label: "Date" },
              { key: "prompts", label: "Prompts", numeric: true },
              { key: "tool_calls", label: "Tool calls", numeric: true },
              { key: "errors", label: "Errors", numeric: true },
              { key: "ide_edits", label: "Developer edits", numeric: true },
              { key: "session", label: "Session time", numeric: true },
            ]}
            rows={daily.map((day) => ({
              key: day.date,
              date: formatDate(day.date),
              prompts: formatNumber(day.prompts),
              tool_calls: formatNumber(day.tool_calls),
              errors: formatNumber(day.errors),
              ide_edits: formatNumber(day.ide_edits),
              session: formatDuration(day.session_seconds),
            }))}
          />
        }
      >
        <DailyColumns days={days} series={[{ key: "prompts", label: "Prompts", color }]} height={150} />
      </ChartCard>

      <div className="study-chart-grid">
        <ChartCard title="Tool calls by kind" subtitle="ACP tool kinds, comparable across runtimes">
          <BarList items={kindItems} color={color} emptyText="No tool calls yet." />
        </ChartCard>
        <ChartCard title="How turns ended" subtitle="Agent stop reasons">
          <BarList items={stopItems} color={color} emptyText="No completed turns yet." />
        </ChartCard>
        <ChartCard title="Approval decisions" subtitle="Permission requests answered in the IDE">
          <BarList items={decisionItems} color={color} emptyText="No approvals requested." />
        </ChartCard>
      </div>

      {context ? (
        <ChartCard
          title="Context window"
          subtitle={
            context.coverage === "UNAVAILABLE"
              ? "Not observable for this runtime (the agent calls its provider directly)."
              : `Prompt tokens per model call vs. the arm's cap of ${formatNumber(context.cap_tokens)} tokens`
          }
        >
          {context.coverage === "UNAVAILABLE" ? (
            <p className="viz-empty">No relay-observed model calls.</p>
          ) : (
            <>
              <CapBullet
                rows={[
                  {
                    key: "participant",
                    label: "This participant",
                    color,
                    cap: context.cap_tokens,
                    p95: context.prompt_tokens_p95,
                    max: context.prompt_tokens_max,
                    note: `${formatNumber(context.over_cap_calls ?? 0)} of ${formatNumber(context.calls_with_prompt_tokens ?? context.model_calls ?? 0)} over cap`,
                  },
                ]}
              />
              <p className="ui-hint">
                Bar: 95th percentile prompt tokens · thin line: maximum · red marker: cap. Median{" "}
                {formatCompact(context.prompt_tokens_p50)} tokens across {formatNumber(context.model_calls)} model
                calls.
              </p>
            </>
          )}
        </ChartCard>
      ) : null}

      {tools.length ? (
        <Card title="Most used tools">
          <DataTable
            caption="Most used tools"
            columns={[
              { key: "tool_name", label: "Tool", render: (row) => <ToolName name={row.tool_name} /> },
              { key: "tool_kind", label: "Kind", render: (row) => humanize(row.tool_kind || "other") },
              { key: "calls", label: "Calls", numeric: true, render: (row) => formatNumber(row.calls) },
              { key: "failures", label: "Failed", numeric: true, render: (row) => formatNumber(row.failures) },
              {
                key: "median_duration_ms",
                label: "Median duration",
                numeric: true,
                render: (row) => (row.median_duration_ms == null ? "—" : formatDuration(row.median_duration_ms / 1000)),
              },
            ]}
            rows={tools.map((tool, index) => ({ ...tool, key: `${tool.tool_name}-${tool.tool_kind}-${index}` }))}
          />
        </Card>
      ) : null}

      {turns.length ? (
        <Card
          title="Recent turns"
          subtitle="One row per prompt: what the agent did and how the turn ended."
          actions={
            turns.length > 15 ? (
              <button type="button" className="ghost-button button-sm" onClick={() => setShowAllTurns((value) => !value)}>
                {showAllTurns ? "Show fewer" : `Show all ${turns.length}`}
              </button>
            ) : null
          }
        >
          <DataTable
            caption="Recent turns"
            columns={[
              { key: "started_at", label: "Started", render: (row) => formatDateTime(row.started_at) },
              { key: "duration", label: "Duration", numeric: true, render: (row) => formatDuration(row.duration_seconds) },
              {
                key: "tools",
                label: "Tool calls",
                numeric: true,
                render: (row) => (
                  <>
                    {formatNumber(row.tool_calls)}
                    {row.tool_failures ? <span className="ui-subtle"> ({formatNumber(row.tool_failures)} failed)</span> : null}
                  </>
                ),
              },
              { key: "permission_requests", label: "Approvals", numeric: true, render: (row) => formatNumber(row.permission_requests) },
              { key: "usage_tokens", label: "Tokens", numeric: true, render: (row) => formatCompact(row.usage_tokens) },
              {
                key: "stop_reason",
                label: "Outcome",
                render: (row) =>
                  row.cancelled ? (
                    <Badge tone="warning">Cancelled</Badge>
                  ) : row.stop_reason ? (
                    <Badge tone={row.stop_reason === "end_turn" ? "success" : "neutral"}>
                      {labelFor(STOP_REASON_LABELS, row.stop_reason)}
                    </Badge>
                  ) : (
                    <span className="ui-subtle">Open</span>
                  ),
              },
            ]}
            rows={visibleTurns.map((turn, index) => ({ ...turn, key: `${turn.session_id || "s"}-${turn.turn_id || "t"}-${index}` }))}
          />
        </Card>
      ) : null}

      {sessions.length ? (
        <Card title="Sessions">
          <DataTable
            caption="Research sessions"
            columns={[
              { key: "opened_at", label: "Opened", render: (row) => formatDateTime(row.opened_at) },
              { key: "state", label: "State", render: (row) => humanize(row.state) },
              { key: "length", label: "Length", numeric: true, render: (row) => formatDuration(row.session_seconds) },
              { key: "prompts", label: "Prompts", numeric: true, render: (row) => formatNumber(row.prompts) },
              { key: "tool_calls", label: "Tool calls", numeric: true, render: (row) => formatNumber(row.tool_calls) },
              { key: "errors", label: "Errors", numeric: true, render: (row) => formatNumber(row.errors) },
              { key: "close_reason", label: "Closed", render: (row) => (row.close_reason ? humanize(row.close_reason) : row.closed_at ? "Closed" : "Open") },
            ]}
            rows={sessions.map((session, index) => ({ ...session, key: session.session_id || `session-${index}` }))}
          />
        </Card>
      ) : null}

      {timeline.length ? (
        <Card title="Activity timeline" subtitle="Most recent events, metadata only (no prompt or code content).">
          <ol className="participant-timeline">
            {timeline.map((event, index) => (
              <li key={`${event.occurred_at}-${index}`}>
                <span className={`timeline-dot tone-${eventTone(event)}`} aria-hidden="true" />
                <span className="timeline-time ui-num">{formatDateTime(event.occurred_at)}</span>
                <span className="timeline-label">{EVENT_LABELS[event.event_type] || event.event_type}</span>
                <span className="timeline-detail ui-muted">{eventDetail(event)}</span>
              </li>
            ))}
          </ol>
        </Card>
      ) : null}

      {metrics.plan_completion_rate !== undefined && metrics.plan_completion_rate !== null ? (
        <p className="ui-hint">Plan completion across turns with a published plan: {formatPercent(metrics.plan_completion_rate)}.</p>
      ) : null}
    </div>
  );
};

export default ParticipantDashboard;
