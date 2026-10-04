import React, { useMemo, useState } from "react";
import Icon from "../../components/common/Icon";
import { Alert, Badge, Card, Drawer, EmptyState, Loading, Meter } from "../../components/common/ui";
import { changeEnrollmentAssignment } from "../../utils/api";
import { formatDuration, formatNumber, formatRelative, formatShortDateTime, formatUsd, microToUsd } from "../../utils/format";
import { ConsentReview } from "./ConsentText";
import ParticipantDashboard from "./ParticipantDashboard";
import { ENROLLMENT_STATUS, HEALTH, RUNTIME_LABELS, armColor, downloadCsv, slugify } from "./studyUtils";

const SET_BY_HAND_TITLE =
  "The research team set this arm by hand before first use; the randomized arm stays recomputable for analysis.";

/** "2 of 3 consent statements ticked" for a participant's recorded answers. */
const consentTicked = (answers, statements) => {
  const ticked = statements.filter((statement) => answers[statement.id] === true).length;
  return `${ticked} of ${statements.length} consent statement${statements.length === 1 ? "" : "s"} ticked`;
};

const HEALTH_FILTERS = [
  { value: "", label: "Any status" },
  { value: "ACTIVE", label: "Active this week" },
  { value: "IDLE", label: "Idle" },
  { value: "NO_TELEMETRY", label: "No telemetry yet" },
  { value: "INACTIVE", label: "Inactive enrollment" },
];

// Metered arms only: a row without a budget belongs to an unmetered (Codex) arm.
const BUDGET_FILTERS = [
  { value: "", label: "Any budget" },
  { value: "EXHAUSTED", label: "Exhausted" },
  { value: "REMAINING", label: "Budget remaining" },
];

/** "$3.12 / $10.00" with a meter of what is committed (spent + reserved). */
const BudgetCell = ({ budget, warningFraction }) => {
  const consumed = Number(budget.consumed_micro_usd) || 0;
  const reserved = Number(budget.reserved_micro_usd) || 0;
  const limit = Number(budget.limit_micro_usd) || 0;
  const title = `${formatUsd(consumed)} spent of ${formatUsd(limit)} · ${formatUsd(reserved)} reserved for calls in flight · ${formatUsd(budget.remaining_micro_usd)} remaining`;
  return (
    <div className="ui-cell-stack" title={title}>
      <span className="ui-nowrap">
        {formatUsd(consumed)} / {formatUsd(limit)}
      </span>
      <Meter
        value={consumed + reserved}
        max={limit}
        exhausted={Boolean(budget.exhausted)}
        warningFraction={warningFraction}
        label="Budget used"
        valueText={`${formatUsd(consumed)} of ${formatUsd(limit)}`}
      />
    </div>
  );
};

const CSV_COLUMNS = [
  { label: "participant_code", value: (row) => row.participant_code },
  { label: "enrollment_status", value: (row) => row.status },
  { label: "health", value: (row) => row.health },
  { label: "arm", value: (row) => row.arm?.name },
  { label: "arm_profile_id", value: (row) => row.arm?.profile_id },
  // DETERMINISTIC_HASH / RANDOM_EQUAL, or MANUAL after an owner override.
  { label: "assignment_strategy", value: (row) => row.arm?.strategy },
  { label: "model", value: (row) => row.arm?.model },
  { label: "runtime", value: (row) => row.arm?.framework_version },
  { label: "enrolled_at", value: (row) => row.enrolled_at },
  // The consent version the participant accepted (null before versions were recorded).
  { label: "consent_digest", value: (row) => row.consent_digest },
  { label: "sessions", value: (row) => row.sessions?.total },
  { label: "session_hours", value: (row) => (row.sessions?.session_seconds == null ? "" : (row.sessions.session_seconds / 3600).toFixed(3)) },
  { label: "prompts", value: (row) => row.activity?.prompts },
  { label: "tool_calls", value: (row) => row.activity?.tool_calls },
  { label: "tool_failures", value: (row) => row.activity?.tool_failures },
  { label: "cancellations", value: (row) => row.activity?.cancellations },
  { label: "permission_requests", value: (row) => row.activity?.permission_requests },
  { label: "permission_denials", value: (row) => row.activity?.permission_denials },
  { label: "errors", value: (row) => row.activity?.errors },
  { label: "agent_file_writes", value: (row) => row.activity?.agent_file_writes },
  { label: "ide_edits", value: (row) => row.activity?.ide_edits },
  { label: "usage_tokens", value: (row) => row.activity?.usage_tokens },
  { label: "active_days", value: (row) => row.activity?.active_days },
  { label: "first_event_at", value: (row) => row.activity?.first_event_at },
  { label: "last_event_at", value: (row) => row.activity?.last_event_at },
  // Budget (metered arms; empty for unmetered ones), decimal USD.
  { label: "spent_usd", value: (row) => microToUsd(row.budget?.consumed_micro_usd) },
  { label: "budget_usd", value: (row) => microToUsd(row.budget?.limit_micro_usd) },
  { label: "reserved_usd", value: (row) => microToUsd(row.budget?.reserved_micro_usd) },
  { label: "budget_exhausted_at", value: (row) => row.budget?.exhausted_at },
];

