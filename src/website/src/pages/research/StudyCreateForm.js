import React, { useMemo, useState } from "react";
import Icon from "../../components/common/Icon";
import { Alert, Badge, MoneyInput } from "../../components/common/ui";
import { formatDateTime, parseUsdInput } from "../../utils/format";
import { ConsentReview } from "./ConsentText";
import {
  DEFAULT_TELEMETRY_CLASSES,
  RUNTIME_CLASS_SHORT_LABELS,
  RUNTIME_LABELS,
  TELEMETRY_CLASS_LABELS,
  collectedClasses,
  isMeteredRuntime,
  resolveFieldClasses,
} from "./studyUtils";

// The server validates and freezes a complete session policy on create
// (SESSION_POLICY_INVALID otherwise), so the form never defaults to {}.
export const DEFAULT_SESSION_POLICY = {
  idle_timeout_seconds: 600,
  resume_grace_seconds: 120,
  heartbeat_seconds: 30,
};

const SESSION_PRESETS = {
  standard: {
    label: "Standard — end after 10 min idle, resumable for 2 min",
    policy: DEFAULT_SESSION_POLICY,
  },
  long: {
    label: "Long — end after 60 min idle, resumable for 15 min",
    policy: { idle_timeout_seconds: 3600, resume_grace_seconds: 900, heartbeat_seconds: 60 },
  },
};

// Content is stored only when the frozen policy carries content_capture=true
// (research/telemetry/content_policy.py); listing CONTENT alone is not enough.
// "Everything" is the metadata default plus content, so no structural field
// the dashboards rely on is dropped at ingestion.
const TELEMETRY_PRESETS = {
  metadata: {},
  everything: {
    allowed_field_classes: [...DEFAULT_TELEMETRY_CLASSES, "CONTENT"],
    content_capture: true,
  },
};

const METADATA_CLASSES = DEFAULT_TELEMETRY_CLASSES;
// The study vocabulary offered when authoring (the API also accepts runtime names).
const AUTHORING_CLASSES = [...DEFAULT_TELEMETRY_CLASSES, "CONTENT"];

export const parsePolicyDraft = (text) => {
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (_) {
    return { ok: false, error: "Invalid JSON. Create stays disabled until it parses." };
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { ok: false, error: "The policy must be a JSON object." };
  }
  return { ok: true, value: parsed };
};

const asPolicyObject = (policy) => (policy && typeof policy === "object" && !Array.isArray(policy) ? policy : {});

// Stored policies come back in the database's key order (PostgreSQL JSONB), so
// presets are matched key-order-insensitively.
const canonical = (value) => {
  if (Array.isArray(value)) return value.map(canonical);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.keys(value)
      .sort()
      .map((key) => [key, canonical(value[key])]),
  );
};

const sameJson = (a, b) => JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));

const presetForSession = (policy) =>
  Object.entries(SESSION_PRESETS).find(([, preset]) => sameJson(preset.policy, policy))?.[0] || "custom";

const presetForTelemetry = (policy) =>
  Object.entries(TELEMETRY_PRESETS).find(([, preset]) => sameJson(preset, policy))?.[0] || "custom";

const storedList = (policy) =>
  collectedClasses(policy)
    .map((name) => RUNTIME_CLASS_SHORT_LABELS[name])
    .join(", ");

const toLocalDateTime = (value) => (value ? new Date(value) : null);

// Limits the server enforces on a custom consent form (research/study/consent.py).
export const CONSENT_LIMITS = { document: 20000, statement: 500, statements: 20 };
const STATEMENT_ID_PATTERN = /^[a-z0-9][a-z0-9_-]{0,31}$/;

let statementKeySeed = 0;
const newStatement = (fields = {}) => {
  statementKeySeed += 1;
  return { key: `statement-${statementKeySeed}`, id: "", text: "", required: true, ...fields };
};

const initialStatements = (consent) =>
  consent && Array.isArray(consent.statements) && consent.custom
    ? consent.statements.map((statement) => newStatement(statement))
    : [newStatement({ text: "I have read the information above and agree to take part in this study." })];

