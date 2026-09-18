import React from "react";
import "./AgentResults.css";

// Headline metrics rendered as side-by-side comparison bars. `better` documents
// the direction researchers care about (it drives the legend, not the layout).
//
// Shapes match the /api/analytics/studies/{id}/agent-evaluation response, which
// aggregates the canonical research_event authority — so an arm running Goose, one
// running Codex, and one running the built-in runtime are all directly
// comparable here.
const BAR_METRICS = [
  {
    key: "completion_rate",
    label: "Task completion rate",
    better: "higher",
    format: (v) => `${((v || 0) * 100).toFixed(1)}%`,
  },
  {
    key: "avg_steps",
    label: "Avg steps / task",
    better: "lower",
    format: (v) => (v == null ? "n/a" : v.toFixed(1)),
  },
  {
    key: "avg_model_latency_ms",
    label: "Avg model-call latency",
    better: "lower",
    format: (v) => (v == null || v === 0 ? "n/a" : `${Math.round(v)}ms`),
  },
];

const armKey = (arm) => arm.profile_id || arm.profile_name;

const ComparisonBars = ({ metric, arms }) => {
  const values = arms.map((a) => (a.metrics || {})[metric.key]);
  const max = Math.max(...values.filter((v) => v != null), 0);

  return (
    <div className="agent-bar-metric">
      <div className="agent-bar-metric-header">
        <span className="agent-bar-label">{metric.label}</span>
        <span className="agent-bar-better">
          {metric.better === "higher" ? "↑ better" : "↓ better"}
        </span>
      </div>
      {arms.map((arm) => {
        const value = (arm.metrics || {})[metric.key];
        const pct = max > 0 && value != null ? (value / max) * 100 : 0;
        return (
          <div key={armKey(arm)} className="agent-bar-row">
            <span className="agent-bar-arm" title={arm.profile_name}>
              {arm.profile_name}
              {arm.is_baseline && <span className="baseline-tag">Baseline</span>}
            </span>
            <div className="agent-bar-track">
              <div
                className={`agent-bar-fill ${arm.is_baseline ? "baseline" : ""}`}
                style={{ width: `${pct}%` }}
              />
            </div>
            <span className="agent-bar-value">{metric.format(value)}</span>
          </div>
        );
      })}
    </div>
  );
};

