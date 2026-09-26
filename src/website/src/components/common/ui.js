import React, { useEffect, useId, useRef, useState } from "react";
import Icon from "./Icon";
import { parseUsdInput } from "../../utils/format";

/*
 * Shared building blocks for the dashboard, research and admin pages. They
 * only emit markup + class names from styles/ui.css, so every page renders
 * the same controls with the same spacing in both themes.
 */

export const PageHeader = ({ title, description, actions, titleId, eyebrow }) => (
  <header className="ui-page-header">
    <div>
      {eyebrow ? <p className="ui-section-title">{eyebrow}</p> : null}
      <h2 className="ui-page-title" id={titleId}>
        {title}
      </h2>
      {description ? <p className="ui-page-description">{description}</p> : null}
    </div>
    {actions ? <div className="ui-page-actions">{actions}</div> : null}
  </header>
);

export const Card = ({
  title,
  subtitle,
  actions,
  children,
  footer,
  className = "",
  bodyClassName = "",
  headerPlain = false,
  as: Element = "section",
  ...rest
}) => (
  <Element className={`ui-card ${className}`.trim()} {...rest}>
    {(title || actions) && (
      <div className={`ui-card-header${headerPlain ? " is-plain" : ""}`}>
        <div>
          {title ? <h3 className="ui-card-title">{title}</h3> : null}
          {subtitle ? <p className="ui-card-subtitle">{subtitle}</p> : null}
        </div>
        {actions ? <div className="ui-row">{actions}</div> : null}
      </div>
    )}
    {children !== undefined && children !== null && children !== false ? (
      <div className={`ui-card-body ${bodyClassName}`.trim()}>{children}</div>
    ) : null}
    {footer ? <div className="ui-card-footer">{footer}</div> : null}
  </Element>
);

const ALERT_ICONS = { danger: "alert", success: "checkCircle", warning: "alert", info: "info" };

// `live={false}` marks a static note (no live-region role), so only real
// status changes are announced.
export const Alert = ({ tone = "info", title, children, onDismiss, className = "", live = true }) => (
  <div
    className={`ui-alert ui-alert-${tone} ${className}`.trim()}
    role={live ? (tone === "danger" ? "alert" : "status") : undefined}
  >
    <Icon name={ALERT_ICONS[tone] || "info"} size={16} />
    <div className="ui-alert-body">
      {title ? <span className="ui-alert-title">{title}</span> : null}
      {children ? <span className={title ? "ui-alert-text" : undefined}>{children}</span> : null}
    </div>
    {onDismiss ? (
      <button type="button" className="ui-alert-dismiss" onClick={onDismiss} aria-label="Dismiss">
        <Icon name="x" size={14} />
      </button>
    ) : null}
  </div>
);

export const Badge = ({ tone = "neutral", children, dot = false, title, className = "" }) => (
  <span className={`ui-badge ui-badge-${tone} ${className}`.trim()} title={title}>
    {dot ? <span className="ui-dot" aria-hidden="true" /> : null}
    {children}
  </span>
);

export const Field = ({ label, htmlFor, hint, error, meta, children, disabled, className = "" }) => (
  <div className={`ui-field${disabled ? " is-disabled" : ""} ${className}`.trim()}>
    {label ? (
      <label className="ui-label" htmlFor={htmlFor}>
        {label}
        {meta ? <span className="ui-label-meta">{meta}</span> : null}
      </label>
    ) : null}
    {children}
    {error ? <p className="ui-field-error">{error}</p> : null}
    {hint ? <p className="ui-hint">{hint}</p> : null}
  </div>
);

/**
 * Accessible on/off control. Renders a real checkbox with role="switch", so
 * it is keyboard operable and announced as a switch with its state.
 */
export const Switch = ({
  checked,
  onChange,
  label,
  description,
  disabled = false,
  ariaLabel,
  name,
  id,
  className = "",
}) => {
  const generated = useId();
  const inputId = id || `switch-${generated}`;
  const descriptionId = `${inputId}-description`;
  // With an explicit name, the visible text is the on/off state (as in the
  // ARIA switch pattern): hidden from assistive tech, which reads the state
  // from aria-checked, while the description stays linked.
  const named = Boolean(ariaLabel);
  return (
    <label className={`ui-switch${disabled ? " is-disabled" : ""} ${className}`.trim()} htmlFor={inputId}>
      <input
        id={inputId}
        type="checkbox"
        role="switch"
        name={name}
        checked={Boolean(checked)}
        aria-checked={Boolean(checked)}
        aria-label={ariaLabel}
        aria-describedby={named && description ? descriptionId : undefined}
        disabled={disabled}
        onChange={(event) => onChange && onChange(event.target.checked, event)}
      />
      <span className="ui-switch-track" aria-hidden="true" />
      {label || description ? (
        <span className="ui-switch-label">
          {label ? named ? <span aria-hidden="true">{label}</span> : label : null}
          {description ? <small id={descriptionId}>{description}</small> : null}
        </span>
      ) : null}
    </label>
  );
};

