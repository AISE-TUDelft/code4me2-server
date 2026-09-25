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
import Icon from "../components/common/Icon";
import { Alert, Badge, EmptyState, FieldErrors, Loading, PageHeader, Switch } from "../components/common/ui";
import "./AgentProfiles.css";

// Runtimes a profile can target. Must stay in sync with SUPPORTED_FRAMEWORKS in
// backend/routers/agent/profiles.py, which rejects anything else.
const FRAMEWORKS = [
  {
    value: "code4me2-agent",
    label: "code4me2-agent (built-in)",
    short: "Built-in",
    hint: "The packaged Code4Me agent. Every setting below is applied by the managed runtime and the inference relay.",
  },
  {
    value: "goose",
    label: "Goose (bring your own agent)",
    short: "Goose",
    hint: "Participant-installed Goose. Settings only apply where the pinned release declares a translation, and the agent uses the participant's own model credentials.",
  },
  {
    value: "codex",
    label: "Codex (bring your own agent)",
    short: "Codex",
    hint: "Participant-installed Codex. Codex manages its own tools; other settings only apply where the pinned release declares a translation.",
  },
];

const APPROVAL_POLICIES = [
  { value: "per_step", label: "Ask per step" },
  { value: "suggestion_only", label: "Suggestion only (never applies edits)" },
  { value: "auto", label: "Auto-approve" },
];

// Mirrors FALLBACK_MAX_CONTEXT_TOKENS in backend/routers/acp/__init__.py.
const DEFAULT_CONTEXT_TOKENS = 16000;
const SYSTEM_PROMPT_MAX_LENGTH = 4000;

// Settings that govern a packaged (built-in) release.
const PACKAGED_FIELDS = [
  "model",
  "temperature",
  "max_steps",
  "tools",
  "approval_policy",
  "max_context_tokens",
];
// Always set on a profile, so a BYOA release must translate them.
const ALWAYS_SET_FIELDS = ["model", "max_steps", "approval_policy"];
// Never forwarded to a participant-installed agent.
const BYOA_NEVER_FIELDS = ["max_context_tokens", "system_prompt"];

const FIELD_LABELS = {
  model: "model",
  temperature: "temperature",
  max_steps: "max steps",
  tools: "tools",
  approval_policy: "approval policy",
  max_context_tokens: "max context tokens",
  system_prompt: "system prompt",
};

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
  system_prompt: "",
};

const TEMPERATURE_MIN = 0;
const TEMPERATURE_MAX = 2;

