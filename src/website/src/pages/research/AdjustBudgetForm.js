import React, { useRef, useState } from "react";
import { FieldErrors, MoneyInput, SegmentedControl } from "../../components/common/ui";
import { formatUsd, parseUsdInput } from "../../utils/format";
import { newIdempotencyKey } from "./studyUtils";

const KINDS = [
  { value: "TOP_UP", label: "Top up" },
  { value: "SET_LIMIT", label: "Set new limit" },
];

/**
 * Top up or replace one participant's budget limit, with the audited reason
 * the server requires. The idempotency key is minted when the form opens and
 * rotated only after a success or an edit, so resubmitting the same failed
 * request replays it on the server instead of applying it twice.
 */
const AdjustBudgetForm = ({ currentLimitMicro = 0, isBusy = false, onSubmit, onCancel }) => {
  const [kind, setKind] = useState("TOP_UP");
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [fieldErrors, setFieldErrors] = useState([]);
  const keyRef = useRef(newIdempotencyKey("adjust"));

  const rotateKey = () => {
    keyRef.current = newIdempotencyKey("adjust");
  };
  // An edited request is a new request: it gets its own key.
  const edit = (setter) => (value) => {
    setter(value);
    rotateKey();
    setError("");
    setFieldErrors([]);
  };

  const parsed = parseUsdInput(amount);
  const amountValid = parsed.ok && (kind === "SET_LIMIT" || parsed.micro > 0);
  const amountError = amount.trim() && !amountValid ? (parsed.ok ? "A top-up must be greater than zero." : parsed.error) : "";
  const reasonText = reason.trim();
  const reasonValid = reasonText.length > 0 && reasonText.length <= 500;
  const currentLimit = Number(currentLimitMicro) || 0;
  const newLimit = amountValid ? (kind === "TOP_UP" ? currentLimit + parsed.micro : parsed.micro) : null;
  const canSubmit = !isBusy && amountValid && reasonValid;

  const submit = async (event) => {
    event.preventDefault();
    if (!canSubmit) return;
    const result = await onSubmit({ kind, amountUsd: parsed.value, reason: reasonText, idempotencyKey: keyRef.current });
    if (result && result.ok) {
      rotateKey();
      setAmount("");
      setReason("");
      setError("");
      setFieldErrors([]);
      return;
    }
    // The key is kept for a retry; only a key the server rejected as reused
    // with a different body is replaced.
    if (result && result.code === "IDEMPOTENCY_KEY_REUSED") rotateKey();
    setError((result && result.error) || "The budget could not be adjusted.");
    setFieldErrors(result && Array.isArray(result.errors) ? result.errors : []);
  };

  const typedErrors = fieldErrors.filter((item) => item && item.field);

  return (
    <form className="ui-stack study-budget-adjust" onSubmit={submit} aria-label="Adjust budget">
      <SegmentedControl label="Adjustment" options={KINDS} value={kind} onChange={edit(setKind)} />
      <div className="ui-form-grid">
        <div className="ui-field">
          <label className="ui-label" htmlFor="adjust-budget-amount">
            {kind === "TOP_UP" ? "Amount to add (USD)" : "New limit (USD)"}
          </label>
          <MoneyInput
            id="adjust-budget-amount"
            value={amount}
            onChange={edit(setAmount)}
            disabled={isBusy}
            invalid={Boolean(amountError)}
            required
          />
          {amountError ? <p className="ui-field-error">{amountError}</p> : null}
          <p className="ui-hint">
            Current limit {formatUsd(currentLimit)}
            {newLimit !== null ? ` → new limit ${formatUsd(newLimit)}` : ""}.
            {kind === "SET_LIMIT" ? " A limit below what is already spent stops further calls." : ""}
          </p>
        </div>
        <div className="ui-field">
          <label className="ui-label" htmlFor="adjust-budget-reason">
            Reason
          </label>
          <input
            id="adjust-budget-reason"
            className="ui-input"
            value={reason}
            onChange={(event) => edit(setReason)(event.target.value)}
            maxLength={500}
            required
            disabled={isBusy}
            placeholder="Kept in the audit log"
          />
          <p className="ui-hint">Required; recorded with your account on the adjustment.</p>
        </div>
      </div>
      {typedErrors.length > 0 ? (
        <FieldErrors errors={typedErrors} />
      ) : error ? (
        <p className="research-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className="ui-row">
        <button type="submit" className="primary-button" disabled={!canSubmit}>
          {kind === "TOP_UP" ? "Top up budget" : "Save new limit"}
        </button>
        {onCancel ? (
          <button type="button" className="ghost-button" onClick={onCancel} disabled={isBusy}>
            Cancel
          </button>
        ) : null}
      </div>
    </form>
  );
};

export default AdjustBudgetForm;