const splitEntries = (text) =>
  String(text || "")
    .split(/[\n,]+/)
    .map((item) => item.trim())
    .filter(Boolean);

/**
 * A list editor for short values (e.g. model ids): type one value — or paste
 * several separated by commas/new lines — and press Enter or the + button.
 * `pending` text that was typed but not yet added is exposed through
 * `onPendingChange` so a surrounding form can include it on submit.
 */
export const ChipInput = ({
  id,
  label,
  values,
  onChange,
  placeholder,
  disabled = false,
  addLabel = "Add",
  hint,
  error,
  pending = "",
  onPendingChange,
  emptyText = "Nothing added yet.",
}) => {
  const generated = useId();
  const inputId = id || `chips-${generated}`;
  const [draft, setDraft] = useState(pending);
  useEffect(() => setDraft(pending), [pending]);

  const updateDraft = (text) => {
    setDraft(text);
    if (onPendingChange) onPendingChange(text);
  };

  const commit = (text = draft) => {
    const additions = splitEntries(text);
    if (additions.length === 0) return;
    const next = Array.from(new Set([...(values || []), ...additions]));
    onChange(next);
    updateDraft("");
  };

  const remove = (value) => onChange((values || []).filter((item) => item !== value));

  return (
    <div className={`ui-field${disabled ? " is-disabled" : ""}`}>
      {label ? (
        <label className="ui-label" htmlFor={inputId}>
          {label}
          <span className="ui-label-meta">{(values || []).length} added</span>
        </label>
      ) : null}
      <div className="ui-input-group">
        <input
          id={inputId}
          className="ui-input"
          value={draft}
          placeholder={placeholder}
          disabled={disabled}
          aria-invalid={error ? "true" : undefined}
          onChange={(event) => {
            const text = event.target.value;
            // Pasting or typing a separator commits immediately.
            if (/[\n,]/.test(text)) {
              commit(text);
            } else {
              updateDraft(text);
            }
          }}
          onPaste={(event) => {
            const text = event.clipboardData ? event.clipboardData.getData("text") : "";
            if (/[\n,]/.test(text)) {
              event.preventDefault();
              commit(`${draft}${text}`);
            }
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              commit();
            } else if (event.key === "Backspace" && !draft && (values || []).length > 0) {
              remove(values[values.length - 1]);
            }
          }}
        />
        <button
          type="button"
          className="secondary-button"
          onClick={() => commit()}
          disabled={disabled || !draft.trim()}
          aria-label={addLabel}
          title={addLabel}
        >
          <Icon name="plus" size={15} />
          <span className="ui-nowrap">Add</span>
        </button>
      </div>
      {(values || []).length > 0 ? (
        <ul className="ui-chips" aria-label={label ? `${label} list` : "Added values"} style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {values.map((value) => (
            <li key={value} className="ui-chip">
              <span className="ui-chip-text" title={value}>
                {value}
              </span>
              <button
                type="button"
                className="ui-chip-remove"
                onClick={() => remove(value)}
                disabled={disabled}
                aria-label={`Remove ${value}`}
              >
                <Icon name="x" size={12} />
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="ui-chip-input-empty">{emptyText}</p>
      )}
      {error ? <p className="ui-field-error">{error}</p> : null}
      {hint ? <p className="ui-hint">{hint}</p> : null}
    </div>
  );
};

export const EmptyState = ({ icon = "info", title, children, action }) => (
  <div className="ui-empty">
    <span className="ui-empty-icon">
      <Icon name={icon} size={20} />
    </span>
    {title ? <p className="ui-empty-title">{title}</p> : null}
    {children ? <p className="ui-empty-text">{children}</p> : null}
    {action || null}
  </div>
);

export const Loading = ({ label = "Loading…" }) => (
  <div className="ui-loading" role="status">
    <span className="ui-spinner" aria-hidden="true" />
    {label}
  </div>
);

export const CopyButton = ({ value, label = "Copy", className = "icon-button" }) => {
  const [copied, setCopied] = useState(false);
  const timer = useRef(null);
  useEffect(() => () => clearTimeout(timer.current), []);
  const copy = async () => {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(String(value));
      } else {
        const area = document.createElement("textarea");
        area.value = String(value);
        document.body.appendChild(area);
        area.select();
        document.execCommand("copy");
        document.body.removeChild(area);
      }
      setCopied(true);
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 1600);
    } catch (_) {
      setCopied(false);
    }
  };
  return (
    <button type="button" className={className} onClick={copy} title={copied ? "Copied" : label} aria-label={copied ? "Copied" : label}>
      <Icon name={copied ? "check" : "copy"} size={15} />
    </button>
  );
};

