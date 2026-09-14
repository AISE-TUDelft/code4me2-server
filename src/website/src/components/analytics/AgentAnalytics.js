import React, { useEffect, useState } from "react";
import Chart from "../visualization/Chart";
import { getAgentOverview, getAgentRunDetail } from "../../utils/api";
import "./AgentAnalytics.css";

const number = (value) => Number(value || 0).toLocaleString();
const percent = (value) => `${(Number(value || 0) * 100).toFixed(1)}%`;
const milliseconds = (value) => `${Math.round(Number(value || 0))}ms`;

const mergeOptions = (previous, values) => [...new Set([...previous, ...values])].sort();

const FilterSelect = ({ label, value, options, onChange, allLabel }) => (
  <label>
    {label}
    <select value={value} onChange={(event) => onChange(event.target.value)}>
      <option value="">{allLabel}</option>
      {options.map((option) => <option key={option} value={option}>{option}</option>)}
    </select>
  </label>
);

const PanelHeading = ({ eyebrow, title, hint }) => (
  <div className="agent-panel-heading">
    <div><span className="agent-eyebrow">{eyebrow}</span><h3>{title}</h3></div>
    {hint && <span className="agent-muted">{hint}</span>}
  </div>
);

const KpiCard = ({ label, value, detail }) => (
  <div><span>{label}</span><strong>{value}</strong><small>{detail}</small></div>
);

const eventDepth = (event, spanIndexes, events) => {
  if (!event.parent_span_id) return 0;
  let parentIndex = spanIndexes.get(event.parent_span_id);
  let depth = 0;
  while (parentIndex !== undefined && depth < 4) {
    depth += 1;
    const parent = events[parentIndex];
    parentIndex = spanIndexes.get(parent?.parent_span_id);
  }
  return depth;
};

