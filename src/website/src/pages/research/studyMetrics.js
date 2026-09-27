import { formatCompact, formatDuration, formatNumber, formatPercent, humanize } from "../../utils/format";

const count = (value) => formatNumber(value, { maximumFractionDigits: 0 });
const decimal1 = (value) => formatNumber(value, { maximumFractionDigits: 1 });
const decimal2 = (value) => formatNumber(value, { maximumFractionDigits: 2 });
const percent = (value) => formatPercent(value, 0);
const hours = (value) => (value === null || value === undefined ? "—" : formatDuration(Number(value) * 3600));

/**
 * Participant-level metrics computed by the study analytics read model. The
 * definitions follow the agent-trace literature (SWE-chat and follow-ups):
 * the participant is the randomisation unit, so arms are compared on
 * per-participant values.
 */
export const METRICS = [
  { key: "prompts", label: "Prompts", format: count, help: "Prompts the participant sent to the agent." },
  { key: "active_days", label: "Active days", format: count, help: "Distinct days with at least one prompt." },
  { key: "session_hours", label: "Session time", format: hours, help: "Wall-clock time inside research sessions." },
  {
    key: "prompts_per_session_hour",
    label: "Prompts per session hour",
    format: decimal1,
    help: "How intensively the agent was used while working.",
  },
  {
    key: "tool_calls_per_prompt",
    label: "Tool calls per prompt",
    format: decimal1,
    help: "Agent actions (reads, edits, commands…) per prompt — autonomy per turn.",
  },
  { key: "tool_failure_rate", label: "Tool failure rate", format: percent, help: "Share of tool calls that failed." },
  {
    key: "auto_run_share",
    label: "Tool calls without approval",
    format: percent,
    help: "Share of tool calls that ran without a permission request.",
  },
  {
    key: "permission_denial_rate",
    label: "Approval denial rate",
    format: percent,
    help: "Rejected / (approved + rejected) permission requests.",
  },
  {
    key: "median_permission_wait_seconds",
    label: "Median approval wait",
    format: formatDuration,
    help: "Time from a permission request to the participant's decision.",
  },
  {
    key: "cancel_rate",
    label: "Interrupted turns",
    format: percent,
    help: "Share of turns the participant cancelled — a proxy for dissatisfaction or course correction.",
  },
  {
    key: "median_turn_seconds",
    label: "Median turn duration",
    format: formatDuration,
    help: "From sending a prompt to the agent finishing its reply.",
  },
  {
    key: "tokens_per_prompt",
    label: "Tokens per prompt",
    format: formatCompact,
    help: "Provider-reported tokens per completed turn (only when the agent reports usage).",
  },
  { key: "errors_per_prompt", label: "Errors per prompt", format: decimal2, help: "Agent, proxy and crash errors per prompt." },
  {
    key: "agent_writes_per_prompt",
    label: "Agent file writes per prompt",
    format: decimal2,
    help: "Files the agent wrote per prompt: per turn, the larger of its successful edit calls and its IDE file writes (one write is often seen as both).",
  },
  {
    key: "seconds_to_first_agent_edit",
    label: "Time to first agent edit",
    format: formatDuration,
    help: "Median per session: first prompt → first agent file change (implementation onset).",
  },
  {
    key: "ide_edits_per_session_hour",
    label: "Developer edits per session hour",
    format: decimal1,
    help: "The participant's own document edits in the IDE per session hour.",
  },
  {
    key: "plan_completion_rate",
    label: "Plan completion",
    format: percent,
    help: "Share of plan steps marked completed at the end of a turn (agents that publish plans).",
  },
];

export const METRIC_BY_KEY = Object.fromEntries(METRICS.map((metric) => [metric.key, metric]));

export const formatMetric = (key, value) => {
  const metric = METRIC_BY_KEY[key];
  if (value === null || value === undefined) return "—";
  return metric ? metric.format(value) : formatNumber(value);
};

export const TOOL_KIND_ORDER = ["read", "search", "edit", "delete", "move", "execute", "fetch", "think", "other"];

export const STOP_REASON_ORDER = ["end_turn", "cancelled", "max_tokens", "max_turn_requests", "refusal"];

export const DECISION_ORDER = ["allow", "reject", "cancelled", "unknown"];

export const STOP_REASON_LABELS = {
  end_turn: "Finished",
  cancelled: "Cancelled by user",
  max_tokens: "Hit token limit",
  max_turn_requests: "Hit step limit",
  refusal: "Refused",
};

export const DECISION_LABELS = {
  allow: "Approved",
  reject: "Rejected",
  cancelled: "Cancelled",
  unknown: "Unknown",
};

export const labelFor = (labels, key) => labels[key] || humanize(key);

/** Stable category order: known keys first (in order), then the rest by name. */
export const orderedKeys = (keys, order) => {
  const unique = Array.from(new Set(keys.filter(Boolean)));
  const known = order.filter((key) => unique.includes(key));
  const rest = unique.filter((key) => !order.includes(key)).sort();
  return [...known, ...rest];
};
