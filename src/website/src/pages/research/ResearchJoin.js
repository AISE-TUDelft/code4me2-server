import React, { useState } from "react";
import { redeemResearchJoinCode, resolveResearchJoinCode } from "../../utils/api";
import "./research.css";

const ResearchJoin = () => {
  const [joinCode, setJoinCode] = useState("");
  const [study, setStudy] = useState(null);
  const [acceptConsent, setAcceptConsent] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [isBusy, setIsBusy] = useState(false);

  const resolve = async (event) => {
    event.preventDefault();
    setIsBusy(true);
    setError("");
    setNotice("");
    const result = await resolveResearchJoinCode(joinCode.trim());
    if (result.ok) {
      setStudy(result.data.study);
      setNotice("Review the study details and accept consent to join.");
    } else {
      setStudy(null);
      setError(result.error || "Join code could not be resolved.");
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
    const result = await redeemResearchJoinCode(joinCode.trim(), true);
    if (result.ok) {
      setStudy(null);
      setNotice("You joined the study. Continue in IntelliJ to activate collection.");
    } else {
      setError(result.error || "The study could not be joined.");
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
            <p className="research-hint">Consent is accepted here in the browser. Agent profile assignment happens on the server.</p>
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
    </section>
  );
};

export default ResearchJoin;
