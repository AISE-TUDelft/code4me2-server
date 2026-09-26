import React from "react";
import { colorForIndex } from "../../components/charts/Charts";
import { formatDuration } from "../../utils/format";

export const STATUS_LABELS = {
  DRAFT: "Draft",
  ACTIVE: "Active",
  STUDY_STOPPED: "Stopped",
};

export const statusClass = (status) =>
  `research-status research-status-${String(status || "DRAFT").toLowerCase()}`;

export const ENROLLMENT_STATUS = {
  ACTIVE: { label: "Active", tone: "success" },
  COMPLETED: { label: "Completed", tone: "info" },
  REVOKED: { label: "Revoked", tone: "danger" },
  STUDY_STOPPED: { label: "Study stopped", tone: "neutral" },
};

export const HEALTH = {
  ACTIVE: { label: "Active this week", tone: "success" },
  IDLE: { label: "Idle", tone: "warning" },
  NO_TELEMETRY: { label: "No telemetry yet", tone: "neutral" },
  INACTIVE: { label: "Inactive", tone: "neutral" },
};

// What content capture stores (the server consent text lists the same).
export const CONTENT_DESCRIPTION = "prompts, model responses and reasoning, tool arguments and output, and file contents";

export const TELEMETRY_CLASS_LABELS = {
  STRUCTURAL: "Agent and session structure (which events and tools ran)",
  METRICS: "Usage and timings (tokens, durations, counts)",
  DIAGNOSTICS: "Errors and diagnostics",
  CODE_METADATA: "File types and languages (code metadata, no code)",
  CONTENT: `${CONTENT_DESCRIPTION.charAt(0).toUpperCase()}${CONTENT_DESCRIPTION.slice(1)} (sensitive)`,
};

// What an empty policy resolves to on the server (metadata only).
export const DEFAULT_TELEMETRY_CLASSES = ["STRUCTURAL", "METRICS", "DIAGNOSTICS", "CODE_METADATA"];

// The runtime enforces four classes; the study vocabulary is coarser than it
// looks (STRUCTURAL and DIAGNOSTICS both enable all agent-activity metadata).
// Mirrors ``_declared_field_classes`` / ``from_study_policy`` in
// research/telemetry/privacy/engine.py (names are upper-cased, not trimmed).
const STUDY_TO_RUNTIME = {
  STRUCTURAL: ["SYSTEM", "BEHAVIORAL"],
  METRICS: ["SYSTEM"],
  DIAGNOSTICS: ["BEHAVIORAL"],
};
const RUNTIME_CLASSES = ["BEHAVIORAL", "SYSTEM", "CODE_METADATA", "CONTENT"];
export const DEFAULT_RUNTIME_CLASSES = ["BEHAVIORAL", "SYSTEM", "CODE_METADATA"];

/**
 * The runtime classes a declared policy resolves to (what is allowed in
 * clear). SECRET and unknown names are ignored, and nothing usable means the
 * default. Content is only stored when the study also sets content_capture.
 */
export const resolveFieldClasses = (declared) => {
  const resolved = new Set();
  (Array.isArray(declared) ? declared : []).forEach((raw) => {
    if (typeof raw !== "string") return;
    const name = raw.toUpperCase();
    const classes = STUDY_TO_RUNTIME[name] || (RUNTIME_CLASSES.includes(name) ? [name] : []);
    classes.forEach((item) => resolved.add(item));
  });
  const list = RUNTIME_CLASSES.filter((name) => resolved.has(name));
  return list.length ? list : [...DEFAULT_RUNTIME_CLASSES];
};

// What each entry stores, stated without softening. Every event's record
// (type, time, correlation ids, token usage, timings, counts) is kept whatever
// the policy; the classes govern its payload, and the built-in agent's own
// reports are stored without the fields a policy excludes. BEHAVIORAL keeps
// tool titles and error messages verbatim, and code metadata that is not
// allowed is still stored, as unsalted hashes.
export const RUNTIME_CLASS_LABELS = {
  EVENTS: "Event records: which agent and IDE events happened and when, with timings, counts, sizes and token usage",
  BEHAVIORAL:
    "Activity details: tools run, approvals, stop reasons, IDE run phases and errors, including tool titles and error messages, which can contain full command lines, file paths, search terms and URLs",
  SYSTEM:
    "System details: can include timings, token counts, edit sizes, exit codes, software versions and platform (OS, architecture, host)",
  CODE_METADATA: "Code metadata: file types and languages, and can include file paths, symbol and repository names (no file contents)",
  CODE_METADATA_HASHED:
    "Code metadata (file types and languages, and any paths or symbols) stored as unsalted hashes, which are easy to reverse for common values",
  CONTENT: `Content: ${CONTENT_DESCRIPTION} (sensitive)`,
};

export const RUNTIME_CLASS_SHORT_LABELS = {
  EVENTS: "event records",
  BEHAVIORAL: "activity details",
  SYSTEM: "system details",
  CODE_METADATA: "code metadata",
  CODE_METADATA_HASHED: "hashed code metadata",
  CONTENT: "content (prompts, responses, tool arguments and output, file contents)",
};

/**
 * What a study stores, in display order. Event records are always kept; code
 * metadata is always stored, in clear when allowed and otherwise hashed;
 * content only with content_capture (and the participant's consent).
 */