/** Statement ids: kept when valid and unique, otherwise s1, s2, … */
export const statementPayload = (statements) => {
  const used = new Set();
  return statements.map((statement, index) => {
    let id = STATEMENT_ID_PATTERN.test(statement.id || "") && !used.has(statement.id) ? statement.id : "";
    for (let next = index + 1; !id; next += 1) {
      if (!used.has(`s${next}`)) id = `s${next}`;
    }
    used.add(id);
    return { id, text: statement.text.trim(), required: Boolean(statement.required) };
  });
};

/** The first problem with a custom consent form, or "" when it is complete. */
export const consentFormError = (document, statements) => {
  if (!document.trim()) return "Write the consent document participants read before joining.";
  if (document.length > CONSENT_LIMITS.document) return "The consent document is limited to 20,000 characters.";
  if (!statements.length) return "Add at least one statement for participants to tick.";
  if (statements.some((statement) => !statement.text.trim())) return "Every statement needs text.";
  if (statements.some((statement) => statement.text.length > CONSENT_LIMITS.statement)) {
    return "Statements are limited to 500 characters.";
  }
  if (!statements.some((statement) => statement.required)) return "At least one statement must be required.";
  return "";
};

/**
 * Create a Draft study, or — with `cloneSource` — a duplicate of any study:
 * every field is prefilled from the source and stays editable, and it is
 * submitted through the normal create (so validation and the frozen digest are
 * the same). `budgetError` is the server's typed message for the budget field
 * (BUDGET_REQUIRED / BUDGET_INVALID / BUDGET_PRICE_MISSING).
 */
