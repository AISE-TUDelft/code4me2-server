import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  adjustEnrollmentBudget,
  getEnrollmentBudget,
  getEnrollmentBudgetAdjustments,
  getEnrollmentBudgetLedger,
} from "../../utils/api";
import { Badge, Card, Loading, Meter } from "../../components/common/ui";
import { formatCompact, formatDateTime, formatNumber, formatShortDateTime, formatUsd, humanize } from "../../utils/format";
import AdjustBudgetForm from "./AdjustBudgetForm";
import { ADJUSTMENT_LABELS, LIMIT_SOURCE_LABELS } from "./studyUtils";

const PAGE = 20;

const STATE_TONES = {
  SETTLED: "success",
  RESERVED: "info",
  VOIDED: "neutral",
  FORFEITED: "warning",
  EXPIRED: "warning",
};

const ADJUSTMENT_ERRORS = {
  STUDY_STOPPED: "The study has been stopped; budgets can no longer change.",
  ENROLLMENT_NOT_METERED: "This participant has no budget balance (the arm is not metered).",
  IDEMPOTENCY_KEY_REUSED: "This request was already submitted with different values; try again.",
};

const signed = (micro) => (Number(micro) > 0 ? `+${formatUsd(micro)}` : formatUsd(micro));

const outcome = (entry) => {
  if (entry.state === "RESERVED") return "In flight";
  const reason = entry.resolution_reason ? humanize(entry.resolution_reason) : humanize(entry.state);
  return entry.upstream_status ? `${reason} (HTTP ${entry.upstream_status})` : reason;
};

/**
 * One participant's budget in the study drawer: balance and meter, the
 * metered calls (ledger, newest first, paged) and the adjustment history.
 * The adjust form is opened from the drawer header ("Adjust budget").
 */