export const collectedClasses = (policy) => {
  const declared = Array.isArray(policy?.allowed_field_classes) ? policy.allowed_field_classes : [];
  const resolved = resolveFieldClasses(declared);
  const entries = ["EVENTS", ...resolved.filter((name) => name === "BEHAVIORAL" || name === "SYSTEM")];
  entries.push(resolved.includes("CODE_METADATA") ? "CODE_METADATA" : "CODE_METADATA_HASHED");
  if (policy?.content_capture === true) entries.push("CONTENT");
  return entries;
};

export const RUNTIME_LABELS = {
  "code4me2-agent": "Built-in",
  goose: "Goose",
  codex: "Codex",
};

/**
 * The study's arms in their frozen selection order, each with a stable colour
 * slot (colour follows the arm, never its rank in a filtered view).
 */
export const armsForStudy = (study, analyticsArms = []) => {
  const byId = new Map();
  (study?.profile_selections || []).forEach((selection, index) => {
    byId.set(selection.profile_id, {
      profile_id: selection.profile_id,
      name: selection.name || selection.profile_id,
      model: selection.model || "",
      framework_version: selection.framework_version || "",
      selection_order: selection.selection_order ?? index,
    });
  });
  (analyticsArms || []).forEach((arm, index) => {
    const existing = byId.get(arm.profile_id) || {};
    byId.set(arm.profile_id, {
      ...existing,
      ...Object.fromEntries(Object.entries(arm).filter(([, value]) => value !== null && value !== undefined)),
      name: arm.name || existing.name || arm.profile_id,
      selection_order: existing.selection_order ?? arm.selection_order ?? index,
    });
  });
  return Array.from(byId.values())
    .sort((a, b) => (a.selection_order ?? 0) - (b.selection_order ?? 0))
    .map((arm, index) => ({ ...arm, color: colorForIndex(index), index }));
};

/**
 * A tool's reported name. The server withholds names that are really tool-call
 * titles (they can name files or quote commands); those read as "Unnamed".
 */
export const ToolName = ({ name }) =>
  name ? (
    <span className="ui-mono">{name}</span>
  ) : (
    <span className="ui-subtle" title="The agent reported a title that can name files or commands, so it is not shown.">
      Unnamed
    </span>
  );

export const armColor = (arms, profileId) => {
  const arm = arms.find((item) => item.profile_id === profileId);
  return arm ? arm.color : "var(--viz-muted)";
};

export const describeSessionPolicy = (policy) => {
  if (!policy || typeof policy !== "object") return [];
  const rows = [];
  if (policy.idle_timeout_seconds !== undefined) {
    rows.push(["Idle timeout", formatDuration(policy.idle_timeout_seconds)]);
  }
  if (policy.resume_grace_seconds !== undefined) {
    rows.push(["Resume grace", formatDuration(policy.resume_grace_seconds)]);
  }
  if (policy.heartbeat_seconds !== undefined) {
    rows.push(["Heartbeat", formatDuration(policy.heartbeat_seconds)]);
  }
  return rows;
};

const csvCell = (value) => {
  if (value === null || value === undefined) return "";
  const text = String(value);
  // Neutralise spreadsheet formula injection and quote when needed.
  const safe = /^[=+\-@\t\r]/.test(text) ? `'${text}` : text;
  return /[",\n]/.test(safe) ? `"${safe.replace(/"/g, '""')}"` : safe;
};

export const downloadCsv = (filename, columns, rows) => {
  const lines = [columns.map((column) => csvCell(column.label)).join(",")];
  rows.forEach((row) => {
    lines.push(columns.map((column) => csvCell(column.value(row))).join(","));
  });
  const blob = new Blob([`${lines.join("\n")}\n`], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};

export const slugify = (value) =>
  String(value || "study")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60) || "study";

// Runtimes that spend from the study's shared provider key through the
// metered relay (a budget per participant applies). Codex signs in with
// ChatGPT and is not metered. Mirrors METERED_FRAMEWORKS in
// research/study/agents/enums.py.
export const METERED_RUNTIMES = ["goose", "code4me2-agent"];

export const isMeteredRuntime = (framework) =>
  METERED_RUNTIMES.includes(String(framework || "").trim().toLowerCase());

export const LIMIT_SOURCE_LABELS = {
  STUDY_DEFAULT: "Study default",
  ADJUSTED: "Adjusted",
};

export const ADJUSTMENT_LABELS = {
  TOP_UP: "Top-up",
  SET_LIMIT: "New limit",
  APPLY_DEFAULT: "Study default applied",
};

/**
 * One key per attempt: the server replays a reused key instead of applying
 * an adjustment twice. Uses crypto.randomUUID where available and always
 * matches the server's ^[A-Za-z0-9_-]{8,128}$.
 */
export const newIdempotencyKey = (prefix = "web") => {
  const cryptoApi = typeof window !== "undefined" ? window.crypto : undefined;
  const random =
    cryptoApi && typeof cryptoApi.randomUUID === "function"
      ? cryptoApi.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}-${Math.random().toString(36).slice(2, 12)}`;
  return `${prefix}-${random}`.replace(/[^A-Za-z0-9_-]/g, "").slice(0, 128);
};