const StudyCreateForm = ({ profiles, cloneSource, isBusy, onSubmit, onCancel, budgetError = "" }) => {
  const initialTelemetry = cloneSource ? asPolicyObject(cloneSource.telemetry_policy) : {};
  // A source without a frozen session policy (none echoed) falls back to the
  // default: the server refuses an empty one.
  const sourceSession = cloneSource ? asPolicyObject(cloneSource.session_policy) : {};
  const initialSession = Object.keys(sourceSession).length ? sourceSession : DEFAULT_SESSION_POLICY;
  // Participant budget: the source's default when cloning (a 0 default means
  // none was set), otherwise empty until a metered arm is selected.
  const sourceBudget = cloneSource ? asPolicyObject(cloneSource.budget_policy) : {};
  const [defaultBudgetUsd, setDefaultBudgetUsd] = useState(
    Number(sourceBudget.default_budget_micro_usd) > 0 && sourceBudget.default_budget_usd
      ? String(sourceBudget.default_budget_usd)
      : "",
  );
  const [warningPercent, setWarningPercent] = useState(
    Number(sourceBudget.warning_fraction) > 0 ? String(Math.round(Number(sourceBudget.warning_fraction) * 100)) : "80",
  );
  const activeProfiles = useMemo(() => profiles.filter((profile) => profile.is_active !== false), [profiles]);
  // A duplicate keeps the source's arms that can still be selected; the others
  // are named in an alert (their profiles were deactivated or are not visible).
  const sourceSelections = cloneSource && Array.isArray(cloneSource.profile_selections) ? cloneSource.profile_selections : [];
  const droppedArms = sourceSelections.filter(
    (selection) => !activeProfiles.some((profile) => profile.profile_id === selection.profile_id),
  );
  const [form, setForm] = useState({
    name: cloneSource ? `${cloneSource.name} (copy)` : "",
    description: cloneSource ? cloneSource.description || "" : "",
    startsAt: "",
    endsAt: "",
    profileIds: sourceSelections
      .map((selection) => selection.profile_id)
      .filter((profileId) => activeProfiles.some((profile) => profile.profile_id === profileId)),
  });
  const [allowManualAssignment, setAllowManualAssignment] = useState(
    cloneSource?.assignment_policy?.manual_override === true,
  );
  const [consentMode, setConsentMode] = useState(cloneSource?.consent?.custom ? "custom" : "standard");
  const [consentDocument, setConsentDocument] = useState(
    cloneSource?.consent?.custom ? cloneSource.consent.document || "" : "",
  );
  const [statements, setStatements] = useState(() => initialStatements(cloneSource?.consent));
  // Raw drafts are the source of truth: invalid input stays visible and the
  // submitted policy is always exactly what the draft parses to (ISSUE-05).
  const [telemetryPolicyText, setTelemetryPolicyText] = useState(JSON.stringify(initialTelemetry));
  const [sessionPolicyText, setSessionPolicyText] = useState(JSON.stringify(initialSession));
  const [telemetryPreset, setTelemetryPreset] = useState(
    cloneSource ? presetForTelemetry(initialTelemetry) : "metadata",
  );
  const [sessionPreset, setSessionPreset] = useState(presetForSession(initialSession));
  const [dateError, setDateError] = useState("");

  const telemetryDraft = parsePolicyDraft(telemetryPolicyText);
  const sessionDraft = parsePolicyDraft(sessionPolicyText);
  const telemetryPolicyError = telemetryDraft.ok ? "" : telemetryDraft.error;
  const sessionPolicyError = sessionDraft.ok ? "" : sessionDraft.error;
  const telemetryClasses = telemetryDraft.ok && Array.isArray(telemetryDraft.value.allowed_field_classes)
    ? telemetryDraft.value.allowed_field_classes
    : [];

  const consentError = consentMode === "custom" ? consentFormError(consentDocument, statements) : "";

  // Budgets apply only when a selected arm runs Goose or the built-in agent
  // (they spend from the study's shared key); a metered model without a
  // server price would refuse every call, so creation is blocked until an
  // administrator prices it.
  const selectedProfiles = activeProfiles.filter((profile) => form.profileIds.includes(profile.profile_id));
  const meteredProfiles = selectedProfiles.filter((profile) => isMeteredRuntime(profile.framework_version));
  const metered = meteredProfiles.length > 0;
  const unpricedProfiles = meteredProfiles.filter((profile) => profile.model_priced === false);
  const budgetDraft = parseUsdInput(defaultBudgetUsd);
  const budgetValid = budgetDraft.ok && budgetDraft.micro > 0;
  const budgetFieldError = !metered || !defaultBudgetUsd.trim()
    ? ""
    : !budgetDraft.ok
      ? budgetDraft.error
      : budgetDraft.micro <= 0
        ? "The budget must be greater than zero."
        : "";
  const warningValue = Number(warningPercent);
  const warningValid = Number.isInteger(warningValue) && warningValue >= 1 && warningValue <= 100;
  const budgetMessage = budgetFieldError || budgetError;

  const applyTelemetryPreset = (preset) => {
    setTelemetryPreset(preset);
    if (TELEMETRY_PRESETS[preset]) {
      setTelemetryPolicyText(JSON.stringify(TELEMETRY_PRESETS[preset]));
    } else if (preset === "custom" && telemetryClasses.length === 0) {
      // Start a custom policy from the explicit metadata classes.
      setTelemetryPolicyText(JSON.stringify({ allowed_field_classes: METADATA_CLASSES }));
    }
  };

  const toggleTelemetryClass = (name) => {
    const current = telemetryDraft.ok ? telemetryDraft.value : {};
    const list = Array.isArray(current.allowed_field_classes) ? current.allowed_field_classes : [];
    const next = list.includes(name) ? list.filter((item) => item !== name) : [...list, name];
    const policy = { ...current, allowed_field_classes: next };
    // Only the sensitive category switches content capture on or off.
    if (name === "CONTENT") {
      if (next.includes("CONTENT")) policy.content_capture = true;
      else delete policy.content_capture;
    }
    setTelemetryPolicyText(JSON.stringify(policy));
    setTelemetryPreset("custom");
  };

  const applySessionPreset = (preset) => {
    setSessionPreset(preset);
    if (SESSION_PRESETS[preset]) setSessionPolicyText(JSON.stringify(SESSION_PRESETS[preset].policy));
  };

  const updateStatement = (key, fields) =>
    setStatements((current) => current.map((statement) => (statement.key === key ? { ...statement, ...fields } : statement)));

  const toggleProfile = (profileId, checked) => {
    setForm((current) => ({
      ...current,
      profileIds: checked
        ? [...current.profileIds, profileId]
        : current.profileIds.filter((id) => id !== profileId),
    }));
  };

  const handleSubmit = (event) => {
    event.preventDefault();
    if (!telemetryDraft.ok || !sessionDraft.ok || consentError) return;
    if (form.startsAt && form.endsAt) {
      const starts = toLocalDateTime(form.startsAt);
      const ends = toLocalDateTime(form.endsAt);
      if (starts && ends && ends <= starts) {
        setDateError("The end must be after the start.");
        return;
      }
    }
    setDateError("");
    if (metered && (!budgetValid || unpricedProfiles.length > 0)) return;
    onSubmit({
      ...form,
      telemetryPolicy: telemetryDraft.value,
      sessionPolicy: sessionDraft.value,
      // Empty/null without a metered arm: the server ignores them for
      // Codex-only studies.
      defaultBudgetUsd: metered ? budgetDraft.value : "",
      budgetWarningFraction: metered ? warningValue / 100 : null,
      allowManualAssignment,
      consent:
        consentMode === "custom" ? { document: consentDocument, statements: statementPayload(statements) } : null,
    });
  };

  const submitDisabled =
    isBusy ||
    !form.name.trim() ||
    form.profileIds.length === 0 ||
    Boolean(telemetryPolicyError) ||
    Boolean(sessionPolicyError) ||
    Boolean(consentError) ||
    (metered && (!budgetValid || unpricedProfiles.length > 0 || !warningValid));

  return (
    <form className="research-card ui-card study-create-form" onSubmit={handleSubmit} aria-labelledby="study-create-title">
      <div className="ui-card-header">
        <div>
          <h3 className="ui-card-title" id="study-create-title">
            {cloneSource ? "Duplicate study" : "New study"}
          </h3>
          <p className="ui-card-subtitle">
            {cloneSource
              ? `Everything below is copied from “${cloneSource.name}” and can be changed. The duplicate starts as a Draft with its own join code.`
              : "The configuration is frozen when the study is created; only the name and description stay editable until the first participant consents."}
          </p>
        </div>
        <button type="button" className="icon-button" onClick={onCancel} aria-label="Close" disabled={isBusy}>
          <Icon name="x" size={18} />
        </button>
      </div>

      <div className="ui-card-body study-create-body">
        {cloneSource ? (
          <p className="research-hint">
            Participants, consent records, assignments, telemetry data, the schedule and the join code are not copied.
          </p>
        ) : null}
        <section className="study-create-section">
          <h4 className="ui-section-title">Basics</h4>
          <div className="ui-form-grid">
            <div className="ui-field ui-span-2">
              <label className="ui-label" htmlFor="study-name">
                Name
              </label>
              <input
                id="study-name"
                className="ui-input"
                value={form.name}
                onChange={(event) => setForm({ ...form, name: event.target.value })}
                required
                disabled={isBusy}
                placeholder="e.g. Context window pilot"
              />
            </div>
            <div className="ui-field ui-span-2">
              <label className="ui-label" htmlFor="study-description">
                Description
              </label>
              <textarea
                id="study-description"
                className="ui-textarea"
                value={form.description}
                onChange={(event) => setForm({ ...form, description: event.target.value })}
                rows={3}
                disabled={isBusy}
                placeholder="Shown to participants when they review the study."
              />
            </div>
            <div className="ui-field">
              <label className="ui-label" htmlFor="study-starts">
                Starts at
              </label>
              <input
                id="study-starts"
                className="ui-input"
                type="datetime-local"
                value={form.startsAt}
                onChange={(event) => setForm({ ...form, startsAt: event.target.value })}
                disabled={isBusy}
              />
            </div>
            <div className="ui-field">
              <label className="ui-label" htmlFor="study-ends">
                Ends at
              </label>
              <input
                id="study-ends"
                className="ui-input"
                type="datetime-local"
                value={form.endsAt}
                onChange={(event) => setForm({ ...form, endsAt: event.target.value })}
                disabled={isBusy}
                aria-invalid={dateError ? "true" : undefined}
              />
              {dateError ? <p className="ui-field-error">{dateError}</p> : null}
              <p className="ui-hint">Enrollments complete automatically once the end has passed.</p>
              {cloneSource && (cloneSource.starts_at || cloneSource.ends_at) ? (
                <p className="ui-hint">
                  The source ran {cloneSource.starts_at ? `from ${formatDateTime(cloneSource.starts_at)}` : "from creation"}
                  {cloneSource.ends_at ? ` until ${formatDateTime(cloneSource.ends_at)}` : " with no end"}.
                </p>
              ) : null}
            </div>
          </div>
        </section>

        <fieldset className="research-policy ui-fieldset">
          <legend>Telemetry policy</legend>
          <div className="ui-option-list">
            <label className={`ui-option-card${telemetryPreset === "metadata" ? " is-selected" : ""}`}>
              <input
                type="radio"
                name="telemetry-preset"
                checked={telemetryPreset === "metadata"}
                onChange={() => applyTelemetryPreset("metadata")}
                disabled={isBusy}
              />
              <span className="ui-check-text">
                <strong>
                  Metadata only — how the agent ran, timings and errors. No prompt, response or file text; tool
                  titles and error messages are kept. (default)
                </strong>
                <small>Recommended unless the research question needs the content itself.</small>
              </span>
            </label>
            <label className={`ui-option-card${telemetryPreset === "everything" ? " is-selected" : ""}`}>
              <input
                type="radio"
                name="telemetry-preset"
                checked={telemetryPreset === "everything"}
                onChange={() => applyTelemetryPreset("everything")}
                disabled={isBusy}
              />
              <span className="ui-check-text">
                <strong>
                  Everything — also collect prompts, model responses and reasoning, tool arguments and output, and
                  file contents.
                </strong>
                <small>Participants see this in the consent notice. Stored only after they consent.</small>
              </span>
            </label>
            <label className={`ui-option-card${telemetryPreset === "custom" ? " is-selected" : ""}`}>
              <input
                type="radio"
                name="telemetry-preset"
                checked={telemetryPreset === "custom"}
                onChange={() => applyTelemetryPreset("custom")}
                disabled={isBusy}
              />
              <span className="ui-check-text">
                <strong>Custom — choose which categories are collected.</strong>
              </span>
            </label>
          </div>
          {telemetryPreset === "custom" ? (
            <div className="research-policy-classes">
              {AUTHORING_CLASSES.map((name) => (
                <label key={name} className="ui-check">
                  <input
                    type="checkbox"
                    checked={telemetryClasses.includes(name)}
                    onChange={() => toggleTelemetryClass(name)}
                    disabled={isBusy || !telemetryDraft.ok}
                  />
                  <span className="ui-check-text">{TELEMETRY_CLASS_LABELS[name]}</span>
                </label>
              ))}
              <p className="research-hint">
                Content is stored only when content capture is on (selecting the sensitive category turns it on)
                and the participant has consented. Provider credentials are never collected: Goose and built-in
                arms use the study's shared provider key on the server, and Codex signs in with the participant's
                ChatGPT account.
              </p>
              {telemetryDraft.ok ? (
                <p className="research-hint">Stored at runtime: {storedList(telemetryDraft.value)}.</p>
              ) : null}
              {!resolveFieldClasses(telemetryClasses).includes("BEHAVIORAL") ? (
                <p className="ui-field-error">
                  Without agent and session structure the study dashboards cannot identify prompts, tool calls or
                  approvals; the built-in agent's own reports keep only their timings and token counts.
                </p>
              ) : null}
            </div>
          ) : null}
          {telemetryPolicyError ? (
            <p id="telemetry-policy-error" className="research-error" role="alert">
              {telemetryPolicyError}
            </p>
          ) : null}
          <details className="research-advanced">
            <summary>Advanced: raw JSON</summary>
            <textarea
              className="ui-textarea ui-mono"
              aria-label="Telemetry policy (JSON)"
              value={telemetryPolicyText}
              onChange={(event) => {
                setTelemetryPolicyText(event.target.value);
                setTelemetryPreset("custom");
              }}
              rows={2}
              disabled={isBusy}
              aria-invalid={Boolean(telemetryPolicyError)}
              aria-describedby={telemetryPolicyError ? "telemetry-policy-error" : undefined}
            />
          </details>
        </fieldset>

        <fieldset className="research-policy ui-fieldset">
          <legend>Session policy</legend>
          <div className="ui-field">
            <label className="ui-label" htmlFor="study-session-preset">
              Session length
            </label>
            <select
              id="study-session-preset"
              className="ui-select"
              value={sessionPreset}
              onChange={(event) => applySessionPreset(event.target.value)}
              disabled={isBusy}
            >
              {Object.entries(SESSION_PRESETS).map(([value, preset]) => (
                <option key={value} value={value}>
                  {preset.label}
                </option>
              ))}
              <option value="custom">Custom (edit the JSON below)</option>
            </select>
            <p className="ui-hint">
              A session ends after the idle timeout; activity within the resume grace continues the same session.
            </p>
          </div>
          {sessionPolicyError ? (
            <p id="session-policy-error" className="research-error" role="alert">
              {sessionPolicyError}
            </p>
          ) : null}
          <details className="research-advanced" open={sessionPreset === "custom"}>
            <summary>Advanced: raw JSON</summary>
            <textarea
              className="ui-textarea ui-mono"
              aria-label="Session policy (JSON)"
              value={sessionPolicyText}
              onChange={(event) => {
                setSessionPolicyText(event.target.value);
                const parsed = parsePolicyDraft(event.target.value);
                setSessionPreset(parsed.ok ? presetForSession(parsed.value) : "custom");
              }}
              rows={2}
              disabled={isBusy}
              aria-invalid={Boolean(sessionPolicyError)}
              aria-describedby={sessionPolicyError ? "session-policy-error" : undefined}
            />
          </details>
        </fieldset>

        <fieldset className="research-profile-selection ui-fieldset">
          <legend>Agent profiles (arms)</legend>
          <p className="ui-hint">
            Each participant is assigned one selected profile (see Assignment below). Profile selection is fixed once
            the study is created.
          </p>
          {droppedArms.length > 0 ? (
            <Alert tone="warning" live={false} title="Some arms of the source study cannot be selected.">
              {droppedArms.map((arm) => arm.name || arm.profile_id).join(", ")}: the profile is no longer active. Arms
              are frozen again from each profile's current settings.
            </Alert>
          ) : null}
          {activeProfiles.length === 0 ? (
            <p className="research-hint">No active agent profiles available.</p>
          ) : (
            <div className="ui-option-list study-profile-options">
              {activeProfiles.map((profile) => {
                const checked = form.profileIds.includes(profile.profile_id);
                return (
                  <label key={profile.profile_id} className={`ui-option-card${checked ? " is-selected" : ""}`}>
                    <input
                      type="checkbox"
                      aria-label={profile.name}
                      checked={checked}
                      onChange={(event) => toggleProfile(profile.profile_id, event.target.checked)}
                      disabled={isBusy}
                    />
                    <span className="ui-check-text">
                      <span className="ui-row">
                        <strong>{profile.name}</strong>
                        {profile.framework_version ? (
                          <Badge tone={profile.framework_version === "code4me2-agent" ? "primary" : "violet"}>
                            {RUNTIME_LABELS[profile.framework_version] || profile.framework_version}
                          </Badge>
                        ) : null}
                        {profile.verified === false ? <Badge tone="warning">Unverified release</Badge> : null}
                        {isMeteredRuntime(profile.framework_version) && profile.model_priced === false ? (
                          <Badge tone="danger" title="Ask an administrator to price this model on its provider connection">
                            Price missing
                          </Badge>
                        ) : null}
                      </span>
                      <small>{profile.model}</small>
                    </span>
                  </label>
                );
              })}
            </div>
          )}
          {form.profileIds.length > 0 ? (
            <p className="ui-hint">
              {form.profileIds.length} arm{form.profileIds.length === 1 ? "" : "s"} selected
              {form.profileIds.length === 1 ? " — a single-arm study has no comparison." : "."}
            </p>
          ) : null}
        </fieldset>

        <fieldset className="research-policy ui-fieldset">
          <legend>Assignment</legend>
          <p className="ui-hint">
            Each participant gets one arm at random with equal probability. The draw is a salted hash of the study and
            the participant's enrollment, so it can be reproduced from exported data, and it never changes once the
            participant has started.
          </p>
          <label className="ui-check">
            <input
              type="checkbox"
              checked={allowManualAssignment}
              onChange={(event) => setAllowManualAssignment(event.target.checked)}
              disabled={isBusy}
            />
            <span className="ui-check-text">
              Allow manual assignment
              <small>
                You can set a participant's arm by hand until they first use the agent. The consent notice then says
                participants are assigned “at random or by the research team”.
              </small>
            </span>
          </label>
        </fieldset>

        <fieldset className="research-policy ui-fieldset">
          <legend>Consent form</legend>
          <div className="ui-option-list">
            <label className={`ui-option-card${consentMode === "standard" ? " is-selected" : ""}`}>
              <input
                type="radio"
                name="consent-mode"
                checked={consentMode === "standard"}
                onChange={() => setConsentMode("standard")}
                disabled={isBusy}
              />
              <span className="ui-check-text">
                <strong>Standard notice — what this study records, with one checkbox. (default)</strong>
              </span>
            </label>
            <label className={`ui-option-card${consentMode === "custom" ? " is-selected" : ""}`}>
              <input
                type="radio"
                name="consent-mode"
                checked={consentMode === "custom"}
                onChange={() => setConsentMode("custom")}
                disabled={isBusy}
              />
              <span className="ui-check-text">
                <strong>Custom form — your own information text and statements participants tick.</strong>
                <small>The platform's notice of what the study records is always shown below your text.</small>
              </span>
            </label>
          </div>
          {consentMode === "custom" ? (
            <div className="ui-stack-sm">
              <div className="ui-field">
                <label className="ui-label" htmlFor="study-consent-document">
                  Consent document
                </label>
                <textarea
                  id="study-consent-document"
                  className="ui-textarea"
                  value={consentDocument}
                  onChange={(event) => setConsentDocument(event.target.value)}
                  rows={10}
                  disabled={isBusy}
                  placeholder="Purpose of the study, what participants do, risks, data protection, contact…"
                />
                <p className="ui-hint">
                  Plain text, shown exactly as typed: line breaks are kept and web or mail links become clickable.{" "}
                  {consentDocument.length.toLocaleString()} / {CONSENT_LIMITS.document.toLocaleString()} characters.
                </p>
              </div>
              <div className="ui-field">
                <span className="ui-label">Statements participants tick</span>
                {statements.map((statement, index) => (
                  <div key={statement.key} className="consent-statement-row">
                    <input
                      className="ui-input"
                      aria-label={`Statement ${index + 1}`}
                      value={statement.text}
                      onChange={(event) => updateStatement(statement.key, { text: event.target.value })}
                      disabled={isBusy}
                    />
                    <label className="ui-check">
                      <input
                        type="checkbox"
                        aria-label={`Statement ${index + 1} is required`}
                        checked={statement.required}
                        onChange={(event) => updateStatement(statement.key, { required: event.target.checked })}
                        disabled={isBusy}
                      />
                      <span className="ui-check-text">Required</span>
                    </label>
                    <button
                      type="button"
                      className="ghost-button button-sm"
                      aria-label={`Remove statement ${index + 1}`}
                      onClick={() => setStatements((current) => current.filter((item) => item.key !== statement.key))}
                      disabled={isBusy || statements.length === 1}
                    >
                      Remove
                    </button>
                  </div>
                ))}
                <div className="ui-row">
                  <button
                    type="button"
                    className="secondary-button button-sm"
                    onClick={() => setStatements((current) => [...current, newStatement({ required: false })])}
                    disabled={isBusy || statements.length >= CONSENT_LIMITS.statements}
                  >
                    Add statement
                  </button>
                </div>
                {consentError ? <p className="ui-field-error">{consentError}</p> : null}
              </div>
              <details className="research-advanced">
                <summary>Preview what participants see</summary>
                <ConsentReview
                  consent={{
                    document: consentDocument,
                    notice: "The platform adds its notice of what this study records here.",
                    statements: statementPayload(statements),
                  }}
                />
              </details>
              <p className="ui-hint">
                The form is frozen with the study. To change it later, duplicate the study (an ethics amendment usually
                means new consent anyway).
              </p>
            </div>
          ) : null}
        </fieldset>

        {form.profileIds.length > 0 ? (
          <fieldset className="research-policy ui-fieldset study-budget-fieldset">
            <legend>Participant budgets</legend>
            {metered ? (
              <>
                <p className="ui-hint">
                  Goose and built-in arms spend from the study's shared provider key on the server, within this budget
                  per participant; individual participants can be topped up later. Codex arms sign in with ChatGPT and
                  are not metered.
                </p>
                <div className="ui-form-grid">
                  <div className="ui-field">
                    <label className="ui-label" htmlFor="study-default-budget">
                      Default budget per participant (USD)
                    </label>
                    <MoneyInput
                      id="study-default-budget"
                      value={defaultBudgetUsd}
                      onChange={setDefaultBudgetUsd}
                      disabled={isBusy}
                      required
                      invalid={Boolean(budgetMessage)}
                      describedBy={budgetMessage ? "study-default-budget-error" : undefined}
                    />
                    {budgetMessage ? (
                      <p id="study-default-budget-error" className="ui-field-error" role={budgetError ? "alert" : undefined}>
                        {budgetMessage}
                      </p>
                    ) : null}
                    <p className="ui-hint">
                      Applies to every participant who joins; changeable later in Settings, even after the consent lock.
                    </p>
                  </div>
                  <div className="ui-field">
                    <label className="ui-label" htmlFor="study-budget-warning">
                      Warn participants at (% of budget used)
                    </label>
                    <input
                      id="study-budget-warning"
                      className="ui-input"
                      type="number"
                      min={1}
                      max={100}
                      step={1}
                      value={warningPercent}
                      onChange={(event) => setWarningPercent(event.target.value)}
                      disabled={isBusy}
                      aria-invalid={warningValid ? undefined : "true"}
                    />
                    {!warningValid ? <p className="ui-field-error">Enter a whole number from 1 to 100.</p> : null}
                  </div>
                </div>
                {unpricedProfiles.length > 0 ? (
                  <Alert tone="warning" live={false} title="A selected model has no price on the server.">
                    {unpricedProfiles.map((profile) => `${profile.model || "the model"} (${profile.name})`).join(", ")}: ask
                    an administrator to price this model on its provider connection. Every call from that arm would be
                    refused, so the study cannot be created until it is priced.
                  </Alert>
                ) : null}
              </>
            ) : (
              <p className="ui-hint">
                No budget needed: the selected arms run Codex, which signs in with the participant's ChatGPT account
                and is not metered.
              </p>
            )}
          </fieldset>
        ) : null}
      </div>

      <div className="ui-card-footer">
        <button type="button" className="secondary-button" onClick={onCancel} disabled={isBusy}>
          Cancel
        </button>
        <button type="submit" className="primary-button" disabled={submitDisabled}>
          Create Draft study
        </button>
      </div>
    </form>
  );
};

export default StudyCreateForm;
