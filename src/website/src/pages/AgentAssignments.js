import React, { useEffect, useMemo, useState } from "react";
import {
  deleteAgentAssignment,
  getAgentAssignments,
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
  const [pinUserId, setPinUserId] = useState("");
  const [pinProfileId, setPinProfileId] = useState("");
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

  // Count of users assigned to each profile, split by how they got there.
  const distribution = useMemo(() => {
    const counts = {};
    assignments.forEach((a) => {
      const key = a.profile_name || profileNameById[a.profile_id] || "unknown";
      if (!counts[key]) counts[key] = { auto: 0, manual: 0, total: 0 };
      counts[key][a.source === "manual" ? "manual" : "auto"] += 1;
      counts[key].total += 1;
    });
    return Object.entries(counts).sort((a, b) => a[0].localeCompare(b[0]));
  }, [assignments, profileNameById]);

  const loadData = async () => {
    setIsLoading(true);
    setError("");
    const [assignmentsRes, profilesRes] = await Promise.all([
      getAgentAssignments(),
      getAgentProfiles(),
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
    setIsLoading(false);
  };

  useEffect(() => {
    loadData();
  }, []);

  const handlePin = async (event) => {
    event.preventDefault();
    if (!pinUserId.trim()) {
      setError("User ID is required.");
      return;
    }
    if (!pinProfileId) {
      setError("Pick a profile to pin the user to.");
      return;
    }

    setIsSaving(true);
    setError("");
    setNotice("");
    const response = await setAgentAssignment(pinUserId.trim(), pinProfileId);
    if (response.ok) {
      setNotice("User pinned to profile.");
      setPinUserId("");
      setPinProfileId("");
      await loadData();
    } else {
      setError(response.error);
    }
    setIsSaving(false);
  };

  const handleReassign = async (userId, profileId) => {
    if (!profileId) return;
    setIsSaving(true);
    setError("");
    setNotice("");
    const response = await setAgentAssignment(userId, profileId);
    if (response.ok) {
      setNotice("Assignment updated (manual).");
      await loadData();
    } else {
      setError(response.error);
    }
    setIsSaving(false);
  };

  const handleClear = async (userId) => {
    const confirmed = window.confirm(
      "Clear this assignment? The user will be re-bucketed at random on their next agent task.",
    );
    if (!confirmed) return;

    setIsSaving(true);
    setError("");
    setNotice("");
    const response = await deleteAgentAssignment(userId);
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
            Which A/B variant each user is pinned to. Assignments are normally
            drawn at random on a user's first agent task; pin a user manually
            only for test accounts or debugging — manual pins are excluded from
            A/B analysis.
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
            {distribution.map(([name, counts]) => (
              <div className="distribution-card" key={name}>
                <span className="distribution-name">{name}</span>
                <span className="distribution-total">{counts.total}</span>
                <span className="distribution-breakdown">
                  {counts.auto} auto · {counts.manual} manual
                  {frameworkByName[name] ? ` · ${frameworkByName[name]}` : ""}
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
            User ID
            <input
              value={pinUserId}
              onChange={(e) => setPinUserId(e.target.value)}
              placeholder="user UUID"
              disabled={isSaving}
            />
          </label>
          <label>
            Profile
            <select
              value={pinProfileId}
              onChange={(e) => setPinProfileId(e.target.value)}
              disabled={isSaving}
            >
              <option value="">Select a profile…</option>
              {profiles.map((p) => (
                <option key={p.profile_id || p.id} value={p.profile_id || p.id}>
                  {p.name}
                  {p.is_active === false ? " (inactive)" : ""}
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
                <th>Profile</th>
                <th>Source</th>
                <th>Assigned at</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {assignments.map((a) => (
                <tr key={a.user_id}>
                  <td title={a.user_id}>{shortId(a.user_id)}</td>
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
                    <div className="table-actions">
                      <select
                        defaultValue=""
                        onChange={(e) => {
                          handleReassign(a.user_id, e.target.value);
                          e.target.value = "";
                        }}
                        disabled={isSaving}
                        title="Reassign to a different profile (becomes manual)"
                      >
                        <option value="">Reassign…</option>
                        {profiles.map((p) => (
                          <option
                            key={p.profile_id || p.id}
                            value={p.profile_id || p.id}
                          >
                            {p.name}
                          </option>
                        ))}
                      </select>
                      <button
                        className="danger-button"
                        onClick={() => handleClear(a.user_id)}
                        disabled={isSaving}
                      >
                        Clear
                      </button>
                    </div>
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