export const Tabs = ({ tabs, active, onChange, label = "Sections", idPrefix = "tab" }) => (
  <div className="ui-tabs" role="tablist" aria-label={label}>
    {tabs.map((tab) => (
      <button
        key={tab.id}
        type="button"
        role="tab"
        id={`${idPrefix}-${tab.id}`}
        aria-selected={active === tab.id}
        // Only the active panel is rendered, so only its tab may point at it.
        aria-controls={active === tab.id ? `${idPrefix}-panel-${tab.id}` : undefined}
        tabIndex={active === tab.id ? 0 : -1}
        className="ui-tab"
        onClick={() => onChange(tab.id)}
        onKeyDown={(event) => {
          const index = tabs.findIndex((item) => item.id === tab.id);
          const targets = {
            ArrowRight: tabs[(index + 1) % tabs.length],
            ArrowLeft: tabs[(index + tabs.length - 1) % tabs.length],
            Home: tabs[0],
            End: tabs[tabs.length - 1],
          };
          const next = targets[event.key];
          if (!next) return;
          event.preventDefault();
          onChange(next.id);
          const element = document.getElementById(`${idPrefix}-${next.id}`);
          if (element) element.focus();
        }}
      >
        {tab.icon ? <Icon name={tab.icon} size={15} /> : null}
        {tab.label}
        {tab.count !== undefined && tab.count !== null ? <span className="ui-tab-count">{tab.count}</span> : null}
      </button>
    ))}
  </div>
);

export const TabPanel = ({ id, idPrefix = "tab", children }) => (
  <div role="tabpanel" id={`${idPrefix}-panel-${id}`} aria-labelledby={`${idPrefix}-${id}`} className="ui-stack">
    {children}
  </div>
);

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

// In the tab order, enabled (a disabled fieldset included) and not hidden.
// `checkVisibility` also catches CSS-hidden elements where the browser has it
// (jsdom does not).
const isTabbable = (element) =>
  element.tabIndex >= 0 &&
  !element.matches(":disabled") &&
  !element.closest("[hidden], [inert], [aria-hidden='true']") &&
  (typeof element.checkVisibility !== "function" ||
    element.checkVisibility({ visibilityProperty: true, checkVisibilityCSS: true }));

/**
 * Right-side panel for drill-downs; Esc and the backdrop close it. Focus stays
 * inside the panel while it is open and returns to the opener afterwards.
 */
