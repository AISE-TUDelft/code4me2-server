import React, { useEffect, useState } from "react";
import Icon from "../../components/common/Icon";
import { Alert, Badge, Card, Loading, MoneyInput } from "../../components/common/ui";
import { formatDateTime, formatNumber, formatUsd, parseUsdInput } from "../../utils/format";
import { newIdempotencyKey } from "./studyUtils";

const percentOf = (fraction) => (Number(fraction) > 0 ? String(Math.round(Number(fraction) * 100)) : "80");

// A 0 default means none was set yet (calls are refused until it is).
const defaultFrom = (data) =>
  data && Number(data.default_budget_micro_usd) > 0 && data.default_budget_usd ? String(data.default_budget_usd) : "";

/**
 * The default budget per participant and the warning threshold, for studies
 * with a metered arm (Goose or the built-in agent spending from the study's
 * shared provider key). Money is not part of what participants consented to,
 * so it stays editable after the consent lock; a stopped study is read-only.
 * "Apply" pushes a saved default to participants still on the old default;
 * individually adjusted budgets are left alone.
 */
const ParticipantBudgetsCard = ({ study, budget, isBusy, onReload, onSave, onApplyDefault }) => {
  const data = budget.data;
  const stopped = study.research_status === "STUDY_STOPPED";
  const [defaultUsd, setDefaultUsd] = useState(() => defaultFrom(data));
  const [warningPercent, setWarningPercent] = useState(() => percentOf(data && data.warning_fraction));
  const [applyReason, setApplyReason] = useState("");
  // The draft follows every (re)load of the policy, adjusted during render so
  // the first paint already shows the stored values (no flash of an empty field).
  const [seen, setSeen] = useState(data);
  if (data !== seen) {
    setSeen(data);
    setDefaultUsd(defaultFrom(data));
    setWarningPercent(percentOf(data && data.warning_fraction));
  }

  if (budget.isLoading && !data) {
    return (
      <Card title="Participant budgets">
        <Loading label="Loading participant budgets…" />
      </Card>
    );
  }
  if (!data) {
    return (
      <Card title="Participant budgets">
        <p className="research-error" role="alert">
          {budget.error || "The participant budgets could not be loaded."}
        </p>
        <button type="button" className="secondary-button button-sm" onClick={onReload} disabled={isBusy}>
          <Icon name="refresh" size={14} />
          Retry
        </button>
      </Card>
    );
  }
  if (!data.metered) {
    return (
      <Card
        title="Participant budgets"
        subtitle="Budgets apply to Goose and built-in arms, which spend from the study's shared provider key."
      >
        <p className="research-hint">
          This study's arms run Codex, which signs in with the participant's ChatGPT account and is not metered, so
          there is nothing to budget.
        </p>
      </Card>
    );
  }

  const editable = !stopped && data.editable !== false;
  const draft = parseUsdInput(defaultUsd);
  const draftValid = draft.ok && draft.micro > 0;
  const fieldError = !defaultUsd.trim()
    ? ""
    : !draft.ok
      ? draft.error
      : draft.micro <= 0
        ? "The budget must be greater than zero."
        : "";
  const warningValue = Number(warningPercent);
  const warningValid = Number.isInteger(warningValue) && warningValue >= 1 && warningValue <= 100;
  const currentDefault = Number(data.default_budget_micro_usd) || 0;
  const defaultChanged = draftValid && draft.micro !== currentDefault;
  const warningChanged = warningValid && warningPercent !== percentOf(data.warning_fraction);
  const changed = defaultChanged || warningChanged;
  const participants = data.participants || {};
  const onOld = Number(participants.on_old_default) || 0;
  const missing = Array.isArray(data.pricing?.missing) ? data.pricing.missing : [];

  const save = async (event) => {
    event.preventDefault();
    if (!editable || !changed || !draftValid || !warningValid) return;
    await onSave({
      ...(defaultChanged ? { defaultBudgetUsd: draft.value } : {}),
      ...(warningChanged ? { warningFraction: warningValue / 100 } : {}),
    });
  };

  const apply = async () => {
    const reason = applyReason.trim();
    if (!reason) return;
    const confirmed = window.confirm(
      `Apply the default budget of ${formatUsd(currentDefault)} to ${onOld} participant${onOld === 1 ? "" : "s"} still on the old default? Participants with an individually adjusted budget keep theirs.`,
    );
    if (!confirmed) return;
    // A fresh key per click: the server replays a reused key, so a retry
    // after a failure is deliberately a new request.
    if (await onApplyDefault({ reason, idempotencyKey: newIdempotencyKey("apply-default") })) setApplyReason("");
  };

  return (
    <Card
      as="form"
      className="study-budget-card"
      onSubmit={save}
      title={
        <span className="ui-row">
          Participant budgets
          {participants.exhausted ? <Badge tone="danger">{formatNumber(participants.exhausted)} exhausted</Badge> : null}
        </span>
      }
      subtitle={
        stopped
          ? "Stopped studies keep their budgets; nothing can be changed."
          : "Goose and built-in arms spend from the study's shared provider key within this budget per participant. Editable at any time, including after the consent lock."
      }
      footer={
        <button
          type="submit"
          className="primary-button"
          disabled={isBusy || !editable || !changed || !draftValid || !warningValid}
        >
          Save budget defaults
        </button>
      }
    >
      <dl className="ui-dl study-budget-facts">
        <div>
          <dt>Participants</dt>
          <dd>{formatNumber(participants.total ?? 0)}</dd>
        </div>
        <div>
          <dt>On the current default</dt>
          <dd>{formatNumber(participants.on_default ?? 0)}</dd>
        </div>
        <div>
          <dt>On an old default</dt>
          <dd>{formatNumber(onOld)}</dd>
        </div>
        <div>
          <dt>Adjusted individually</dt>
          <dd>{formatNumber(participants.custom ?? 0)}</dd>
        </div>
        <div>
          <dt>Metered spend</dt>
          <dd>
            {formatUsd(data.metered_spend_micro_usd)}
            {data.metered_calls !== undefined ? <small className="ui-subtle"> · {formatNumber(data.metered_calls)} calls</small> : null}
          </dd>
        </div>
        <div>
          <dt>Reserved</dt>
          <dd>
            {formatUsd(data.reserved_micro_usd)} <small className="ui-subtle">for calls in flight</small>
          </dd>
        </div>
      </dl>

      {missing.length > 0 ? (
        <Alert tone="danger" live={false} title="A metered model has no price on the server.">
          {missing.map((item) => `${item.model || "the model"} (${item.name || item.profile_id})`).join(", ")}: every call from
          that arm is refused until an administrator prices the model on its provider connection.
        </Alert>
      ) : null}

      <div className="ui-form-grid">
        <div className="ui-field">
          <label className="ui-label" htmlFor="study-budget-default">
            Default budget per participant (USD)
          </label>
          <MoneyInput
            id="study-budget-default"
            value={defaultUsd}
            onChange={setDefaultUsd}
            disabled={isBusy || !editable}
            invalid={Boolean(fieldError)}
            describedBy={fieldError ? "study-budget-default-error" : undefined}
          />
          {fieldError ? (
            <p id="study-budget-default-error" className="ui-field-error">
              {fieldError}
            </p>
          ) : null}
          <p className="ui-hint">
            Applies to participants who join from now on
            {onOld > 0 ? "; use Apply below for those still on the old default." : "."}
            {data.updated_at ? ` Last changed ${formatDateTime(data.updated_at)}${data.updated_by ? ` by ${data.updated_by}` : ""}.` : ""}
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
            disabled={isBusy || !editable}
            aria-invalid={warningValid ? undefined : "true"}
          />
          {!warningValid ? <p className="ui-field-error">Enter a whole number from 1 to 100.</p> : null}
        </div>
      </div>

      {editable && onOld > 0 ? (
        <div className="study-budget-apply">
          <p className="ui-muted" style={{ margin: 0 }}>
            {formatNumber(onOld)} participant{onOld === 1 ? " is" : "s are"} still on an old default budget. Save the
            new default first, then apply it here; individually adjusted budgets are left alone.
          </p>
          <div className="research-actions ui-row">
            <input
              className="ui-input"
              style={{ flex: "1 1 280px", maxWidth: 420 }}
              aria-label="Reason for applying the default"
              value={applyReason}
              onChange={(event) => setApplyReason(event.target.value)}
              placeholder="Reason (required, kept in the audit log)"
              maxLength={500}
              disabled={isBusy}
            />
            <button
              type="button"
              className="secondary-button"
              onClick={apply}
              disabled={isBusy || changed || !applyReason.trim()}
            >
              Apply new default to {formatNumber(onOld)} participant{onOld === 1 ? "" : "s"} still on the old default
            </button>
          </div>
        </div>
      ) : null}
    </Card>
  );
};

