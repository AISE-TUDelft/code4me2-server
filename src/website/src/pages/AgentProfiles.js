import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  createAgentProfile,
  deleteAgentProfile,
  getAgentAvailableTools,
  getAgentProfiles,
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

const EMPTY_FORM = {
  name: "",
  model: "",
  framework_version: "code4me2-agent",
  base_url: "",
  api_key_ref: "",
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

const getProfileId = (profile) => profile.profile_id || profile.id || profile.name;

const AgentProfiles = () => {
  const [profiles, setProfiles] = useState([]);
  const [availableTools, setAvailableTools] = useState([]);
  const [form, setForm] = useState(EMPTY_FORM);
  const [editingProfileId, setEditingProfileId] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState("");
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
    const response = await getAgentProfiles();
    if (response.ok) {
      setProfiles(Array.isArray(response.data) ? response.data : []);
    } else {
      setError(response.error);
    }
    setIsLoading(false);
  };

  useEffect(() => {
    loadProfiles();
  }, []);

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

  const validateForm = () => {
    if (!form.name.trim()) return "Profile name is required.";
    if (!/^[a-z0-9_-]+$/.test(form.name.trim())) {
      return "Use lowercase letters, numbers, hyphens, or underscores for the name.";
    }
    if (!form.model.trim()) return "Model is required.";
    if (form.temperature !== "" && form.temperature !== null) {
      const t = Number(form.temperature);
      if (Number.isNaN(t) || t < TEMPERATURE_MIN || t > TEMPERATURE_MAX) {
        return "Temperature must be a number between 0 and 2 (or blank).";
      }
    }
    if (form.base_url.trim() && !/^https?:\/\//i.test(form.base_url.trim())) {
      return "Base URL must start with http:// or https://";
    }
    // Guard against the obvious mistake of pasting the key itself. The backend
    // rejects this too; catching it here avoids a round-trip and, more
    // importantly, avoids sending a real credential over the wire at all.
    const keyRef = form.api_key_ref.trim();
    if (keyRef && (keyRef.length > 128 || /[-.\s/:]/.test(keyRef))) {
      return "API key ref must be the NAME of an environment variable (e.g. OPENAI_API_KEY), not the key itself.";
    }
    return "";
  };

  const resetForm = () => {
    setForm(EMPTY_FORM);
    setEditingProfileId(null);
    setError("");
    setNotice("");
  };

  const handleChange = (event) => {
    const { name, value, type, checked } = event.target;
    setForm((current) => ({
      ...current,
      [name]: type === "checkbox" ? checked : value,
    }));
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
      return;
    }

    setIsSaving(true);
    setError("");
    setNotice("");

    const payload = {
      name: form.name.trim(),
      model: form.model.trim(),
      framework_version: form.framework_version,
      // Blank means "use the server default upstream", which the backend
      // represents as NULL rather than an empty string.
      base_url: form.base_url.trim() || null,
      api_key_ref: form.api_key_ref.trim() || null,
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
    }

    setIsSaving(false);
  };

  const handleEdit = (profile) => {
    setEditingProfileId(getProfileId(profile));
    let selectedTools = [];
    try {
      const parsed = JSON.parse(profile.tools_json || "[]");
      selectedTools = Array.isArray(parsed) ? parsed : [];
    } catch (_) {
      selectedTools = [];
    }
    setForm({
      name: profile.name || "",
      model: profile.model || "",
      framework_version: profile.framework_version || "code4me2-agent",
      base_url: profile.base_url || "",
      api_key_ref: profile.api_key_ref || "",
      tools: selectedTools,
      approval_policy: profile.approval_policy || "per_step",
      max_steps: profile.max_steps || 15,
      max_context_tokens:
        profile.max_context_tokens === null ||
        profile.max_context_tokens === undefined
          ? ""
          : profile.max_context_tokens,
      is_active: profile.is_active !== undefined ? Boolean(profile.is_active) : true,
      temperature:
        profile.temperature === null || profile.temperature === undefined
          ? ""
          : profile.temperature,
    });
    setError("");
    setNotice("");
  };

  const handleDelete = async (profile) => {
    const profileId = getProfileId(profile);
    const confirmed = window.confirm(
      `Delete agent profile "${profile.name}"? Historical agent tasks reference ` +
        `profiles by name, so past telemetry stays attributable — but any A/B ` +
        `assignments pointing at this profile are removed. To retire an arm ` +
        `mid-study, uncheck "Active" instead.`,
    );
    if (!confirmed) return;

    setIsSaving(true);
    setError("");
    setNotice("");

    const response = await deleteAgentProfile(profileId);
    if (response.ok) {
      setNotice("Agent profile deleted.");
      if (editingProfileId === profileId) resetForm();
      await loadProfiles();
    } else {
      setError(response.error);
    }

    setIsSaving(false);
  };

  const selectedSet = new Set(form.tools);
  const activeFramework = FRAMEWORKS.find(
    (f) => f.value === form.framework_version,
  );
  const toolsIgnored = form.framework_version === "codex";

  return (
    <section className="agent-profiles-page">
      <div className="agent-profiles-header">
        <div>
          <h2>Agent Profiles</h2>
          <p>
            Define the experiment arms for agent runs: which runtime, which
            OpenAI-compatible provider, which model, tools, approval policy and
            step limit. A profile's settings are snapshotted onto each task when
            it starts, so editing a profile never changes tasks already running.
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
          {error || notice}
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
            Model
            <input
              name="model"
              value={form.model}
              onChange={handleChange}
              placeholder="qwen2.5-coder:7b"
              disabled={isSaving}
            />
          </label>

          <label>
            Provider base URL
            <input
              name="base_url"
              value={form.base_url}
              onChange={handleChange}
              placeholder="http://localhost:11434/v1 (blank = server default)"
              disabled={isSaving}
            />
          </label>
          <p className="profile-field-hint">
            Any endpoint speaking the OpenAI-compatible chat-completions format:
            Ollama, Groq, OpenRouter, vLLM, or OpenAI itself. Leave blank to use
            the server's configured default.
          </p>

          <label>
            API key env var
            <input
              name="api_key_ref"
              value={form.api_key_ref}
              onChange={handleChange}
              placeholder="OPENAI_API_KEY"
              disabled={isSaving}
            />
          </label>
          <p className="profile-field-hint">
            The <strong>name</strong> of an environment variable on the server —
            never the key itself. Keys are never stored in the database.
          </p>

          <label>
            Approval policy
            <select
              name="approval_policy"
              value={form.approval_policy}
              onChange={handleChange}
              disabled={isSaving}
            >
              {APPROVAL_POLICIES.map((policy) => (
                <option key={policy.value} value={policy.value}>
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
            Active (eligible for A/B assignment)
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
              No agent profiles found. At least one active profile is required
              before any agent task can be created.
            </div>
          ) : (
            <table className="profiles-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Runtime</th>
                  <th>Model</th>
                  <th>Provider</th>
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
                    <td>{profile.base_url || "server default"}</td>
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