export const Drawer = ({ open, title, subtitle, onClose, actions, children, labelId }) => {
  const closeRef = useRef(null);
  const panelRef = useRef(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const generated = useId();
  const headingId = labelId || `drawer-${generated}`;
  useEffect(() => {
    if (!open) return undefined;
    const previous = document.activeElement;
    const onKey = (event) => {
      if (event.key === "Escape") {
        onCloseRef.current();
        return;
      }
      const panel = panelRef.current;
      if (event.key !== "Tab" || !panel) return;
      const focusable = Array.from(panel.querySelectorAll(FOCUSABLE)).filter(isTabbable);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const inside = panel.contains(document.activeElement);
      if (event.shiftKey && (!inside || document.activeElement === first)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (!inside || document.activeElement === last)) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    const body = document.body;
    const overflow = body.style.overflow;
    body.style.overflow = "hidden";
    if (closeRef.current) closeRef.current.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      body.style.overflow = overflow;
      if (previous && previous.focus) previous.focus();
    };
  }, [open]);
  if (!open) return null;
  return (
    <>
      <div className="ui-drawer-backdrop" onClick={onClose} aria-hidden="true" />
      <aside className="ui-drawer" role="dialog" aria-modal="true" aria-labelledby={headingId} ref={panelRef}>
        <div className="ui-drawer-header">
          <div>
            <h2 className="ui-page-title" id={headingId} style={{ fontSize: 19 }}>
              {title}
            </h2>
            {subtitle ? <div className="ui-card-subtitle">{subtitle}</div> : null}
          </div>
          <div className="ui-row">
            {actions}
            <button type="button" className="icon-button" onClick={onClose} ref={closeRef} aria-label="Close">
              <Icon name="x" size={18} />
            </button>
          </div>
        </div>
        <div className="ui-drawer-body">{children}</div>
      </aside>
    </>
  );
};

export const KpiTile = ({ label, value, detail, title }) => (
  <div className="ui-kpi" title={title}>
    <span className="ui-kpi-label">{label}</span>
    <span className="ui-kpi-value">{value}</span>
    {detail ? <span className="ui-kpi-detail">{detail}</span> : null}
  </div>
);

export const SegmentedControl = ({ options, value, onChange, label }) => (
  <div className="ui-segmented" role="group" aria-label={label}>
    {options.map((option) => (
      <button
        key={option.value}
        type="button"
        aria-pressed={value === option.value}
        className={value === option.value ? "is-active" : undefined}
        onClick={() => onChange(option.value)}
      >
        {option.label}
      </button>
    ))}
  </div>
);

/** Renders typed field errors returned by the API helpers. */
export const FieldErrors = ({ errors }) =>
  Array.isArray(errors) && errors.length > 0 ? (
    <ul className="research-field-errors" role="alert">
      {errors.map((item, position) => (
        <li key={`${item.field || "error"}-${position}`}>
          {item.field ? <code>{item.field}</code> : null} {item.message || item.code || "invalid value"}
        </li>
      ))}
    </ul>
  ) : null;

/**
 * Bounded quantity such as a participant budget: `value` used out of `max`
 * (plain numbers; integer micro-USD works). The tone follows the share used
 * unless given: ok, warning at `warningFraction`, exhausted at the limit.
 */
export const Meter = ({
  value,
  max,
  label,
  tone,
  warningFraction = 0.8,
  exhausted = false,
  valueText,
  className = "",
}) => {
  const total = Number(max) > 0 ? Number(max) : 0;
  const used = Math.max(0, Number(value) || 0);
  const fraction = total > 0 ? Math.min(used / total, 1) : used > 0 ? 1 : 0;
  const atLimit = total > 0 ? used >= total : used > 0;
  const resolved = tone || (exhausted || atLimit ? "exhausted" : fraction >= warningFraction ? "warning" : "ok");
  return (
    <span
      className={`ui-meter is-${resolved} ${className}`.trim()}
      role="meter"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={total}
      aria-valuenow={Math.min(used, total)}
      aria-valuetext={valueText}
    >
      <span className="ui-meter-fill" style={{ width: `${Math.round(fraction * 1000) / 10}%` }} />
    </span>
  );
};

/**
 * Decimal USD text input with a "$" prefix. Deliberately not type="number"
 * (that drops trailing zeros and accepts exponents): the raw text is handed
 * to the parent, which parses it with `parseUsdInput`. A non-empty value that
 * does not parse is flagged through aria-invalid unless `invalid` is given.
 */
export const MoneyInput = ({
  id,
  name,
  value,
  onChange,
  disabled = false,
  invalid,
  placeholder = "0.00",
  ariaLabel,
  describedBy,
  required = false,
  className = "",
}) => {
  const text = value === null || value === undefined ? "" : String(value);
  const bad = invalid !== undefined ? Boolean(invalid) : Boolean(text.trim()) && !parseUsdInput(text).ok;
  return (
    <span className={`ui-money${disabled ? " is-disabled" : ""} ${className}`.trim()}>
      <span className="ui-money-prefix" aria-hidden="true">
        $
      </span>
      <input
        id={id}
        name={name}
        className="ui-input"
        type="text"
        inputMode="decimal"
        autoComplete="off"
        spellCheck={false}
        value={text}
        onChange={(event) => onChange && onChange(event.target.value, event)}
        disabled={disabled}
        placeholder={placeholder}
        aria-label={ariaLabel}
        aria-describedby={describedBy}
        aria-invalid={bad ? "true" : undefined}
        required={required}
      />
    </span>
  );
};
