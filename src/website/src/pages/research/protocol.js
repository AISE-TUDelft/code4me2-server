// Helpers for authoring a StudyProtocolV1 document in the browser.
//
// The backend validates protocols with `extra="forbid"`, so the form -> JSON
// builder below emits only fields that exist on the Pydantic model. The form is
// intentionally a *lossy* convenience view: a value the researcher leaves blank
// becomes an explicit `null` (meaning "inherit the server default"), while the
// literal token `unknown` becomes the typed ExplicitUnknown marker the protocol
// distinguishes from null.
//
// See code4me2-server/src/research/study/protocol/models.py for the contract.

import CryptoJS from "crypto-js";

export const SCHEMA_VERSION = "1";

export const SCHEDULE_KINDS = [
  { value: "FIXED", label: "Fixed window (absolute start/end)" },
  { value: "ROLLING", label: "Rolling window (duration from enrollment)" },
];

export const ASSIGNMENT_UNITS = [{ value: "ENROLLMENT", label: "Enrollment" }];

export const ASSIGNMENT_STRATEGIES = [
  { value: "WEIGHTED_RANDOM", label: "Weighted random" },
  { value: "DETERMINISTIC_HASH", label: "Deterministic hash" },
  { value: "STRATIFIED", label: "Stratified" },
];

export const TELEMETRY_FIELD_CLASSES = [
  { value: "STRUCTURAL", label: "Structural" },
  { value: "CONTENT", label: "Content" },
  { value: "METRICS", label: "Metrics" },
  { value: "DIAGNOSTICS", label: "Diagnostics" },
];

export const RETENTION_ACTIONS = [
  { value: "RETAIN_ANONYMIZED", label: "Retain anonymized" },
  { value: "DELETE_IDENTIFIABLE", label: "Delete identifiable" },
];

export const COMPLETION_POLICIES = [
  { value: "MANUAL", label: "Manual" },
  { value: "TARGET_CAPACITY", label: "Target capacity" },
  { value: "SCHEDULE_END", label: "Schedule end" },
];

const UNKNOWN_TOKEN = "unknown";

const blankToNull = (value) => {
  if (value === undefined || value === null) return null;
  const trimmed = String(value).trim();
  return trimmed === "" ? null : trimmed;
};

