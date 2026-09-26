import React, { useEffect, useMemo, useState } from "react";
import { getStudyAnalyticsSummary } from "../../utils/api";
import Icon from "../../components/common/Icon";
import {
  ArmStripPlot,
  CapBullet,
  ChartCard,
  DailyColumns,
  DataTable,
  Heatmap,
  Legend,
  ShareBars,
  colorForIndex,
} from "../../components/charts/Charts";
import { Alert, Card, EmptyState, KpiTile, Loading, SegmentedControl } from "../../components/common/ui";
import { formatCompact, formatDate, formatDuration, formatNumber, formatPercent, humanize } from "../../utils/format";
import {
  DECISION_LABELS,
  DECISION_ORDER,
  METRICS,
  METRIC_BY_KEY,
  STOP_REASON_LABELS,
  STOP_REASON_ORDER,
  TOOL_KIND_ORDER,
  formatMetric,
  labelFor,
  orderedKeys,
} from "./studyMetrics";
import { ToolName, armsForStudy } from "./studyUtils";

const KEY_METRICS = [
  "tool_calls_per_prompt",
  "tool_failure_rate",
  "cancel_rate",
  "median_turn_seconds",
  "prompts_per_session_hour",
  "tokens_per_prompt",
];

const RANGE_OPTIONS = [
  { value: "all", label: "All time" },
  { value: "30", label: "Last 30 days" },
  { value: "7", label: "Last 7 days" },
  { value: "custom", label: "Custom" },
];

const isoDay = (date) => date.toISOString().slice(0, 10);

const windowFor = (range, custom) => {
  if (range === "all") return {};
  if (range === "custom") return { start: custom.start || undefined, end: custom.end || undefined };
  const end = new Date();
  const start = new Date(end.getTime() - (Number(range) - 1) * 86400000);
  return { start: isoDay(start), end: isoDay(end) };
};

// An unapplied custom range is the same request as "All time".
const isAllTime = (range, custom) => range === "all" || (range === "custom" && !custom.start && !custom.end);

const LOADING = { isLoading: true, error: "", data: null };

const shortDay = (date) => {
  const parsed = new Date(`${date}T00:00:00Z`);
  return Number.isNaN(parsed.getTime())
    ? date
    : parsed.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
};

const COVERAGE_TEXT = {
  usage_tokens: "Token usage",
  turn_correlation: "Turn correlation",
  tool_kind: "Tool kinds",
};

const COVERAGE_TONE = { AVAILABLE: "success", PARTIAL: "warning", UNAVAILABLE: "danger" };

/**
 * Study-level dashboards: totals, activity over time and arm comparisons.
 *
 * "All time" shows the summary the study workspace already loaded
 * (``allTime``, reloaded through ``onReloadAllTime``); only a narrower range
 * makes its own request, and a superseded request never replaces a newer one.
 */
