import React, { useEffect, useMemo, useState } from "react";
import {
  deleteAgentAssignment,
  getAgentAssignments,
  getAgentAssignmentOptions,
  getAgentProfiles,
  setAgentAssignment,
} from "../utils/api";
import "./AgentAssignments.css";

const formatDate = (value) => {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
};

const shortId = (id) => (id ? `${String(id).slice(0, 8)}…` : "—");

const AgentAssignments = () => {
  const [assignments, setAssignments] = useState([]);
  const [profiles, setProfiles] = useState([]);
  const [users, setUsers] = useState([]);
  const [studies, setStudies] = useState([]);
  const [pinUserId, setPinUserId] = useState("");
  const [pinProfileId, setPinProfileId] = useState("");
  const [pinStudyId, setPinStudyId] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const profileNameById = useMemo(() => {
    const map = {};
    profiles.forEach((p) => {
      map[p.profile_id || p.id] = p.name;
    });
    return map;
  }, [profiles]);

  const userById = useMemo(
    () => Object.fromEntries(users.map((user) => [user.user_id, user])),
    [users],
  );

  const studyById = useMemo(
    () => Object.fromEntries(studies.map((study) => [study.study_id, study])),
    [studies],
  );

  const selectedStudy = useMemo(
    () =>
      studyById[pinStudyId] || studies.find((study) => study.is_active) || null,
    [pinStudyId, studies, studyById],
  );

  const eligibleProfiles = useMemo(() => {
    if (!selectedStudy) return profiles;
    const profileIds = new Set(selectedStudy.profile_ids || []);
    return profiles.filter((profile) => profileIds.has(profile.profile_id || profile.id));
  }, [profiles, selectedStudy]);

  useEffect(() => {
    if (
      pinProfileId &&
      !eligibleProfiles.some((profile) => (profile.profile_id || profile.id) === pinProfileId)
    ) {
      setPinProfileId("");
    }
  }, [eligibleProfiles, pinProfileId]);

  // Which runtime each arm targets. Worth surfacing here because arms in the
  // same study can now run entirely different agents (Goose vs Codex vs the
  // built-in runtime), and that is usually the variable under test.
  const frameworkByName = useMemo(() => {
    const map = {};
    profiles.forEach((p) => {
      if (p.name) map[p.name] = p.framework_version;
    });
    return map;
  }, [profiles]);

  // Treatment distribution is meaningful only within a study.
  const distribution = useMemo(() => {
    const counts = {};
    assignments.forEach((a) => {
      const profileName =
        a.profile_name || profileNameById[a.profile_id] || "unknown";
      const key = `${a.study_id}:${profileName}`;
      if (!counts[key]) {
        counts[key] = {
          auto: 0,
          manual: 0,
          total: 0,
          profileName,
          studyId: a.study_id,
        };
      }
      counts[key][a.source === "manual" ? "manual" : "auto"] += 1;
      counts[key].total += 1;
    });
    return Object.values(counts).sort((a, b) =>
      `${a.studyId}:${a.profileName}`.localeCompare(
        `${b.studyId}:${b.profileName}`,
      ),
    );
  }, [assignments, profileNameById]);

  const loadData = async () => {
    setIsLoading(true);
    setError("");
    const [assignmentsRes, profilesRes, optionsRes] = await Promise.all([
      getAgentAssignments(),
      getAgentProfiles(),
      getAgentAssignmentOptions(),
    ]);
    if (assignmentsRes.ok) {
      setAssignments(assignmentsRes.data || []);
    } else {
      setError(assignmentsRes.error);
    }
    if (profilesRes.ok) {
      setProfiles(
        Array.isArray(profilesRes.data) ? profilesRes.data : [],
      );
    }
    if (optionsRes.ok) {
      setUsers(optionsRes.data.users || []);
      setStudies(optionsRes.data.studies || []);
    } else {
      setError(optionsRes.error);
    }
    setIsLoading(false);
  };

  useEffect(() => {
    loadData();
  }, []);

  const handlePin = async (event) => {
    event.preventDefault();
    if (!pinUserId) {
      setError("Pick a user.");
      return;
    }
    if (!pinProfileId) {
      setError("Pick a profile to pin the user to.");
      return;
    }

    setIsSaving(true);
    setError("");
    setNotice("");
    const response = await setAgentAssignment(
      pinUserId,
      pinProfileId,
      pinStudyId || undefined,
    );
    if (response.ok) {
      setNotice("Study assignment created.");
      setPinUserId("");
      setPinProfileId("");
      setPinStudyId("");
      await loadData();
    } else {
      setError(response.error);
    }
    setIsSaving(false);
  };

  const handleClear = async (userId, studyId) => {
    const confirmed = window.confirm(
      "Clear this assignment? The user will be re-drawn on their next agent task.",
    );
    if (!confirmed) return;

    setIsSaving(true);
    setError("");
    setNotice("");
    const response = await deleteAgentAssignment(userId, studyId);
    if (response.ok) {
      setNotice("Assignment cleared.");
      await loadData();
    } else {
      setError(response.error);
    }
    setIsSaving(false);
  };

  return (
    <section className="agent-assignments-page">
      <div className="agent-assignments-header">
        <div>
          <h2>Agent Assignments</h2>
          <p>
            A/B assignments, grouped by study. New assignments are
            normally drawn on a user's first agent task; add a manual row only
            for test accounts or debugging.
          </p>
        </div>
        <button
          className="secondary-button"
          onClick={loadData}
          disabled={isLoading}
        >
          Refresh
        </button>
      </div>

      {(error || notice) && (
        <div className={`assignment-message ${error ? "error" : "success"}`}>
          {error || notice}
        </div>
      )}

      <div className="distribution-panel">
        <h3>Bucket distribution</h3>
        {distribution.length === 0 ? (
          <div className="assignments-empty">No assignments yet.</div>
        ) : (
          <div className="distribution-grid">
            {distribution.map((counts) => (
              <div
                className="distribution-card"
                key={`${counts.studyId}:${counts.profileName}`}
              >
                <span className="distribution-name">{counts.profileName}</span>
                <span className="distribution-total">{counts.total}</span>
                <span className="distribution-breakdown">
                  {counts.auto} auto · {counts.manual} manual
                  {frameworkByName[counts.profileName]
                    ? ` · ${frameworkByName[counts.profileName]}`
                    : ""}
                </span>
                <span className="distribution-breakdown" title={counts.studyId}>
                  Study {shortId(counts.studyId)}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      <form className="pin-form" onSubmit={handlePin}>
        <h3>Pin a user</h3>
        <div className="pin-form-row">
          <label>
            User
            <select
              value={pinUserId}
              onChange={(e) => setPinUserId(e.target.value)}
              disabled={isSaving}
            >
              <option value="">Select a user…</option>
              {users.map((user) => (
                <option key={user.user_id} value={user.user_id}>
                  {user.name} ({user.email})
                </option>
              ))}
            </select>
          </label>
          <label>
            Profile
            <select
              value={pinProfileId}
              onChange={(e) => setPinProfileId(e.target.value)}
              disabled={isSaving || eligibleProfiles.length === 0}
            >
              <option value="">
                {eligibleProfiles.length === 0
                  ? "No agent arms in this study"
                  : "Select a profile…"}
              </option>
              {eligibleProfiles.map((p) => (
                <option key={p.profile_id || p.id} value={p.profile_id || p.id}>
                  {p.name}
                  {p.is_active === false ? " (inactive)" : ""}
                </option>
              ))}
            </select>
          </label>
          <label>
            Study
            <select
              value={pinStudyId}
              onChange={(e) => setPinStudyId(e.target.value)}
              disabled={isSaving}
            >
              <option value="">Use the active study</option>
              {studies.map((study) => (
                <option key={study.study_id} value={study.study_id}>
                  {study.name}{study.is_active ? "" : " (inactive)"}
                </option>
              ))}
            </select>
          </label>
          <button className="primary-button" type="submit" disabled={isSaving}>
            Pin user
          </button>
        </div>
      </form>

      <div className="assignments-table-panel">
        {isLoading ? (
          <div className="assignments-empty">Loading assignments…</div>
        ) : assignments.length === 0 ? (
          <div className="assignments-empty">
            No users have been assigned a profile yet.
          </div>
        ) : (
          <table className="assignments-table">
            <thead>
              <tr>
                <th>User</th>
                <th>Study</th>
                <th>Profile</th>
                <th>Source</th>
                <th>Assigned at</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {assignments.map((a) => (
                <tr key={a.assignment_id}>
                  <td title={a.user_id}>
                    {userById[a.user_id]
                      ? `${userById[a.user_id].name} (${userById[a.user_id].email})`
                      : shortId(a.user_id)}
                  </td>
                  <td title={a.study_id}>
                    {studyById[a.study_id]?.name || shortId(a.study_id)}
                  </td>
                  <td>
                    {a.profile_name ||
                      profileNameById[a.profile_id] ||
                      shortId(a.profile_id)}
                  </td>
                  <td>
                    <span className={`source-badge ${a.source}`}>
                      {a.source}
                    </span>
                  </td>
                  <td>{formatDate(a.assigned_at)}</td>
                  <td>
                    <button
                      className="danger-button"
                      onClick={() => handleClear(a.user_id, a.study_id)}
                      disabled={isSaving}
                    >
                      Clear
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
};

export default AgentAssignments;
