import React, { useEffect, useState } from "react";
import { getStudyParticipantCoverage } from "../../utils/api";

// Study-owner-scoped participant coverage (ISSUE-13). Rows only contain
// study-local participant codes, the frozen enrollment assignment and
// session/event counts, so nothing here is a cross-study personal timeline.

const formatTimestamp = (value) => {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
};

const formatCounts = (counts) => {
  const entries = Object.entries(counts || {});
  if (entries.length === 0) return "—";
  return entries.map(([key, value]) => `${key}: ${value}`).join(" · ");
};

const EMPTY_STATE = {
  isLoading: true,
  forbidden: false,
  missing: false,
  error: "",
  data: null,
};

const StudyParticipantCoverage = ({ studyId }) => {
  const [state, setState] = useState(EMPTY_STATE);
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    let cancelled = false;
    if (!studyId) {
      setState({ ...EMPTY_STATE, isLoading: false });
      return () => {
        cancelled = true;
      };
    }
    setState({ ...EMPTY_STATE, isLoading: true });
    getStudyParticipantCoverage(studyId)
      .then((result) => {
        if (cancelled) return;
        if (!result) {
          setState({
            ...EMPTY_STATE,
            isLoading: false,
            error: "Participant coverage could not be loaded.",
          });
        } else if (result.ok) {
          setState({
            ...EMPTY_STATE,
            isLoading: false,
            data: result.data || {},
          });
        } else if (result.forbidden || result.status === 403) {
          setState({
            ...EMPTY_STATE,
            isLoading: false,
            forbidden: true,
            error:
              result.error ||
              "Only the study owner or an administrator can view study participant coverage.",
          });
        } else if (result.missing) {
          setState({
            ...EMPTY_STATE,
            isLoading: false,
            missing: true,
            error:
              result.error ||
              "Study participant coverage is not available on this server yet.",
          });
        } else {
          setState({
            ...EMPTY_STATE,
            isLoading: false,
            error: result.error || "Participant coverage could not be loaded.",
          });
        }
      })
      .catch(() => {
        if (cancelled) return;
        setState({
          ...EMPTY_STATE,
          isLoading: false,
          error: "Participant coverage could not be loaded.",
        });
      });
    return () => {
      cancelled = true;
    };
  }, [studyId, reloadToken]);

  const participants = state.data?.participants || [];

  return (
    <div className="research-participant-coverage">
      <div className="research-card-header">
        <h4>Study participant coverage</h4>
        <button
          type="button"
          className="secondary-button"
          onClick={() => setReloadToken((value) => value + 1)}
          disabled={state.isLoading}
        >
          Refresh
        </button>
      </div>
      <p className="research-scope-hint">
        Study participants — study-scoped, participant-local codes
      </p>

      {state.isLoading && (
        <p className="research-hint" role="status">
          Loading participant coverage...
        </p>
      )}
      {state.forbidden && (
        <p className="research-error" role="alert">
          {state.error}
        </p>
      )}
      {state.missing && (
        <p className="research-error" role="alert">
          {state.error}
        </p>
      )}
      {state.error && !state.forbidden && !state.missing && (
        <p className="research-error" role="alert">
          {state.error}
        </p>
      )}

      {!state.isLoading && !state.forbidden && !state.missing && !state.error && (
        participants.length === 0 ? (
          <p className="research-hint">
            {state.data?.coverage_reason
              ? `No participant coverage: ${state.data.coverage_reason}.`
              : "No participants are enrolled in this study yet."}
          </p>
        ) : (
          <div className="research-table-wrap">
            <table className="research-table">
              <caption className="research-visually-hidden">
                Study-local participant coverage
              </caption>
              <thead>
                <tr>
                  <th>Participant</th>
                  <th>Enrollment status</th>
                  <th>Frozen assignment</th>
                  <th>Sessions</th>
                  <th>Events</th>
                </tr>
              </thead>
              <tbody>
                {participants.map((row) => (
                  <tr key={row.enrollment_id || row.participant_code}>
                    <td>
                      <strong>{row.participant_code || "—"}</strong>
                      <small>Enrolled {formatTimestamp(row.enrolled_at)}</small>
                    </td>
                    <td>
                      <span className="research-status">
                        {row.status || "Unknown"}
                      </span>
                    </td>
                    <td>
                      {row.assignment ? (
                        <>
                          <span
                            className="research-participant-profile"
                            title={row.assignment.agent_profile_id}
                          >
                            {row.assignment.agent_profile_id}
                          </span>
                          <small>
                            {row.assignment.strategy} · epoch{" "}
                            {row.assignment.randomization_epoch} ·{" "}
                            {row.assignment.status}
                          </small>
                        </>
                      ) : (
                        <span className="research-hint">
                          No frozen assignment
                        </span>
                      )}
                    </td>
                    <td>
                      <span>
                        {row.sessions?.total ?? 0} total ·{" "}
                        {row.sessions?.active ?? 0} active ·{" "}
                        {row.sessions?.terminal ?? 0} terminal
                      </span>
                      <small>
                        Last activity:{" "}
                        {formatTimestamp(row.sessions?.last_activity_at)}
                      </small>
                      <small>
                        Last heartbeat:{" "}
                        {formatTimestamp(row.sessions?.last_heartbeat_at)}
                      </small>
                    </td>
                    <td>
                      <span>{row.events?.total ?? 0} events</span>
                      <small>
                        By type: {formatCounts(row.events?.by_event_type)}
                      </small>
                      <small>
                        By source: {formatCounts(row.events?.by_source)}
                      </small>
                      <small>
                        Last event:{" "}
                        {formatTimestamp(row.events?.last_occurred_at)}
                      </small>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}
    </div>
  );
};

export default StudyParticipantCoverage;