const toIntOrNull = (value) => {
  if (value === undefined || value === null || String(value).trim() === "") {
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.trunc(parsed) : null;
};

const toFloatOrNull = (value) => {
  if (value === undefined || value === null || String(value).trim() === "") {
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};

// "" -> null (inherit), the literal "unknown" -> ExplicitUnknown, otherwise int.
export const parseCapacity = (value) => {
  const trimmed = String(value ?? "").trim();
  if (trimmed === "") return null;
  if (trimmed.toLowerCase() === UNKNOWN_TOKEN) return { kind: "UNKNOWN" };
  return toIntOrNull(trimmed);
};

export const parseHostKind = (value) => {
  const trimmed = String(value ?? "").trim();
  if (trimmed === "") return null;
  if (trimmed.toLowerCase() === UNKNOWN_TOKEN) return { kind: "UNKNOWN" };
  return trimmed;
};

const parseTriBool = (value) => {
  if (value === "" || value === undefined || value === null) return null;
  if (value === true || value === "true") return true;
  if (value === false || value === "false") return false;
  return null;
};

export const splitList = (value) => {
  if (!value) return [];
  return String(value)
    .split(/[,\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
};

const localDateTimeToIso = (value) => {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
};

const isoToLocalInput = (value) => {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
};

// Distribution modes for the admin profile editor. A profile IS a distribution:
// PACKAGED pins an immutable registry release, BYOA_EXTERNAL pins a
// participant-installed package/command. Researchers never pick a mode; the
// server resolves it from the distribution they select.
export const DISTRIBUTION_MODES = [
  { value: "PACKAGED", label: "Packaged (digest-pinned artifact)" },
  { value: "BYOA_EXTERNAL", label: "BYOA (participant-installed agent)" },
];

/**
 * A blank condition. The researcher makes exactly one pick — the distribution —
 * and the server resolves everything else (release, artifact, mode, verified
 * state). Identity fields are convenience labels only.
 */
export const emptyCondition = () => ({
  distributionId: "",
  conditionId: "",
  name: "",
  weight: "1",
  adapterVersion: "",
  model: "",
  frameworkVersion: "",
});

export const emptyProtocolForm = (studyId = "") => ({
  studyId,
  name: "",
  description: "",
  owner: "",
  scheduleKind: "FIXED",
  startAt: isoToLocalInput(new Date().toISOString()),
  endAt: "",
  durationSeconds: "",
  capacity: "",
  allowReentry: "",
  assignmentUnit: "ENROLLMENT",
  strategy: "WEIGHTED_RANDOM",
  stratification: "",
  conditions: [],
  idleTimeoutSeconds: "",
  resumeGraceSeconds: "",
  heartbeatSeconds: "",
  telemetryClasses: ["STRUCTURAL", "METRICS"],
  retentionAction: "DELETE_IDENTIFIABLE",
  retentionDays: "",
  localeRefs: "",
  expectedProtocolVersion: "1",
  requiredCapabilities: [],
  hostKind: "",
  completionPolicy: "MANUAL",
  targetEnrollments: "",
});

/**
 * Convert an existing StudyProtocolV1 JSON document back into form state.
 * Unknown/extra fields are dropped rather than echoed (the form is a view).
 */
export const protocolToForm = (protocol = {}, studyId = "") => {
  const base = emptyProtocolForm(studyId);
  if (!protocol || typeof protocol !== "object") return base;

  const metadata = protocol.metadata || {};
  const schedule = protocol.schedule || {};
  const enrollment = protocol.enrollment || {};
  const assignment = protocol.assignment || {};
  const session = protocol.session_policy || {};
  const telemetry = protocol.telemetry_policy || {};
  const privacy = protocol.privacy_policy || {};
  const environment = protocol.environment_requirements || {};
  const completion = protocol.completion || {};

  const capacityToInput = (value) => {
    if (value === null || value === undefined) return "";
    if (typeof value === "object" && value.kind === "UNKNOWN") return "unknown";
    return String(value);
  };

  return {
    ...base,
    studyId: protocol.study_id || studyId,
    name: metadata.name || "",
    description: metadata.description || "",
    owner: metadata.owner || "",
    scheduleKind: schedule.kind || "FIXED",
    startAt: isoToLocalInput(schedule.start_at) || base.startAt,
    endAt: isoToLocalInput(schedule.end_at),
    durationSeconds:
      schedule.duration_seconds === null || schedule.duration_seconds === undefined
        ? ""
        : String(schedule.duration_seconds),
    capacity: capacityToInput(enrollment.capacity),
    allowReentry:
      enrollment.allow_reentry === null || enrollment.allow_reentry === undefined
        ? ""
        : String(enrollment.allow_reentry),
    assignmentUnit: assignment.unit || "ENROLLMENT",
    strategy: assignment.strategy || "WEIGHTED_RANDOM",
    stratification: (assignment.stratification || []).join(", "),
    conditions: (protocol.conditions || []).map((condition) => {
      const frozen = condition.resolved_distribution || {};
      const overrides = condition.declared_overrides || {};
      return {
        // The single authoritative link: the opaque distribution id. The name
        // is only the display label.
        distributionId: condition.distribution_id || "",
        conditionId: condition.condition_id || "",
        name: condition.name || overrides.agent_profile || frozen.agent_id || "",
        weight: condition.weight === undefined ? "1" : String(condition.weight),
        adapterVersion: condition.adapter_version || "",
        model: overrides.model || "",
        frameworkVersion: overrides.framework_version || "",
      };
    }),
    idleTimeoutSeconds: session.idle_timeout_seconds ?? "",
    resumeGraceSeconds: session.resume_grace_seconds ?? "",
    heartbeatSeconds: session.heartbeat_seconds ?? "",
    telemetryClasses: telemetry.allowed_field_classes || [],
    retentionAction: privacy.retention_action || "DELETE_IDENTIFIABLE",
    retentionDays: privacy.retention_days ?? "",
    expectedProtocolVersion:
      environment.expected_protocol_version || base.expectedProtocolVersion,
    requiredCapabilities: (environment.required_capabilities || []).map(
      (item) => ({
        capability: item.capability || "",
        requireState: item.require_state || "SUPPORTED",
      }),
    ),
    hostKind:
      environment.host_kind &&
      typeof environment.host_kind === "object" &&
      environment.host_kind.kind === "UNKNOWN"
        ? "unknown"
        : environment.host_kind || "",
    completionPolicy: completion.policy || "MANUAL",
    targetEnrollments: completion.target_enrollments ?? "",
  };
};

/**
 * Build the StudyProtocolV1 request body from form state.
 *
 * Only model fields are emitted (extra="forbid"); blank strings become null.
 */
export const buildProtocolFromForm = (form) => {
  const isRolling = form.scheduleKind === "ROLLING";
  const conditions = (form.conditions || []).map((condition) => ({
    condition_id: (condition.conditionId || "").trim(),
    name: blankToNull(condition.name),
    weight: toFloatOrNull(condition.weight),
    // The single pick. The server resolves the distribution into a frozen
    // `resolved_distribution` at publication; the client never authors it.
    distribution_id: blankToNull(condition.distributionId),
    adapter_version: blankToNull(condition.adapterVersion),
    declared_overrides:
      condition.model || condition.frameworkVersion
        ? {
            agent_profile:
              condition.name || condition.conditionId || null,
            model: condition.model || null,
            framework_version: condition.frameworkVersion || null,
          }
        : {},
  }));

  return {
    schema_version: SCHEMA_VERSION,
    study_id: (form.studyId || "").trim(),
    metadata: {
      name: (form.name || "").trim(),
      description: blankToNull(form.description),
      owner: blankToNull(form.owner),
    },
    schedule: isRolling
      ? {
          kind: "ROLLING",
          duration_seconds: toIntOrNull(form.durationSeconds),
        }
      : {
          kind: "FIXED",
          start_at: localDateTimeToIso(form.startAt),
          end_at: localDateTimeToIso(form.endAt),
        },
    enrollment: {
      capacity: parseCapacity(form.capacity),
      allow_reentry: parseTriBool(form.allowReentry),
    },
    assignment: {
      unit: form.assignmentUnit || "ENROLLMENT",
      strategy: form.strategy || "WEIGHTED_RANDOM",
      stratification:
        form.strategy === "STRATIFIED" ? splitList(form.stratification) : null,
    },
    conditions,
    session_policy: {
      idle_timeout_seconds: toIntOrNull(form.idleTimeoutSeconds),
      resume_grace_seconds: toIntOrNull(form.resumeGraceSeconds),
      heartbeat_seconds: toIntOrNull(form.heartbeatSeconds),
    },
    telemetry_policy: {
      allowed_field_classes: form.telemetryClasses || [],
    },
    privacy_policy: {
      retention_action: form.retentionAction,
      retention_days: toIntOrNull(form.retentionDays),
    },
    environment_requirements: {
      expected_protocol_version: blankToNull(form.expectedProtocolVersion),
      required_capabilities: (form.requiredCapabilities || []).map((item) => ({
        capability: (item.capability || "").trim(),
        require_state: item.requireState || "SUPPORTED",
        evidence_ref: null,
      })),
      host_kind: parseHostKind(form.hostKind),
    },
    completion: {
      policy: form.completionPolicy || "MANUAL",
      target_enrollments:
        form.completionPolicy === "TARGET_CAPACITY"
          ? toIntOrNull(form.targetEnrollments)
          : null,
    },
    survey_hooks: [],
  };
};

/** Index backend ValidationError entries by their dotted field path. */
export const indexErrorsByField = (errors = []) => {
  const index = {};
  errors.forEach((error) => {
    const field = error.field || "";
    if (!index[field]) index[field] = [];
    index[field].push(error);
  });
  return index;
};

/** All field paths whose key equals or nests under `prefix`. */
export const errorsForPrefix = (index, prefix) =>
  Object.entries(index)
    .filter(
      ([field]) =>
        field === prefix ||
        field.startsWith(`${prefix}.`) ||
        field.startsWith(`${prefix}[`),
    )
    .flatMap(([field, list]) =>
      list.map((error) => ({ ...error, field })),
    );

export const shortId = (value, size = 8) =>
  value ? `${String(value).slice(0, size)}…` : "—";

export const formatDateTime = (value) => {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
};
