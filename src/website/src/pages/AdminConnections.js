import React, { useEffect, useState } from "react";
import {
  createProviderConnection,
  deleteProviderConnection,
  getProviderConnections,
  updateProviderConnection,
} from "../utils/api";
import "./research/research.css";
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
  modelsText: "",
  is_active: true,
};

const parseModels = (text) =>
  Array.from(
    new Set(
      String(text || "")
        .split(/[\n,]+/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  );

const AdminConnections = () => {
  const [connections, setConnections] = useState([]);
  const [form, setForm] = useState(EMPTY_FORM);
  const [editingId, setEditingId] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState("");
  const [fieldErrors, setFieldErrors] = useState([]);
  const [notice, setNotice] = useState("");

  const loadConnections = async () => {
    setIsLoading(true);
    setError("");
    setFieldErrors([]);
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

  const resetForm = () => {
    setForm(EMPTY_FORM);
    setEditingId("");
    clearMessages();
  };

  const handleChange = (event) => {
    const { name, value, type, checked } = event.target;
    setForm((current) => ({
      ...current,
      [name]: type === "checkbox" ? checked : value,
    }));
  };

  const validate = () => {
    if (!form.label.trim()) return "A label is required.";
    if (!/^https?:\/\//.test(form.base_url.trim())) {
      return "Base URL must start with http:// or https://.";
    }
    if (!/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(form.secret_ref.trim())) {
      return (
        "Secret name must be the NAME of an environment variable " +
        "(e.g. OPENAI_API_KEY), not the key value."
      );
    }
    if (parseModels(form.modelsText).length === 0) {
      return "Add at least one allowed model.";
    }
    return "";
  };

  const handleSubmit = async (event) => {
    event.preventDefault();
    const validationError = validate();
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
      models: parseModels(form.modelsText),
      is_active: Boolean(form.is_active),
    };

    const response = editingId
      ? await updateProviderConnection(editingId, payload)
      : await createProviderConnection(payload);

    if (response.ok) {
      setNotice(editingId ? "Provider connection updated." : "Provider connection created.");
      setForm(EMPTY_FORM);
      setEditingId("");
      await loadConnections();
    } else {
      setError(response.error);
      setFieldErrors(Array.isArray(response.errors) ? response.errors : []);
    }
    setIsSaving(false);
  };

  const handleEdit = (connection) => {
    setEditingId(connection.connection_id);
    setForm({
      label: connection.label || "",
      base_url: connection.base_url || "",
      secret_ref: connection.secret_ref || "",
      modelsText: (connection.models || []).join("\n"),
      is_active: connection.is_active !== false,
    });
    clearMessages();
  };

  const handleDelete = async (connection) => {
    const confirmed = window.confirm(
      `Delete provider connection "${connection.label}"? Profiles that pin it will no longer be usable.`,
    );
    if (!confirmed) return;
    setIsSaving(true);
    clearMessages();
    const response = await deleteProviderConnection(connection.connection_id);
    if (response.ok) {
      setNotice("Provider connection deleted.");
      if (editingId === connection.connection_id) resetForm();
      await loadConnections();
    } else {
      setError(response.error);
      setFieldErrors(Array.isArray(response.errors) ? response.errors : []);
    }
    setIsSaving(false);
  };

  return (
    <section className="admin-page" aria-labelledby="admin-connections-title">
      <header className="research-header">
        <div>
          <h2 id="admin-connections-title">Provider Connections</h2>
          <p>
            Upstream endpoints profiles may pin. Only the <strong>name</strong> of
            the deployment secret is stored here; its value is read from the
            server environment and is never sent to the browser.
          </p>
        </div>
        <button
          type="button"
          className="secondary-button"
          onClick={loadConnections}
          disabled={isLoading}
        >
          Refresh
        </button>
      </header>

      {error && (
        <p className="research-error" role="alert">
          {error}
        </p>
      )}
      {fieldErrors.length > 0 && (
        <ul className="research-error" role="alert">
          {fieldErrors.map((item, position) => (
            <li key={`${item.field || "error"}-${position}`}>
              {item.field ? `${item.field}: ` : ""}
              {item.message || item.code || "invalid value"}
            </li>
          ))}
        </ul>
      )}
      {notice && (
        <p className="research-notice" role="status">
          {notice}
        </p>
      )}

      <form className="admin-section" onSubmit={handleSubmit}>
        <h3>{editingId ? "Edit connection" : "New connection"}</h3>
        <div className="admin-form-grid">
          <label>
            Label
            <input
              name="label"
              value={form.label}
              onChange={handleChange}
              disabled={isSaving}
              required
            />
          </label>
          <label>
            Base URL
            <input
              name="base_url"
              value={form.base_url}
              onChange={handleChange}
              placeholder="https://provider.example/v1"
              disabled={isSaving}
              required
            />
          </label>
          <label>
            Secret name (env var)
            <input
              name="secret_ref"
              value={form.secret_ref}
              onChange={handleChange}
              placeholder="OPENAI_API_KEY"
              disabled={isSaving}
              required
            />
          </label>
          <label className="admin-span">
            Models (one per line)
            <textarea
              name="modelsText"
              value={form.modelsText}
              onChange={handleChange}
              rows={3}
              disabled={isSaving}
            />
          </label>
          <label className="admin-toggle">
            <input
              type="checkbox"
              name="is_active"
              checked={form.is_active}
              onChange={handleChange}
              disabled={isSaving}
            />
            Active
          </label>
        </div>
        <p className="admin-hint">
          The secret value stays in the deployment environment. Enter only the
          environment variable name, never a key.
        </p>
        <div className="admin-actions">
          <button type="submit" className="primary-button" disabled={isSaving}>
            {editingId ? "Save connection" : "Create connection"}
          </button>
          {editingId && (
            <button
              type="button"
              className="secondary-button"
              onClick={resetForm}
              disabled={isSaving}
            >
              Cancel
            </button>
          )}
        </div>
      </form>

      {isLoading && (
        <p className="research-hint" role="status">
          Loading provider connections...
        </p>
      )}
      {!isLoading && !error && connections.length === 0 && (
        <p className="research-hint">No provider connections yet.</p>
      )}

      {connections.length > 0 && (
        <div className="admin-section">
          <table className="admin-table">
            <thead>
              <tr>
                <th scope="col">Label</th>
                <th scope="col">Base URL</th>
                <th scope="col">Secret name</th>
                <th scope="col">Models</th>
                <th scope="col">Active</th>
                <th scope="col">Readiness</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {connections.map((connection) => (
                <tr key={connection.connection_id}>
                  <td>{connection.label}</td>
                  <td>
                    <code>{connection.base_url || "—"}</code>
                  </td>
                  <td>
                    <code>{connection.secret_ref || "—"}</code>
                  </td>
                  <td>{(connection.models || []).join(", ") || "—"}</td>
                  <td>{connection.is_active ? "Yes" : "No"}</td>
                  <td>
                    {connection.ready ? (
                      <span className="admin-status-badge ready">
                        Ready — secret present in deployment
                      </span>
                    ) : (
                      <span className="admin-status-badge not-ready">
                        Not ready — secret missing or inactive
                      </span>
                    )}
                  </td>
                  <td>
                    <div className="admin-actions">
                      <button
                        type="button"
                        className="secondary-button"
                        onClick={() => handleEdit(connection)}
                        disabled={isSaving}
                      >
                        Edit
                      </button>
                      <button
                        type="button"
                        className="danger-button"
                        onClick={() => handleDelete(connection)}
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
        </div>
      )}
    </section>
  );
};

export default AdminConnections;