const EventGraph = ({ events }) => {
  const spanIndexes = new Map(events.map((event, index) => [event.span_id, index]));
  return (
    <div className="agent-event-scroll">
      <div className="agent-event-graph">
        {events.map((event) => {
          const depth = eventDepth(event, spanIndexes, events);
          const isTool = event.event_type.includes("tool");
          const isFailure = event.event_type.includes("failed") || event.upstream_status >= 400;
          return (
            <div className="agent-graph-row" key={`${event.event_index}-${event.event_type}`} style={{ paddingLeft: `${depth * 28}px` }}>
              <div className={`agent-graph-rail ${isTool ? "tool" : "model"} ${isFailure ? "failure" : ""}`} />
              <div className={`agent-graph-node ${isTool ? "tool" : "model"} ${isFailure ? "failure" : ""}`}>
                <div className="agent-graph-node-main">
                  <span className="agent-event-index">{event.event_index}</span>
                  <strong>{event.event_type}</strong>
                  <small>{event.source || "unknown source"}{event.tool_name ? ` · ${event.tool_name}` : event.model ? ` · ${event.model}` : ""}</small>
                </div>
                <div className="agent-graph-node-stats">
                  {event.latency_ms ? <span>{milliseconds(event.latency_ms)}</span> : null}
                  {event.total_tokens ? <span>{number(event.total_tokens)} tokens</span> : null}
                  {event.upstream_status ? <span>HTTP {event.upstream_status}</span> : null}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};

const AgentAnalytics = ({ timeWindow = "7d" }) => {
  const [data, setData] = useState(null);
  const [framework, setFramework] = useState("");
  const [model, setModel] = useState("");
  const [profile, setProfile] = useState("");
  const [filterOptions, setFilterOptions] = useState({
    frameworks: [],
    models: [],
    profiles: [],
  });
  const [selectedRun, setSelectedRun] = useState(null);
  const [runDetail, setRunDetail] = useState(null);
  const [isRunLoading, setIsRunLoading] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      setIsLoading(true);
      setError(null);
      const params = { time_window: timeWindow };
      if (framework) params.framework = framework;
      if (model) params.model = model;
      if (profile) params.profile = profile;
      const response = await getAgentOverview(params);
      if (cancelled) return;
      if (response.ok) {
        setData(response.data);
        const nextProfiles = response.data.profiles || [];
        setFilterOptions((previous) => ({
          frameworks: mergeOptions(previous.frameworks, nextProfiles.map((item) => item.framework_version)),
          models: mergeOptions(previous.models, nextProfiles.map((item) => item.model)),
          profiles: mergeOptions(previous.profiles, nextProfiles.map((item) => item.profile_name)),
        }));
      } else setError(response.error);
      setIsLoading(false);
    };
    load().catch((loadError) => {
      console.error("Agent telemetry error:", loadError);
      setError("Failed to load agent telemetry");
      setIsLoading(false);
    });
    return () => { cancelled = true; };
  }, [timeWindow, framework, model, profile]);

  const openRun = async (taskId) => {
    setSelectedRun(taskId);
    setIsRunLoading(true);
    const response = await getAgentRunDetail(taskId);
    setRunDetail(response.ok ? response.data : { error: response.error });
    setIsRunLoading(false);
  };

  if (isLoading) {
    return <div className="agent-analytics"><div className="loading-message"><h3>Loading Agent Telemetry...</h3><div className="loading-spinner" /></div></div>;
  }

  if (error) {
    return <div className="agent-analytics"><div className="error-message"><h3>Failed to load agent telemetry</h3><p>{error}</p><button onClick={() => window.location.reload()}>Retry</button></div></div>;
  }

  const summary = data?.summary || {};
  const profiles = data?.profiles || [];
  const tools = data?.tools || [];
  const runs = data?.recent_runs || [];
  const trend = data?.trend || [];
  const latencyDistribution = data?.latency_distribution || [];
  const maxLatencyCalls = Math.max(...latencyDistribution.map((item) => item.calls), 1);
  const eventTypes = data?.event_types || [];
  const errors = data?.errors || [];
  const frameworks = filterOptions.frameworks;
  const models = filterOptions.models;
  const availableProfiles = filterOptions.profiles;

  return (
    <div className="agent-analytics">
      <div className="agent-page-header">
        <div>
          <span className="agent-eyebrow">Operational telemetry</span>
          <h2>Agent Performance</h2>
          <p>Understand how agents run, where they spend time, and where execution fails.</p>
        </div>
        <div className="agent-window">Last {timeWindow.replace("d", " days")}</div>
      </div>

      <div className="agent-controls">
        <FilterSelect label="Runtime" value={framework} options={frameworks} onChange={setFramework} allLabel="All runtimes" />
        <FilterSelect label="Model" value={model} options={models} onChange={setModel} allLabel="All models" />
        <FilterSelect label="Profile" value={profile} options={availableProfiles} onChange={setProfile} allLabel="All profiles" />
        {(framework || model || profile) && <button className="agent-clear" onClick={() => { setFramework(""); setModel(""); setProfile(""); }}>Clear filters</button>}
      </div>

      <div className="agent-kpis">
        <KpiCard label="Total runs" value={number(summary.total_tasks)} detail={`${number(summary.open_tasks)} active or pending`} />
        <KpiCard label="Completion rate" value={percent(summary.completion_rate)} detail={`${number(summary.failed_tasks)} failed runs`} />
        <KpiCard label="Model latency" value={milliseconds(summary.avg_model_latency_ms)} detail={`${milliseconds(summary.p95_model_latency_ms)} p95 · ${number(summary.model_calls)} calls`} />
        <KpiCard label="Tool activity" value={number(summary.tool_calls)} detail={`${number(summary.failures)} execution failures`} />
        <KpiCard label="Avg steps" value={Number(summary.avg_steps || 0).toFixed(1)} detail={`${milliseconds(summary.avg_task_duration_ms)} per run`} />
        <KpiCard label="Edit acceptance" value={percent(summary.edit_acceptance_rate)} detail={`${number(summary.total_edits)} decisions`} />
      </div>

      <section className="agent-panel agent-token-panel">
        <PanelHeading eyebrow="Token and request accounting" title="What each model call costs" hint="Provider usage is exact where reported; byte-based values are estimates." />
        <div className="agent-token-grid">
          <div><span>Provider input tokens</span><strong>{number(summary.provider_input_tokens)}</strong><small>reported by model provider</small></div>
          <div><span>Conversation context</span><strong>~{number(summary.conversation_tokens_estimated)}</strong><small>estimated from message bytes</small></div>
          <div><span>Tool schema</span><strong>~{number(summary.tool_schema_tokens_estimated)}</strong><small>estimated from schema bytes</small></div>
          <div><span>Tool results</span><strong>~{number(summary.tool_result_tokens_estimated)}</strong><small>estimated from result bytes</small></div>
          <div><span>Model output</span><strong>{number(summary.model_output_tokens)}</strong><small>reported completion tokens</small></div>
          <div><span>Successful calls</span><strong>{number(summary.successful_model_calls)}</strong><small>{number(summary.rate_limit_retries)} rate-limit retries</small></div>
        </div>
      </section>

      <div className="agent-analysis-grid">
        <section className="agent-panel">
          <PanelHeading eyebrow="Trend" title="Run volume" hint="Daily task outcomes" />
          {trend.length ? <Chart data={trend.map((item) => ({ label: item.day.slice(5, 10), value: item.runs }))} title="Runs per day" color="#0f766e" xKey="label" xType="category" /> : <p className="agent-empty">No trend data in this time range.</p>}
        </section>
        <section className="agent-panel">
          <PanelHeading eyebrow="Tail behavior" title="Model latency distribution" hint="Completed model calls" />
          {latencyDistribution.length ? <div className="agent-distribution">{latencyDistribution.map((item) => <div className="agent-distribution-row" key={item.bucket}><span>{item.bucket}</span><div><i style={{ width: `${(item.calls / maxLatencyCalls) * 100}%` }} /></div><strong>{number(item.calls)}</strong></div>)}</div> : <p className="agent-empty">No latency data in this time range.</p>}
        </section>
        <section className="agent-panel">
          <PanelHeading eyebrow="Signal mix" title="Event types" />
          {eventTypes.length ? <div className="agent-signal-list">{eventTypes.slice(0, 8).map((item) => <div key={item.event_type}><span>{item.event_type}</span><strong>{number(item.events)}</strong></div>)}</div> : <p className="agent-empty">No event data in this time range.</p>}
        </section>
        <section className="agent-panel">
          <PanelHeading eyebrow="Reliability" title="Failure reasons" />
          {errors.length ? <div className="agent-signal-list agent-error-list">{errors.map((item) => <div key={item.reason}><span>{item.reason}</span><strong>{number(item.events)}</strong></div>)}</div> : <p className="agent-empty">No recorded failures in this time range.</p>}
        </section>
      </div>

      <div className="agent-layout">
        <section className="agent-panel agent-wide">
          <PanelHeading eyebrow="Comparison" title="Agent profiles" hint="Completion is higher-is-better; steps are lower-is-better." />
          {profiles.length ? <div className="agent-table-scroll"><table className="agent-table"><thead><tr><th>Profile</th><th>Runtime</th><th>Model</th><th>Runs</th><th>Completion</th><th>Failed</th><th>Avg steps</th></tr></thead><tbody>{profiles.map((item) => <tr key={`${item.profile_name}-${item.model}-${item.framework_version}`}><td>{item.profile_name}</td><td>{item.framework_version}</td><td>{item.model}</td><td>{number(item.tasks)}</td><td><span className="agent-rate">{percent(item.completion_rate)}</span></td><td>{number(item.failed)}</td><td>{Number(item.avg_steps || 0).toFixed(1)}</td></tr>)}</tbody></table></div> : <p className="agent-empty">No profile telemetry in this time range.</p>}
        </section>

        <section className="agent-panel">
          <PanelHeading eyebrow="Execution surface" title="Tool behavior" />
          {tools.length ? <div className="agent-tool-list">{tools.map((item) => <div className="agent-tool" key={item.tool_name}><div><strong>{item.tool_name}</strong><span>{number(item.calls)} calls</span></div><div><span>{milliseconds(item.avg_latency_ms)}</span><span className={item.failures ? "agent-bad" : "agent-good"}>{number(item.failures)} failed</span></div></div>)}</div> : <p className="agent-empty">No tool telemetry in this time range.</p>}
        </section>

        <section className="agent-panel agent-wide">
          <PanelHeading eyebrow="Drill-down" title="Recent runs" hint="Content payloads are intentionally excluded." />
          {runs.length ? <div className="agent-table-scroll agent-runs-scroll"><table className="agent-table agent-runs"><thead><tr><th>Status</th><th>Profile</th><th>Model</th><th>Created</th><th>Steps</th><th>Model calls</th><th>Tool calls</th><th>Failures</th><th /></tr></thead><tbody>{runs.map((run) => <tr key={run.task_id}><td><span className={`agent-status ${run.status}`}>{run.status}</span></td><td>{run.profile_name || "unknown"}<small>{run.framework_version}</small></td><td>{run.model}</td><td>{run.created_at ? new Date(run.created_at).toLocaleString() : "n/a"}</td><td>{number(run.steps)}</td><td>{number(run.model_calls)}</td><td>{number(run.tool_calls)}</td><td className={run.failures ? "agent-bad" : ""}>{number(run.failures)}</td><td><button className="agent-inspect" onClick={() => openRun(run.task_id)}>Inspect</button></td></tr>)}</tbody></table></div> : <p className="agent-empty">No agent runs in this time range.</p>}
        </section>
      </div>

      {selectedRun && <div className="agent-inspector"><div className="agent-inspector-heading"><PanelHeading eyebrow="Run trace" title="Structural event timeline" hint="Scroll to inspect the full run." /><button className="agent-close" onClick={() => { setSelectedRun(null); setRunDetail(null); }}>Close</button></div>{isRunLoading && <p className="agent-empty">Loading run events...</p>}{runDetail?.error && <p className="agent-error-text">{runDetail.error}</p>}{runDetail?.run && <div className="agent-run-meta"><span>{runDetail.run.profile_name}</span><span>{runDetail.run.model}</span><span className={`agent-status ${runDetail.run.status}`}>{runDetail.run.status}</span></div>}{runDetail?.events?.length > 0 && <EventGraph events={runDetail.events} />}{runDetail?.events?.length === 0 && !isRunLoading && <p className="agent-empty">This run has no persisted events.</p>}</div>}
    </div>
  );
};

export default AgentAnalytics;