/**
 * Editable study metadata (until the first consent), participant budgets,
 * administrator operations (kill switch) and the terminal stop action.
 */
const StudySettings = ({
  study,
  isAdmin,
  isBusy,
  onSaveMetadata,
  onStop,
  onKillSwitch,
  budget,
  onReloadBudget,
  onSaveBudget,
  onApplyDefault,
}) => {
  const [metadata, setMetadata] = useState({ name: study.name || "", description: study.description || "" });
  const [killSwitchReason, setKillSwitchReason] = useState("");

  useEffect(() => {
    setMetadata({ name: study.name || "", description: study.description || "" });
  }, [study.study_id, study.name, study.description]);

  const stopped = study.research_status === "STUDY_STOPPED";
  const locked = Boolean(study.consent_locked_at) || stopped;
  const switchEngaged = Boolean(study.kill_switch?.switch_id) && study.kill_switch.status !== "RELEASED";

  return (
    <div className="ui-stack">
      {!stopped ? (
        <Card
          as="form"
          title="Study details"
          subtitle={
            locked
              ? "Locked: the first participant has consented, so the name and description they agreed to stay fixed."
              : "Shown to participants when they review the study. Editable until the first participant consents."
          }
          onSubmit={(event) => {
            event.preventDefault();
            onSaveMetadata(metadata);
          }}
          footer={
            <button type="submit" className="primary-button" disabled={isBusy || locked || !metadata.name.trim()}>
              Save metadata
            </button>
          }
        >
          <div className="ui-field">
            <label className="ui-label" htmlFor="study-settings-name">
              Name
            </label>
            <input
              id="study-settings-name"
              className="ui-input"
              value={metadata.name}
              onChange={(event) => setMetadata({ ...metadata, name: event.target.value })}
              disabled={isBusy || locked}
            />
          </div>
          <div className="ui-field">
            <label className="ui-label" htmlFor="study-settings-description">
              Description
            </label>
            <textarea
              id="study-settings-description"
              className="ui-textarea"
              value={metadata.description}
              onChange={(event) => setMetadata({ ...metadata, description: event.target.value })}
              rows={3}
              disabled={isBusy || locked}
            />
          </div>
          {locked ? (
            <p className="ui-hint profile-locked">
              <Icon name="lock" size={13} />
              Metadata locked {study.consent_locked_at ? `since ${formatDateTime(study.consent_locked_at)}` : ""}.
            </p>
          ) : null}
        </Card>
      ) : (
        <Card title="Study details">
          <p className="research-hint">Stopped studies cannot be edited. Clone the study to run it again.</p>
        </Card>
      )}

      {budget ? (
        <ParticipantBudgetsCard
          study={study}
          budget={budget}
          isBusy={isBusy}
          onReload={onReloadBudget}
          onSave={onSaveBudget}
          onApplyDefault={onApplyDefault}
        />
      ) : null}

      {isAdmin && !stopped ? (
        <Card
          title={
            <span className="ui-row">
              <Icon name="shield" size={16} />
              Operations
              {switchEngaged ? <Badge tone="danger">Kill switch engaged</Badge> : null}
            </span>
          }
          subtitle="Administrator only. The kill switch pauses telemetry intake and agent use for this study until it is released."
        >
          <div className="research-actions ui-row">
            {!switchEngaged ? (
              <input
                className="ui-input"
                style={{ flex: "1 1 280px", maxWidth: 420 }}
                aria-label="Kill switch reason"
                value={killSwitchReason}
                onChange={(event) => setKillSwitchReason(event.target.value)}
                placeholder="Reason (required)"
                disabled={isBusy}
              />
            ) : null}
            <button
              type="button"
              className={switchEngaged ? "secondary-button" : "danger-button"}
              onClick={async () => {
                // Keep the typed reason when the request fails or is cancelled.
                if (await onKillSwitch(killSwitchReason.trim())) setKillSwitchReason("");
              }}
              disabled={isBusy || (!switchEngaged && !killSwitchReason.trim())}
            >
              {switchEngaged ? "Release kill switch" : "Engage kill switch"}
            </button>
          </div>
        </Card>
      ) : null}
      {isAdmin && study.kill_switch ? (
        <p className="research-hint">
          Kill switch: {study.kill_switch.status || "RELEASED"}. Reason: {study.kill_switch.reason || "Not provided"}
        </p>
      ) : null}

      {!stopped ? (
        <Card className="study-danger-zone" title="Stop the study" subtitle="Permanent: ends collection for every participant.">
          <div className="ui-row-between">
            <p className="ui-muted" style={{ margin: 0, maxWidth: 620 }}>
              Stopping is terminal. Enrollments close, new joins are refused, and all research data collected so far is
              retained for analysis. You can clone a stopped study to run it again.
            </p>
            <button type="button" className="danger-button" onClick={onStop} disabled={isBusy}>
              <Icon name="power" size={15} />
              Stop study
            </button>
          </div>
        </Card>
      ) : null}
    </div>
  );
};

export default StudySettings;
