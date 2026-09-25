import React, { useEffect, useRef, useState } from "react";
import {
  createProviderConnection,
  deleteProviderConnection,
  getProviderConnections,
  updateProviderConnection,
} from "../utils/api";
import Icon from "../components/common/Icon";
import {
  Badge,
  Card,
  ChipInput,
  EmptyState,
  Field,
  FieldErrors,
  Loading,
  PageHeader,
  Switch,
} from "../components/common/ui";
import "./AdminPages.css";

/**
 * Admin-only provider connection registry. A connection owns the upstream
 * endpoint and the *name* of the deployment secret; the secret value is never
 * sent to or rendered by the browser. `ready` is the server's derived
 * "secret present in this deployment" flag.
 */
const EMPTY_FORM = {
  label: "",
  base_url: "",
  secret_ref: "",
  models: [],
  is_active: true,
};

const splitModels = (text) =>
  String(text || "")
    .split(/[\n,]+/)
    .map((item) => item.trim())
    .filter(Boolean);

const mergeModels = (models, pendingText) =>
  Array.from(new Set([...(models || []), ...splitModels(pendingText)]));

const SECRET_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;

const payloadFor = (connection, overrides = {}) => ({
  label: connection.label,
  base_url: connection.base_url,
  secret_ref: connection.secret_ref,
  models: connection.models || [],
  is_active: connection.is_active !== false,
  ...overrides,
});

const Readiness = ({ ready }) =>
  ready ? (
    <span className="ui-row ui-muted" style={{ color: "var(--success-color)" }}>
      <Icon name="checkCircle" size={15} />
      Ready — secret present in deployment
    </span>
  ) : (
    <span className="ui-row" style={{ color: "var(--warning-color)" }}>
      <Icon name="alert" size={15} />
      Not ready — secret missing or inactive
    </span>
  );

