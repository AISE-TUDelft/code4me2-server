import React, { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { getResearchStudy, updateResearchStudyMetadata } from "../../utils/api";
import "./research.css";

const ResearchStudyEditor = () => {
  const { studyId } = useParams();
  const navigate = useNavigate();
  const [study, setStudy] = useState(null);
  const [form, setForm] = useState({ name: "", description: "" });
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [isBusy, setIsBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getResearchStudy(studyId).then((result) => {
      if (cancelled) return;
      if (result.ok) {
        setStudy(result.data);
        setForm({ name: result.data.name || "", description: result.data.description || "" });
      } else {
        setError(result.error || "Study could not be loaded.");
      }
    });
    return () => {
      cancelled = true;
    };
  }, [studyId]);

  const locked = !study || Boolean(study.consent_locked_at) || study.research_status === "STUDY_STOPPED";

  const saveMetadata = async (event) => {
    event.preventDefault();
    setIsBusy(true);
    setError("");
    setNotice("");
    const result = await updateResearchStudyMetadata(studyId, form);
    if (result.ok) {
      setStudy(result.data.study || result.data);
      setNotice("Study metadata updated.");
    } else {
      setError(result.error || "Study metadata is locked.");
    }
    setIsBusy(false);
  };

  if (!study && !error) return <section className="research-page">Loading study...</section>;

  return (
    <section className="research-page" aria-labelledby="study-editor-title">
      <button type="button" className="secondary-button" onClick={() => navigate("/research/studies")}>
        Back to studies
      </button>
      <h2 id="study-editor-title">Study metadata</h2>
      {error && <p className="research-error" role="alert">{error}</p>}
      {notice && <p className="research-notice" role="status">{notice}</p>}
      {study && (
        <form className="research-card" onSubmit={saveMetadata}>
          <p className="research-hint">Configuration, agent profiles and telemetry policy are fixed separately from editable metadata.</p>
          <dl className="research-metrics">
            <div><dt>Status</dt><dd>{study.research_status || "DRAFT"}</dd></div>
            <div><dt>Join code</dt><dd>{study.join_code || "Not available"}</dd></div>
          </dl>
          <label>
            Name
            <input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} disabled={locked || isBusy} required />
          </label>
          <label>
            Description
            <textarea value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} disabled={locked || isBusy} rows={4} />
          </label>
          <button type="submit" className="primary-button" disabled={locked || isBusy}>Save metadata</button>
          {locked && <p className="research-hint">Metadata is locked after consent or study stop.</p>}
        </form>
      )}
    </section>
  );
};

export default ResearchStudyEditor;