const StudyAnalytics = ({ study, allTime, onReloadAllTime, refreshToken = 0 }) => {
  const [range, setRange] = useState("all");
  const [custom, setCustom] = useState({ start: "", end: "" });
  const [appliedCustom, setAppliedCustom] = useState({ start: "", end: "" });
  const [customError, setCustomError] = useState("");
  const [windowed, setWindowed] = useState(LOADING);
  const [reloadCount, setReloadCount] = useState(0);
  const [showAllMetrics, setShowAllMetrics] = useState(false);
  const [transitionArm, setTransitionArm] = useState("");
  const usesAllTime = isAllTime(range, appliedCustom);

  useEffect(() => {
    if (usesAllTime) return undefined;
    let cancelled = false;
    setWindowed((current) => ({ ...current, isLoading: true, error: "" }));
    Promise.resolve(getStudyAnalyticsSummary(study.study_id, windowFor(range, appliedCustom))).then((result) => {
      if (cancelled) return;
      if (result && result.ok) setWindowed({ isLoading: false, error: "", data: result.data });
      else setWindowed({ isLoading: false, error: (result && result.error) || "Study analytics could not be loaded.", data: null });
    });
    return () => {
      cancelled = true;
    };
  }, [usesAllTime, study.study_id, range, appliedCustom, refreshToken, reloadCount]);

  const state = usesAllTime ? allTime || LOADING : windowed;
  const refresh = () => {
    if (usesAllTime) {
      if (onReloadAllTime) onReloadAllTime();
    } else {
      setReloadCount((value) => value + 1);
    }
  };

  const applyCustom = (event) => {
    event.preventDefault();
    if (custom.start && custom.end && custom.start > custom.end) {
      setCustomError("The start date must be on or before the end date.");
      return;
    }
    setCustomError("");
    setAppliedCustom(custom);
  };

  const data = state.data;
  const arms = useMemo(() => armsForStudy(study, data?.arms || []), [study, data]);
  const armById = useMemo(() => new Map((data?.arms || []).map((arm) => [arm.profile_id, arm])), [data]);
  const legendItems = arms.map((arm) => ({ key: arm.profile_id, label: arm.name, color: arm.color }));

  const totals = data?.totals || {};
  const daily = Array.isArray(data?.daily) ? data.daily : [];
  const noTelemetry = data && !(totals.prompts > 0) && !(totals.tool_calls > 0) && !(totals.sessions > 0);

  const series = arms.map((arm) => ({ key: arm.profile_id, label: arm.name, color: arm.color }));
  const days = daily.map((day) => ({
    key: day.date,
    label: shortDay(day.date),
    fullLabel: formatDate(day.date),
    values: Object.fromEntries(arms.map((arm) => [arm.profile_id, Number(day.by_arm?.[arm.profile_id]?.prompts) || 0])),
  }));

  const metricKeys = showAllMetrics ? METRICS.map((metric) => metric.key) : KEY_METRICS;

  const shareCategories = (field, order, labels) => {
    const keys = orderedKeys(
      (data?.arms || []).flatMap((arm) => (Array.isArray(arm[field]) ? arm[field] : []).map((item) => item.tool_kind || item.stop_reason || item.decision)),
      order,
    );
    // Colour follows the category's fixed slot, never its rank among the
    // categories present; "other"/unknown kinds fold into the muted grey.
    return keys.map((key) => ({
      key,
      label: labels ? labelFor(labels, key) : humanize(key),
      color: key === "other" ? "var(--viz-muted)" : colorForIndex(order.indexOf(key)),
    }));
  };

  const shareRows = (field, keyField, valueField) =>
    arms.map((arm) => {
      const source = armById.get(arm.profile_id);
      const list = Array.isArray(source?.[field]) ? source[field] : [];
      return {
        key: arm.profile_id,
        label: arm.name,
        swatch: arm.color,
        values: Object.fromEntries(list.map((item) => [item[keyField] || "other", Number(item[valueField]) || 0])),
      };
    });

  const kindCategories = shareCategories("tool_kinds", TOOL_KIND_ORDER);
  const stopCategories = shareCategories("stop_reasons", STOP_REASON_ORDER, STOP_REASON_LABELS);
  const decisionCategories = shareCategories("permission_decisions", DECISION_ORDER, DECISION_LABELS);

  const shareTable = (categories, rows) => (
    <DataTable
      columns={[
        { key: "label", label: "Arm" },
        ...categories.map((category) => ({
          key: category.key,
          label: category.label,
          numeric: true,
          render: (row) => formatNumber(row.values[category.key] || 0),
        })),
      ]}
      rows={rows}
    />
  );

  const contextRows = arms.map((arm) => {
    const context = armById.get(arm.profile_id)?.context || {};
    const observed = context.coverage && context.coverage !== "UNAVAILABLE";
    return {
      key: arm.profile_id,
      label: arm.name,
      color: arm.color,
      cap: context.cap_tokens,
      p95: observed ? context.prompt_tokens_p95 : null,
      max: observed ? context.prompt_tokens_max : null,
      note: observed
        ? `${formatPercent(context.over_cap_share, 1)} of calls over cap`
        : "Not observable (Codex)",
      context,
    };
  });
  const anyContext = contextRows.some((row) => row.context && row.context.coverage && row.context.coverage !== "UNAVAILABLE");

  const selectedTransitionArm = transitionArm || arms[0]?.profile_id || "";
  const transitions = armById.get(selectedTransitionArm)?.transitions || [];
  const transitionKeys = orderedKeys(transitions.flatMap((cell) => [cell.from, cell.to]), TOOL_KIND_ORDER);

  const tools = Array.isArray(data?.tools) ? data.tools : [];

  return (
    <div className="ui-stack study-analytics">
      <div className="ui-toolbar study-analytics-filters">
        <SegmentedControl options={RANGE_OPTIONS} value={range} onChange={setRange} label="Time range" />
        {range === "custom" ? (
          <form className="ui-row" onSubmit={applyCustom} noValidate>
            <input
              className="ui-input"
              type="date"
              value={custom.start}
              max={custom.end || undefined}
              onChange={(event) => setCustom({ ...custom, start: event.target.value })}
              aria-label="From date"
              aria-invalid={customError ? "true" : undefined}
            />
            <span className="ui-subtle">to</span>
            <input
              className="ui-input"
              type="date"
              value={custom.end}
              min={custom.start || undefined}
              onChange={(event) => setCustom({ ...custom, end: event.target.value })}
              aria-label="To date"
              aria-invalid={customError ? "true" : undefined}
            />
            <button type="submit" className="secondary-button button-sm">
              Apply
            </button>
          </form>
        ) : null}
        <button type="button" className="secondary-button button-sm" onClick={refresh} disabled={state.isLoading} style={{ marginLeft: "auto" }}>
          <Icon name="refresh" size={14} />
          Refresh
        </button>
      </div>

      {range === "custom" && customError ? (
        <p className="research-error" role="alert">
          {customError}
        </p>
      ) : null}

      {state.error ? (
        <p className="research-error" role="alert">
          {state.error}
        </p>
      ) : null}

      {state.isLoading && !data ? <Loading label="Loading study analytics…" /> : null}

      {data ? (
        <div className={`ui-stack${state.isLoading ? " is-refreshing" : ""}`}>
          <div className="ui-kpis is-compact">
            <KpiTile
              label="Participants with telemetry"
              value={`${formatNumber(totals.participants_with_telemetry)} / ${formatNumber(totals.participants_enrolled)}`}
              detail={`${formatNumber(totals.participants_active)} active enrollments`}
            />
            <KpiTile label="Prompts" value={formatCompact(totals.prompts)} detail={`${formatNumber(totals.sessions)} sessions`} />
            <KpiTile
              label="Tool calls"
              value={formatCompact(totals.tool_calls)}
              detail={totals.tool_calls ? `${formatPercent((totals.tool_failures || 0) / totals.tool_calls, 1)} failed` : "—"}
            />
            <KpiTile label="Session time" value={formatDuration(totals.session_seconds)} detail="Across all participants" />
            <KpiTile
              label="Approvals"
              value={formatCompact(totals.permission_requests)}
              detail={
                totals.permission_requests
                  ? `${formatNumber(totals.permission_rejected)} rejected · ${formatNumber(totals.permission_cancelled)} cancelled`
                  : "No approval requests"
              }
            />
            <KpiTile label="Interrupted turns" value={formatCompact(totals.cancellations)} detail={`${formatNumber(totals.errors)} errors`} />
            <KpiTile
              label="Tokens"
              value={totals.usage_tokens === null || totals.usage_tokens === undefined ? "—" : formatCompact(totals.usage_tokens)}
              detail={
                totals.usage_coverage === null || totals.usage_coverage === undefined
                  ? "Not reported by the agents"
                  : `${formatPercent(totals.usage_coverage)} of turns report usage`
              }
            />
          </div>

          {noTelemetry ? (
            <Card>
              <EmptyState icon="activity" title="No telemetry in this period">
                Charts fill in as soon as participants use the research agent in their IDE. Check the Participants tab
                for anyone who has not started yet.
              </EmptyState>
            </Card>
          ) : null}

          <ChartCard
            title="Prompts per day by arm"
            subtitle="Stacked by assigned arm (UTC days)"
            legend={legendItems.length > 1 ? <Legend items={legendItems} /> : null}
            table={
              <DataTable
                caption="Prompts per day by arm"
                columns={[
                  { key: "date", label: "Date" },
                  { key: "active", label: "Active participants", numeric: true },
                  ...arms.map((arm) => ({
                    key: arm.profile_id,
                    label: arm.name,
                    numeric: true,
                    render: (row) => formatNumber(row.values[arm.profile_id] || 0),
                  })),
                ]}
                rows={daily.map((day, index) => ({
                  key: day.date,
                  date: days[index].fullLabel,
                  active: formatNumber(day.active_participants),
                  values: days[index].values,
                }))}
              />
            }
          >
            <DailyColumns days={days} series={series} height={200} />
          </ChartCard>

          <section className="ui-stack-sm">
            <div className="ui-row-between">
              <div>
                <h3 className="ui-card-title">Arm comparison</h3>
                <p className="ui-card-subtitle">
                  One dot per participant (the randomisation unit); the dark tick marks the median, the shaded band the
                  interquartile range.
                </p>
              </div>
              <button type="button" className="secondary-button button-sm" onClick={() => setShowAllMetrics((value) => !value)}>
                {showAllMetrics ? "Show key metrics" : `Show all ${METRICS.length} metrics`}
              </button>
            </div>
            {legendItems.length > 1 ? <Legend items={legendItems} /> : null}
            <div className="study-chart-grid is-wide">
              {metricKeys.map((key) => {
                const metric = METRIC_BY_KEY[key];
                const armStats = arms.map((arm) => {
                  const stats = armById.get(arm.profile_id)?.metrics?.[key] || {};
                  return {
                    key: arm.profile_id,
                    label: arm.name,
                    color: arm.color,
                    values: Array.isArray(stats.values) ? stats.values : [],
                    n: stats.n,
                    median: stats.median,
                    p25: stats.p25,
                    p75: stats.p75,
                    mean: stats.mean,
                  };
                });
                return (
                  <ChartCard
                    key={key}
                    title={metric.label}
                    subtitle={metric.help}
                    table={
                      <DataTable
                        caption={metric.label}
                        columns={[
                          { key: "label", label: "Arm" },
                          { key: "n", label: "n", numeric: true, render: (row) => formatNumber(row.n ?? row.values.length) },
                          { key: "median", label: "Median", numeric: true, render: (row) => formatMetric(key, row.median) },
                          { key: "p25", label: "P25", numeric: true, render: (row) => formatMetric(key, row.p25) },
                          { key: "p75", label: "P75", numeric: true, render: (row) => formatMetric(key, row.p75) },
                          { key: "mean", label: "Mean", numeric: true, render: (row) => formatMetric(key, row.mean) },
                        ]}
                        rows={armStats}
                      />
                    }
                  >
                    <ArmStripPlot arms={armStats} format={(value) => formatMetric(key, value)} />
                  </ChartCard>
                );
              })}
            </div>
          </section>

          <div className="study-chart-grid">
            <ChartCard
              title="Tool mix by arm"
              subtitle="Share of tool calls per ACP kind"
              legend={kindCategories.length ? <Legend items={kindCategories} /> : null}
              table={shareTable(kindCategories, shareRows("tool_kinds", "tool_kind", "calls"))}
            >
              <ShareBars rows={shareRows("tool_kinds", "tool_kind", "calls")} categories={kindCategories} emptyText="No tool calls yet." />
            </ChartCard>
            <ChartCard
              title="How turns ended"
              subtitle="Agent stop reasons per arm"
              legend={stopCategories.length ? <Legend items={stopCategories} /> : null}
              table={shareTable(stopCategories, shareRows("stop_reasons", "stop_reason", "count"))}
            >
              <ShareBars rows={shareRows("stop_reasons", "stop_reason", "count")} categories={stopCategories} emptyText="No completed turns yet." />
            </ChartCard>
            <ChartCard
              title="Approval decisions"
              subtitle="Permission requests per arm"
              legend={decisionCategories.length ? <Legend items={decisionCategories} /> : null}
              table={shareTable(decisionCategories, shareRows("permission_decisions", "decision", "count"))}
            >
              <ShareBars
                rows={shareRows("permission_decisions", "decision", "count")}
                categories={decisionCategories}
                emptyText="No approvals requested."
              />
            </ChartCard>
          </div>

          <div className="study-chart-grid is-wide">
            <ChartCard
              title="Context cap compliance"
              subtitle="Provider-reported prompt tokens per model call vs. each arm's max context tokens"
              table={
                <DataTable
                  caption="Context cap compliance"
                  columns={[
                    { key: "label", label: "Arm" },
                    { key: "cap", label: "Cap", numeric: true, render: (row) => formatNumber(row.cap) },
                    { key: "calls", label: "Model calls", numeric: true, render: (row) => formatNumber(row.context.model_calls) },
                    { key: "p50", label: "P50", numeric: true, render: (row) => formatNumber(row.context.prompt_tokens_p50) },
                    { key: "p95", label: "P95", numeric: true, render: (row) => formatNumber(row.context.prompt_tokens_p95) },
                    { key: "max", label: "Max", numeric: true, render: (row) => formatNumber(row.context.prompt_tokens_max) },
                    { key: "over", label: "Over cap", numeric: true, render: (row) => formatNumber(row.context.over_cap_calls) },
                  ]}
                  rows={contextRows}
                />
              }
            >
              {anyContext ? (
                <>
                  <CapBullet rows={contextRows} />
                  <p className="ui-hint">
                    Bar: 95th percentile · thin line: maximum · red marker: the arm's cap. Built-in and Goose arms route
                    model calls through the metered relay; Codex signs in with ChatGPT and bypasses it, so it cannot be
                    observed here.
                  </p>
                </>
              ) : (
                <p className="viz-empty">No relay-observed model calls yet.</p>
              )}
            </ChartCard>

            <ChartCard
              title="Tool transitions"
              subtitle="Consecutive tool kinds within a turn (count); hover for the lift"
              actions={
                arms.length > 1 ? (
                  <select
                    className="ui-select"
                    value={selectedTransitionArm}
                    onChange={(event) => setTransitionArm(event.target.value)}
                    aria-label="Arm for tool transitions"
                    style={{ width: "auto", minHeight: 30, paddingTop: 4, paddingBottom: 4 }}
                  >
                    {arms.map((arm) => (
                      <option key={arm.profile_id} value={arm.profile_id}>
                        {arm.name}
                      </option>
                    ))}
                  </select>
                ) : null
              }
            >
              <Heatmap
                rowsLabel="From"
                columnsLabel="to"
                keys={transitionKeys}
                cells={transitions.map((cell) => ({
                  from: cell.from,
                  to: cell.to,
                  value: cell.count,
                  detail: cell.lift === null || cell.lift === undefined ? null : `lift ${formatNumber(cell.lift, { maximumFractionDigits: 2 })}`,
                }))}
              />
            </ChartCard>
          </div>

          {tools.length ? (
            <Card title="Most used tools" subtitle="Across the study, with calls per arm">
              <DataTable
                caption="Most used tools"
                columns={[
                  { key: "tool_name", label: "Tool", render: (row) => <ToolName name={row.tool_name} /> },
                  { key: "tool_kind", label: "Kind", render: (row) => humanize(row.tool_kind || "other") },
                  { key: "calls", label: "Calls", numeric: true, render: (row) => formatNumber(row.calls) },
                  {
                    key: "failures",
                    label: "Failure rate",
                    numeric: true,
                    render: (row) => (row.calls ? formatPercent((row.failures || 0) / row.calls, 1) : "—"),
                  },
                  ...arms.map((arm) => ({
                    key: arm.profile_id,
                    label: arm.name,
                    numeric: true,
                    render: (row) => formatNumber(row.by_arm?.[arm.profile_id] || 0),
                  })),
                ]}
                rows={tools.map((tool, index) => ({ ...tool, key: `${tool.tool_name}-${tool.tool_kind}-${index}` }))}
              />
            </Card>
          ) : null}

          <Card title="Data coverage and method" subtitle="Read the comparisons with these limits in mind.">
            <div className="ui-row">
              {Object.entries(data.coverage || {}).map(([key, value]) => (
                <span key={key} className={`ui-badge ui-badge-${COVERAGE_TONE[value] || "neutral"}`}>
                  {COVERAGE_TEXT[key] || humanize(key)}: {humanize(value)}
                </span>
              ))}
            </div>
            <ul className="study-method-notes">
              <li>Metrics are computed per participant first; arms are compared on those participant-level values.</li>
              <li>Medians and quartiles are descriptive — no significance test is applied; small arms are noisy.</li>
              <li>Tool kinds follow the ACP vocabulary so runtimes that name their tools differently stay comparable.</li>
              <li>
                Telemetry is metadata only unless the study captures content. Code survival, task success and prompt
                intent need additional instrumentation.
              </li>
            </ul>
          </Card>
        </div>
      ) : null}

      {!data && !state.isLoading && !state.error ? (
        <Alert tone="info">No analytics available for this study.</Alert>
      ) : null}
    </div>
  );
};

export default StudyAnalytics;
