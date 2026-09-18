import React, { useEffect, useState } from "react";
import { redeemResearchJoinCode, resolveResearchJoinCode } from "../../utils/api";
import "./research.css";

export const RESEARCH_JOIN_INTENT_KEY = "code4me.research.join.intent";
const SAFE_JOIN_PATH = "/research/join";

const errorMessage = (result) => {
  if (result.code === "CONSENT_REQUIRED") return "Consent is required before joining this study.";
  if (result.code === "STUDY_STOPPED") return "This study has stopped and cannot accept new participants.";
  if (result.code === "ALREADY_ENROLLED") return "You are already enrolled in this study.";
  if (result.code === "ACTIVE_ENROLLMENT_EXISTS") return "You already have an active research enrollment.";
  if (result.status === 401) return "Please sign in to review and join this study.";
  if (result.status === 403) return "Your account is not permitted to join this study.";
  if (result.status === 422) return result.error || "The join request needs correction.";
  if (result.status === 404 || result.missing) return "That join code is invalid or no longer available.";
  return result.error || "The research service is temporarily unavailable. Please try again.";
};

const ResearchJoin = () => {
  const [joinCode, setJoinCode] = useState("");
  const [study, setStudy] = useState(null);
  const [acceptConsent, setAcceptConsent] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [handoff, setHandoff] = useState(null);
  const [isBusy, setIsBusy] = useState(false);

  useEffect(() => {
    const pending = sessionStorage.getItem(RESEARCH_JOIN_INTENT_KEY);
    if (pending && pending.length <= 128) setJoinCode(pending);
  }, []);

  const clearJoinIntent = () => sessionStorage.removeItem(RESEARCH_JOIN_INTENT_KEY);
  const rememberJoinIntent = (code) => {
    sessionStorage.setItem(RESEARCH_JOIN_INTENT_KEY, code.slice(0, 128));
    window.history.replaceState({}, "", SAFE_JOIN_PATH);
    window.location.assign("/login");
  };

  const resolve = async (event) => {
    event.preventDefault();
    setIsBusy(true);
    setError("");
    setNotice("");
    const code = joinCode.trim();
    const result = await resolveResearchJoinCode(code);
    if (result.ok) {
      clearJoinIntent();
      setStudy({ ...result.data.study, consentText: result.data.consentText || result.data.policyText || "" });
      setNotice("Review the study details and accept consent to join.");
    } else {
      setStudy(null);
      if (result.status === 401) return rememberJoinIntent(code);
      if (result.code === "STUDY_STOPPED" || result.status === 404 || result.missing) clearJoinIntent();
      setError(errorMessage(result));
    }
    setIsBusy(false);
  };

  const join = async (event) => {
    event.preventDefault();
    if (!acceptConsent) {
      setError("You must accept the consent notice before joining.");
      return;
    }
    setIsBusy(true);
    setError("");
    const code = joinCode.trim();
    const result = await redeemResearchJoinCode(code, true);
    if (result.ok) {
      setStudy(null);
      setHandoff(result.data);
      clearJoinIntent();
      setNotice(result.data.reused ? "You are already enrolled in this study." : "Enrollment complete. Assignment is managed by the study server. IntelliJ activation and bootstrap happen after enrollment.");
    } else {
      if (result.status === 401) return rememberJoinIntent(code);
      if (["STUDY_STOPPED", "ALREADY_ENROLLED", "ACTIVE_ENROLLMENT_EXISTS"].includes(result.code)) clearJoinIntent();
      setError(errorMessage(result));
    }
    setIsBusy(false);
  };

  return (
    <section className="research-page" aria-labelledby="research-join-title">
      <h2 id="research-join-title">Join a research study</h2>
      <form className="research-card" onSubmit={study ? join : resolve}>
        <label>
          Join code
          <input value={joinCode} onChange={(event) => setJoinCode(event.target.value)} required disabled={isBusy} />
        </label>
        {study && (
          <>
            <h3>{study.name || "Research study"}</h3>
            {study.description && <p>{study.description}</p>}
            <p className="research-hint">Assignment happens on the study server. IntelliJ is only used for post-enrollment activation and bootstrap.</p>
            {study.consentText && <p>{study.consentText}</p>}
            <label>
              <input type="checkbox" checked={acceptConsent} onChange={(event) => setAcceptConsent(event.target.checked)} disabled={isBusy} />
              I accept the study consent notice.
            </label>
          </>
        )}
        <button type="submit" className="primary-button" disabled={isBusy || !joinCode.trim()}>
          {study ? "Accept and join" : "Review study"}
        </button>
      </form>
      {error && <p className="research-error" role="alert">{error}</p>}
      {notice && <p className="research-notice" role="status">{notice}</p>}
      {handoff && (
        <div className="research-handoff" aria-label="Enrollment handoff" data-enrollment-id={handoff.enrollment_id} data-assignment-id={handoff.assignment_id} data-agent-profile-id={handoff.agent_profile_id}>
          <p>Enrollment is ready for post-enrollment activation.</p>
          <span>{handoff.reused ? "Existing enrollment reused." : "New enrollment created."}</span>
        </div>
      )}
    </section>
  );
};

export default ResearchJoin;
