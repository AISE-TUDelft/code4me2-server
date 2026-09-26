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
  MoneyInput,
  PageHeader,
  Switch,
} from "../components/common/ui";
import { parseUsdInput } from "../utils/format";
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
  // {model: {input, output, cached}} as typed (USD per million tokens).
  prices: {},
};

const splitModels = (text) =>
  String(text || "")
    .split(/[\n,]+/)
    .map((item) => item.trim())
    .filter(Boolean);

const mergeModels = (models, pendingText) =>
  Array.from(new Set([...(models || []), ...splitModels(pendingText)]));

const SECRET_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;

// Model prices: USD per million tokens, decimal strings with at most six
// places (the server's limit), never floats. Metered arms (Goose and the
// built-in agent) are charged against participant budgets with them; a model
// without a price refuses every metered call.
const PRICE_MAX_USD = 10000;
const EMPTY_PRICE = { input: "", output: "", cached: "" };
const PRICE_FIELDS = [
  ["input", "input"],
  ["output", "output"],
  ["cached", "cached input"],
];

// "0.500000" from the server → "0.50" in the editor (never fewer than two places).
const trimPrice = (value) => {
  if (value === null || value === undefined || value === "") return "";
  const parsed = parseUsdInput(value, { max: PRICE_MAX_USD });
  return parsed.ok ? parsed.value.replace(/(\.\d\d\d*?)0+$/, "$1") : String(value);
};

const pricesFrom = (modelPrices) =>
  Object.fromEntries(
    Object.entries(modelPrices && typeof modelPrices === "object" ? modelPrices : {})
      .filter(([, price]) => price && typeof price === "object")
      .map(([model, price]) => [
        model,
        {
          input: trimPrice(price.input_usd_per_million),
          output: trimPrice(price.output_usd_per_million),
          cached: trimPrice(price.cached_input_usd_per_million),
        },
      ]),
  );

// The `model_prices` request map: a price row per allowed model, or null to
// keep (or make) it unpriced; `error` when a row is incomplete or invalid.
const pricePayload = (models, prices) => {
  const result = {};
  for (const model of models) {
    const row = prices[model] || EMPTY_PRICE;
    const input = String(row.input || "").trim();
    const output = String(row.output || "").trim();
    const cached = String(row.cached || "").trim();
    if (!input && !output && !cached) {
      result[model] = null;
      continue;
    }
    if (!input || !output) {
      return {
        error: `Enter both an input and an output price for ${model} (USD per million tokens), or leave both empty.`,
      };
    }
    const parsed = {
      input: parseUsdInput(input, { max: PRICE_MAX_USD }),
      output: parseUsdInput(output, { max: PRICE_MAX_USD }),
      cached: cached ? parseUsdInput(cached, { max: PRICE_MAX_USD }) : null,
    };
    const bad = PRICE_FIELDS.find(([key]) => parsed[key] && !parsed[key].ok);
    if (bad) return { error: `${model} ${bad[1]} price: ${parsed[bad[0]].error}` };
    result[model] = {
      input_usd_per_million: parsed.input.value,
      output_usd_per_million: parsed.output.value,
      cached_input_usd_per_million: parsed.cached ? parsed.cached.value : null,
    };
  }
  return { prices: result };
};

// Priced/missing counts for a card; null when the server predates prices.
const pricingFor = (connection) => {
  const models = Array.isArray(connection.models) ? connection.models : [];
  const prices = connection.model_prices;
  const missing = Array.isArray(connection.pricing?.missing_models)
    ? connection.pricing.missing_models
    : prices && typeof prices === "object"
      ? models.filter((model) => !prices[model])
      : null;
  if (missing === null) return null;
  const missingKnown = missing.filter((model) => models.includes(model));
  return { total: models.length, priced: models.length - missingKnown.length, missing: missingKnown };
};

const Pricing = ({ pricing }) =>
  pricing ? (
    <span
      className="ui-row"
      style={{ color: pricing.missing.length ? "var(--warning-color)" : "var(--success-color)" }}
    >
      <Icon name={pricing.missing.length ? "alert" : "checkCircle"} size={15} />
      Prices: {pricing.priced} of {pricing.total} models
      {pricing.missing.length ? " — metered calls with an unpriced model are refused" : ""}
    </span>
  ) : null;

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
        prices: pricesFrom(connection.model_prices),
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

  const setPrice = (model, key, value) =>
    setForm((current) => ({
      ...current,
      prices: { ...current.prices, [model]: { ...(current.prices[model] || EMPTY_PRICE), [key]: value } },
    }));

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
    const priced = pricePayload(models, form.prices);
    if (priced.error) {
      setError(priced.error);
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
      // Every allowed model: its price, or null to keep/make it unpriced.
      // (The card switch uses payloadFor, which omits this and leaves prices alone.)
      model_prices: priced.prices,
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
              <div className="ui-span-2 ui-field">
                <span className="ui-label">Model prices (USD per million tokens)</span>
                <p className="ui-hint">
                  Goose and built-in arms are charged against participant budgets with these prices; a model
                  without a price refuses every metered call. Leave a row empty to keep the model unpriced.
                </p>
                {formModels.length === 0 ? (
                  <p className="ui-chip-input-empty">Add a model above to price it.</p>
                ) : (
                  <div className="ui-table-wrap">
                    <table className="ui-table price-table">
                      <caption className="ui-visually-hidden">Model prices</caption>
                      <thead>
                        <tr>
                          <th scope="col">Model</th>
                          <th scope="col">Input</th>
                          <th scope="col">Output</th>
                          <th scope="col">
                            Cached input <span className="ui-label-meta">optional</span>
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {formModels.map((model) => {
                          const row = form.prices[model] || EMPTY_PRICE;
                          return (
                            <tr key={model}>
                              <th scope="row" className="ui-mono" title={model}>
                                {model}
                              </th>
                              {PRICE_FIELDS.map(([key, label]) => (
                                <td key={key}>
                                  <MoneyInput
                                    value={row[key]}
                                    onChange={(value) => setPrice(model, key, value)}
                                    disabled={formDisabled}
                                    ariaLabel={`${model} ${label} price`}
                                    placeholder={key === "cached" ? "optional" : "0.00"}
                                  />
                                </td>
                              ))}
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
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
            const pricing = pricingFor(connection);
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
                <Pricing pricing={pricing} />
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
                        {connection.models.map((model) => {
                          const unpriced = pricing ? pricing.missing.includes(model) : false;
                          return (
                            <li key={model} className={`ui-chip is-static${unpriced ? " is-unpriced" : ""}`}>
                              <span className="ui-chip-text" title={model}>
                                {model}
                              </span>
                              {unpriced ? (
                                <Badge tone="warning" title="No budget price; metered calls with this model are refused">
                                  price missing
                                </Badge>
                              ) : null}
                            </li>
                          );
                        })}
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