const ParticipantBudgetPanel = ({
  studyId,
  enrollmentId,
  warningFraction = 0.8,
  adjustOpen = false,
  onAdjustClose,
  canAdjust = false,
  onChanged,
}) => {
  const [budget, setBudget] = useState({ isLoading: true, error: "", data: null });
  const [ledger, setLedger] = useState({ isLoading: false, error: "", entries: [], nextCursor: null });
  const [history, setHistory] = useState({ isLoading: false, error: "", items: [], nextCursor: null });
  const [notice, setNotice] = useState("");
  const [isAdjusting, setIsAdjusting] = useState(false);
  // Responses for a participant no longer shown are dropped.
  const scope = useRef(0);

  const loadBudget = useCallback(async () => {
    const token = scope.current;
    setBudget((current) => ({ ...current, isLoading: true, error: "" }));
    const result = await getEnrollmentBudget(studyId, enrollmentId);
    if (scope.current !== token) return;
    if (result && result.ok) setBudget({ isLoading: false, error: "", data: result.data || {} });
    else setBudget({ isLoading: false, error: (result && result.error) || "The budget could not be loaded.", data: null });
  }, [studyId, enrollmentId]);

  const loadLedger = useCallback(
    async (cursor) => {
      const token = scope.current;
      setLedger((current) => ({ ...current, isLoading: true, error: "" }));
      const result = await getEnrollmentBudgetLedger(studyId, enrollmentId, { limit: PAGE, cursor });
      if (scope.current !== token) return;
      if (result && result.ok) {
        const entries = Array.isArray(result.data?.entries) ? result.data.entries : [];
        setLedger((current) => ({
          isLoading: false,
          error: "",
          entries: cursor ? [...current.entries, ...entries] : entries,
          nextCursor: result.data?.next_cursor || null,
        }));
      } else {
        setLedger((current) => ({
          ...current,
          isLoading: false,
          error: (result && result.error) || "The metered calls could not be loaded.",
        }));
      }
    },
    [studyId, enrollmentId],
  );

  const loadHistory = useCallback(
    async (cursor) => {
      const token = scope.current;
      setHistory((current) => ({ ...current, isLoading: true, error: "" }));
      const result = await getEnrollmentBudgetAdjustments(studyId, enrollmentId, { limit: PAGE, cursor });
      if (scope.current !== token) return;
      if (result && result.ok) {
        const items = Array.isArray(result.data?.adjustments) ? result.data.adjustments : [];
        setHistory((current) => ({
          isLoading: false,
          error: "",
          items: cursor ? [...current.items, ...items] : items,
          nextCursor: result.data?.next_cursor || null,
        }));
      } else {
        setHistory((current) => ({
          ...current,
          isLoading: false,
          error: (result && result.error) || "The adjustment history could not be loaded.",
        }));
      }
    },
    [studyId, enrollmentId],
  );

  useEffect(() => {
    scope.current += 1;
    setNotice("");
    loadBudget();
    loadLedger();
    loadHistory();
    return () => {
      scope.current += 1;
    };
  }, [loadBudget, loadLedger, loadHistory]);

  const submitAdjustment = async (payload) => {
    setIsAdjusting(true);
    setNotice("");
    const result = await adjustEnrollmentBudget(studyId, enrollmentId, payload);
    setIsAdjusting(false);
    if (result && result.ok) {
      const adjustment = result.data?.adjustment || {};
      const balance = result.data?.balance || null;
      setBudget((current) => ({
        ...current,
        data: { ...(current.data || {}), balance: balance || current.data?.balance || null },
      }));
      setNotice(
        result.data?.replayed
          ? "This adjustment had already been applied; nothing changed."
          : adjustment.kind === "TOP_UP"
            ? `Topped up by ${formatUsd(adjustment.delta_micro_usd)}; the limit is now ${formatUsd(adjustment.limit_after_micro_usd)}.`
            : `Limit set to ${formatUsd(adjustment.limit_after_micro_usd)} (was ${formatUsd(adjustment.limit_before_micro_usd)}).`,
      );
      loadHistory();
      if (onChanged) onChanged();
      if (onAdjustClose) onAdjustClose();
      return result;
    }
    return {
      ...(result || {}),
      ok: false,
      error: ADJUSTMENT_ERRORS[result && result.code] || (result && result.error) || "The budget could not be adjusted.",
    };
  };

  const balance = budget.data?.balance || null;
  const committed = balance ? (Number(balance.consumed_micro_usd) || 0) + (Number(balance.reserved_micro_usd) || 0) : 0;

  return (
    <Card
      className="study-budget-panel"
      title={
        <span className="ui-row">
          Budget
          {balance?.exhausted ? <Badge tone="danger">Exhausted</Badge> : null}
        </span>
      }
      subtitle="Spend from the study's shared provider key, metered per model call."
    >
      <div className="ui-stack">
        {notice ? (
          <p className="research-notice" role="status">
            {notice}
          </p>
        ) : null}

        {adjustOpen && canAdjust ? (
          <div className="study-budget-adjust-wrap">
            <h4 className="ui-section-title">Adjust budget</h4>
            <AdjustBudgetForm
              currentLimitMicro={balance ? balance.limit_micro_usd : 0}
              isBusy={isAdjusting}
              onSubmit={submitAdjustment}
              onCancel={onAdjustClose}
            />
          </div>
        ) : null}

        {budget.isLoading && !budget.data ? (
          <Loading label="Loading budget…" />
        ) : budget.error ? (
          <p className="research-error" role="alert">
            {budget.error}
          </p>
        ) : !balance ? (
          <p className="research-hint">This participant has no budget balance.</p>
        ) : (
          <>
            <Meter
              value={committed}
              max={balance.limit_micro_usd}
              exhausted={Boolean(balance.exhausted)}
              warningFraction={Number(balance.warning_fraction) > 0 ? Number(balance.warning_fraction) : warningFraction}
              label="Budget used"
              valueText={`${formatUsd(balance.consumed_micro_usd)} spent of ${formatUsd(balance.limit_micro_usd)}`}
            />
            <dl className="ui-dl study-budget-facts">
              <div>
                <dt>Limit</dt>
                <dd>
                  {formatUsd(balance.limit_micro_usd)}{" "}
                  <small className="ui-subtle">{LIMIT_SOURCE_LABELS[balance.limit_source] || humanize(balance.limit_source)}</small>
                </dd>
              </div>
              <div>
                <dt>Spent</dt>
                <dd>{formatUsd(balance.consumed_micro_usd)}</dd>
              </div>
              <div>
                <dt>Reserved</dt>
                <dd>
                  {formatUsd(balance.reserved_micro_usd)} <small className="ui-subtle">for calls in flight</small>
                </dd>
              </div>
              <div>
                <dt>Remaining</dt>
                <dd>{formatUsd(balance.remaining_micro_usd)}</dd>
              </div>
              <div>
                <dt>Model calls</dt>
                <dd>
                  {formatNumber(balance.call_count ?? 0)}
                  {balance.refused_count ? ` · ${formatNumber(balance.refused_count)} refused` : ""}
                </dd>
              </div>
              <div>
                <dt>Last call</dt>
                <dd>{balance.last_call_at ? formatDateTime(balance.last_call_at) : "—"}</dd>
              </div>
              <div>
                <dt>Tokens settled</dt>
                <dd>
                  {formatCompact(balance.settled_prompt_tokens ?? 0)} in · {formatCompact(balance.settled_completion_tokens ?? 0)} out
                </dd>
              </div>
              {balance.exhausted_at ? (
                <div>
                  <dt>Exhausted since</dt>
                  <dd>{formatDateTime(balance.exhausted_at)}</dd>
                </div>
              ) : null}
            </dl>
          </>
        )}

        <div className="ui-stack-sm">
          <span className="ui-section-title">Metered calls</span>
          {ledger.entries.length === 0 ? (
            ledger.isLoading ? (
              <Loading label="Loading metered calls…" />
            ) : (
              <p className="research-hint">{ledger.error || "No metered model calls yet."}</p>
            )
          ) : (
            <div className="ui-table-wrap">
              <table className="ui-table study-ledger-table">
                <caption className="ui-visually-hidden">Metered model calls</caption>
                <thead>
                  <tr>
                    <th scope="col">When</th>
                    <th scope="col">Model</th>
                    <th scope="col">State</th>
                    <th scope="col" className="is-num">
                      Hold
                    </th>
                    <th scope="col" className="is-num">
                      Charged
                    </th>
                    <th scope="col" className="is-num">
                      Tokens
                    </th>
                    <th scope="col">Outcome</th>
                  </tr>
                </thead>
                <tbody>
                  {ledger.entries.map((entry) => (
                    <tr key={entry.reservation_id}>
                      <td className="ui-nowrap">{formatShortDateTime(entry.reserved_at)}</td>
                      <td>
                        <span className="ui-mono">{entry.model || "—"}</span>
                        {entry.entry_point ? <small className="ui-subtle"> · {entry.entry_point}</small> : null}
                      </td>
                      <td>
                        <Badge tone={STATE_TONES[entry.state] || "neutral"}>{humanize(entry.state)}</Badge>
                      </td>
                      <td className="is-num">{formatUsd(entry.hold_micro_usd)}</td>
                      <td className="is-num">{entry.charged_micro_usd == null ? "—" : formatUsd(entry.charged_micro_usd)}</td>
                      <td className="is-num">
                        <div className="ui-cell-stack">
                          <span>
                            {formatCompact(entry.prompt_tokens ?? entry.estimated_prompt_tokens)} in ·{" "}
                            {formatCompact(entry.completion_tokens)} out
                          </span>
                          {entry.usage_source ? <small>{humanize(entry.usage_source)}</small> : null}
                        </div>
                      </td>
                      <td>{outcome(entry)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {ledger.error && ledger.entries.length > 0 ? (
            <p className="research-error" role="alert">
              {ledger.error}
            </p>
          ) : null}
          {ledger.nextCursor ? (
            <div>
              <button
                type="button"
                className="secondary-button button-sm"
                onClick={() => loadLedger(ledger.nextCursor)}
                disabled={ledger.isLoading}
              >
                {ledger.isLoading ? "Loading…" : "Load more"}
              </button>
            </div>
          ) : null}
        </div>

        <div className="ui-stack-sm">
          <span className="ui-section-title">Adjustment history</span>
          {history.items.length === 0 ? (
            history.isLoading ? (
              <Loading label="Loading adjustments…" />
            ) : (
              <p className="research-hint">{history.error || "No adjustments yet; the limit is the study default."}</p>
            )
          ) : (
            <div className="ui-table-wrap">
              <table className="ui-table study-history-table">
                <caption className="ui-visually-hidden">Budget adjustments</caption>
                <thead>
                  <tr>
                    <th scope="col">When</th>
                    <th scope="col">Kind</th>
                    <th scope="col" className="is-num">
                      Change
                    </th>
                    <th scope="col" className="is-num">
                      Limit
                    </th>
                    <th scope="col">By</th>
                    <th scope="col">Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {history.items.map((item) => (
                    <tr key={item.adjustment_id}>
                      <td className="ui-nowrap">{formatShortDateTime(item.occurred_at)}</td>
                      <td>{ADJUSTMENT_LABELS[item.kind] || humanize(item.kind)}</td>
                      <td className="is-num">{signed(item.delta_micro_usd)}</td>
                      <td className="is-num">
                        {formatUsd(item.limit_before_micro_usd)} → {formatUsd(item.limit_after_micro_usd)}
                      </td>
                      <td>{item.actor || "—"}</td>
                      <td>{item.reason || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {history.error && history.items.length > 0 ? (
            <p className="research-error" role="alert">
              {history.error}
            </p>
          ) : null}
          {history.nextCursor ? (
            <div>
              <button
                type="button"
                className="secondary-button button-sm"
                onClick={() => loadHistory(history.nextCursor)}
                disabled={history.isLoading}
              >
                {history.isLoading ? "Loading…" : "Load more adjustments"}
              </button>
            </div>
          ) : null}
        </div>
      </div>
    </Card>
  );
};

export default ParticipantBudgetPanel;