const AdminConnections = () => {
  const [connections, setConnections] = useState([]);
  const [form, setForm] = useState(EMPTY_FORM);
  const [pendingModel, setPendingModel] = useState("");
  const [editingId, setEditingId] = useState("");
  const [isEditorOpen, setIsEditorOpen] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [busyId, setBusyId] = useState("");
  const [error, setError] = useState("");
  const [fieldErrors, setFieldErrors] = useState([]);
  const [notice, setNotice] = useState("");
  const editorRef = useRef(null);
  // A typed conflict on the label (409 "A connection with that label already
  // exists") belongs on the field, not in the page-level error list.
  const labelError = (fieldErrors.find((item) => item && item.field === "label") || {}).message || "";

  const loadConnections = async () => {
    setIsLoading(true);
    const response = await getProviderConnections();
    if (response.ok) {
      setConnections(Array.isArray(response.data) ? response.data : []);
    } else {
      setError(response.error);
      setFieldErrors(Array.isArray(response.errors) ? response.errors : []);
    }
    setIsLoading(false);
  };

  useEffect(() => {
    loadConnections();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const clearMessages = () => {
    setError("");
    setFieldErrors([]);
    setNotice("");
  };

  const openEditor = (connection) => {
    clearMessages();
    if (connection) {
      setEditingId(connection.connection_id);
      setForm({
        label: connection.label || "",
        base_url: connection.base_url || "",
        secret_ref: connection.secret_ref || "",
        models: Array.isArray(connection.models) ? connection.models : [],
        is_active: connection.is_active !== false,
      });
    } else {
      setEditingId("");
      setForm(EMPTY_FORM);
    }
    setPendingModel("");
    setIsEditorOpen(true);
    setTimeout(() => {
      if (editorRef.current) {
        if (editorRef.current.scrollIntoView) {
          editorRef.current.scrollIntoView({ behavior: "smooth", block: "start" });
        }
        const first = editorRef.current.querySelector("input");
        if (first) first.focus();
      }
    }, 0);
  };

  const closeEditor = () => {
    setIsEditorOpen(false);
    setEditingId("");
    setForm(EMPTY_FORM);
    setPendingModel("");
  };

  const handleChange = (event) => {
    const { name, value } = event.target;
    setForm((current) => ({ ...current, [name]: value }));
  };

  const validate = (models) => {
    if (!form.label.trim()) return "A label is required.";
    if (!/^https?:\/\//.test(form.base_url.trim())) {
      return "Base URL must start with http:// or https://.";
    }
    if (!SECRET_NAME.test(form.secret_ref.trim())) {
      return (
        "Secret name must be the NAME of an environment variable " +
        "(e.g. OPENAI_API_KEY), not the key value."
      );
    }
    if (models.length === 0) return "Add at least one allowed model.";
    return "";
  };

  const handleSubmit = async (event) => {
    event.preventDefault();
    // Text typed into the model box but not yet added still counts.
    const models = mergeModels(form.models, pendingModel);
    const validationError = validate(models);
    if (validationError) {
      setError(validationError);
      setFieldErrors([]);
      return;
    }
    setIsSaving(true);
    clearMessages();
    const payload = {
      label: form.label.trim(),
      base_url: form.base_url.trim(),
      secret_ref: form.secret_ref.trim(),
      models,
      is_active: Boolean(form.is_active),
    };
    const response = editingId
      ? await updateProviderConnection(editingId, payload)
      : await createProviderConnection(payload);
    if (response.ok) {
      setNotice(editingId ? "Provider connection updated." : "Provider connection created.");
      closeEditor();
      await loadConnections();
    } else {
      setError(response.error);
      setFieldErrors(Array.isArray(response.errors) ? response.errors : []);
    }
    setIsSaving(false);
  };

  const toggleActive = async (connection, isActive) => {
    setBusyId(connection.connection_id);
    clearMessages();
    const response = await updateProviderConnection(
      connection.connection_id,
      payloadFor(connection, { is_active: isActive }),
    );
    if (response.ok) {
      setNotice(`${connection.label} is now ${isActive ? "active" : "inactive"}.`);
      await loadConnections();
    } else {
      setError(response.error);
      setFieldErrors(Array.isArray(response.errors) ? response.errors : []);
    }
    setBusyId("");
  };

  const handleDelete = async (connection) => {
    const usage = Number(connection.profile_count || 0);
    const confirmed = window.confirm(
      usage > 0
        ? `"${connection.label}" is used by ${usage} agent profile${usage === 1 ? "" : "s"}. The server will refuse to delete it until those profiles use another connection. Try anyway?`
        : `Delete provider connection "${connection.label}"? This cannot be undone.`,
    );
    if (!confirmed) return;
    setBusyId(connection.connection_id);
    clearMessages();
    const response = await deleteProviderConnection(connection.connection_id);
    if (response.ok) {
      setNotice("Provider connection deleted.");
      if (editingId === connection.connection_id) closeEditor();
      await loadConnections();
    } else {
      setError(response.error);
      setFieldErrors(Array.isArray(response.errors) ? response.errors : []);
    }
    setBusyId("");
  };

  const formModels = form.models;
  const formDisabled = isSaving;

  return (
    <section className="ui-page admin-page" aria-labelledby="admin-connections-title">
      <PageHeader
        titleId="admin-connections-title"
        title="Provider connections"
        description={
          <>
            Upstream model endpoints that agent profiles can pin. Only the <strong>name</strong> of
            the deployment secret is stored; its value is read from the server environment and is
            never sent to the browser.
          </>
        }
        actions={
          <>
            <button type="button" className="secondary-button" onClick={loadConnections} disabled={isLoading}>
              <Icon name="refresh" size={15} />
              Refresh
            </button>
            <button type="button" className="primary-button" onClick={() => openEditor(null)} disabled={isSaving}>
              <Icon name="plus" size={15} />
              New connection
            </button>
          </>
        }
      />

      {error && fieldErrors.length === 0 ? (
        <p className="research-error" role="alert">
          {error}
        </p>
      ) : null}
      <FieldErrors errors={fieldErrors.filter((item) => item.field !== "label")} />
      {notice ? (
        <p className="research-notice" role="status">
          {notice}
        </p>
      ) : null}

      {isEditorOpen ? (
        <div ref={editorRef}>
          <Card
            as="form"
            onSubmit={handleSubmit}
            title={editingId ? `Edit ${form.label || "connection"}` : "New connection"}
            subtitle="Researchers see active connections and may pick any of the models listed here."
            footer={
              <>
                <button type="button" className="secondary-button" onClick={closeEditor} disabled={formDisabled}>
                  Cancel
                </button>
                <button type="submit" className="primary-button" disabled={formDisabled}>
                  {editingId ? "Save connection" : "Create connection"}
                </button>
              </>
            }
          >
            <div className="ui-form-grid">
              <Field
                label="Label"
                htmlFor="connection-label"
                hint="Shown to researchers when they pick a connection."
                error={labelError}
              >
                <input
                  id="connection-label"
                  className="ui-input"
                  name="label"
                  value={form.label}
                  onChange={handleChange}
                  placeholder="openrouter-main"
                  disabled={formDisabled}
                  required
                />
              </Field>
              <Field label="Base URL" htmlFor="connection-base-url" hint="OpenAI-compatible endpoint, e.g. …/v1.">
                <input
                  id="connection-base-url"
                  className="ui-input"
                  name="base_url"
                  value={form.base_url}
                  onChange={handleChange}
                  placeholder="https://provider.example/v1"
                  disabled={formDisabled}
                  required
                />
              </Field>
              <Field
                label="Secret name (env var)"
                htmlFor="connection-secret-ref"
                hint="The environment variable that holds the key on the server — never the key itself."
              >
                <input
                  id="connection-secret-ref"
                  className="ui-input ui-mono"
                  name="secret_ref"
                  value={form.secret_ref}
                  onChange={handleChange}
                  placeholder="OPENAI_API_KEY"
                  autoComplete="off"
                  spellCheck={false}
                  disabled={formDisabled}
                  required
                />
              </Field>
              <div className="ui-field">
                <span className="ui-label">Availability</span>
                <Switch
                  checked={form.is_active}
                  onChange={(checked) => setForm((current) => ({ ...current, is_active: checked }))}
                  disabled={formDisabled}
                  ariaLabel="Availability"
                  label={form.is_active ? "Active" : "Inactive"}
                  description={
                    form.is_active
                      ? "Researchers can select it; inference is allowed."
                      : "Hidden from researchers; inference is refused."
                  }
                />
              </div>
              <div className="ui-span-2">
                <ChipInput
                  id="connection-models"
                  label="Models"
                  values={formModels}
                  onChange={(models) => setForm((current) => ({ ...current, models }))}
                  pending={pendingModel}
                  onPendingChange={setPendingModel}
                  placeholder="e.g. openai/gpt-4o-mini — paste several separated by commas or new lines"
                  addLabel="Add model"
                  disabled={formDisabled}
                  emptyText="No models yet. Profiles can only use models listed here."
                  hint="Press Enter or + to add. Pasting a list adds every model at once."
                />
              </div>
            </div>
          </Card>
        </div>
      ) : null}

      {isLoading && connections.length === 0 ? <Loading label="Loading provider connections..." /> : null}

      {!isLoading && !error && connections.length === 0 ? (
        <Card>
          <EmptyState
            icon="plug"
            title="No provider connections yet."
            action={
              <button type="button" className="primary-button" onClick={() => openEditor(null)}>
                <Icon name="plus" size={15} />
                Add the first connection
              </button>
            }
          >
            Agent profiles pin a connection to reach a model provider. Add one to let researchers
            create profiles.
          </EmptyState>
        </Card>
      ) : null}

      {connections.length > 0 ? (
        <div className={`connection-grid${isLoading ? " is-refreshing" : ""}`}>
          {connections.map((connection) => {
            const busy = busyId === connection.connection_id;
            const usage = connection.profile_count;
            return (
              <Card
                key={connection.connection_id}
                className={`connection-card${connection.is_active ? "" : " is-inactive"}`}
                title={
                  <span className="connection-card-title">
                    <Icon name="plug" size={16} />
                    {connection.label}
                    {connection.is_active ? (
                      <Badge tone="success" dot>
                        Active
                      </Badge>
                    ) : (
                      <Badge tone="neutral" dot>
                        Inactive
                      </Badge>
                    )}
                  </span>
                }
                actions={
                  <Switch
                    checked={connection.is_active !== false}
                    disabled={busy || isSaving}
                    ariaLabel={`${connection.label} active`}
                    onChange={(checked) => toggleActive(connection, checked)}
                  />
                }
                footer={
                  <>
                    <button
                      type="button"
                      className="secondary-button button-sm"
                      onClick={() => openEditor(connection)}
                      disabled={busy || isSaving}
                    >
                      <Icon name="pencil" size={14} />
                      Edit
                    </button>
                    <button
                      type="button"
                      className="danger-button button-sm"
                      onClick={() => handleDelete(connection)}
                      disabled={busy || isSaving}
                    >
                      <Icon name="trash" size={14} />
                      Delete
                    </button>
                  </>
                }
              >
                <Readiness ready={connection.ready} />
                <dl className="connection-meta">
                  <dt>Endpoint</dt>
                  <dd>
                    <code>{connection.base_url || "—"}</code>
                  </dd>
                  <dt>Secret name</dt>
                  <dd>
                    <code>{connection.secret_ref || "—"}</code>
                  </dd>
                  <dt>Used by</dt>
                  <dd>
                    {usage === undefined || usage === null
                      ? "—"
                      : `${usage} agent profile${usage === 1 ? "" : "s"}`}
                  </dd>
                  <dt>Models</dt>
                  <dd>
                    {(connection.models || []).length ? (
                      <ul className="ui-chips" style={{ listStyle: "none", margin: 0, padding: 0 }}>
                        {connection.models.map((model) => (
                          <li key={model} className="ui-chip is-static">
                            <span className="ui-chip-text" title={model}>
                              {model}
                            </span>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      "—"
                    )}
                  </dd>
                </dl>
              </Card>
            );
          })}
        </div>
      ) : null}
    </section>
  );
};

export default AdminConnections;
