import React, { useEffect, useState } from "react";
import {
  cloneResearchStudy,
  createResearchStudy,
  engageResearchKillSwitch,
  getAgentProfiles,
  getCurrentUser,
  listResearchStudies,
  releaseResearchKillSwitch,
  stopResearchStudy,
  updateResearchStudyMetadata,
} from "../../utils/api";
import "./research.css";
import "./ResearchStudies.css";

const STATUS_LABELS = {
  DRAFT: "Draft",
  ACTIVE: "Active",
  STUDY_STOPPED: "Stopped",
};

const ResearchStudies = () => {
  const [studies, setStudies] = useState([]);
  const [selectedStudy, setSelectedStudy] = useState(null);
  const [isCreating, setIsCreating] = useState(false);
  const [form, setForm] = useState({ name: "", description: "", profileIds: [] });
  const [profiles, setProfiles] = useState([]);
  const [metadata, setMetadata] = useState({ name: "", description: "" });
  const [isBusy, setIsBusy] = useState(false);
  const [isAdmin, setIsAdmin] = useState(false);
  const [killSwitchId, setKillSwitchId] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const loadStudies = async () => {
    setError("");
    const result = await listResearchStudies();
    if (result.ok) {
      const nextStudies = Array.isArray(result.data) ? result.data : [];
      setStudies(nextStudies);
      if (selectedStudy) {
        setSelectedStudy(
          nextStudies.find((study) => study.study_id === selectedStudy.study_id) || null,
        );
      }
    } else {
      setError(result.error || "Research studies are unavailable.");
    }
  };

  useEffect(() => {
    loadStudies();
    // Initial load only; actions explicitly refresh state below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    Promise.resolve(getAgentProfiles()).then((result) => {
      if (result && result.ok) setProfiles((result.data || []).filter((profile) => profile.is_active !== false));
    });
  }, []);

  useEffect(() => {
    getCurrentUser().then((result) => {
      if (result && result.ok && result.user) setIsAdmin(result.user.is_admin === true);
    });
  }, []);

  const openStudy = (study) => {
    setSelectedStudy(study);
    setMetadata({ name: study.name || "", description: study.description || "" });
    setNotice("");
    setError("");
  };

  const handleCreate = async (event) => {
    event.preventDefault();
    setIsBusy(true);
    setError("");
    setNotice("");
    const result = await createResearchStudy(form);
    if (result.ok) {
      setForm({ name: "", description: "", profileIds: [] });
      setIsCreating(false);
      setNotice("Study created in Draft state.");
      await loadStudies();
    } else {
      setError(result.error || "Study could not be created.");
    }
    setIsBusy(false);
  };

  const handleMetadataSave = async (event) => {
    event.preventDefault();
    if (!selectedStudy) return;
    setIsBusy(true);
    setError("");
    setNotice("");
    const result = await updateResearchStudyMetadata(selectedStudy.study_id, metadata);
    if (result.ok) {
      setNotice("Study metadata updated.");
      await loadStudies();
    } else {
      setError(result.error || "Study metadata is locked.");
    }
    setIsBusy(false);
  };

  const handleStop = async () => {
    if (!selectedStudy || !window.confirm("Stop this study permanently? Research data will be retained.")) {
      return;
    }
    setIsBusy(true);
    setError("");
    setNotice("");
    const result = await stopResearchStudy(selectedStudy.study_id);
    if (result.ok) {
      setNotice("Study stopped. Collection is revoked and retained data remains available.");
      await loadStudies();
    } else {
      setError(result.error || "Study could not be stopped.");
    }
    setIsBusy(false);
  };

  const handleClone = async () => {
    if (!selectedStudy) return;
    setIsBusy(true);
    setError("");
    setNotice("");
    const result = await cloneResearchStudy(selectedStudy.study_id);
    if (result.ok) {
      setNotice("A new Draft study was created without participants or agent profiles.");
      await loadStudies();
    } else {
      setError(result.error || "Only stopped studies can be cloned.");
    }
    setIsBusy(false);
  };

  const handleKillSwitch = async () => {
    if (!selectedStudy) return;
    setIsBusy(true);
    setError("");
    const result = killSwitchId
      ? await releaseResearchKillSwitch(killSwitchId)
      : await engageResearchKillSwitch(selectedStudy.study_id, "Admin maintenance");
    if (result.ok) {
      setKillSwitchId(killSwitchId ? "" : result.data.switch_id || "");
      setNotice(killSwitchId ? "Kill switch released." : "Kill switch engaged.");
    } else {
      setError(result.error || "Kill switch operation failed.");
    }
    setIsBusy(false);
  };

  return (
    <section className="research-page" aria-labelledby="research-studies-title">
      <header className="research-header">
        <div>
          <h2 id="research-studies-title">Research Studies</h2>
          <p>Create a study once, monitor its lifecycle, and stop it when collection ends.</p>
        </div>
        <button
          type="button"
          className="primary-button"
          onClick={() => setIsCreating((value) => !value)}
          disabled={isBusy}
        >
          {isCreating ? "Close" : "New study"}
        </button>
      </header>

      {error && <p className="research-error" role="alert">{error}</p>}
      {notice && <p className="research-notice" role="status">{notice}</p>}

      {isCreating && (
        <form className="research-card" onSubmit={handleCreate}>
          <h3>New study</h3>
          <label>
            Name
            <input
              value={form.name}
              onChange={(event) => setForm({ ...form, name: event.target.value })}
              required
              disabled={isBusy}
            />
          </label>
          <label>
            Description
            <textarea
              value={form.description}
              onChange={(event) => setForm({ ...form, description: event.target.value })}
              rows={3}
              disabled={isBusy}
            />
          </label>
          <fieldset className="research-profile-selection">
            <legend>Agent profiles</legend>
            {profiles.length === 0 ? (
              <p className="research-hint">No active agent profiles available.</p>
            ) : (
              profiles.map((profile) => (
                <label key={profile.profile_id}>
                  <input
                    type="checkbox"
                    aria-label={profile.name}
                    checked={form.profileIds.includes(profile.profile_id)}
                    onChange={(event) => {
                      const nextIds = event.target.checked
                        ? [...form.profileIds, profile.profile_id]
                        : form.profileIds.filter((id) => id !== profile.profile_id);
                      setForm({ ...form, profileIds: nextIds });
                    }}
                    disabled={isBusy}
                  />
                  <span>{profile.name}</span>
                  <small>{profile.model}</small>
                </label>
              ))
            )}
          </fieldset>
          <button
            type="submit"
            className="primary-button"
            disabled={isBusy || !form.name.trim() || form.profileIds.length === 0}
          >
            Create Draft study
          </button>
        </form>
      )}

      <div className="research-layout">
        <section className="research-card" aria-label="Study list">
          <div className="research-card-header">
            <h3>Studies</h3>
            <button type="button" className="secondary-button" onClick={loadStudies} disabled={isBusy}>
              Refresh
            </button>
          </div>
          {studies.length === 0 ? (
            <p className="research-hint">No research studies yet.</p>
          ) : (
            <ul className="research-list">
              {studies.map((study) => (
                <li key={study.study_id}>
                  <button type="button" className="research-list-item" onClick={() => openStudy(study)}>
                    <span>
                      <strong>{study.name}</strong>
                      <small>{study.description || "No description"}</small>
                    </span>
                    <span className={`research-status research-status-${String(study.research_status || "DRAFT").toLowerCase()}`}>
                      {STATUS_LABELS[study.research_status] || study.research_status || "Draft"}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        {selectedStudy && (
          <section className="research-card" aria-label="Study details">
            <h3>{selectedStudy.name}</h3>
            <dl className="research-metrics">
              <div><dt>Status</dt><dd>{STATUS_LABELS[selectedStudy.research_status] || selectedStudy.research_status}</dd></div>
              <div><dt>Join code</dt><dd>{selectedStudy.join_code || "Not available"}</dd></div>
              <div><dt>Study ID</dt><dd>{selectedStudy.study_id}</dd></div>
            </dl>
            <div className="research-profile-summary">
              <h4>Selected agent profiles</h4>
              {selectedStudy.profile_selections?.length ? (
                <ul>
                  {selectedStudy.profile_selections.map((profile) => (
                    <li key={profile.profile_id}>
                      <strong>{profile.name || profile.profile_id}</strong>
                      <small>{profile.model || "Model not specified"}</small>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="research-hint">No profiles selected.</p>
              )}
              <p className="research-hint">Profile selection is fixed after study creation.</p>
            </div>
            {selectedStudy.research_status !== "STUDY_STOPPED" && (
              <form onSubmit={handleMetadataSave}>
                <label>
                  Name
                  <input value={metadata.name} onChange={(event) => setMetadata({ ...metadata, name: event.target.value })} disabled={isBusy || Boolean(selectedStudy.consent_locked_at)} />
                </label>
                <label>
                  Description
                  <textarea value={metadata.description} onChange={(event) => setMetadata({ ...metadata, description: event.target.value })} rows={3} disabled={isBusy || Boolean(selectedStudy.consent_locked_at)} />
                </label>
                <div className="research-actions">
                  <button type="submit" className="secondary-button" disabled={isBusy || Boolean(selectedStudy.consent_locked_at)}>Save metadata</button>
                  <button type="button" className="danger-button" onClick={handleStop} disabled={isBusy}>Stop study</button>
                </div>
              </form>
            )}
            {isAdmin && selectedStudy.research_status !== "STUDY_STOPPED" && (
              <button type="button" className="secondary-button" onClick={handleKillSwitch} disabled={isBusy}>
                {killSwitchId ? "Release kill switch" : "Engage kill switch"}
              </button>
            )}
            {selectedStudy.research_status === "STUDY_STOPPED" && (
              <button type="button" className="primary-button" onClick={handleClone} disabled={isBusy}>Clone as new Draft</button>
            )}
          </section>
        )}
      </div>
    </section>
  );
};

export default ResearchStudies;
