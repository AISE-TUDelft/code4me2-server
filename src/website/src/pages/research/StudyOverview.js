import React from "react";
import { ChartCard, DailyColumns, DataTable } from "../../components/charts/Charts";
import { Alert, Badge, Card, KpiTile } from "../../components/common/ui";
import { formatCompact, formatDate, formatDateTime, formatNumber, humanize } from "../../utils/format";
import { CONTENT_DESCRIPTION, RUNTIME_CLASS_LABELS, RUNTIME_LABELS, collectedClasses, describeSessionPolicy } from "./studyUtils";

const unavailable = (value) => (value === null || value === undefined ? "Unavailable" : value);

const shortDay = (date) => {
  const parsed = new Date(`${date}T00:00:00Z`);
  return Number.isNaN(parsed.getTime())
    ? date
    : parsed.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
};

/** Study summary: headline numbers, arms, recent activity and configuration. */
const StudyOverview = ({ study, arms, participantsData, summary, summaryError, onReloadSummary, onOpenTab }) => {
  const totals = summary?.totals || {};
  const telemetryPolicy = study.telemetry_policy && typeof study.telemetry_policy === "object" ? study.telemetry_policy : {};
  const classes = Array.isArray(telemetryPolicy.allowed_field_classes) ? telemetryPolicy.allowed_field_classes : [];
  const contentCapture = telemetryPolicy.content_capture === true;
  const sessionRows = describeSessionPolicy(study.session_policy);
  // Assigned participants per arm: the summary carries them, so the overview
  // does not need the (heavier) participants list.
  const armCounts = summary?.arms || participantsData?.arms || [];
  const participantsByArm = new Map(armCounts.map((arm) => [arm.profile_id, arm.participants]));
  const assigned = arms.reduce((sum, arm) => sum + (Number(participantsByArm.get(arm.profile_id)) || 0), 0);

  const recentDays = (summary?.daily || []).slice(-21);
  const series = arms.map((arm) => ({ key: arm.profile_id, label: arm.name, color: arm.color }));
  const days = recentDays.map((day) => ({
    key: day.date,
    label: shortDay(day.date),
    fullLabel: formatDate(day.date),
    values: Object.fromEntries(
      arms.map((arm) => [arm.profile_id, Number(day.by_arm?.[arm.profile_id]?.prompts) || 0]),
    ),
  }));

  return (
    <div className="ui-stack">
      <div className="ui-kpis is-compact">
        <KpiTile
          label="Enrollments"
          value={`${unavailable(study.enrollment_count)} (${unavailable(study.active_enrollment_count)} active)`}
          detail="Participants who consented"
        />
        <KpiTile label="Assignments" value={unavailable(study.assignment_count)} detail="One sticky arm per enrollment" />
        <KpiTile
          label="Sessions"
          value={`${unavailable(study.active_session_count)} active`}
          detail={totals.sessions !== undefined ? `${formatNumber(totals.sessions)} in total` : "IDE research sessions"}
        />
        <KpiTile
          label="Agent prompts"
          value={totals.prompts !== undefined ? formatCompact(totals.prompts) : "—"}
          detail={totals.tool_calls !== undefined ? `${formatCompact(totals.tool_calls)} tool calls` : "From study telemetry"}
        />
        <KpiTile
          label="Collection"
          value={study.collection_status ? humanize(study.collection_status) : "Unavailable"}
          detail={study.kill_switch && study.kill_switch.status !== "RELEASED" ? "Kill switch engaged" : "Telemetry intake"}
        />
      </div>

      <div className="ui-grid-2 study-overview-grid">
        <Card
          title="Selected agent profiles"
          subtitle="Equal-probability random assignment, sticky per enrollment."
          className="research-profile-summary"
          actions={
            arms.length ? (
              <button type="button" className="ghost-button button-sm" onClick={() => onOpenTab("participants")}>
                View participants
              </button>
            ) : null
          }
        >
          {arms.length ? (
            <ul className="study-arms">
              {arms.map((arm) => {
                const count = participantsByArm.get(arm.profile_id);
                const share = assigned > 0 && count !== undefined ? (Number(count) || 0) / assigned : null;
                return (
                  <li key={arm.profile_id}>
                    <span className="viz-swatch is-dot" style={{ backgroundColor: arm.color }} aria-hidden="true" />
                    <div className="ui-cell-stack study-arm-text">
                      <strong>{arm.name || arm.profile_id}</strong>
                      <small>{arm.model || "Model not specified"}</small>
                    </div>
                    {arm.framework_version ? (
                      <Badge tone={arm.framework_version === "code4me2-agent" ? "primary" : "violet"}>
                        {RUNTIME_LABELS[arm.framework_version] || arm.framework_version}
                      </Badge>
                    ) : null}
                    <span className="study-arm-count">
                      {count === undefined ? "—" : `${count} participant${Number(count) === 1 ? "" : "s"}`}
                      {share !== null ? <small>{Math.round(share * 100)}%</small> : null}
                    </span>
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="research-hint">
              No profiles selected. A study without selected agent profiles cannot be joined.
            </p>
          )}
          <p className="research-hint">Profile selection is fixed after study creation.</p>
        </Card>

        <Card title="Configuration" subtitle="Frozen when the study was created.">
          <dl className="ui-dl">
            <div>
              <dt>Starts</dt>
              <dd>{study.starts_at ? formatDateTime(study.starts_at) : "Immediately"}</dd>
            </div>
            <div>
              <dt>Ends</dt>
              <dd>{study.ends_at ? formatDateTime(study.ends_at) : "No end date"}</dd>
            </div>
            <div>
              <dt>Created</dt>
              <dd>{formatDateTime(study.created_at)}</dd>
            </div>
            <div>
              <dt>First consent</dt>
              <dd>{study.consent_locked_at ? formatDateTime(study.consent_locked_at) : "Not yet"}</dd>
            </div>
            {sessionRows.map(([label, value]) => (
              <div key={label}>
                <dt>{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
          <div className="ui-stack-sm">
            <span className="ui-section-title">Telemetry collected</span>
            <ul className="study-collect">
              {collectedClasses(telemetryPolicy)
                .filter((name) => name !== "CONTENT")
                .map((name) => (
                  <li key={name}>{RUNTIME_CLASS_LABELS[name]}</li>
                ))}
            </ul>
            {classes.length ? (
              <span className="ui-hint">Declared as {classes.join(", ")}.</span>
            ) : (
              <span className="ui-hint">No classes declared: the server's metadata default applies.</span>
            )}
            {contentCapture ? (
              <Badge tone="warning">Content capture on — {CONTENT_DESCRIPTION} are stored after consent</Badge>
            ) : (
              <span className="ui-hint">
                Prompt and response text, tool arguments and output, and file contents are not stored
                {collectedClasses(telemetryPolicy).includes("BEHAVIORAL")
                  ? "; tool titles and error messages can still quote commands."
                  : "."}
              </span>
            )}
          </div>
        </Card>
      </div>

      {summary ? (
        <ChartCard
          title="Recent prompts by arm"
          subtitle={recentDays.length ? `Last ${recentDays.length} active days` : "No activity yet"}
          legend={
            series.length > 1 ? (
              <ul className="viz-legend">
                {series.map((item) => (
                  <li key={item.key}>
                    <span className="viz-swatch" style={{ backgroundColor: item.color }} aria-hidden="true" />
                    {item.label}
                  </li>
                ))}
              </ul>
            ) : null
          }
          table={
            <DataTable
              caption="Prompts per day by arm"
              columns={[
                { key: "date", label: "Date" },
                ...arms.map((arm) => ({
                  key: arm.profile_id,
                  label: arm.name,
                  numeric: true,
                  render: (row) => formatNumber(row.values[arm.profile_id]),
                })),
              ]}
              rows={days.map((day) => ({ key: day.key, date: day.fullLabel, values: day.values }))}
            />
          }
          actions={
            <button type="button" className="ghost-button button-sm" onClick={() => onOpenTab("analytics")}>
              Open analytics
            </button>
          }
        >
          <DailyColumns days={days} series={series} height={170} emptyText="No prompts recorded yet." />
        </ChartCard>
      ) : null}

      {!summary && summaryError ? (
        <Alert tone="warning" title="Study telemetry could not be loaded.">
          {summaryError}{" "}
          {onReloadSummary ? (
            <button type="button" className="ghost-button button-sm" onClick={onReloadSummary}>
              Retry
            </button>
          ) : null}
        </Alert>
      ) : null}
    </div>
  );
};

export default StudyOverview;
