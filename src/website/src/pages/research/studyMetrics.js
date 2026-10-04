import { formatCompact, formatDuration, formatNumber, formatPercent, humanize } from "../../utils/format";

const count = (value) => formatNumber(value, { maximumFractionDigits: 0 });
const decimal1 = (value) => formatNumber(value, { maximumFractionDigits: 1 });
const decimal2 = (value) => formatNumber(value, { maximumFractionDigits: 2 });
const percent = (value) => formatPercent(value, 0);
const hours = (value) => (value === null || value === undefined ? "—" : formatDuration(Number(value) * 3600));

/**
 * Metric categories, in display order. The analytics page shows one
 * collapsible section per category; `defaultOpen` sets the first-visit state.
 */
export const METRIC_CATEGORIES = [
  { key: "engagement", label: "Engagement", defaultOpen: true, help: "How much and how intensively the agent was used." },
  { key: "autonomy", label: "Agent autonomy", defaultOpen: true, help: "How much the agent did per prompt and how fast it got there." },
  {
    key: "oversight",
    label: "Human oversight",
    defaultOpen: false,
    help: "Approvals, rejections, revisions and interruptions: how participants steered the agent.",
  },
  { key: "reliability", label: "Reliability & cost", defaultOpen: false, help: "Failures, errors and token use." },
  { key: "chats", label: "Chats", defaultOpen: false, help: "How participants organised their work into chats." },
];

/**
 * Participant-level metrics computed by the study analytics read model. The
 * definitions follow the agent-trace literature (SWE-chat and follow-ups):
 * the participant is the randomisation unit, so arms are compared on
 * per-participant values.
 */
export const METRICS = [
  { key: "prompts", category: "engagement", label: "Prompts", format: count, help: "Prompts the participant sent to the agent." },
  { key: "active_days", category: "engagement", label: "Active days", format: count, help: "Distinct days with at least one prompt." },
  { key: "session_hours", category: "engagement", label: "Session time", format: hours, help: "Wall-clock time inside research sessions." },
  {
    key: "prompts_per_session_hour", category: "engagement",
    label: "Prompts per session hour",
    format: decimal1,
    help: "How intensively the agent was used while working.",
  },
  {
    key: "tool_calls_per_prompt", category: "autonomy",
    label: "Tool calls per prompt",
    format: decimal1,
    help: "Agent actions (reads, edits, commands…) per prompt — autonomy per turn.",
  },
  { key: "tool_failure_rate", category: "reliability", label: "Tool failure rate", format: percent, help: "Share of tool calls that failed." },
  {
    key: "auto_run_share", category: "autonomy",
    label: "Tool calls without approval",
    format: percent,
    help: "Share of tool calls that ran without a permission request.",
  },
  {
    key: "permission_denial_rate", category: "oversight",
    label: "Approval denial rate",
    format: percent,
    help: "Rejected / (approved + rejected) permission requests.",
  },
  {
    key: "median_permission_wait_seconds", category: "oversight",
    label: "Median approval wait",
    format: formatDuration,
    help: "Time from a permission request to the participant's decision.",
  },
  {
    key: "cancel_rate", category: "oversight",
    label: "Interrupted turns",
    format: percent,
    help: "Share of turns the participant cancelled — a proxy for dissatisfaction or course correction.",
  },
  {
    key: "median_turn_seconds", category: "autonomy",
    label: "Median turn duration",
    format: formatDuration,
    help: "From sending a prompt to the agent finishing its reply.",
  },
  {
    key: "tokens_per_prompt", category: "reliability",
    label: "Tokens per prompt",
    format: formatCompact,
    help: "Provider-reported tokens per completed turn (only when the agent reports usage).",
  },
  { key: "errors_per_prompt", category: "reliability", label: "Errors per prompt", format: decimal2, help: "Agent, proxy and crash errors per prompt." },
  {
    key: "agent_writes_per_prompt", category: "autonomy",
    label: "Agent file writes per prompt",
    format: decimal2,
    help: "Files the agent wrote per prompt: per turn, the larger of its successful edit calls and its IDE file writes (one write is often seen as both).",
  },
  {
    key: "seconds_to_first_agent_edit", category: "autonomy",
    label: "Time to first agent edit",
    format: formatDuration,
    help: "Median per session: first prompt → first agent file change (implementation onset).",
  },
  {
    key: "ide_edits_per_session_hour", category: "engagement",
    label: "Developer edits per session hour",
    format: decimal1,
    help: "The participant's own document edits in the IDE per session hour.",
  },
  {
    key: "plan_completion_rate", category: "autonomy",
    label: "Plan completion",
    format: percent,
    help: "Share of plan steps marked completed at the end of a turn (agents that publish plans).",
  },
  {
    key: "revision_rate",
    category: "oversight",
    label: "Revise chosen",
    format: percent,
    help: "Share of answered approval requests where the participant chose Revise… to keep part of the change and have the rest redone (counted when chosen, even if the follow-up form was then dismissed). Only arms whose agent offers it; empty for the others.",
  },
  {
    key: "reprompt_after_rejection_rate",
    category: "oversight",
    label: "Re-prompt after rejection",
    format: percent,
    help: "Share of rejected approval requests followed by another prompt in the same chat (works for every agent).",
  },
  {
    key: "chats_opened",
    category: "chats",
    label: "Chats opened",
    format: count,
    help: "New chats in which the participant sent a prompt; reopening an earlier chat, or a chat the IDE opened without a prompt, is not counted.",
  },
  {
    key: "prompts_per_chat",
    category: "chats",
    label: "Prompts per chat",
    format: decimal1,
    help: "Prompts per chat that received at least one prompt.",
  },
];

export const metricsInCategory = (category) => METRICS.filter((metric) => metric.category === category);

export const METRIC_BY_KEY = Object.fromEntries(METRICS.map((metric) => [metric.key, metric]));

export const formatMetric = (key, value) => {
  const metric = METRIC_BY_KEY[key];
  if (value === null || value === undefined) return "—";
  return metric ? metric.format(value) : formatNumber(value);
};

export const TOOL_KIND_ORDER = ["read", "search", "edit", "delete", "move", "execute", "fetch", "think", "other"];

export const STOP_REASON_ORDER = ["end_turn", "cancelled", "max_tokens", "max_turn_requests", "refusal"];

export const DECISION_ORDER = ["allow", "reject", "revise", "cancelled", "unknown"];

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
  revise: "Revise chosen",
  cancelled: "Cancelled",
  unknown: "Unknown",
};

// How a chat ended (research.telemetry.chat_lifecycle). IntelliJ ends a chat's
// agent process when the chat is deleted from its history (and on IDE exit).
export const CHAT_END_LABELS = {
  close: "Closed by the IDE",
  host_closed: "IDE ended the agent (chat deleted or IDE closed)",
  agent_exited: "Agent exited",
  signal_terminated: "Agent stopped by a signal (chat deleted or IDE closed)",
  session_stale: "Research session expired",
  open: "Still open",
  unknown: "Unknown (older telemetry)",
};

export const labelFor = (labels, key) => labels[key] || humanize(key);

/** Stable category order: known keys first (in order), then the rest by name. */
export const orderedKeys = (keys, order) => {
  const unique = Array.from(new Set(keys.filter(Boolean)));
  const known = order.filter((key) => unique.includes(key));
  const rest = unique.filter((key) => !order.includes(key)).sort();
  return [...known, ...rest];
};
