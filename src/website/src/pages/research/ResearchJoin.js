import React, { useState } from "react";
import { Link } from "react-router-dom";
import {
  getMyResearchEnrollments,
  redeemResearchJoinCode,
  resolveResearchJoinCode,
} from "../../utils/api";
import { shortId } from "./protocol";
import "./research.css";
import "./ResearchEnrollment.css";

const ResearchJoin = () => {
  const [code, setCode] = useState("");
  const [study, setStudy] = useState(null);
  const [acceptConsent, setAcceptConsent] = useState(false);
  const [enrollment, setEnrollment] = useState(null);
  const [otherStudy, setOtherStudy] = useState(null);
  const [isBusy, setIsBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const trimmedCode = () => code.trim();

  const resetResults = () => {
    setStudy(null);
    setEnrollment(null);
    setOtherStudy(null);
    setAcceptConsent(false);
    setError("");
    setNotice("");
  };

  // The `/participants/me` projection names the revision `study_revision_id`;
  // normalize it so the same entry can be rendered by the enrollment card.
  const toEnrollmentView = (record) => ({
    enrollment_id: record.enrollment_id || "",
    study_id: record.study_id || "",
    revision_id: record.study_revision_id || record.revision_id || "",
    status: record.status || "",
  });

  const handleResolve = async (event) => {
    event.preventDefault();
    const value = trimmedCode();
    if (!value) {
      setError("Enter the join code from your study invitation.");
      return;
    }
    setIsBusy(true);
    resetResults();
    const response = await resolveResearchJoinCode(value);
    if (!(response && response.ok)) {
      setError(
        (response && response.error) || "That join code could not be resolved.",
      );
      setIsBusy(false);
      return;
    }
    const resolved = response.data || {};

    // Resolve the code first, then branch on the caller's own enrollments so an
    // already-active participant is never asked to consent a second time. The
    // server only returns the caller's own study-local projections.
    const enrollmentsResponse = await getMyResearchEnrollments();
    const enrollments =
      enrollmentsResponse &&
      enrollmentsResponse.ok &&
      Array.isArray(enrollmentsResponse.data)
        ? enrollmentsResponse.data
        : [];

    const matchesCode = (record) => {
      const revisionId = resolved.revision && resolved.revision.revisionId;
      const studyId = resolved.study && resolved.study.studyId;
      if (revisionId) {
        return record.study_revision_id === revisionId;
      }
      if (studyId) {
        return record.study_id === studyId;
      }
      return false;
    };

    const activeForCode = enrollments.find(
      (record) => record.status === "ACTIVE" && matchesCode(record),
    );
    if (activeForCode) {
      setEnrollment(toEnrollmentView(activeForCode));
      setNotice(
        "You are already enrolled in this study. Your enrollment is active — no further action is needed.",
      );
      setIsBusy(false);
      return;
    }

    const otherActive = enrollments.find(
      (record) =>
        record.status === "ACTIVE" &&
        (!(resolved.study && resolved.study.studyId) ||
          record.study_id !== resolved.study.studyId),
    );
    if (otherActive) {
      setOtherStudy(toEnrollmentView(otherActive));
      setError(
        "You are already enrolled in another research study. Leave that study first, or view your current enrollment below.",
      );
      setIsBusy(false);
      return;
    }

    setStudy(resolved);
    setNotice("Code found. Review the study policy and accept it to join.");
    setIsBusy(false);
  };

  const handleRedeem = async () => {
    const value = trimmedCode();
    if (!value) {
      setError("Enter the join code from your study invitation.");
      return;
    }
    if (!acceptConsent) {
      setError("Accept the study policy to join.");
      return;
    }
    setIsBusy(true);
    setError("");
    setNotice("");
    const response = await redeemResearchJoinCode(value, true);
    if (response && response.ok) {
      setEnrollment(response.data);
      setOtherStudy(null);
      setStudy(null);
      setNotice("Enrollment complete.");
    } else {
      setEnrollment(null);
      setError(
        (response && response.error) || "That join code could not be redeemed.",
      );
    }
    setIsBusy(false);
  };

  return (
    <section className="research-page" aria-labelledby="research-join-title">
      <div className="research-header">
        <div>
          <h2 id="research-join-title">Join a study</h2>
          <p>
            Enter the code from your study invitation. You can review the study
            and its policy before redeeming the code.
          </p>
        </div>
        <Link to="/dashboard" className="secondary-button">
          Back to dashboard
        </Link>
      </div>

      {(error || notice) && (
        <div
          className={`research-message ${error ? "error" : "success"}`}
          role={error ? "alert" : "status"}
        >
          {error || notice}
        </div>
      )}

      <form className="research-card research-join-form" onSubmit={handleResolve}>
        <h3>Enter join code</h3>
        <label>
          Join code
          <input
            value={code}
            onChange={(event) => setCode(event.target.value)}
            disabled={isBusy}
            placeholder="e.g. AB12-CD34"
            aria-describedby="research-join-hint"
          />
        </label>
        <p className="research-hint" id="research-join-hint">
          The join code is case-insensitive and is provided by the researcher.
        </p>
        <div className="research-actions">
          <button type="submit" className="primary-button" disabled={isBusy}>
            Check code
          </button>
        </div>
      </form>

      {study && (
        <div className="research-card">
          <h3>{study.name || "Research study"}</h3>
          <p className="research-hint">
            Confirm this is the study you were invited to before enrolling.
          </p>
          <dl className="research-metrics">
            <div>
              <dt>Study</dt>
              <dd title={study.study && study.study.studyId}>{shortId(study.study && study.study.studyId)}</dd>
            </div>
            <div>
              <dt>Revision</dt>
              <dd title={study.revision && study.revision.revisionId}>{shortId(study.revision && study.revision.revisionId)}</dd>
            </div>
            <div>
              <dt>Status</dt>
              <dd>{(study.revision && study.revision.status) || "—"}</dd>
            </div>
          </dl>
          {study.policyText ? (
            <div className="research-policy" data-testid="research-policy-text">
              <h3>Study policy</h3>
              <p>{study.policyText}</p>
            </div>
          ) : null}
          <label className="research-checkbox">
            <input
              type="checkbox"
              checked={acceptConsent}
              onChange={(event) => setAcceptConsent(event.target.checked)}
              disabled={isBusy}
            />
            I have read the study policy and agree to participate.
          </label>
          <div className="research-actions">
            <button
              type="button"
              className="primary-button"
              onClick={handleRedeem}
              disabled={isBusy}
            >
              Accept &amp; join
            </button>
          </div>
        </div>
      )}

      {enrollment && (
        <div className="research-card">
          <h3>Enrollment status</h3>
          <dl className="research-metrics">
            <div>
              <dt>Status</dt>
              <dd>{enrollment.status || "—"}</dd>
            </div>
            <div>
              <dt>Study</dt>
              <dd title={enrollment.study_id}>{shortId(enrollment.study_id)}</dd>
            </div>
            <div>
              <dt>Revision</dt>
              <dd title={enrollment.revision_id}>
                {shortId(enrollment.revision_id)}
              </dd>
            </div>
            <div>
              <dt>Enrollment</dt>
              <dd title={enrollment.enrollment_id}>
                {shortId(enrollment.enrollment_id)}
              </dd>
            </div>
          </dl>
        </div>
      )}

      {otherStudy && (
        <div className="research-card">
          <h3>Your current enrollment</h3>
          <p className="research-hint">
            You are already participating in another study. Leave that study
            before joining a new one.
          </p>
          <dl className="research-metrics">
            <div>
              <dt>Status</dt>
              <dd>{otherStudy.status || "—"}</dd>
            </div>
            <div>
              <dt>Study</dt>
              <dd title={otherStudy.study_id}>{shortId(otherStudy.study_id)}</dd>
            </div>
            <div>
              <dt>Revision</dt>
              <dd title={otherStudy.revision_id}>
                {shortId(otherStudy.revision_id)}
              </dd>
            </div>
            <div>
              <dt>Enrollment</dt>
              <dd title={otherStudy.enrollment_id}>
                {shortId(otherStudy.enrollment_id)}
              </dd>
            </div>
          </dl>
        </div>
      )}
    </section>
  );
};

export default ResearchJoin;
