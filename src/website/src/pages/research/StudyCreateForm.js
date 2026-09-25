import React, { useMemo, useState } from "react";
import Icon from "../../components/common/Icon";
import { Badge } from "../../components/common/ui";
import {
  DEFAULT_TELEMETRY_CLASSES,
  RUNTIME_CLASS_SHORT_LABELS,
  RUNTIME_LABELS,
  TELEMETRY_CLASS_LABELS,
  collectedClasses,
  describeSessionPolicy,
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

const sameJson = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const presetForSession = (policy) =>
  Object.entries(SESSION_PRESETS).find(([, preset]) => sameJson(preset.policy, policy))?.[0] || "custom";

const presetForTelemetry = (policy) =>
  Object.entries(TELEMETRY_PRESETS).find(([, preset]) => sameJson(preset, policy))?.[0] || "custom";

const storedList = (policy) =>
  collectedClasses(policy)
    .map((name) => RUNTIME_CLASS_SHORT_LABELS[name])
    .join(", ");

const capitalised = (text) => text.charAt(0).toUpperCase() + text.slice(1);

// What the policy actually stores (the runtime's classes, content included).
const telemetrySummary = (policy) => {
  const classes = Array.isArray(policy.allowed_field_classes) ? policy.allowed_field_classes : [];
  if (classes.length === 0 && policy.content_capture !== true) return "Metadata only (server default)";
  return capitalised(storedList(policy));
};

const toLocalDateTime = (value) => (value ? new Date(value) : null);

/**
 * Create a Draft study, or — with `cloneSource` — the profile-selection step of
 * cloning a stopped study (the server copies the stored configuration).
 */
const StudyCreateForm = ({ profiles, cloneSource, isBusy, onSubmit, onCancel }) => {
  const initialTelemetry = cloneSource ? asPolicyObject(cloneSource.telemetry_policy) : {};
  const initialSession = cloneSource ? asPolicyObject(cloneSource.session_policy) : DEFAULT_SESSION_POLICY;
  const [form, setForm] = useState({
    name: cloneSource ? `${cloneSource.name} (copy)` : "",
    description: cloneSource ? cloneSource.description || "" : "",
    startsAt: "",
    endsAt: "",
    profileIds: [],
  });
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

  const activeProfiles = useMemo(() => profiles.filter((profile) => profile.is_active !== false), [profiles]);

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
    if (!telemetryDraft.ok || !sessionDraft.ok) return;
    if (!cloneSource && form.startsAt && form.endsAt) {
      const starts = toLocalDateTime(form.startsAt);
      const ends = toLocalDateTime(form.endsAt);
      if (starts && ends && ends <= starts) {
        setDateError("The end must be after the start.");
        return;
      }
    }
    setDateError("");
    onSubmit({
      ...form,
      telemetryPolicy: telemetryDraft.value,
      sessionPolicy: sessionDraft.value,
    });
  };

  const submitDisabled =
    isBusy ||
    !form.name.trim() ||
    form.profileIds.length === 0 ||
    Boolean(telemetryPolicyError) ||
    Boolean(sessionPolicyError);

  return (
    <form className="research-card ui-card study-create-form" onSubmit={handleSubmit} aria-labelledby="study-create-title">
      <div className="ui-card-header">
        <div>
          <h3 className="ui-card-title" id="study-create-title">
            {cloneSource ? "Clone study" : "New study"}
          </h3>
          <p className="ui-card-subtitle">
            {cloneSource
              ? "A clone starts as a Draft with its own join code."
              : "The configuration is frozen when the study is created; only the name and description stay editable until the first participant consents."}
          </p>
        </div>
        <button type="button" className="icon-button" onClick={onCancel} aria-label="Close" disabled={isBusy}>
          <Icon name="x" size={18} />
        </button>
      </div>

      <div className="ui-card-body study-create-body">
        {cloneSource ? (
          <>
            <p className="research-hint">
              Copies the name, description, schedule, telemetry policy, and session policy from “{cloneSource.name}”.
              Participants, consent, assignments, telemetry data, join code, and study ID are not copied. Agent
              profiles are not copied either: select them below, and without a selection the clone cannot be joined.
            </p>
            <dl className="ui-dl study-create-summary">
              <div>
                <dt>Name</dt>
                <dd>{form.name}</dd>
              </div>
              <div>
                <dt>Description</dt>
                <dd>{form.description || "No description"}</dd>
              </div>
              <div>
                <dt>Telemetry policy</dt>
                <dd>{telemetryDraft.ok ? telemetrySummary(telemetryDraft.value) : "—"}</dd>
              </div>
              <div>
                <dt>Session policy</dt>
                <dd>
                  {sessionDraft.ok
                    ? describeSessionPolicy(sessionDraft.value)
                        .map(([label, value]) => `${label} ${value}`)
                        .join(" · ") || "—"
                    : "—"}
                </dd>
              </div>
            </dl>
          </>
        ) : (
          <>
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
                    and the participant has consented. Provider credentials are never collected.
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
          </>
        )}

        <fieldset className="research-profile-selection ui-fieldset">
          <legend>Agent profiles (arms)</legend>
          <p className="ui-hint">
            Each participant is randomly assigned one selected profile with equal probability; the assignment never
            changes. Profile selection is fixed once the study is created.
          </p>
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
      </div>

      <div className="ui-card-footer">
        <button type="button" className="secondary-button" onClick={onCancel} disabled={isBusy}>
          Cancel
        </button>
        <button type="submit" className="primary-button" disabled={submitDisabled}>
          {cloneSource ? "Clone Draft study" : "Create Draft study"}
        </button>
      </div>
    </form>
  );
};

export default StudyCreateForm;
