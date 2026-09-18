import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  createResearchStudy,
  getResearchDraft,
  getResearchEnrollmentCoverage,
  getResearchExposures,
  listResearchDrafts,
  listResearchRevisions,
  listResearchStudies,
  publishResearchDraft,
  retireResearchRevision,
  validateResearchProtocol,
} from "../../utils/api";
import { formatDateTime, shortId } from "./protocol";
import "./research.css";
import "./ResearchStudies.css";

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const RevisionStatusBadge = ({ status }) => {
  const normalized = String(status || "").toLowerCase();
  return (
    <span className={`research-status research-status-${normalized}`}>
      {status || "UNKNOWN"}
    </span>
  );
};

const ResearchStudies = () => {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  const [studyId, setStudyId] = useState(searchParams.get("study_id") || "");
  const [appliedStudyId, setAppliedStudyId] = useState(
    searchParams.get("study_id") || "",
  );
  const [studies, setStudies] = useState([]);
  const [studiesMissing, setStudiesMissing] = useState(false);
  const [studiesForbidden, setStudiesForbidden] = useState(false);

  const [revisions, setRevisions] = useState([]);
  const [drafts, setDrafts] = useState([]);
  const [isLoading, setIsLoading] = useState(false);
  const [isBusy, setIsBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [validationErrors, setValidationErrors] = useState([]);

  const [selectedRevisionId, setSelectedRevisionId] = useState("");
  const [exposures, setExposures] = useState([]);
  const [enrollmentCoverage, setEnrollmentCoverage] = useState(null);
  const [coverageError, setCoverageError] = useState("");

  // "New study" mints the study UUID server-side so the researcher never pastes
  // one. The form stays inline (no modal) and degrades to the manual UUID entry
  // below when the create endpoint is unavailable on an older server.
  const [isCreatingStudy, setIsCreatingStudy] = useState(false);
  const [newStudyName, setNewStudyName] = useState("");
  const [newStudyDescription, setNewStudyDescription] = useState("");

  const selectedRevision = useMemo(
    () =>
      revisions.find((revision) => revision.revision_id === selectedRevisionId) ||
      null,
    [revisions, selectedRevisionId],
  );

  // Study index may not exist on the server; the guarded call degrades to
  // manual study-id entry and a visible note rather than a hard failure.
  useEffect(() => {
    let cancelled = false;
    listResearchStudies().then((response) => {
      if (cancelled) return;
      if (response.ok) {
        setStudies(Array.isArray(response.data) ? response.data : []);
        setStudiesMissing(false);
        setStudiesForbidden(false);
      } else {
        setStudies([]);
        setStudiesMissing(!!response.missing);
        setStudiesForbidden(!!response.forbidden);
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const loadStudy = useCallback(async (id) => {
    const trimmed = (id || "").trim();
    if (!trimmed) {
      setError("Enter a study ID first.");
      return;
    }
    setIsLoading(true);
    setError("");
    setNotice("");
    setValidationErrors([]);
    setAppliedStudyId(trimmed);
    setSelectedRevisionId("");
    setExposures([]);
    setEnrollmentCoverage(null);
    setCoverageError("");

    const [revisionsRes, draftsRes] = await Promise.all([
      listResearchRevisions(trimmed),
      listResearchDrafts(trimmed),
    ]);

    if (revisionsRes.ok) {
      setRevisions(Array.isArray(revisionsRes.data) ? revisionsRes.data : []);
    } else {
      setRevisions([]);
      setError(revisionsRes.error);
    }
    if (draftsRes.ok) {
      setDrafts(Array.isArray(draftsRes.data) ? draftsRes.data : []);
    } else {
      setDrafts([]);
      if (revisionsRes.ok) setError(draftsRes.error);
    }
    setIsLoading(false);
  }, []);

  useEffect(() => {
    const fromUrl = searchParams.get("study_id");
    if (fromUrl) loadStudy(fromUrl);
    // Only auto-load once from the initial URL; subsequent navigations use the
    // form so we do not fight the researcher's edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Derived assignment/exposure counts for the selected revision. These come
  // from researcher read models; a failure is shown as "unavailable" rather
  // than as zero.
  useEffect(() => {
    if (!appliedStudyId || !selectedRevisionId) return undefined;
    let cancelled = false;
    (async () => {
      const [exposuresRes, coverageRes] = await Promise.all([
        getResearchExposures(appliedStudyId, selectedRevisionId),
        getResearchEnrollmentCoverage(appliedStudyId, selectedRevisionId),
      ]);
      if (cancelled) return;
      setExposures(exposuresRes.ok ? exposuresRes.data : []);
      setEnrollmentCoverage(coverageRes.ok ? coverageRes.data : null);
      setCoverageError(
        !exposuresRes.ok && !coverageRes.ok
          ? exposuresRes.error || coverageRes.error
          : !exposuresRes.ok
            ? exposuresRes.error
            : !coverageRes.ok
              ? coverageRes.error
              : "",
      );
    })();
    return () => {
      cancelled = true;
    };
  }, [appliedStudyId, selectedRevisionId]);

  const handleLoad = (event) => {
    event.preventDefault();
    if (!UUID_RE.test(studyId.trim())) {
      setError("A study ID must be a UUID.");
      return;
    }
    loadStudy(studyId);
  };

  const openNewDraft = () => {
    navigate(
      `/research/studies/${encodeURIComponent(appliedStudyId)}/editor`,
    );
  };

  const handleCreateStudy = async (event) => {
    event.preventDefault();
    const name = newStudyName.trim();
    if (!name) {
      setError("A study name is required.");
      return;
    }
    setIsBusy(true);
    setError("");
    setNotice("");
    setValidationErrors([]);
    const description = newStudyDescription.trim();
    const result = await createResearchStudy({
      name,
      description: description || null,
    });
    if (result.ok) {
      const study = result.data.study || {};
      setNewStudyName("");
      setNewStudyDescription("");
      setIsCreatingStudy(false);
      const query = new URLSearchParams({ name });
      if (description) query.set("description", description);
      navigate(
        `/research/studies/${encodeURIComponent(study.study_id)}/editor?${query.toString()}`,
      );
    } else {
      setError(result.error);
    }
    setIsBusy(false);
  };

  const openDraft = (draftId) => {
    navigate(
      `/research/studies/${encodeURIComponent(appliedStudyId)}/editor?draft_id=${encodeURIComponent(draftId)}`,
    );
  };

  const supersedeRevision = (revisionId) => {
    navigate(
      `/research/studies/${encodeURIComponent(appliedStudyId)}/editor?revision_id=${encodeURIComponent(revisionId)}`,
    );
  };

  const handleValidateDraft = async (draftId) => {
    setIsBusy(true);
    setError("");
    setNotice("");
    setValidationErrors([]);
    const draftRes = await getResearchDraft(draftId);
    if (!draftRes.ok) {
      setError(draftRes.error);
      setIsBusy(false);
      return;
    }
    const validation = await validateResearchProtocol(draftRes.protocol);
    if (validation.ok && validation.valid) {
      setNotice("Draft is valid and publishable.");
    } else {
      setValidationErrors(validation.errors || []);
      setError(validation.error || "Draft failed validation.");
    }
    setIsBusy(false);
  };

  const handlePublishDraft = async (draftId) => {
    setIsBusy(true);
    setError("");
    setNotice("");
    setValidationErrors([]);
    const draftRes = await getResearchDraft(draftId);
    if (!draftRes.ok) {
      setError(draftRes.error);
      setIsBusy(false);
      return;
    }
    const result = await publishResearchDraft(draftId, {
      protocol: draftRes.protocol,
    });
    if (result.ok) {
      const revision = result.data.revision;
      setNotice(
        `Published revision #${revision ? revision.revision_number : "?"} (${
          revision ? shortId(revision.protocol_digest) : ""
        }).`,
      );
      await loadStudy(appliedStudyId);
    } else if (result.status === 409) {
      setError("Publication conflict: a newer revision exists. Reload and retry.");
    } else {
      setValidationErrors(result.errors || []);
      setError(result.error);
    }
    setIsBusy(false);
  };

  const handleRetire = async (revision) => {
    const confirmed = window.confirm(
      `Retire revision #${revision.revision_number}? Its stored content and digest are unchanged; retirement is a lifecycle status only.`,
    );
    if (!confirmed) return;
    setIsBusy(true);
    setError("");
    setNotice("");
    const result = await retireResearchRevision(revision.revision_id);
    if (result.ok) {
      setNotice(
        result.data.retired
          ? `Revision #${revision.revision_number} retired.`
          : result.data.message || "Revision was already retired.",
      );
      await loadStudy(appliedStudyId);
    } else {
      setError(result.error);
    }
    setIsBusy(false);
  };

  return (
    <section className="research-page" aria-labelledby="research-studies-title">
      <div className="research-header">
        <div>
          <h2 id="research-studies-title">Research Studies</h2>
          <p>
            Draft, validate, publish and supersede versioned study protocols.
            Published revisions are immutable and content-addressed: editing one
            creates a successor revision rather than mutating it.
          </p>
        </div>
        <div className="research-header-actions">
          <button
            type="button"
            className="secondary-button"
            onClick={() => appliedStudyId && loadStudy(appliedStudyId)}
            disabled={isLoading || !appliedStudyId}
          >
            Refresh
          </button>
          <button
            type="button"
            className="primary-button"
            onClick={() => setIsCreatingStudy((current) => !current)}
            disabled={isBusy}
          >
            New study
          </button>
        </div>
      </div>

      {isCreatingStudy && (
        <form className="research-card" onSubmit={handleCreateStudy}>
          <h3>New study</h3>
          <p className="research-hint">
            The server mints the study UUID; you are taken straight to its
            editor. No identifier needs to be copied and pasted.
          </p>
          <label>
            Name
            <input
              value={newStudyName}
              onChange={(event) => setNewStudyName(event.target.value)}
              disabled={isBusy}
              placeholder="Adaptive agent study"
            />
          </label>
          <label>
            Description (optional)
            <textarea
              value={newStudyDescription}
              onChange={(event) => setNewStudyDescription(event.target.value)}
              disabled={isBusy}
              rows={2}
            />
          </label>
          <div className="research-actions">
            <button
              type="submit"
              className="primary-button"
              disabled={isBusy || !newStudyName.trim()}
            >
              Create study
            </button>
            <button
              type="button"
              className="secondary-button"
              onClick={() => setIsCreatingStudy(false)}
              disabled={isBusy}
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {studies.length === 0 && (studiesForbidden || studiesMissing) && (
        <div className="research-message error" role="alert">
          {studiesForbidden
            ? "Your account is not enabled for research. Ask an administrator to enable researcher access."
            : "The study index is not available on this server yet. Enter a study UUID below to continue."}
        </div>
      )}

      {studies.length > 0 && (
        <div className="research-card">
          <h3>Study index</h3>
          <p className="research-hint">
            Indexed studies with their study-scoped join code and latest revision
            status. Open a study to manage its revisions, or enter its UUID below
            when it is not listed.
          </p>
          <div className="research-table-wrap">
            <table className="research-table">
              <caption className="research-visually-hidden">
                Indexed research studies
              </caption>
              <thead>
                <tr>
                  <th scope="col">Study</th>
                  <th scope="col">Join code</th>
                  <th scope="col">Latest revision</th>
                  <th scope="col">Status</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {studies.map((study) => (
                  <tr
                    key={study.study_id}
                    className={
                      study.study_id === appliedStudyId
                        ? "research-row-selected"
                        : ""
                    }
                  >
                    <td>
                      <div className="research-study-name">
                        {study.name || "Untitled study"}
                      </div>
                      <span
                        className="research-study-id"
                        title={study.study_id}
                      >
                        {shortId(study.study_id)}
                      </span>
                    </td>
                    <td>
                      {study.join_code ? (
                        <code className="research-code-inline">
                          {study.join_code}
                        </code>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td>
                      {(() => {
                        const latest = study.latest_revision || {};
                        const number = latest.revision_number;
                        if (number === null || number === undefined) {
                          return "—";
                        }
                        return (
                          <>
                            <span>#{number}</span>
                            {latest.protocol_digest ? (
                              <span
                                className="research-draft-meta"
                                title={latest.protocol_digest}
                              >
                                {shortId(latest.protocol_digest)}
                              </span>
                            ) : null}
                          </>
                        );
                      })()}
                    </td>
                    <td>
                      <RevisionStatusBadge
                        status={(study.latest_revision || {}).status}
                      />
                    </td>
                    <td>
                      <button
                        type="button"
                        className="secondary-button"
                        onClick={() => {
                          setStudyId(study.study_id);
                          loadStudy(study.study_id);
                        }}
                        disabled={isLoading || isBusy}
                      >
                        Open
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <form className="research-card research-study-scope" onSubmit={handleLoad}>
        <h3>Study scope</h3>
        <label>
          Study ID (UUID)
          <input
            value={studyId}
            onChange={(event) => setStudyId(event.target.value)}
            placeholder="00000000-0000-0000-0000-000000000000"
            disabled={isLoading || isBusy}
            aria-describedby={
              studiesMissing ? "research-study-index-note" : undefined
            }
          />
        </label>
        {studiesMissing && (
          <p className="research-hint" id="research-study-index-note">
            This server does not expose a study index yet, so enter the study UUID
            (from the seed or bootstrap step) to load its revisions.
          </p>
        )}
        <div className="research-actions">
          <button
            type="submit"
            className="primary-button"
            disabled={isLoading || isBusy}
          >
            Load study
          </button>
          <button
            type="button"
            className="secondary-button"
            onClick={openNewDraft}
            disabled={!appliedStudyId || isBusy}
          >
            New draft
          </button>
          <button
            type="button"
            className="secondary-button"
            onClick={() =>
              navigate(
                `/research/enrollment?study_id=${encodeURIComponent(appliedStudyId)}`,
              )
            }
            disabled={!appliedStudyId}
          >
            Enrollment &amp; join code
          </button>
        </div>
      </form>

      {(error || notice) && (
        <div
          className={`research-message ${error ? "error" : "success"}`}
          role={error ? "alert" : "status"}
        >
          {error || notice}
        </div>
      )}

      {validationErrors.length > 0 && (
        <div className="research-card research-validation" role="alert">
          <h3>Validation problems</h3>
          <ul>
            {validationErrors.map((validationError, index) => (
              <li key={`${validationError.field}-${index}`}>
                <code>{validationError.field || "protocol"}</code>{" "}
                <span className="research-severity">
                  {validationError.severity || "ERROR"}
                </span>
                {validationError.message}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="research-card">
        <h3>Revisions</h3>
        {isLoading ? (
          <p className="research-empty">Loading revisions…</p>
        ) : revisions.length === 0 ? (
          <p className="research-empty">
            No published revisions for this study yet.
          </p>
        ) : (
          <div className="research-table-wrap">
            <table className="research-table">
              <caption className="research-visually-hidden">
                Immutable study revisions
              </caption>
              <thead>
                <tr>
                  <th scope="col">Rev</th>
                  <th scope="col">Revision ID</th>
                  <th scope="col">Digest</th>
                  <th scope="col">Status</th>
                  <th scope="col">Published</th>
                  <th scope="col">Supersedes</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {revisions.map((revision) => (
                  <tr
                    key={revision.revision_id}
                    className={
                      revision.revision_id === selectedRevisionId
                        ? "research-row-selected"
                        : ""
                    }
                  >
                    <td>{revision.revision_number}</td>
                    <td title={revision.revision_id}>
                      {shortId(revision.revision_id)}
                    </td>
                    <td title={revision.protocol_digest}>
                      <code>{shortId(revision.protocol_digest)}</code>
                    </td>
                    <td>
                      <RevisionStatusBadge status={revision.status} />
                    </td>
                    <td>{formatDateTime(revision.published_at)}</td>
                    <td title={revision.supersedes_revision_id || ""}>
                      {revision.supersedes_revision_id
                        ? shortId(revision.supersedes_revision_id)
                        : "—"}
                    </td>
                    <td>
                      <div className="research-actions research-actions-inline">
                        <button
                          type="button"
                          className="secondary-button"
                          onClick={() =>
                            setSelectedRevisionId(revision.revision_id)
                          }
                          aria-pressed={
                            revision.revision_id === selectedRevisionId
                          }
                        >
                          View counts
                        </button>
                        <button
                          type="button"
                          className="secondary-button"
                          onClick={() => supersedeRevision(revision.revision_id)}
                          disabled={isBusy}
                        >
                          Supersede
                        </button>
                        <button
                          type="button"
                          className="danger-button"
                          onClick={() => handleRetire(revision)}
                          disabled={isBusy || revision.status === "RETIRED"}
                        >
                          Retire
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {selectedRevision && (
        <div className="research-card">
          <h3>
            Derived counts for revision #{selectedRevision.revision_number}
          </h3>
          <p className="research-hint">
            Population is the revision; missing data is shown as unavailable,
            never as zero.
          </p>
          {coverageError && (
            <p className="research-empty" role="status">
              Coverage unavailable: {coverageError}
            </p>
          )}
          {enrollmentCoverage && (
            <dl className="research-metrics">
              <div>
                <dt>Enrollments</dt>
                <dd>
                  {enrollmentCoverage.total_enrollments ?? "unavailable"}
                </dd>
              </div>
              <div>
              </div>
              <div>
                <dt>Coverage</dt>
                <dd>{enrollmentCoverage.coverage || "UNKNOWN"}</dd>
              </div>
            </dl>
          )}
          {!coverageError && exposures.length === 0 && (
            <p className="research-empty">No condition exposures recorded yet.</p>
          )}
          {exposures.length > 0 && (
            <div className="research-table-wrap">
              <table className="research-table">
                <caption className="research-visually-hidden">
                  Assignment and exposure counts per condition
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Condition</th>
                    <th scope="col">Assigned</th>
                    <th scope="col">Exposed</th>
                    <th scope="col">Non-exposure</th>
                    <th scope="col">Exposure rate</th>
                    <th scope="col">Coverage</th>
                  </tr>
                </thead>
                <tbody>
                  {exposures.map((exposure) => (
                    <tr key={exposure.condition_id}>
                      <td>{exposure.condition_id}</td>
                      <td>{exposure.assigned_count}</td>
                      <td>{exposure.exposed_count}</td>
                      <td>{exposure.non_exposure_count}</td>
                      <td>
                        {exposure.exposure_rate === null ||
                        exposure.exposure_rate === undefined
                          ? "unavailable"
                          : exposure.exposure_rate.toFixed(3)}
                      </td>
                      <td>{exposure.coverage || "UNKNOWN"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      <div className="research-card">
        <h3>Drafts</h3>
        {isLoading ? (
          <p className="research-empty">Loading drafts…</p>
        ) : drafts.length === 0 ? (
          <p className="research-empty">
            No editable drafts. Use “New draft” to author a protocol.
          </p>
        ) : (
          <ul className="research-draft-list">
            {drafts.map((draft) => (
              <li key={draft.draft_id} className="research-draft-item">
                <div>
                  <strong>{draft.name || "Untitled draft"}</strong>
                  <span className="research-draft-meta" title={draft.draft_id}>
                    {shortId(draft.draft_id)} · schema {draft.schema_version}
                  </span>
                </div>
                <div className="research-actions research-actions-inline">
                  <button
                    type="button"
                    className="secondary-button"
                    onClick={() => openDraft(draft.draft_id)}
                    disabled={isBusy}
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    className="secondary-button"
                    onClick={() => handleValidateDraft(draft.draft_id)}
                    disabled={isBusy}
                  >
                    Validate
                  </button>
                  <button
                    type="button"
                    className="primary-button"
                    onClick={() => handlePublishDraft(draft.draft_id)}
                    disabled={isBusy}
                  >
                    Publish
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
};

export default ResearchStudies;
