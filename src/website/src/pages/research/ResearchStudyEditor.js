import React, { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getResearchStudy, updateResearchStudyMetadata } from "../../utils/api";
import Icon from "../../components/common/Icon";
import { Card, Loading, PageHeader } from "../../components/common/ui";
import { STATUS_LABELS, statusClass } from "./studyUtils";
import "./research.css";

/**
 * Stand-alone metadata editor (deep link `/research/studies/:id/editor`). The
 * same controls live in the study workspace's Settings tab.
 */
const ResearchStudyEditor = () => {
  const { studyId } = useParams();
  const [study, setStudy] = useState(null);
  const [form, setForm] = useState({ name: "", description: "" });
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [isBusy, setIsBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getResearchStudy(studyId).then((result) => {
      if (cancelled) return;
      if (result && result.ok) {
        setStudy(result.data);
        setForm({ name: result.data.name || "", description: result.data.description || "" });
      } else {
        setError((result && result.error) || "Study could not be loaded.");
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
    if (result && result.ok) {
      setStudy(result.data.study || result.data);
      setNotice("Study metadata updated.");
    } else {
      setError((result && result.error) || "Study metadata is locked.");
    }
    setIsBusy(false);
  };

  const back = (
    <Link className="secondary-button" to={`/research/studies/${encodeURIComponent(studyId)}`}>
      <Icon name="chevronLeft" size={15} />
      Back to study
    </Link>
  );

  const header = (
    <PageHeader
      titleId="study-editor-title"
      title="Study metadata"
      description="Configuration, agent profiles and telemetry policy are fixed separately from editable metadata."
      actions={back}
    />
  );

  if (!study && !error) {
    return (
      <section className="ui-page research-page" aria-labelledby="study-editor-title">
        {header}
        <Loading label="Loading study..." />
      </section>
    );
  }

  return (
    <section className="ui-page research-page" aria-labelledby="study-editor-title">
      {header}
      {error ? (
        <p className="research-error" role="alert">
          {error}
        </p>
      ) : null}
      {notice ? (
        <p className="research-notice" role="status">
          {notice}
        </p>
      ) : null}
      {study ? (
        <Card
          as="form"
          onSubmit={saveMetadata}
          title={
            <span className="ui-row">
              {study.name}
              <span className={statusClass(study.research_status)}>
                {STATUS_LABELS[study.research_status] || study.research_status || "Draft"}
              </span>
            </span>
          }
          subtitle={study.join_code ? `Join code ${study.join_code}` : "Join code not available"}
          footer={
            <button type="submit" className="primary-button" disabled={locked || isBusy}>
              Save metadata
            </button>
          }
        >
          <div className="ui-field">
            <label className="ui-label" htmlFor="study-editor-name">
              Name
            </label>
            <input
              id="study-editor-name"
              className="ui-input"
              value={form.name}
              onChange={(event) => setForm({ ...form, name: event.target.value })}
              disabled={locked || isBusy}
              required
            />
          </div>
          <div className="ui-field">
            <label className="ui-label" htmlFor="study-editor-description">
              Description
            </label>
            <textarea
              id="study-editor-description"
              className="ui-textarea"
              value={form.description}
              onChange={(event) => setForm({ ...form, description: event.target.value })}
              disabled={locked || isBusy}
              rows={4}
            />
          </div>
          {locked ? <p className="research-hint">Metadata is locked after consent or study stop.</p> : null}
        </Card>
      ) : null}
    </section>
  );
};

export default ResearchStudyEditor;
