import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  createAgentProfile,
  deleteAgentProfile,
  getAgentAvailableTools,
  getAgentProfiles,
  getProviderConnections,
  getReleaseCatalogue,
  updateAgentProfile,
} from "../utils/api";
import "./AgentProfiles.css";

// Runtimes a profile can target. Must stay in sync with SUPPORTED_FRAMEWORKS in
// backend/routers/agent/profiles.py, which rejects anything else.
const FRAMEWORKS = [
  {
    value: "code4me2-agent",
    label: "code4me2-agent (built-in)",
    hint: "Our own ReAct loop. Self-reports telemetry; tools below are enforced by the runtime.",
  },
  {
    value: "goose",
    label: "Goose",
    hint: "Third-party ACP agent. Observed via the plugin proxy; tools below act as an allowlist filter.",
  },
  {
    value: "codex",
    label: "Codex",
    hint: "Third-party ACP agent on OpenAI's Responses API. Manages its own tools — the selection below is ignored.",
  },
];

const APPROVAL_POLICIES = [
  { value: "per_step", label: "Ask per step" },
  { value: "suggestion_only", label: "Suggestion only (never applies edits)" },
  { value: "auto", label: "Auto-approve" },
];

// Only the fields the backend accepts. Legacy BYOA/provider fields
// (base_url/api_key_ref/distribution_mode/agent_package/...) were removed from
// the contract and are rejected via extra="forbid".
const EMPTY_FORM = {
  name: "",
  model: "",
  framework_version: "code4me2-agent",
  connection_id: "",
  release_id: "",
  tools: [],
  approval_policy: "per_step",
  max_steps: 15,
  max_context_tokens: "",
  is_active: true,
  temperature: "", // blank = provider default (no override)
};

const TEMPERATURE_MIN = 0;
const TEMPERATURE_MAX = 2;

const formatTools = (toolsJson) => {
  try {
    const parsed = JSON.parse(toolsJson || "[]");
    return Array.isArray(parsed) ? parsed.join(", ") : "";
  } catch (_) {
    return toolsJson || "";
  }
};

const formatPlatforms = (platforms) => {
  if (!Array.isArray(platforms) || platforms.length === 0) return "none declared";
  return platforms
    .map((platform) => `${platform.os || "?"}/${platform.arch || "?"}`)
    .join(", ");
};

const getProfileId = (profile) => profile.profile_id || profile.id || profile.name;

// A profile may select one of the connection's allowed models. The backend
// re-validates this, so the UI constraint is only to avoid an obvious 422.
const connectionModels = (connection) =>
  Array.isArray(connection?.models) ? connection.models : [];