const parseTools = (toolsJson) => {
  try {
    const parsed = JSON.parse(toolsJson || "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch (_) {
    return [];
  }
};

const formatPlatforms = (platforms) => {
  if (!Array.isArray(platforms) || platforms.length === 0) return "none declared";
  return platforms.map((platform) => `${platform.os || "?"}/${platform.arch || "?"}`).join(", ");
};

const getProfileId = (profile) => profile.profile_id || profile.id || profile.name;

// A profile may select one of the connection's allowed models. The backend
// re-validates this, so the UI constraint is only to avoid an obvious 422.
const connectionModels = (connection) => (Array.isArray(connection?.models) ? connection.models : []);

const frameworkMeta = (value) => FRAMEWORKS.find((framework) => framework.value === value);

// Which runtimes may pin a release. Newer servers say so explicitly; older
// ones are inferred from the distribution mode the server validates against.
const releaseFrameworks = (release) => {
  if (!release) return [];
  if (Array.isArray(release.compatible_frameworks) && release.compatible_frameworks.length) {
    return release.compatible_frameworks;
  }
  if (!release.is_byoa && release.distribution_mode !== "BYOA_EXTERNAL") return ["code4me2-agent"];
  const agentId = String(release.agent_id || "").toLowerCase();
  if (agentId.includes("goose")) return ["goose"];
  if (agentId.includes("codex")) return ["codex"];
  return ["goose", "codex"];
};

// The profile settings that actually govern the selected release. `null`
// means "unknown" (older server without the capability list).
const governedFields = (release, framework) => {
  if (release && Array.isArray(release.configurable_fields)) {
    return new Set(release.configurable_fields);
  }
  if (framework === "code4me2-agent") return new Set(PACKAGED_FIELDS);
  return null;
};

const AgentProfiles = ({ user = {} }) => {
  const [profiles, setProfiles] = useState([]);
  const [availableTools, setAvailableTools] = useState([]);
  // The runtime whose tool list `availableTools` holds.
  const [toolsFor, setToolsFor] = useState(null);
  // A failed tool-list request must not lock or clear the profile's tools.
  const [toolsError, setToolsError] = useState("");
  const [toolsReload, setToolsReload] = useState(0);
  // Bumped whenever the form is (re)loaded, so a profile opened for editing or
  // cloning is re-checked against what its runtime can honour.
  const [formRevision, setFormRevision] = useState(0);
  const [connections, setConnections] = useState([]);
  const [catalogue, setCatalogue] = useState([]);
  const [form, setForm] = useState(EMPTY_FORM);
  // While another runtime's list is still shown (the new request in flight),
  // it neither locks nor clears the form's tools.
  const toolsLoaded = toolsFor === form.framework_version;
  const [editingProfileId, setEditingProfileId] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState("");
  const [fieldErrors, setFieldErrors] = useState([]);
  const [notice, setNotice] = useState("");
  const [query, setQuery] = useState("");
  const editorRef = useRef(null);

  const sortedProfiles = useMemo(
    () => [...profiles].sort((a, b) => (a.name || "").localeCompare(b.name || "")),
    [profiles],
  );

  const visibleProfiles = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return sortedProfiles;
    return sortedProfiles.filter((profile) =>
      [profile.name, profile.model, profile.framework_version, profile.release_id]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(needle)),
    );
  }, [sortedProfiles, query]);

  const loadProfiles = async () => {
    setIsLoading(true);
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

  // Provider connections the caller may use (admin: all; researcher: active).
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
          agent_id: release.agent_id || "",
          release_version: release.version || "",
          qualification_status: release.qualification_status || "UNQUALIFIED",
          // A release is only selectable for a non-admin when it is qualified.
          verified: release.qualification_status === "QUALIFIED",
          distribution_mode: release.distribution_mode || "PACKAGED",
          is_byoa: release.is_byoa === true || release.distribution_mode === "BYOA_EXTERNAL",
          supported_platforms: Array.isArray(release.supported_platforms) ? release.supported_platforms : [],
          // Approval options this release's conformance evidence verifies.
          // Missing on older servers: treat as unconstrained.
          verified_approval_options: Array.isArray(release.verified_approval_options)
            ? release.verified_approval_options
            : null,
          compatible_frameworks: release.compatible_frameworks,
          configurable_fields: release.configurable_fields,
          required_bindings_missing: Array.isArray(release.required_bindings_missing)
            ? release.required_bindings_missing
            : [],
        }))
        .sort((a, b) => String(a.release_id).localeCompare(String(b.release_id))),
    [catalogue],
  );

  // The selectable tool set depends on the runtime, so reload it whenever the
  // runtime changes rather than showing tools the agent can't actually call.
  useEffect(() => {
    let cancelled = false;
    const framework = form.framework_version;
    setToolsError("");
    Promise.resolve(getAgentAvailableTools(framework)).then((res) => {
      if (cancelled) return;
      if (res && res.ok) {
        setAvailableTools(Array.isArray(res.data?.tools) ? res.data.tools : []);
        setToolsFor(framework);
      } else {
        setAvailableTools([]);
        setToolsFor(null);
        setToolsError((res && res.error) || "The tool list could not be loaded.");
      }
    });
    return () => {
      cancelled = true;
    };
  }, [form.framework_version, toolsReload]);

  const selectedConnection = connections.find((connection) => connection.connection_id === form.connection_id);
  const selectableModels = connectionModels(selectedConnection);
  const selectedRelease = releaseCatalogue.find((release) => release.release_id === form.release_id);
  const isByoaRuntime = form.framework_version !== "code4me2-agent";
  const governed = governedFields(selectedRelease, form.framework_version);
  const supportsSystemPrompt = Boolean(governed && governed.has("system_prompt"));

  // A field is editable unless we know the runtime cannot honour it.
  const fieldEnabled = (field) => {
    if (isByoaRuntime && BYOA_NEVER_FIELDS.includes(field)) return false;
    if (field === "tools" && toolsLoaded && availableTools.length === 0) return false;
    if (!governed) return true;
    return governed.has(field);
  };

  const missingBindings = useMemo(() => {
    if (!selectedRelease || !selectedRelease.is_byoa) return [];
    if (selectedRelease.required_bindings_missing.length) return selectedRelease.required_bindings_missing;
    if (!governed) return [];
    return ALWAYS_SET_FIELDS.filter((field) => !governed.has(field));
  }, [selectedRelease, governed]);

  // The approval options the selected release's evidence verifies; null means
  // an older catalogue with no evidence detail (leave unconstrained).
  const verifiedApprovals =
    selectedRelease && Array.isArray(selectedRelease.verified_approval_options)
      ? new Set(selectedRelease.verified_approval_options)
      : null;

  // Keep values the runtime cannot honour at their "unset" value, so the
  // saved profile never carries a setting that would silently be ignored.
  useEffect(() => {
    setForm((current) => {
      const next = { ...current };
      let changed = false;
      if (!fieldEnabled("temperature") && current.temperature !== "") {
        next.temperature = "";
        changed = true;
      }
      if (!fieldEnabled("max_context_tokens") && current.max_context_tokens !== "") {
        next.max_context_tokens = "";
        changed = true;
      }
      if (!fieldEnabled("tools") && current.tools.length > 0) {
        next.tools = [];
        changed = true;
      }
      if (!supportsSystemPrompt && current.system_prompt) {
        next.system_prompt = "";
        changed = true;
      }
      return changed ? next : current;
    });
    // fieldEnabled only reads the values listed below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [form.framework_version, form.release_id, toolsLoaded, availableTools, catalogue, supportsSystemPrompt, formRevision]);

  const validateForm = () => {
    if (!form.name.trim()) return "Profile name is required.";
    if (!/^[a-z0-9_-]+$/.test(form.name.trim())) {
      return "Use lowercase letters, numbers, hyphens, or underscores for the name.";
    }
    if (!form.connection_id) {
      return "Select a provider connection. An administrator manages connections.";
    }
    if (!form.model.trim()) return "Select a model.";
    if (selectedConnection && selectableModels.length > 0 && !selectableModels.includes(form.model.trim())) {
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
    if (!Number.isInteger(Number(form.max_steps)) || Number(form.max_steps) < 1) {
      return "Max steps must be a whole number of at least 1.";
    }
    if (form.max_context_tokens !== "" && (!Number.isInteger(Number(form.max_context_tokens)) || Number(form.max_context_tokens) < 1)) {
      return "Max context tokens must be a whole number of at least 1 (or blank).";
    }
    if (form.system_prompt.length > SYSTEM_PROMPT_MAX_LENGTH) {
      return `The system prompt must not exceed ${SYSTEM_PROMPT_MAX_LENGTH.toLocaleString()} characters.`;
    }
    if (!user?.is_admin && selectedRelease && !selectedRelease.verified) {
      return "This release is not verified. Only an administrator can pin an unverified release.";
    }
    if (verifiedApprovals && !verifiedApprovals.has(form.approval_policy)) {
      return "The selected approval policy is not verified for this release.";
    }
    if (missingBindings.length > 0) {
      return `This release does not translate ${missingBindings
        .map((field) => FIELD_LABELS[field] || field)
        .join(", ")}; the server would reject a profile pinned to it.`;
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
    setFormRevision((value) => value + 1);
    setEditingProfileId(null);
    clearMessages();
  };

  const focusEditor = () => {
    setTimeout(() => {
      if (editorRef.current && editorRef.current.scrollIntoView) {
        editorRef.current.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    }, 0);
  };

  const handleChange = (event) => {
    const { name, value, type, checked } = event.target;
    setForm((current) => ({ ...current, [name]: type === "checkbox" ? checked : value }));
  };

  const handleFrameworkChange = (event) => {
    const framework = event.target.value;
    setForm((current) => {
      const release = releaseCatalogue.find((item) => item.release_id === current.release_id);
      // A release pinned for another runtime would fail validation.
      const keepRelease = !release || releaseFrameworks(release).includes(framework);
      return {
        ...current,
        framework_version: framework,
        release_id: keepRelease ? current.release_id : "",
        tools: [],
      };
    });
  };

  const handleReleaseChange = (event) => {
    const releaseId = event.target.value;
    const release = releaseCatalogue.find((item) => item.release_id === releaseId);
    setForm((current) => {
      const frameworks = releaseFrameworks(release);
      if (release && frameworks.length && !frameworks.includes(current.framework_version)) {
        // Selecting a release implies its runtime.
        return { ...current, release_id: releaseId, framework_version: frameworks[0], tools: [] };
      }
      return { ...current, release_id: releaseId };
    });
  };

  const handleConnectionChange = (event) => {
    const connectionId = event.target.value;
    setForm((current) => {
      const connection = connections.find((candidate) => candidate.connection_id === connectionId);
      const models = connectionModels(connection);
      // A stale model from the previous connection would fail validation.
      const keepModel = models.length === 0 || models.includes(current.model);
      return { ...current, connection_id: connectionId, model: keepModel ? current.model : "" };
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
      if (selected.has(toolName)) selected.delete(toolName);
      else selected.add(toolName);
      return { ...current, tools: Array.from(selected) };
    });
  };

  const allToolsSelected = availableTools.length > 0 && form.tools.length === availableTools.length;

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

    // A locked setting is saved unset, whatever the form still holds, so the
    // profile never carries a value its runtime would silently ignore.
    const hasTemperature = fieldEnabled("temperature") && form.temperature !== "" && form.temperature !== null;
    const hasContext = fieldEnabled("max_context_tokens") && form.max_context_tokens !== "";
    const payload = {
      name: form.name.trim(),
      model: form.model.trim(),
      framework_version: form.framework_version,
      connection_id: form.connection_id,
      release_id: form.release_id.trim() || null,
      // The API takes a JSON string, not an array.
      tools_json: JSON.stringify(fieldEnabled("tools") ? form.tools : []),
      approval_policy: form.approval_policy,
      max_steps: Number(form.max_steps),
      max_context_tokens: hasContext ? Number(form.max_context_tokens) : null,
      is_active: Boolean(form.is_active),
      temperature: hasTemperature ? Number(form.temperature) : null,
    };
    // Only sent when the server advertises the field (extra="forbid" otherwise).
    if (supportsSystemPrompt) {
      payload.system_prompt = form.system_prompt.trim() || null;
    }

    const response = editingProfileId
      ? await updateAgentProfile(editingProfileId, payload)
      : await createAgentProfile(payload);

    if (response.ok) {
      const saved = editingProfileId ? "Agent profile updated." : "Agent profile created.";
      resetForm();
      setNotice(saved);
      await loadProfiles();
    } else {
      setError(response.error);
      setFieldErrors(Array.isArray(response.errors) ? response.errors : []);
    }
    setIsSaving(false);
  };

  // Map a stored profile onto the editable form. The response exposes a
  // `connection` summary plus the resolved release pin and derived flags.
  const formFromProfile = (profile) => ({
    name: profile.name || "",
    model: profile.model || "",
    framework_version: profile.framework_version || "code4me2-agent",
    connection_id: profile.connection?.connection_id || profile.connection_id || "",
    release_id: profile.release_id || "",
    tools: parseTools(profile.tools_json),
    approval_policy: profile.approval_policy || "per_step",
    max_steps: profile.max_steps || 15,
    max_context_tokens:
      profile.max_context_tokens === null || profile.max_context_tokens === undefined ? "" : profile.max_context_tokens,
    is_active: profile.is_active !== undefined ? Boolean(profile.is_active) : true,
    temperature: profile.temperature === null || profile.temperature === undefined ? "" : profile.temperature,
    system_prompt: profile.system_prompt || "",
  });

  const handleEdit = (profile) => {
    setEditingProfileId(getProfileId(profile));
    setForm(formFromProfile(profile));
    setFormRevision((value) => value + 1);
    clearMessages();
    focusEditor();
  };

  // Clone populates the *create* form with an existing profile's configuration
  // under a new name, so a researcher can express v1 vs v2 as two templates that
  // differ only in their release.
  const handleClone = (profile) => {
    setEditingProfileId(null);
    setForm({ ...formFromProfile(profile), name: `${profile.name || "profile"}-copy` });
    setFormRevision((value) => value + 1);
    clearMessages();
    setNotice(`Cloning "${profile.name}". Change what differs (e.g. the release) and create the new profile.`);
    focusEditor();
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
      if (editingProfileId === profileId) setEditingProfileId(null);
      await loadProfiles();
    } else {
      setError(response.error);
      setFieldErrors(Array.isArray(response.errors) ? response.errors : []);
    }
    setIsSaving(false);
  };

  const activeFramework = frameworkMeta(form.framework_version);
  const editingProfile = editingProfileId
    ? profiles.find((profile) => getProfileId(profile) === editingProfileId) || null
    : null;

  const releaseLabel = (release) =>
    [
      release.release_id,
      release.release_version ? `v${release.release_version}` : "",
      release.qualification_status,
      release.distribution_mode,
      release.is_byoa ? "BYOA" : "",
      formatPlatforms(release.supported_platforms) === "none declared" ? "" : formatPlatforms(release.supported_platforms),
    ]
      .filter(Boolean)
      .join(" · ");

  const releaseGroups = FRAMEWORKS.map((framework) => ({
    framework,
    releases: releaseCatalogue.filter((release) => releaseFrameworks(release)[0] === framework.value),
  })).filter((group) => group.releases.length > 0);

  const disabledReason = (field) => {
    if (isByoaRuntime && BYOA_NEVER_FIELDS.includes(field)) {
      return "Not forwarded to agents that participants install themselves.";
    }
    if (field === "tools" && toolsLoaded && availableTools.length === 0) {
      return form.framework_version === "codex"
        ? "Codex manages its own tools; there is nothing to select."
        : "This runtime exposes no selectable tools.";
    }
    return "The pinned release declares no translation for this setting, so it cannot govern the agent.";
  };

  const lockedHint = (field) => (
    <p className="ui-hint profile-locked">
      <Icon name="lock" size={13} />
      {disabledReason(field)}
    </p>
  );

  const toolsEnabled = fieldEnabled("tools");
  const temperatureEnabled = fieldEnabled("temperature");
  const contextEnabled = fieldEnabled("max_context_tokens");
  const promptLength = form.system_prompt.length;

  return (
    <section className="ui-page agent-profiles-page" aria-labelledby="agent-profiles-title">
      <PageHeader
        titleId="agent-profiles-title"
        title="Agent profiles"
        description="Each profile is one experiment arm: the agent runtime and exact release, the model it may use, and the behaviour settings the runtime enforces. Studies freeze a snapshot of the profiles they select, so later edits never change a running study."
        actions={
          <>
            <button className="secondary-button" onClick={loadProfiles} disabled={isLoading} type="button">
              <Icon name="refresh" size={15} />
              Refresh
            </button>
            <button
              className="primary-button"
              type="button"
              onClick={() => {
                resetForm();
                focusEditor();
              }}
              disabled={isSaving}
            >
              <Icon name="plus" size={15} />
              New profile
            </button>
          </>
        }
      />

      {error || notice ? (
        <div className={error ? "research-error" : "research-notice"} role={error ? "alert" : "status"}>
          <div>
            <span>{error || notice}</span>
            <FieldErrors errors={fieldErrors} />
          </div>
        </div>
      ) : null}

      <div className="agent-profiles-layout">
        <aside className="ui-card profiles-list-panel" aria-label="Agent profiles list">
          <div className="profiles-list-header">
            <div className="ui-search">
              <Icon name="search" size={15} />
              <input
                className="ui-input"
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Filter profiles"
                aria-label="Filter profiles"
              />
            </div>
            <span className="ui-subtle">{sortedProfiles.length} total</span>
          </div>
          {isLoading && profiles.length === 0 ? (
            <div className="profiles-list-body">
              <Loading label="Loading agent profiles..." />
            </div>
          ) : sortedProfiles.length === 0 ? (
            <EmptyState icon="sliders" title="No agent profiles found.">
              Create one to define an agent configuration (an experiment arm) for your studies.
            </EmptyState>
          ) : visibleProfiles.length === 0 ? (
            <EmptyState icon="search" title="No matching profiles" />
          ) : (
            <ul className="profiles-list">
              {visibleProfiles.map((profile) => {
                const id = getProfileId(profile);
                const tools = parseTools(profile.tools_json);
                const framework = frameworkMeta(profile.framework_version);
                return (
                  <li key={id} className={`profile-item${editingProfileId === id ? " is-selected" : ""}`}>
                    <div className="profile-item-top">
                      <span className="profile-item-name">{profile.name}</span>
                      <div className="ui-row">
                        {profile.is_active === false ? (
                          <Badge tone="neutral">Retired</Badge>
                        ) : (
                          <Badge tone="success" dot>
                            Active
                          </Badge>
                        )}
                      </div>
                    </div>
                    <div className="profile-item-meta">
                      <Badge tone={profile.framework_version === "code4me2-agent" ? "primary" : "violet"}>
                        {framework ? framework.short : profile.framework_version || "—"}
                      </Badge>
                      <span className="ui-truncate" title={profile.model}>
                        {profile.model}
                      </span>
                    </div>
                    <dl className="profile-item-facts">
                      <div>
                        <dt>Release</dt>
                        <dd title={profile.release_id || ""}>
                          {profile.release_version ? `v${profile.release_version}` : profile.release_id || "unresolved"}{" "}
                          <span
                            className={`profile-badge ${profile.verified ? "verified" : "unverified"}`}
                          >
                            {profile.verified ? "Verified" : "Unverified"}
                          </span>
                        </dd>
                      </div>
                      <div>
                        <dt>Connection</dt>
                        <dd>
                          {profile.connection?.label || profile.connection?.connection_id || "unassigned"}
                          {profile.connection?.ready === false ? " (secret missing)" : ""}
                        </dd>
                      </div>
                      <div>
                        <dt>Policy</dt>
                        <dd>
                          {(APPROVAL_POLICIES.find((policy) => policy.value === profile.approval_policy) || {}).label ||
                            profile.approval_policy}
                          {" · "}
                          {profile.max_steps} step{profile.max_steps === 1 ? "" : "s"} · {tools.length} tool{tools.length === 1 ? "" : "s"}
                        </dd>
                      </div>
                      <div>
                        <dt>Platforms</dt>
                        <dd>{formatPlatforms(profile.supported_platforms)}</dd>
                      </div>
                    </dl>
                    <div className="profile-item-actions">
                      <button className="secondary-button button-sm" type="button" onClick={() => handleEdit(profile)} disabled={isSaving}>
                        <Icon name="pencil" size={13} />
                        Edit
                      </button>
                      <button className="secondary-button button-sm" type="button" onClick={() => handleClone(profile)} disabled={isSaving}>
                        <Icon name="copy" size={13} />
                        Clone
                      </button>
                      <button className="danger-button button-sm" type="button" onClick={() => handleDelete(profile)} disabled={isSaving}>
                        <Icon name="trash" size={13} />
                        Delete
                      </button>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </aside>

        <form className="ui-card profile-editor" onSubmit={handleSubmit} ref={editorRef} aria-labelledby="profile-editor-title">
          <div className="ui-card-header">
            <div>
              <h3 className="ui-card-title" id="profile-editor-title">
                {editingProfileId ? `Edit ${editingProfile?.name || "profile"}` : "Create profile"}
              </h3>
              <p className="ui-card-subtitle">
                {editingProfileId
                  ? "Saving creates a new immutable version; studies keep the snapshot they froze."
                  : "Define a new experiment arm."}
              </p>
            </div>
            {editingProfile ? (
              <span className={`profile-badge ${editingProfile.verified ? "verified" : "unverified"}`}>
                {editingProfile.verified ? "Verified" : "Unverified"}
              </span>
            ) : null}
          </div>

          <div className="ui-card-body profile-editor-body">
            <fieldset className="ui-fieldset">
              <legend>Identity</legend>
              <div className="ui-form-grid">
                <div className="ui-field">
                  <label className="ui-label" htmlFor="profile-name">
                    Profile name
                  </label>
                  <input
                    id="profile-name"
                    className="ui-input"
                    name="name"
                    value={form.name}
                    onChange={handleChange}
                    placeholder="my-experiment-arm"
                    disabled={isSaving}
                  />
                  <p className="ui-hint">Lowercase letters, numbers, hyphens and underscores.</p>
                </div>
                <div className="ui-field">
                  <span className="ui-label">Assignment</span>
                  <Switch
                    checked={form.is_active}
                    onChange={(checked) => setForm((current) => ({ ...current, is_active: checked }))}
                    disabled={isSaving}
                    label="Active"
                    description="Eligible for assignment in new studies."
                  />
                </div>
              </div>
            </fieldset>

            <fieldset className="ui-fieldset">
              <legend>Runtime</legend>
              <div className="ui-form-grid">
                <div className="ui-field">
                  <label className="ui-label" htmlFor="profile-framework">
                    Agent runtime
                  </label>
                  <select
                    id="profile-framework"
                    className="ui-select"
                    name="framework_version"
                    value={form.framework_version}
                    onChange={handleFrameworkChange}
                    disabled={isSaving}
                  >
                    {FRAMEWORKS.map((framework) => (
                      <option key={framework.value} value={framework.value}>
                        {framework.label}
                      </option>
                    ))}
                  </select>
                  {activeFramework ? <p className="ui-hint">{activeFramework.hint}</p> : null}
                </div>
                <div className="ui-field">
                  <label className="ui-label" htmlFor="profile-release">
                    Registered release
                  </label>
                  <select
                    id="profile-release"
                    className="ui-select"
                    name="release_id"
                    value={form.release_id}
                    onChange={handleReleaseChange}
                    disabled={isSaving}
                  >
                    <option value="">Select a registered release…</option>
                    {form.release_id && !releaseCatalogue.some((release) => release.release_id === form.release_id) ? (
                      <option value={form.release_id}>{form.release_id} (not in catalogue)</option>
                    ) : null}
                    {releaseGroups.map((group) => (
                      <optgroup key={group.framework.value} label={group.framework.label}>
                        {group.releases.map((release) => (
                          <option
                            key={release.release_id}
                            value={release.release_id}
                            disabled={!user?.is_admin && !release.verified}
                          >
                            {releaseLabel(release)}
                          </option>
                        ))}
                      </optgroup>
                    ))}
                  </select>
                  <p className="ui-hint">
                    Pins the exact approved artifact. Pin a different release in a second profile to
                    compare versions. Only administrators may pin unverified releases.
                  </p>
                </div>
              </div>
              {selectedRelease ? (
                <dl className="ui-dl profile-derived-fields">
                  <div>
                    <dt>Qualification</dt>
                    <dd>{selectedRelease.qualification_status}</dd>
                  </div>
                  <div>
                    <dt>Distribution</dt>
                    <dd>
                      {selectedRelease.distribution_mode}
                      {selectedRelease.is_byoa ? " (BYOA)" : ""}
                    </dd>
                  </div>
                  <div>
                    <dt>Release version</dt>
                    <dd>{selectedRelease.release_version || "—"}</dd>
                  </div>
                  <div>
                    <dt>Supported platforms</dt>
                    <dd>{formatPlatforms(selectedRelease.supported_platforms)}</dd>
                  </div>
                </dl>
              ) : null}
              {editingProfile && editingProfile.release_id !== form.release_id ? (
                <p className="ui-hint">
                  Currently resolved: {editingProfile.release_id || "—"}
                  {editingProfile.release_version ? ` (${editingProfile.release_version})` : ""}
                </p>
              ) : null}
              {missingBindings.length > 0 ? (
                <Alert tone="warning" title="This release cannot govern a profile">
                  It declares no translation for {missingBindings.map((field) => FIELD_LABELS[field] || field).join(", ")}
                  , which every profile sets. Choose another release or ask an administrator to import one that maps
                  these settings.
                </Alert>
              ) : null}
              {isByoaRuntime ? (
                <Alert tone="info" live={false}>
                  Participants install this agent themselves. Only settings the release translates reach the agent;
                  the others are locked below so the arm never carries a setting that would be silently ignored.
                </Alert>
              ) : null}
            </fieldset>

            <fieldset className="ui-fieldset">
              <legend>Model</legend>
              <div className="ui-form-grid">
                <div className="ui-field">
                  <label className="ui-label" htmlFor="profile-connection">
                    Provider connection
                  </label>
                  <select
                    id="profile-connection"
                    className="ui-select"
                    name="connection_id"
                    value={form.connection_id}
                    onChange={handleConnectionChange}
                    disabled={isSaving}
                  >
                    <option value="">Select an authorized connection…</option>
                    {form.connection_id &&
                    !connections.some((connection) => connection.connection_id === form.connection_id) ? (
                      <option value={form.connection_id}>{form.connection_id} (not granted)</option>
                    ) : null}
                    {connections.map((connection) => (
                      <option key={connection.connection_id} value={connection.connection_id}>
                        {connection.label}
                        {connection.ready === false ? " — secret missing" : ""}
                        {connection.is_active === false ? " (inactive)" : ""}
                      </option>
                    ))}
                  </select>
                  <p className="ui-hint">
                    {isByoaRuntime
                      ? "A participant-installed agent uses the participant's own credentials; the connection only defines which model names are allowed."
                      : "Administrator-managed. The endpoint and secret stay on the server."}
                  </p>
                </div>
                <div className="ui-field">
                  <label className="ui-label" htmlFor="profile-model">
                    Model
                  </label>
                  <select
                    id="profile-model"
                    className="ui-select"
                    name="model"
                    value={form.model}
                    onChange={handleChange}
                    disabled={isSaving || selectableModels.length === 0}
                  >
                    <option value="">{form.connection_id ? "Select a model…" : "Select a connection first…"}</option>
                    {form.model && !selectableModels.includes(form.model) ? (
                      <option value={form.model}>{form.model} (not allowed)</option>
                    ) : null}
                    {selectableModels.map((model) => (
                      <option key={model} value={model}>
                        {model}
                      </option>
                    ))}
                  </select>
                  {selectedConnection ? (
                    <p className="ui-hint">
                      {selectableModels.length} model{selectableModels.length === 1 ? "" : "s"} allowed by{" "}
                      {selectedConnection.label}
                      {selectedConnection.ready === false ? " · secret missing on the server" : ""}
                    </p>
                  ) : null}
                </div>
              </div>
            </fieldset>

            <fieldset className="ui-fieldset">
              <legend>Behaviour</legend>
              <div className="ui-form-grid">
                <div className="ui-field">
                  <label className="ui-label" htmlFor="profile-approval">
                    Approval policy
                  </label>
                  <select
                    id="profile-approval"
                    className="ui-select"
                    name="approval_policy"
                    value={form.approval_policy}
                    onChange={handleChange}
                    disabled={isSaving}
                  >
                    {APPROVAL_POLICIES.map((policy) => (
                      <option
                        key={policy.value}
                        value={policy.value}
                        disabled={Boolean(verifiedApprovals) && !verifiedApprovals.has(policy.value)}
                      >
                        {policy.label}
                      </option>
                    ))}
                  </select>
                  <p className="ui-hint">How tool calls are confirmed in the IDE.</p>
                </div>
                <div className="ui-field">
                  <label className="ui-label" htmlFor="profile-max-steps">
                    Max steps
                  </label>
                  <input
                    id="profile-max-steps"
                    className="ui-input"
                    name="max_steps"
                    type="number"
                    min="1"
                    value={form.max_steps}
                    onChange={handleChange}
                    disabled={isSaving}
                  />
                  <p className="ui-hint">Upper bound on model calls per prompt.</p>
                </div>
                <div className={`ui-field${temperatureEnabled ? "" : " is-disabled"}`}>
                  <label className="ui-label" htmlFor="profile-temperature">
                    Temperature
                  </label>
                  <input
                    id="profile-temperature"
                    className="ui-input"
                    name="temperature"
                    type="number"
                    min={TEMPERATURE_MIN}
                    max={TEMPERATURE_MAX}
                    step="0.1"
                    placeholder="Provider default"
                    value={form.temperature}
                    onChange={handleTemperatureChange}
                    disabled={isSaving || !temperatureEnabled}
                  />
                  {temperatureEnabled ? (
                    <p className="ui-hint">Blank uses the provider's default.</p>
                  ) : (
                    lockedHint("temperature")
                  )}
                </div>
                <div className={`ui-field${contextEnabled ? "" : " is-disabled"}`}>
                  <label className="ui-label" htmlFor="profile-context">
                    Max context tokens
                  </label>
                  <input
                    id="profile-context"
                    className="ui-input"
                    name="max_context_tokens"
                    type="number"
                    min="1"
                    placeholder={contextEnabled ? `Default ${DEFAULT_CONTEXT_TOKENS.toLocaleString()}` : "Not applicable"}
                    value={form.max_context_tokens}
                    onChange={handleChange}
                    disabled={isSaving || !contextEnabled}
                  />
                  {contextEnabled ? (
                    <p className="ui-hint">
                      Blank uses the runtime default of {DEFAULT_CONTEXT_TOKENS.toLocaleString()} tokens. The built-in
                      agent drops its oldest turns to stay under this estimate; compliance per model call is shown in
                      the study analytics.
                    </p>
                  ) : (
                    lockedHint("max_context_tokens")
                  )}
                </div>
              </div>

              <div className={`ui-field${toolsEnabled ? "" : " is-disabled"}`}>
                <div className="ui-row-between">
                  <span className="ui-label" id="profile-tools-label">
                    Tools
                    <span className="ui-label-meta">
                      {toolsLoaded ? `${form.tools.length} / ${availableTools.length} selected` : `${form.tools.length} selected`}
                    </span>
                  </span>
                  {toolsEnabled && toolsLoaded && availableTools.length > 0 ? (
                    <div className="ui-row">
                      <button
                        type="button"
                        className="ghost-button button-sm"
                        onClick={() => setForm((current) => ({ ...current, tools: [...availableTools] }))}
                        disabled={isSaving || allToolsSelected}
                      >
                        Select all
                      </button>
                      <button
                        type="button"
                        className="ghost-button button-sm"
                        onClick={() => setForm((current) => ({ ...current, tools: [] }))}
                        disabled={isSaving || form.tools.length === 0}
                      >
                        None
                      </button>
                    </div>
                  ) : null}
                </div>
                {!toolsEnabled ? (
                  lockedHint("tools")
                ) : toolsError ? (
                  <div className="ui-stack-sm">
                    <Alert tone="warning" title="The tools this runtime offers could not be loaded.">
                      {form.tools.length
                        ? "The profile keeps its current selection; you can remove tools below."
                        : "Retry to choose tools."}{" "}
                      ({toolsError})
                    </Alert>
                    <div className="ui-row">
                      <button type="button" className="secondary-button button-sm" onClick={() => setToolsReload((value) => value + 1)}>
                        <Icon name="refresh" size={14} />
                        Retry
                      </button>
                    </div>
                    {form.tools.length ? (
                      <div className="tool-grid" role="group" aria-labelledby="profile-tools-label">
                        {form.tools.map((tool) => (
                          <label key={tool} className="tool-option is-selected">
                            <input type="checkbox" checked onChange={() => handleToolToggle(tool)} disabled={isSaving} />
                            <span className="ui-mono">{tool}</span>
                          </label>
                        ))}
                      </div>
                    ) : null}
                  </div>
                ) : !toolsLoaded ? (
                  <p className="ui-hint">Loading tools…</p>
                ) : (
                  <div className="tool-grid" role="group" aria-labelledby="profile-tools-label">
                    {availableTools.map((tool) => (
                      <label key={tool} className={`tool-option${form.tools.includes(tool) ? " is-selected" : ""}`}>
                        <input
                          type="checkbox"
                          checked={form.tools.includes(tool)}
                          onChange={() => handleToolToggle(tool)}
                          disabled={isSaving}
                        />
                        <span className="ui-mono">{tool}</span>
                      </label>
                    ))}
                  </div>
                )}
                {toolsEnabled && toolsLoaded && availableTools.length > 0 ? (
                  <p className="ui-hint">
                    Selecting no tools is a meaningful condition, not an omission: it produces a genuinely tool-free
                    arm.
                  </p>
                ) : null}
              </div>

              {supportsSystemPrompt ? (
                <div className="ui-field">
                  <label className="ui-label" htmlFor="profile-system-prompt">
                    System prompt
                    <span className={`ui-counter${promptLength > SYSTEM_PROMPT_MAX_LENGTH ? " is-over" : ""}`}>
                      {promptLength.toLocaleString()} / {SYSTEM_PROMPT_MAX_LENGTH.toLocaleString()}
                    </span>
                  </label>
                  <textarea
                    id="profile-system-prompt"
                    className="ui-textarea"
                    name="system_prompt"
                    rows={6}
                    value={form.system_prompt}
                    onChange={handleChange}
                    placeholder="Leave blank to use the runtime's default instructions."
                    disabled={isSaving}
                    aria-invalid={promptLength > SYSTEM_PROMPT_MAX_LENGTH ? "true" : undefined}
                  />
                  <p className="ui-hint">
                    Researcher-authored instructions for this arm. They are frozen with the study like every other
                    setting; the runtime still appends the workspace location.
                  </p>
                </div>
              ) : null}
            </fieldset>
          </div>

          <div className="ui-card-footer">
            {editingProfileId || form.name ? (
              <button className="secondary-button" type="button" onClick={resetForm} disabled={isSaving}>
                {editingProfileId ? "Cancel" : "Clear"}
              </button>
            ) : null}
            <button className="primary-button" type="submit" disabled={isSaving}>
              {editingProfileId ? "Save changes" : "Create profile"}
            </button>
          </div>
        </form>
      </div>
    </section>
  );
};

export default AgentProfiles;