// One column per consent statement any exported participant answered, so
// optional statements (e.g. reuse of data) can be honoured downstream.
const consentColumns = (rows) => {
  const ids = [];
  rows.forEach((row) =>
    Object.keys(row.consent_answers || {}).forEach((id) => {
      if (!ids.includes(id)) ids.push(id);
    }),
  );
  return ids.map((id) => ({
    label: `consent_${id}`,
    value: (row) => (row.consent_answers && id in row.consent_answers ? String(row.consent_answers[id]) : ""),
  }));
};

const ARM_CHANGE_ERRORS = {
  ASSIGNMENT_IN_USE: "The participant has already used their arm, so it can no longer be changed.",
  MANUAL_ASSIGNMENT_DISABLED: "This study does not allow manual assignment.",
  ENROLLMENT_NOT_ACTIVE: "The enrollment is no longer active.",
  STUDY_STOPPED: "Stopped studies cannot be changed.",
};

/** Set one participant's arm by hand (manual-assignment studies, before first use). */
const ChangeArmForm = ({ study, row, arms, onDone, onCancel }) => {
  const [profileId, setProfileId] = useState(row.arm?.profile_id || "");
  const [state, setState] = useState({ busy: false, error: "" });
  const unchanged = !profileId || profileId === row.arm?.profile_id;

  const submit = async (event) => {
    event.preventDefault();
    if (unchanged) return;
    const target = arms.find((arm) => arm.profile_id === profileId);
    const confirmed = window.confirm(
      `Assign ${row.participant_code} to “${target?.name || profileId}”? The change is recorded as a manual assignment.`,
    );
    if (!confirmed) return;
    setState({ busy: true, error: "" });
    const result = await changeEnrollmentAssignment(study.study_id, row.enrollment_id, profileId);
    if (result && result.ok) {
      setState({ busy: false, error: "" });
      onDone();
    } else {
      setState({
        busy: false,
        error: ARM_CHANGE_ERRORS[result && result.code] || (result && result.error) || "The arm could not be changed.",
      });
    }
  };

  return (
    <form className="ui-card ui-card-body ui-stack-sm" onSubmit={submit} aria-label="Change arm">
      <div className="ui-field">
        <label className="ui-label" htmlFor={`change-arm-${row.enrollment_id}`}>
          Arm
        </label>
        <select
          id={`change-arm-${row.enrollment_id}`}
          className="ui-select"
          value={profileId}
          onChange={(event) => setProfileId(event.target.value)}
          disabled={state.busy}
        >
          {arms.map((arm) => (
            <option key={arm.profile_id} value={arm.profile_id}>
              {arm.name || arm.profile_id}
            </option>
          ))}
        </select>
        <p className="ui-hint">
          Possible until the participant first uses the agent. The randomized arm stays recomputable for analysis.
        </p>
      </div>
      {state.error ? (
        <p className="research-error" role="alert">
          {state.error}
        </p>
      ) : null}
      <div className="ui-row">
        <button type="submit" className="primary-button button-sm" disabled={state.busy || unchanged}>
          Change arm
        </button>
        <button type="button" className="ghost-button button-sm" onClick={onCancel} disabled={state.busy}>
          Cancel
        </button>
      </div>
    </form>
  );
};