// The accept / reject / modify breakdown. Called out on its own because it is
// the only metric that reports what developers did with the agent's output
// rather than how much output there was — an arm that proposes more edits but
// gets fewer accepted is performing worse, not better.
const EditDecisionPanel = ({ arms }) => {
  const anyEdits = arms.some((a) => (a.metrics || {}).total_edits > 0);
  if (!anyEdits) return null;

  return (
    <div className="agent-edit-panel">
      <h5>Human-in-the-loop decisions</h5>
      <p className="agent-edit-hint">
        What developers did with each arm's proposed file edits. Undecided edits
        are excluded from the acceptance rate.
      </p>
      <table className="profiles-table">
        <thead>
          <tr>
            <th>Arm</th>
            <th>Proposed</th>
            <th>Accepted</th>
            <th>Rejected</th>
            <th>Modified first</th>
            <th>Acceptance</th>
          </tr>
        </thead>
        <tbody>
          {arms.map((arm) => {
            const m = arm.metrics || {};
            return (
              <tr key={armKey(arm)}>
                <td>
                  {arm.profile_name}
                  {arm.is_baseline && (
                    <span className="baseline-tag">Baseline</span>
                  )}
                </td>
                <td>{m.total_edits || 0}</td>
                <td>{m.accepted_edits || 0}</td>
                <td>{m.rejected_edits || 0}</td>
                <td>{m.modified_edits || 0}</td>
                <td>{`${((m.edit_acceptance_rate || 0) * 100).toFixed(1)}%`}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
};

const Uplift = ({ label, value, lowerIsBetter }) => {
  if (value == null) return null;
  const isGood = lowerIsBetter ? value <= 0 : value >= 0;
  return (
    <div className="comparison-item">
      <span className="comparison-label">{label}:</span>
      <span className={`comparison-value ${isGood ? "positive" : "negative"}`}>
        {value > 0 ? "+" : ""}
        {value.toFixed(1)}%
      </span>
    </div>
  );
};

const ArmCard = ({ arm }) => {
  const m = arm.metrics || {};
  const avgTokens = (total) =>
    m.total_tasks > 0 ? Math.round((total || 0) / m.total_tasks) : null;

  return (
    <div className="config-result">
      <div className="config-header">
        <span className="config-name">
          {arm.profile_name}
          {arm.is_baseline && <span className="baseline-tag">Baseline</span>}
        </span>
        <span className="config-model">
          {arm.model}
          {arm.framework_version && (
            <span className="agent-runtime-chip">{arm.framework_version}</span>
          )}
        </span>
      </div>

      <div className="config-metrics">
        <div className="metric-row">
          <span className="metric-label">Tasks:</span>
          <span className="metric-value">
            {m.total_tasks || 0} ({m.completed_tasks || 0} completed)
          </span>
        </div>
        <div className="metric-row">
          <span className="metric-label">Failed tasks:</span>
          <span className="metric-value">{m.failed_tasks || 0}</span>
        </div>
        <div className="metric-row">
          <span className="metric-label">Completion:</span>
          <span className="metric-value">
            {`${((m.completion_rate || 0) * 100).toFixed(1)}%`}
          </span>
        </div>
        <div className="metric-row">
          <span className="metric-label">Participants:</span>
          <span className="metric-value">{m.total_participants || 0}</span>
        </div>
        <div className="metric-row">
          <span className="metric-label">Model events:</span>
          <span className="metric-value">
            {m.model_requests || 0} requested / {m.model_calls || 0} completed
          </span>
        </div>
        <div className="metric-row">
          <span className="metric-label">Tool events:</span>
          <span className="metric-value">
            {m.tool_requests || 0} requested / {m.tool_calls || 0} completed
          </span>
        </div>
        <div className="metric-row">
          <span className="metric-label">Telemetry failures:</span>
          <span className="metric-value">{m.failures || 0}</span>
        </div>
        <div className="metric-row">
          <span className="metric-label">Avg steps:</span>
          <span className="metric-value">
            {m.avg_steps == null ? "n/a" : m.avg_steps.toFixed(1)}
          </span>
        </div>
        <div className="metric-row">
          <span className="metric-label">Avg latency:</span>
          <span className="metric-value">
            {!m.avg_model_latency_ms
              ? "n/a"
              : `${Math.round(m.avg_model_latency_ms)}ms`}
          </span>
        </div>
        <div className="metric-row">
          <span className="metric-label">Avg tokens (in/out):</span>
          <span className="metric-value">
            {avgTokens(m.total_input_tokens)?.toLocaleString() ?? "n/a"} /{" "}
            {avgTokens(m.total_output_tokens)?.toLocaleString() ?? "n/a"}
          </span>
        </div>
        <div className="metric-row">
          <span className="metric-label">Total tokens:</span>
          <span className="metric-value">
            {(m.total_tokens || 0).toLocaleString()}
          </span>
        </div>
        <div className="metric-row">
          <span className="metric-label">Edits accepted:</span>
          <span className="metric-value">unavailable</span>
        </div>
      </div>

      {arm.uplift && (
        <div className="baseline-comparison">
          <Uplift
            label="Completion vs baseline"
            value={arm.uplift.completion_rate_change_pct}
          />
          <Uplift
            label="Steps"
            value={arm.uplift.avg_steps_change_pct}
            lowerIsBetter
          />
          <Uplift
            label="Latency"
            value={arm.uplift.latency_change_pct}
            lowerIsBetter
          />
        </div>
      )}
    </div>
  );
};

const AgentResults = ({ data, profiles }) => {
  if (!data) {
    return (
      <div className="agent-results">
        <h4>Agent Results</h4>
        <p className="form-hint">
          Loading agent telemetry… If this persists, no agent data has been
          collected for this study's arms yet.
        </p>
      </div>
    );
  }

  const arms = data.results || [];
  if (arms.length === 0) {
    return (
      <div className="agent-results">
        <h4>Agent Results</h4>
        <p className="form-hint">
          {profiles?.length || 0} agent profile(s) attached, but no agent tasks
          ran within the study window yet.
        </p>
      </div>
    );
  }

  const hasAnyTasks = arms.some((a) => (a.metrics || {}).total_tasks > 0);

  return (
    <div className="agent-results">
      <h4>Agent Results</h4>
      {!hasAnyTasks && (
        <p className="form-hint">
          No agent tasks have run for these arms within the study window yet —
          metrics will populate as developers use the agent.
        </p>
      )}

      <div className="agent-bars">
        {BAR_METRICS.map((metric) => (
          <ComparisonBars key={metric.key} metric={metric} arms={arms} />
        ))}
      </div>

      <EditDecisionPanel arms={arms} />

      <div className="results-grid">
        {arms.map((arm) => (
          <ArmCard key={armKey(arm)} arm={arm} />
        ))}
      </div>
    </div>
  );
};

export default AgentResults;