const AgentProfiles = ({ user = {} }) => {
  const [profiles, setProfiles] = useState([]);
  const [availableTools, setAvailableTools] = useState([]);
  const [connections, setConnections] = useState([]);
  const [catalogue, setCatalogue] = useState([]);
  const [form, setForm] = useState(EMPTY_FORM);
  const [editingProfileId, setEditingProfileId] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState("");
  const [fieldErrors, setFieldErrors] = useState([]);
  const [notice, setNotice] = useState("");
  const [isToolsOpen, setIsToolsOpen] = useState(false);
  const toolsDropdownRef = useRef(null);

  const sortedProfiles = useMemo(
    () => [...profiles].sort((a, b) => (a.name || "").localeCompare(b.name || "")),
    [profiles],
  );

  const loadProfiles = async () => {
    setIsLoading(true);
    setError("");
    setFieldErrors([]);
    const response = await getAgentProfiles();
    if (response.ok) {
      setProfiles(Array.isArray(response.data) ? response.data : []);
    } else {
      setError(response.error);
      setFieldErrors(Array.isArray(response.errors) ? response.errors : []);
    }
    setIsLoading(false);
  };

  useEffect(() => {
    loadProfiles();
  }, []);

  // Provider connections the caller may use (admin: all; researcher: granted).
  useEffect(() => {
    let cancelled = false;
    getProviderConnections().then((response) => {
      if (!cancelled && response && response.ok) {
        setConnections(Array.isArray(response.data) ? response.data : []);
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // The release catalogue is independent of existing profiles, so a fresh
  // install with zero profiles can still author its first profile (ISSUE-11).
  // It is researcher-readable, read-only and non-secret; each entry carries the
  // derived qualification status, distribution mode and supported platforms.
  useEffect(() => {
    let cancelled = false;
    getReleaseCatalogue().then((response) => {
      if (!cancelled && response && response.ok) {
        setCatalogue(Array.isArray(response.data) ? response.data : []);
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const releaseCatalogue = useMemo(
    () =>
      catalogue
        .filter((release) => release && release.release_id)
        .map((release) => ({
          release_id: release.release_id,
          release_version: release.version || "",
          qualification_status: release.qualification_status || "UNQUALIFIED",
          // A release is only selectable for a non-admin when it is qualified.
          verified: release.qualification_status === "QUALIFIED",
          distribution_mode: release.distribution_mode || "PACKAGED",
          is_byoa: release.is_byoa === true,
          supported_platforms: Array.isArray(release.supported_platforms)
            ? release.supported_platforms
            : [],
          // Approval options this release's conformance evidence verifies.
          // Missing on older servers: treat as unconstrained.
          verified_approval_options: Array.isArray(
            release.verified_approval_options,
          )
            ? release.verified_approval_options
            : null,
        }))
        .sort((a, b) =>
          String(a.release_id).localeCompare(String(b.release_id)),
        ),
    [catalogue],
  );

  // The selectable tool set depends on the runtime, so reload it whenever the
  // runtime changes rather than showing tools the agent can't actually call.
  useEffect(() => {
    let cancelled = false;
    getAgentAvailableTools(form.framework_version).then((res) => {
      if (!cancelled && res.ok) setAvailableTools(res.data.tools || []);
    });
    return () => {
      cancelled = true;
    };
  }, [form.framework_version]);

  useEffect(() => {
    if (!isToolsOpen) return undefined;
    const handleClickOutside = (event) => {
      if (
        toolsDropdownRef.current &&
        !toolsDropdownRef.current.contains(event.target)
      ) {
        setIsToolsOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [isToolsOpen]);

  const selectedConnection = connections.find(
    (connection) => connection.connection_id === form.connection_id,
  );
  const selectableModels = connectionModels(selectedConnection);
  const selectedRelease = releaseCatalogue.find(
    (release) => release.release_id === form.release_id,
  );
  // The approval options the selected release's evidence verifies; null means
  // an older catalogue with no evidence detail (leave unconstrained).
  const verifiedApprovals =
    selectedRelease && Array.isArray(selectedRelease.verified_approval_options)
      ? new Set(selectedRelease.verified_approval_options)
      : null;

  const validateForm = () => {
    if (!form.name.trim()) return "Profile name is required.";
    if (!/^[a-z0-9_-]+$/.test(form.name.trim())) {
      return "Use lowercase letters, numbers, hyphens, or underscores for the name.";
    }
    if (!form.connection_id) {
      return "Select a provider connection. An administrator grants connections.";
    }
    if (!form.model.trim()) return "Select a model.";
    if (
      selectedConnection &&
      selectableModels.length > 0 &&
      !selectableModels.includes(form.model.trim())
    ) {
      return "The selected model is not allowed by this provider connection.";
    }
    if (!form.release_id) {
      return "Select a registered release to pin this profile to an artifact.";
    }
    if (form.temperature !== "" && form.temperature !== null) {
      const t = Number(form.temperature);
      if (Number.isNaN(t) || t < TEMPERATURE_MIN || t > TEMPERATURE_MAX) {
        return "Temperature must be a number between 0 and 2 (or blank).";
      }
    }
    if (!user?.is_admin && selectedRelease && !selectedRelease.verified) {
      return "This release is not verified. Only an administrator can pin an unverified release.";
    }
    if (verifiedApprovals && !verifiedApprovals.has(form.approval_policy)) {
      return "The selected approval policy is not verified for this release.";
    }
    return "";
  };

  const clearMessages = () => {
    setError("");
    setFieldErrors([]);
    setNotice("");
  };

  const resetForm = () => {
    setForm(EMPTY_FORM);
    setEditingProfileId(null);
    clearMessages();
  };

  const handleChange = (event) => {
    const { name, value, type, checked } = event.target;
    setForm((current) => ({
      ...current,
      [name]: type === "checkbox" ? checked : value,
    }));
  };

  const handleConnectionChange = (event) => {
    const connectionId = event.target.value;
    setForm((current) => {
      const connection = connections.find(
        (candidate) => candidate.connection_id === connectionId,
      );
      const models = connectionModels(connection);
      // A stale model from the previous connection would fail validation.
      const keepModel = models.length === 0 || models.includes(current.model);
      return {
        ...current,
        connection_id: connectionId,
        model: keepModel ? current.model : "",
      };
    });
  };

  const handleTemperatureChange = (event) => {
    const { value } = event.target;
    if (value === "") {
      setForm((current) => ({ ...current, temperature: "" }));
      return;
    }
    const numeric = Number(value);
    if (Number.isNaN(numeric)) return;
    const clamped = Math.min(TEMPERATURE_MAX, Math.max(TEMPERATURE_MIN, numeric));
    setForm((current) => ({ ...current, temperature: clamped }));
  };

  const handleToolToggle = (toolName) => {
    setForm((current) => {
      const selected = new Set(current.tools);
      if (selected.has(toolName)) {
        selected.delete(toolName);
      } else {
        selected.add(toolName);
      }
      return { ...current, tools: Array.from(selected) };
    });
  };

  const allToolsSelected =
    availableTools.length > 0 && form.tools.length === availableTools.length;

  const handleToggleSelectAllTools = () => {
    setForm((current) => ({
      ...current,
      tools: allToolsSelected ? [] : [...availableTools],
    }));
  };

  const handleSubmit = async (event) => {
    event.preventDefault();
    const validationError = validateForm();
    if (validationError) {
      setError(validationError);
      setFieldErrors([]);
      return;
    }

    setIsSaving(true);
    clearMessages();

    const payload = {
      name: form.name.trim(),
      model: form.model.trim(),
      framework_version: form.framework_version,
      connection_id: form.connection_id,
      release_id: form.release_id.trim() || null,
      // The API takes a JSON string, not an array.
      tools_json: JSON.stringify(form.tools),
      approval_policy: form.approval_policy,
      max_steps: Number(form.max_steps),
      max_context_tokens:
        form.max_context_tokens === "" ? null : Number(form.max_context_tokens),
      is_active: Boolean(form.is_active),
      temperature:
        form.temperature === "" || form.temperature === null
          ? null
          : Number(form.temperature),
    };

    const response = editingProfileId
      ? await updateAgentProfile(editingProfileId, payload)
      : await createAgentProfile(payload);

    if (response.ok) {
      setNotice(editingProfileId ? "Agent profile updated." : "Agent profile created.");
      resetForm();
      await loadProfiles();
    } else {
      setError(response.error);
      setFieldErrors(Array.isArray(response.errors) ? response.errors : []);
    }

    setIsSaving(false);
  };

  // Map a stored profile onto the editable form. The response exposes a
  // `connection` summary plus the resolved release pin and derived flags.
  const formFromProfile = (profile) => {
    let selectedTools = [];
    try {
      const parsed = JSON.parse(profile.tools_json || "[]");
      selectedTools = Array.isArray(parsed) ? parsed : [];
    } catch (_) {
      selectedTools = [];
    }
    return {
      name: profile.name || "",
      model: profile.model || "",
      framework_version: profile.framework_version || "code4me2-agent",
      connection_id:
        profile.connection?.connection_id || profile.connection_id || "",
      release_id: profile.release_id || "",
      tools: selectedTools,
      approval_policy: profile.approval_policy || "per_step",
      max_steps: profile.max_steps || 15,
      max_context_tokens:
        profile.max_context_tokens === null ||
        profile.max_context_tokens === undefined
          ? ""
          : profile.max_context_tokens,
      is_active:
        profile.is_active !== undefined ? Boolean(profile.is_active) : true,
      temperature:
        profile.temperature === null || profile.temperature === undefined
          ? ""
          : profile.temperature,
    };
  };

  const handleEdit = (profile) => {
    setEditingProfileId(getProfileId(profile));
    setForm(formFromProfile(profile));
    clearMessages();
  };

  // Clone populates the *create* form with an existing profile's configuration
  // under a new name, so a researcher can express v1 vs v2 as two templates that
  // differ only in their release.
  const handleClone = (profile) => {
    setEditingProfileId(null);
    setForm({ ...formFromProfile(profile), name: `${profile.name || "profile"}-copy` });
    clearMessages();
    setNotice(
      `Cloning "${profile.name}". Change the release pin and save to create a new profile.`,
    );
  };

  const handleDelete = async (profile) => {
    const profileId = getProfileId(profile);
    const confirmed = window.confirm(
      `Retire agent profile "${profile.name}"? It will no longer be assigned ` +
        `to new participants, while its immutable version history and existing ` +
        `study assignments remain available for analysis.`,
    );
    if (!confirmed) return;

    setIsSaving(true);
    clearMessages();

    const response = await deleteAgentProfile(profileId);
    if (response.ok) {
      setNotice("Agent profile retired.");
      if (editingProfileId === profileId) resetForm();
      await loadProfiles();
    } else {
      setError(response.error);
      setFieldErrors(Array.isArray(response.errors) ? response.errors : []);
    }

    setIsSaving(false);
  };

  const selectedSet = new Set(form.tools);
  const activeFramework = FRAMEWORKS.find(
    (f) => f.value === form.framework_version,
  );
  const toolsIgnored = form.framework_version === "codex";
  const editingProfile = editingProfileId
    ? profiles.find((profile) => getProfileId(profile) === editingProfileId) ||
      null
    : null;

  const releaseLabel = (release) => {
    const platforms = (release.supported_platforms || [])
      .map((platform) => `${platform.os || "?"}/${platform.arch || "?"}`)
      .join(", ");
    return [
      release.release_id,
      release.release_version ? `v${release.release_version}` : "",
      release.qualification_status,
      release.distribution_mode,
      release.is_byoa ? "BYOA" : "",
      platforms,
    ]
      .filter(Boolean)
      .join(" · ");
  };

  return (
    <section className="agent-profiles-page">
      <div className="agent-profiles-header">
        <div>
          <h2>Agent Profiles</h2>
          <p>
            Define the experiment arms for agent runs: which runtime, which
            authorized provider connection, which model, tools, approval policy
            and step limit. A profile's settings are snapshotted onto each task
            when it starts, so editing a profile never changes tasks already
            running.
          </p>
        </div>
        <button
          className="secondary-button"
          onClick={loadProfiles}
          disabled={isLoading}
        >
          Refresh
        </button>
      </div>

      {(error || notice) && (
        <div className={`profile-message ${error ? "error" : "success"}`}>
          <span>{error || notice}</span>
          {fieldErrors.length > 0 && (
            <ul className="profile-field-errors" role="alert">
              {fieldErrors.map((item, position) => (
                <li key={`${item.field || "field"}-${position}`}>
                  {item.field ? <code>{item.field}</code> : null} {item.message}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <div className="agent-profiles-layout">
        <form className="profile-form" onSubmit={handleSubmit}>
          <h3>{editingProfileId ? "Edit Profile" : "Create Profile"}</h3>

          <label>
            Profile name
            <input
              name="name"
              value={form.name}
              onChange={handleChange}
              placeholder="my-experiment-arm"
              disabled={isSaving}
            />
          </label>

          <label>
            Agent runtime
            <select
              name="framework_version"
              value={form.framework_version}
              onChange={handleChange}
              disabled={isSaving}
            >
              {FRAMEWORKS.map((framework) => (
                <option key={framework.value} value={framework.value}>
                  {framework.label}
                </option>
              ))}
            </select>
          </label>
          {activeFramework && (
            <p className="profile-field-hint">{activeFramework.hint}</p>
          )}

          <label>
            Provider connection
            <select
              name="connection_id"
              value={form.connection_id}
              onChange={handleConnectionChange}
              disabled={isSaving}
            >
              <option value="">Select an authorized connection…</option>
              {form.connection_id &&
                !connections.some(
                  (connection) =>
                    connection.connection_id === form.connection_id,
                ) && (
                  <option value={form.connection_id}>
                    {form.connection_id} (not granted)
                  </option>
                )}
              {connections.map((connection) => (
                <option
                  key={connection.connection_id}
                  value={connection.connection_id}
                >
                  {connection.label}
                  {connection.ready === false ? " — secret missing" : ""}
                  {connection.is_active === false ? " (inactive)" : ""}
                </option>
              ))}
            </select>
          </label>
          <p className="profile-field-hint">
            Connections are administrator-maintained. The endpoint and secret
            stay on the server; never enter a URL or API key here.
          </p>
          {selectedConnection && (
            <p className="profile-field-hint">
              Allowed models:{" "}
              {connectionModels(selectedConnection).join(", ") || "none declared"}
              {selectedConnection.ready === false
                ? " · readiness: secret missing"
                : ""}
            </p>
          )}

          <label>
            Model
            <select
              name="model"
              value={form.model}
              onChange={handleChange}
              disabled={isSaving || selectableModels.length === 0}
            >
              <option value="">
                {form.connection_id
                  ? "Select a model…"
                  : "Select a connection first…"}
              </option>
              {form.model &&
                !selectableModels.includes(form.model) && (
                  <option value={form.model}>
                    {form.model} (not allowed)
                  </option>
                )}
              {selectableModels.map((model) => (
                <option key={model} value={model}>
                  {model}
                </option>
              ))}
            </select>
          </label>

          <label>
            Registered release
            <select
              name="release_id"
              value={form.release_id}
              onChange={handleChange}
              disabled={isSaving}
            >
              <option value="">Select a registered release…</option>
              {form.release_id &&
                !releaseCatalogue.some(
                  (release) => release.release_id === form.release_id,
                ) && (
                  <option value={form.release_id}>
                    {form.release_id} (not in catalogue)
                  </option>
                )}
              {releaseCatalogue.map((release) => (
                <option
                  key={release.release_id}
                  value={release.release_id}
                  disabled={!user?.is_admin && !release.verified}
                >
                  {releaseLabel(release)}
                </option>
              ))}
            </select>
          </label>
          {selectedRelease && (
            <p className="profile-field-hint">
              Qualification: {selectedRelease.qualification_status} · mode:{" "}
              {selectedRelease.distribution_mode}
              {selectedRelease.is_byoa ? " (BYOA)" : ""} · platforms:{" "}
              {formatPlatforms(selectedRelease.supported_platforms)}
            </p>
          )}
          <p className="profile-field-hint">
            Pins the exact approved artifact. Create a second profile that pins a
            different release to express a v1 vs v2 arm. Unverified releases may
            only be pinned by an administrator.
          </p>

          {editingProfile && (
            <div className="profile-derived">
              <span
                className={`profile-badge ${
                  editingProfile.verified ? "verified" : "unverified"
                }`}
              >
                {editingProfile.verified ? "Verified" : "Unverified"}
              </span>
              <dl className="profile-derived-fields">
                <div>
                  <dt>Resolved release</dt>
                  <dd>{editingProfile.release_id || "—"}</dd>
                </div>
                <div>
                  <dt>Release version</dt>
                  <dd>{editingProfile.release_version || "—"}</dd>
                </div>
                <div>
                  <dt>Supported platforms</dt>
                  <dd>{formatPlatforms(editingProfile.supported_platforms)}</dd>
                </div>
              </dl>
              <p className="profile-field-hint">
                Derived server-side from conformance evidence; read-only.
              </p>
            </div>
          )}

          <label>
            Approval policy
            <select
              name="approval_policy"
              value={form.approval_policy}
              onChange={handleChange}
              disabled={isSaving}
            >
              {APPROVAL_POLICIES.map((policy) => (
                <option
                  key={policy.value}
                  value={policy.value}
                  disabled={
                    Boolean(verifiedApprovals) &&
                    !verifiedApprovals.has(policy.value)
                  }
                >
                  {policy.label}
                </option>
              ))}
            </select>
          </label>

          <div className="tools-field">
            <span className="tools-field-label">
              Tools
              <span className="tools-count">
                {form.tools.length} / {availableTools.length} selected
              </span>
            </span>
            <div className="tools-dropdown" ref={toolsDropdownRef}>
              <button
                type="button"
                className="tools-dropdown-trigger"
                onClick={() => setIsToolsOpen((open) => !open)}
                disabled={isSaving || availableTools.length === 0}
                aria-expanded={isToolsOpen}
              >
                <span className="tools-dropdown-summary">
                  {availableTools.length === 0
                    ? toolsIgnored
                      ? "Not applicable for Codex"
                      : "Loading tools…"
                    : form.tools.length === 0
                      ? "Select tools…"
                      : allToolsSelected
                        ? "All tools selected"
                        : form.tools.join(", ")}
                </span>
                <span className="tools-dropdown-caret" aria-hidden="true">
                  ▾
                </span>
              </button>
              {isToolsOpen && availableTools.length > 0 && (
                <div className="tools-dropdown-menu" role="listbox">
                  <label className="tool-checkbox-label tools-select-all">
                    <input
                      type="checkbox"
                      checked={allToolsSelected}
                      onChange={handleToggleSelectAllTools}
                    />
                    Select all
                  </label>
                  <div className="tools-dropdown-divider" />
                  {availableTools.map((tool) => (
                    <label key={tool} className="tool-checkbox-label">
                      <input
                        type="checkbox"
                        checked={selectedSet.has(tool)}
                        onChange={() => handleToolToggle(tool)}
                      />
                      {tool}
                    </label>
                  ))}
                </div>
              )}
            </div>
          </div>
          <p className="profile-field-hint">
            Selecting no tools is a meaningful condition, not an omission: it
            produces a genuinely tool-free arm.
          </p>

          <label>
            Max steps
            <input
              name="max_steps"
              type="number"
              min="1"
              value={form.max_steps}
              onChange={handleChange}
              disabled={isSaving}
            />
          </label>

          <label>
            Max context tokens
            <input
              name="max_context_tokens"
              type="number"
              min="1"
              placeholder="Model maximum"
              value={form.max_context_tokens}
              onChange={handleChange}
              disabled={isSaving}
            />
          </label>

          <label>
            Temperature
            <input
              name="temperature"
              type="number"
              min={TEMPERATURE_MIN}
              max={TEMPERATURE_MAX}
              step="0.1"
              placeholder="Provider default"
              value={form.temperature}
              onChange={handleTemperatureChange}
              disabled={isSaving}
            />
          </label>

          <label className="checkbox-label">
            <input
              name="is_active"
              type="checkbox"
              checked={form.is_active}
              onChange={handleChange}
              disabled={isSaving}
            />
            Active (eligible for assignment)
          </label>

          <div className="profile-form-actions">
            <button className="primary-button" type="submit" disabled={isSaving}>
              {editingProfileId ? "Save Changes" : "Create Profile"}
            </button>
            {editingProfileId && (
              <button
                className="secondary-button"
                type="button"
                onClick={resetForm}
                disabled={isSaving}
              >
                Cancel
              </button>
            )}
          </div>
        </form>

        <div className="profiles-table-panel">
          {isLoading ? (
            <div className="profiles-empty">Loading agent profiles...</div>
          ) : sortedProfiles.length === 0 ? (
            <div className="profiles-empty">
              No agent profiles found. Create one to define an agent
              configuration for your studies.
            </div>
          ) : (
            <table className="profiles-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Runtime</th>
                  <th>Model</th>
                  <th>Connection</th>
                  <th>Release</th>
                  <th>Verified</th>
                  <th>Platforms</th>
                  <th>Approval</th>
                  <th>Tools</th>
                  <th>Steps</th>
                  <th>Temp</th>
                  <th>Active</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {sortedProfiles.map((profile) => (
                  <tr key={getProfileId(profile)}>
                    <td>{profile.name}</td>
                    <td>{profile.framework_version || "—"}</td>
                    <td>{profile.model}</td>
                    <td>
                      {profile.connection?.label ||
                        profile.connection?.connection_id ||
                        "unassigned"}
                      {profile.connection?.ready === false
                        ? " (secret missing)"
                        : ""}
                    </td>
                    <td>
                      {profile.release_id || "unresolved"}
                      <span className="profile-derived-release">
                        {profile.release_version
                          ? ` · v${profile.release_version}`
                          : ""}
                      </span>
                    </td>
                    <td>
                      <span
                        className={`profile-badge ${
                          profile.verified ? "verified" : "unverified"
                        }`}
                      >
                        {profile.verified ? "Verified" : "Unverified"}
                      </span>
                    </td>
                    <td>{formatPlatforms(profile.supported_platforms)}</td>
                    <td>{profile.approval_policy}</td>
                    <td>{formatTools(profile.tools_json) || "none"}</td>
                    <td>{profile.max_steps}</td>
                    <td>
                      {profile.temperature === null ||
                      profile.temperature === undefined
                        ? "default"
                        : profile.temperature}
                    </td>
                    <td>{profile.is_active === false ? "No" : "Yes"}</td>
                    <td>
                      <div className="table-actions">
                        <button
                          className="secondary-button"
                          onClick={() => handleEdit(profile)}
                          disabled={isSaving}
                        >
                          Edit
                        </button>
                        <button
                          className="secondary-button"
                          onClick={() => handleClone(profile)}
                          disabled={isSaving}
                        >
                          Clone
                        </button>
                        <button
                          className="danger-button"
                          onClick={() => handleDelete(profile)}
                          disabled={isSaving}
                        >
                          Delete
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </section>
  );
};

export default AgentProfiles;