/** Enrolled participants with their frozen arm and an activity summary. */
const StudyParticipants = ({ study, arms, state, onReload }) => {
  const [query, setQuery] = useState("");
  const [armFilter, setArmFilter] = useState("");
  const [healthFilter, setHealthFilter] = useState("");
  const [budgetFilter, setBudgetFilter] = useState("");
  const [openEnrollment, setOpenEnrollment] = useState(null);
  // The drawer's "Adjust budget" form (closed again for another participant).
  const [adjustOpen, setAdjustOpen] = useState(false);
  // The drawer's "Change arm" form (manual-assignment studies only).
  const [armFormOpen, setArmFormOpen] = useState(false);

  const participants = useMemo(
    () => (Array.isArray(state.data?.participants) ? state.data.participants : []),
    [state.data],
  );

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return participants.filter((row) => {
      if (needle && !String(row.participant_code || "").toLowerCase().includes(needle)) return false;
      if (armFilter && row.arm?.profile_id !== armFilter) return false;
      if (healthFilter && row.health !== healthFilter) return false;
      if (budgetFilter === "EXHAUSTED" && !row.budget?.exhausted) return false;
      if (budgetFilter === "REMAINING" && (!row.budget || row.budget.exhausted)) return false;
      return true;
    });
  }, [participants, query, armFilter, healthFilter, budgetFilter]);

  const openRow = participants.find((row) => row.enrollment_id === openEnrollment) || null;
  const stopped = study.research_status === "STUDY_STOPPED";
  // Answers are shown for a custom consent form (the stock form has one checkbox).
  const consentStatements = study.consent?.custom && Array.isArray(study.consent.statements) ? study.consent.statements : [];
  const metered = participants.some((row) => row.budget);
  const warningFraction = Number(study.budget_policy?.warning_fraction) > 0 ? Number(study.budget_policy.warning_fraction) : 0.8;
  const openParticipant = (enrollmentId) => {
    setOpenEnrollment(enrollmentId);
    setAdjustOpen(false);
    setArmFormOpen(false);
  };
  const canChangeArm = Boolean(
    openRow && study.assignment_policy?.manual_override && openRow.reassignable && !stopped,
  );

  if (state.isLoading && !state.data) return <Loading label="Loading participants…" />;
  if (state.error) {
    return (
      <p className="research-error" role="alert">
        {state.error}
      </p>
    );
  }

  const exportCsv = () =>
    downloadCsv(`${slugify(study.name)}-participants.csv`, [...CSV_COLUMNS, ...consentColumns(visible)], visible);

  return (
    <div className="ui-stack">
      <Card
        title="Participants"
        subtitle="Study-local participant codes only; account identities never appear here."
        actions={
          <>
            <button type="button" className="secondary-button button-sm" onClick={exportCsv} disabled={visible.length === 0}>
              <Icon name="external" size={14} />
              Export CSV
            </button>
            <button type="button" className="secondary-button button-sm" onClick={onReload} disabled={state.isLoading}>
              <Icon name="refresh" size={14} />
              Refresh
            </button>
          </>
        }
      >
        <div className="ui-toolbar" role="search">
          <div className="ui-search">
            <Icon name="search" size={15} />
            <input
              className="ui-input"
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search participant code"
              aria-label="Search participant code"
            />
          </div>
          <select className="ui-select" value={armFilter} onChange={(event) => setArmFilter(event.target.value)} aria-label="Filter by arm">
            <option value="">All arms</option>
            {arms.map((arm) => (
              <option key={arm.profile_id} value={arm.profile_id}>
                {arm.name}
              </option>
            ))}
          </select>
          <select className="ui-select" value={healthFilter} onChange={(event) => setHealthFilter(event.target.value)} aria-label="Filter by status">
            {HEALTH_FILTERS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          {metered ? (
            <select className="ui-select" value={budgetFilter} onChange={(event) => setBudgetFilter(event.target.value)} aria-label="Filter by budget">
              {BUDGET_FILTERS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          ) : null}
          <span className="ui-toolbar-meta">
            {visible.length === participants.length
              ? `${participants.length} participant${participants.length === 1 ? "" : "s"}`
              : `${visible.length} of ${participants.length}`}
          </span>
        </div>

        {participants.length === 0 ? (
          <EmptyState icon="users" title="No participants yet">
            {study.join_code
              ? `Share the join code ${study.join_code}. Participants appear here as soon as they consent.`
              : "Participants appear here as soon as they consent."}
          </EmptyState>
        ) : visible.length === 0 ? (
          <EmptyState icon="search" title="No participants match these filters" />
        ) : (
          <div className={`ui-table-wrap${state.isLoading ? " is-refreshing" : ""}`}>
            <table className="ui-table study-participants-table">
              <caption className="ui-visually-hidden">Enrolled participants</caption>
              <thead>
                <tr>
                  <th scope="col">Participant</th>
                  <th scope="col">Arm</th>
                  <th scope="col">Status</th>
                  <th scope="col" className="is-num">
                    Sessions
                  </th>
                  <th scope="col" className="is-num">
                    Prompts
                  </th>
                  <th scope="col" className="is-num">
                    Tool calls
                  </th>
                  <th scope="col" className="is-num">
                    Errors
                  </th>
                  <th scope="col" className="is-num">
                    Spent
                  </th>
                  <th scope="col">Budget</th>
                  <th scope="col">Last seen</th>
                  <th scope="col">
                    <span className="ui-visually-hidden">Open</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {visible.map((row) => {
                  const enrollment = ENROLLMENT_STATUS[row.status] || { label: row.status || "Unknown", tone: "neutral" };
                  const health = HEALTH[row.health];
                  const activity = row.activity || {};
                  const sessions = row.sessions || {};
                  const open = () => openParticipant(row.enrollment_id);
                  return (
                    <tr key={row.enrollment_id} className="is-clickable" onClick={open}>
                      <td>
                        <div className="ui-cell-stack">
                          <span className="ui-cell-primary ui-mono">{row.participant_code || "—"}</span>
                          <small className="ui-nowrap">Enrolled {formatShortDateTime(row.enrolled_at)}</small>
                          {consentStatements.length && row.consent_answers ? (
                            <small className="ui-nowrap">{consentTicked(row.consent_answers, consentStatements)}</small>
                          ) : null}
                        </div>
                      </td>
                      <td>
                        {row.arm ? (
                          <div className="study-arm-cell">
                            <span className="viz-swatch is-dot" style={{ backgroundColor: armColor(arms, row.arm.profile_id) }} aria-hidden="true" />
                            <div className="ui-cell-stack">
                              <span className="ui-cell-primary">{row.arm.name || row.arm.profile_id}</span>
                              <small>
                                {row.arm.model}
                                {row.arm.framework_version ? ` · ${RUNTIME_LABELS[row.arm.framework_version] || row.arm.framework_version}` : ""}
                              </small>
                              {row.arm.strategy === "MANUAL" ? (
                                <Badge tone="info" title={SET_BY_HAND_TITLE}>
                                  Set by hand
                                </Badge>
                              ) : null}
                            </div>
                          </div>
                        ) : (
                          <span className="ui-subtle">Not assigned yet</span>
                        )}
                      </td>
                      <td>
                        <div className="ui-row">
                          <Badge tone={enrollment.tone}>{enrollment.label}</Badge>
                          {health && row.health !== "INACTIVE" ? <Badge tone={health.tone}>{health.label}</Badge> : null}
                          {row.budget?.exhausted ? (
                            <Badge tone="danger" title="The budget is used up; model calls are refused until it is topped up">
                              Exhausted
                            </Badge>
                          ) : null}
                        </div>
                      </td>
                      <td className="is-num">
                        <div className="ui-cell-stack">
                          <span>
                            {formatNumber(sessions.total ?? 0)}
                            {sessions.active ? <span className="ui-subtle"> · {sessions.active} live</span> : null}
                          </span>
                          <small>{sessions.session_seconds ? formatDuration(sessions.session_seconds) : "—"}</small>
                        </div>
                      </td>
                      <td className="is-num">{formatNumber(activity.prompts ?? 0)}</td>
                      <td className="is-num">
                        <div className="ui-cell-stack">
                          <span>{formatNumber(activity.tool_calls ?? 0)}</span>
                          {activity.tool_failures ? <small>{formatNumber(activity.tool_failures)} failed</small> : null}
                        </div>
                      </td>
                      <td className="is-num">{formatNumber(activity.errors ?? 0)}</td>
                      <td className="is-num">
                        {row.budget ? (
                          <div className="ui-cell-stack">
                            <span>{formatUsd(row.budget.consumed_micro_usd)}</span>
                            {Number(row.budget.reserved_micro_usd) > 0 ? (
                              <small className="ui-nowrap">{formatUsd(row.budget.reserved_micro_usd)} held</small>
                            ) : null}
                          </div>
                        ) : (
                          <span className="ui-subtle" title="This arm is not metered">
                            —
                          </span>
                        )}
                      </td>
                      <td className="study-budget-cell">
                        {row.budget ? (
                          <BudgetCell budget={row.budget} warningFraction={warningFraction} />
                        ) : (
                          <span className="ui-subtle" title="This arm is not metered">
                            —
                          </span>
                        )}
                      </td>
                      <td>
                        <div className="ui-cell-stack">
                          <span>{activity.last_event_at ? formatRelative(activity.last_event_at) : "—"}</span>
                          {activity.last_event_at ? <small className="ui-nowrap">{formatShortDateTime(activity.last_event_at)}</small> : null}
                        </div>
                      </td>
                      <td className="study-row-chevron">
                        <button
                          type="button"
                          className="icon-button"
                          onClick={(event) => {
                            event.stopPropagation();
                            open();
                          }}
                          aria-label={`Open dashboard for participant ${row.participant_code}`}
                          title="Open participant dashboard"
                        >
                          <Icon name="chevronRight" size={16} />
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {participants.some((row) => row.health === "NO_TELEMETRY") ? (
        <Alert tone="info" live={false}>
          Participants marked “No telemetry yet” have consented but no IDE session has reported data. They may not
          have installed the plugin or signed in — check the setup steps with them.
        </Alert>
      ) : null}

      <Drawer
        open={Boolean(openRow)}
        onClose={() => openParticipant(null)}
        title={openRow ? `Participant ${openRow.participant_code}` : ""}
        actions={
          openRow && ((openRow.budget && !stopped) || canChangeArm) ? (
            <>
              {canChangeArm ? (
                <button
                  type="button"
                  className="secondary-button button-sm"
                  onClick={() => setArmFormOpen((value) => !value)}
                  aria-pressed={armFormOpen}
                >
                  <Icon name="layers" size={14} />
                  Change arm
                </button>
              ) : null}
              {openRow.budget && !stopped ? (
                <button
                  type="button"
                  className="secondary-button button-sm"
                  onClick={() => setAdjustOpen((value) => !value)}
                  aria-pressed={adjustOpen}
                >
                  <Icon name="sliders" size={14} />
                  Adjust budget
                </button>
              ) : null}
            </>
          ) : null
        }
        subtitle={
          openRow && openRow.arm ? (
            <span className="ui-row">
              <span className="viz-swatch is-dot" style={{ backgroundColor: armColor(arms, openRow.arm.profile_id) }} aria-hidden="true" />
              {openRow.arm.name} · {openRow.arm.model}
              {openRow.arm.strategy === "MANUAL" ? (
                <Badge tone="info" title={SET_BY_HAND_TITLE}>
                  Set by hand
                </Badge>
              ) : null}
            </span>
          ) : null
        }
      >
        {openRow && canChangeArm && armFormOpen ? (
          <ChangeArmForm
            key={openRow.enrollment_id}
            study={study}
            row={openRow}
            arms={arms}
            onDone={() => {
              setArmFormOpen(false);
              onReload();
            }}
            onCancel={() => setArmFormOpen(false)}
          />
        ) : null}
        {openRow && consentStatements.length && openRow.consent_answers ? (
          <details className="research-advanced">
            <summary>Consent answers · {consentTicked(openRow.consent_answers, consentStatements)}</summary>
            <ConsentReview
              consent={{ statements: consentStatements }}
              answers={openRow.consent_answers}
              idPrefix={`participant-consent-${openRow.enrollment_id}`}
            />
          </details>
        ) : null}
        {openRow ? (
          <ParticipantDashboard
            studyId={study.study_id}
            participant={openRow}
            color={openRow.arm ? armColor(arms, openRow.arm.profile_id) : "var(--viz-1)"}
            warningFraction={warningFraction}
            budgetAdjustOpen={adjustOpen}
            onBudgetAdjustClose={() => setAdjustOpen(false)}
            canAdjustBudget={!stopped}
            onBudgetChanged={onReload}
          />
        ) : null}
      </Drawer>
    </div>
  );
};

export default StudyParticipants;
