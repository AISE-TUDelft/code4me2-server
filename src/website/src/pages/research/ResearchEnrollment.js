import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  getAgentDistributions,
  getResearchEnrollment,
  getResearchPackages,
  getResearchRevision,
  getResearchStudyJoinCode,
  requestResearchEnrollment,
} from "../../utils/api";
import { formatDateTime, shortId } from "./protocol";
import "./research.css";
import "./ResearchEnrollment.css";

// A condition's single pick is an opaque `distribution_id`. The server resolves
// it into a frozen `resolved_distribution` at publication (mode, release pin and
// agent identity), which is authoritative for a published revision. A draft that
// has not been resolved yet only names the id, so fall back to the read-only
// distribution registry keyed by that id.
export const isPackagedCondition = (condition, distributionById = {}) => {
  const resolved = condition.resolved_distribution || {};
  if (resolved.distribution_mode) {
    return resolved.distribution_mode === "PACKAGED";
  }
  const distribution = distributionById[condition.distribution_id] || {};
  if (distribution.distribution_mode) {
    return distribution.distribution_mode === "PACKAGED";
  }
  // Last resort for an unregistered distribution: a frozen release/digest pin
  // implies a packaged artifact.
  return Boolean(resolved.artifact_digest || resolved.release_id);
};

const ResearchEnrollment = () => {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  const [studyId, setStudyId] = useState(searchParams.get("study_id") || "");
  const [revisionId, setRevisionId] = useState(
    searchParams.get("revision_id") || "",
  );
  const [revision, setRevision] = useState(null);
  const [protocol, setProtocol] = useState(null);
  const [distributions, setDistributions] = useState([]);
  const [packages, setPackages] = useState([]);
  const [packagesUnavailable, setPackagesUnavailable] = useState(false);
  const [enrollment, setEnrollment] = useState(null);
  // Study-scoped join code (from the study index / join-code endpoint). This is
  // the primary share value; the per-account enrollment id is only a fallback
  // when the endpoint is not available on the server yet.
  const [studyJoinCode, setStudyJoinCode] = useState("");
  const [studyJoinCodeRevisionId, setStudyJoinCodeRevisionId] = useState("");
  const [studyJoinCodeStatus, setStudyJoinCodeStatus] = useState("");
  const [studyJoinCodeMissing, setStudyJoinCodeMissing] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [isBusy, setIsBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [copied, setCopied] = useState(false);

  const distributionById = useMemo(() => {
    const map = {};
    distributions.forEach((distribution) => {
      if (distribution && distribution.distribution_id) {
        map[distribution.distribution_id] = distribution;
      }
    });
    return map;
  }, [distributions]);

  useEffect(() => {
    let cancelled = false;
    getAgentDistributions().then((response) => {
      if (!cancelled && response && response.ok) {
        setDistributions(Array.isArray(response.data) ? response.data : []);
      }
    });
    getResearchPackages().then((response) => {
      if (cancelled) return;
      if (response.ok) {
        setPackages(Array.isArray(response.data) ? response.data : []);
        setPackagesUnavailable(false);
      } else {
        setPackages([]);
        setPackagesUnavailable(true);
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const loadRevision = useCallback(async (id) => {
    const trimmed = (id || "").trim();
    if (!trimmed) {
      setError("A revision ID is required to show the enrollment details.");
      return;
    }
    setIsLoading(true);
    setError("");
    setNotice("");
    setEnrollment(null);
    const response = await getResearchRevision(trimmed);
    if (response.ok) {
      setRevision(response.revision);
      setProtocol(response.protocol);
    } else {
      setRevision(null);
      setProtocol(null);
      setError(response.error);
    }
    setIsLoading(false);
  }, []);

  const loadJoinCode = useCallback(async (id) => {
    const trimmed = (id || "").trim();
    if (!trimmed) {
      setError("A study ID is required to load the join code.");
      return;
    }
    setIsBusy(true);
    setError("");
    setNotice("");
    const response = await getResearchStudyJoinCode(trimmed);
    if (response && response.ok) {
      setStudyJoinCode(response.join_code || "");
      setStudyJoinCodeRevisionId(response.revision_id || "");
      setStudyJoinCodeStatus(response.status || "");
      setStudyJoinCodeMissing(false);
      setNotice(
        response.join_code
          ? "Study join code loaded. Share it with participants."
          : "This study has no join code yet.",
      );
    } else if (response && response.missing) {
      // The server does not expose the study join-code endpoint yet; fall back
      // to the per-account enrollment id instead of failing the page.
      setStudyJoinCode("");
      setStudyJoinCodeRevisionId("");
      setStudyJoinCodeStatus("");
      setStudyJoinCodeMissing(true);
    } else {
      setStudyJoinCode("");
      setStudyJoinCodeRevisionId("");
      setStudyJoinCodeStatus("");
      setStudyJoinCodeMissing(false);
      setError(
        (response && response.error) || "The study join code could not be loaded.",
      );
    }
    setIsBusy(false);
  }, []);

  useEffect(() => {
    const urlRevisionId = searchParams.get("revision_id");
    if (urlRevisionId) loadRevision(urlRevisionId);
    const urlStudyId = searchParams.get("study_id");
    if (urlStudyId) loadJoinCode(urlStudyId);
    // Initial URL only; the form drives subsequent loads.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Primary submit: load the study-scoped join code (from the study index /
  // join-code endpoint) rather than minting a per-account enrollment.
  const handleGetJoinCode = async (event) => {
    event.preventDefault();
    await loadJoinCode(studyId);
  };

  // Secondary: mint (or reuse) the signed-in account's own enrollment. Kept so
  // researchers can inspect the account-scoped enrollment status.
  const handleEnrollAccount = async () => {
    if (!studyId.trim() || !revisionId.trim()) {
      setError("Both a study ID and a revision ID are required to enroll.");
      return;
    }
    setIsBusy(true);
    setError("");
    setNotice("");
    const response = await requestResearchEnrollment(
      studyId.trim(),
      revisionId.trim(),
    );
    if (response && response.ok) {
      setEnrollment(response.data.enrollment);
      setNotice(
        response.data.created
          ? "Enrollment created for this account."
          : "Existing enrollment reused for this account.",
      );
    } else {
      setEnrollment(null);
      setError((response && response.error) || "Enrollment failed.");
    }
    setIsBusy(false);
  };

  const handleInspect = async () => {
    if (!enrollment?.enrollment_id) return;
    setIsBusy(true);
    setError("");
    const response = await getResearchEnrollment(enrollment.enrollment_id);
    if (response && response.ok) {
      setEnrollment(response.data);
      setNotice("Enrollment refreshed.");
    } else {
      setError((response && response.error) || "Enrollment could not be loaded.");
    }
    setIsBusy(false);
  };

  const displayJoinCode = studyJoinCode || enrollment?.enrollment_id || "";

  const handleCopy = async () => {
    const code = displayJoinCode;
    if (!code) return;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(code);
        setCopied(true);
        window.setTimeout(() => setCopied(false), 2000);
        return;
      }
    } catch (_) {
      // Clipboard can be unavailable (permissions/insecure context).
    }
    setNotice("Copy the join code manually:");
  };

  const conditions = protocol?.conditions || [];
  const packagedCount = conditions.filter((condition) =>
    isPackagedCondition(condition, distributionById),
  ).length;
  const byoaCount = conditions.length - packagedCount;

  return (
    <section className="research-page" aria-labelledby="research-enrollment-title">
      <div className="research-header">
        <div>
          <h2 id="research-enrollment-title">Research Enrollment</h2>
          <p>
            After publishing, share the join code with participants. They install
            the plugin, sign in, and enter the code to enroll.
          </p>
        </div>
        <button
          type="button"
          className="secondary-button"
          onClick={() =>
            navigate(
              `/research/studies?study_id=${encodeURIComponent(studyId)}`,
            )
          }
        >
          Back to studies
        </button>
      </div>

      {(error || notice) && (
        <div
          className={`research-message ${error ? "error" : "success"}`}
          role={error ? "alert" : "status"}
        >
          {error || notice}
        </div>
      )}

      <form className="research-card" onSubmit={handleGetJoinCode}>
        <h3>Study revision</h3>
        <label>
          Study ID (UUID)
          <input
            value={studyId}
            onChange={(event) => setStudyId(event.target.value)}
            disabled={isBusy}
            placeholder="00000000-0000-0000-0000-000000000000"
          />
        </label>
        <label>
          Revision ID (UUID)
          <input
            value={revisionId}
            onChange={(event) => setRevisionId(event.target.value)}
            disabled={isBusy}
            placeholder="00000000-0000-0000-0000-000000000000"
          />
        </label>
        <div className="research-actions">
          <button
            type="button"
            className="secondary-button"
            onClick={() => loadRevision(revisionId)}
            disabled={isBusy || isLoading}
          >
            Load revision details
          </button>
          <button
            type="submit"
            className="primary-button"
            disabled={isBusy}
            aria-describedby="research-join-code-note"
          >
            Get join code
          </button>
          <button
            type="button"
            className="secondary-button"
            onClick={handleEnrollAccount}
            disabled={isBusy}
          >
            Enroll this account
          </button>
        </div>
        <p className="research-hint" id="research-join-code-note">
          The join code is study-scoped and shared by every participant. Loading
          it does not enroll your account; use “Enroll this account” to mint
          (or reuse) your own enrollment for inspection.
        </p>
      </form>

      {isLoading ? (
        <p className="research-empty">Loading revision…</p>
      ) : (
        revision && (
          <div className="research-card">
            <h3>Revision #{revision.revision_number}</h3>
            <dl className="research-metrics">
              <div>
                <dt>Status</dt>
                <dd>{revision.status}</dd>
              </div>
              <div>
                <dt>Digest</dt>
                <dd title={revision.protocol_digest}>
                  <code>{shortId(revision.protocol_digest)}</code>
                </dd>
              </div>
              <div>
                <dt>Published</dt>
                <dd>{formatDateTime(revision.published_at)}</dd>
              </div>
            </dl>
          </div>
        )
      )}

      <div className="research-card research-join-code">
        <h3>Join code</h3>
        <p className="research-hint">
          {studyJoinCode
            ? "Study join code. Share it with participants after they install the plugin and sign in."
            : studyJoinCodeMissing
              ? "This server does not expose the study join-code endpoint yet, so the per-account enrollment id is shown as a fallback."
              : "Load the study join code to get the value participants enter after signing up and signing in."}
        </p>
        {displayJoinCode ? (
          <div className="research-code-row">
            <output className="research-code" aria-label="Study join code">
              {displayJoinCode}
            </output>
            <button
              type="button"
              className="secondary-button"
              onClick={handleCopy}
            >
              {copied ? "Copied" : "Copy code"}
            </button>
          </div>
        ) : (
          <p className="research-empty">No join code loaded yet.</p>
        )}
        {(studyJoinCode || studyJoinCodeStatus || studyJoinCodeRevisionId) && (
          <dl className="research-metrics">
            <div>
              <dt>Scope</dt>
              <dd>{studyJoinCode ? "Study" : "Account fallback"}</dd>
            </div>
            <div>
              <dt>Status</dt>
              <dd>{studyJoinCodeStatus || "—"}</dd>
            </div>
            <div>
              <dt>Revision</dt>
              <dd title={studyJoinCodeRevisionId}>
                {studyJoinCodeRevisionId
                  ? shortId(studyJoinCodeRevisionId)
                  : "—"}
              </dd>
            </div>
          </dl>
        )}
      </div>

      {enrollment && (
        <div className="research-card">
          <h3>Account enrollment</h3>
          <p className="research-hint">
            Your signed-in account’s enrollment in this revision. This is not the
            shareable study code.
          </p>
          <dl className="research-metrics">
            <div>
              <dt>Status</dt>
              <dd>{enrollment.status}</dd>
            </div>
            <div>
              <dt>Participant code</dt>
              <dd>{enrollment.participant_code || "—"}</dd>
            </div>
            <div>
              <dt>Eligible</dt>
              <dd>
                {enrollment.eligible === null || enrollment.eligible === undefined
                  ? "unknown"
                  : String(enrollment.eligible)}
              </dd>
            </div>
          </dl>
          <div className="research-actions">
            <button
              type="button"
              className="secondary-button"
              onClick={handleInspect}
              disabled={isBusy}
            >
              Inspect
            </button>
          </div>
        </div>
      )}

      <div className="research-card">
        <h3>Participant instructions</h3>
        <ol className="research-instructions">
          <li>
            Install the Code4me2 plugin from the{" "}
            <a
              href="https://plugins.jetbrains.com/vendor/code4me-team"
              target="_blank"
              rel="noreferrer"
            >
              JetBrains Marketplace
            </a>{" "}
            (or from the study ZIP when a packaged build is provided).
          </li>
          <li>Sign up or sign in with your Code4me2 account.</li>
          <li>Enter the join code in the research/enrollment panel.</li>
          <li>Accept the study policy to activate enrollment.</li>
        </ol>
      </div>

      <div className="research-card">
        <h3>Agent packaging</h3>
        {conditions.length === 0 ? (
          <p className="research-empty">
            Load a revision to see whether its conditions use packaged agents or
            bring-your-own-agent (BYOA) runtimes.
          </p>
        ) : (
          <>
            <dl className="research-metrics">
              <div>
                <dt>Packaged conditions</dt>
                <dd>{packagedCount}</dd>
              </div>
              <div>
                <dt>BYOA conditions</dt>
                <dd>{byoaCount}</dd>
              </div>
            </dl>
            <div className="research-table-wrap">
              <table className="research-table">
                <caption className="research-visually-hidden">
                  Agent distribution per condition
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Condition</th>
                    <th scope="col">Agent</th>
                    <th scope="col">Distribution</th>
                    <th scope="col">Release</th>
                  </tr>
                </thead>
                <tbody>
                  {conditions.map((condition) => {
                    const resolved = condition.resolved_distribution || {};
                    const distribution =
                      distributionById[condition.distribution_id] || {};
                    const overrides = condition.declared_overrides || {};
                    const packaged = isPackagedCondition(
                      condition,
                      distributionById,
                    );
                    const agentLabel =
                      overrides.agent_profile ||
                      resolved.agent_id ||
                      distribution.name ||
                      "—";
                    const releaseLabel = packaged
                      ? resolved.release_id ||
                        distribution.release_id ||
                        resolved.version ||
                        distribution.release_version ||
                        "unpinned"
                      : resolved.agent_command ||
                        resolved.agent_package ||
                        distribution.agent_command ||
                        distribution.agent_package ||
                        "participant-installed";
                    return (
                      <tr key={condition.condition_id}>
                        <td>{condition.name || condition.condition_id}</td>
                        <td>{agentLabel}</td>
                        <td>
                          <span
                            className={`research-distribution ${
                              packaged ? "packaged" : "byoa"
                            }`}
                          >
                            {packaged ? "Packaged" : "BYOA"}
                          </span>
                        </td>
                        <td>{releaseLabel}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </>
        )}
        {packagesUnavailable ? (
          <p className="research-hint" role="status">
            Runtime package registry unavailable; distribution is inferred from
            the protocol and the distribution registry.
          </p>
        ) : (
          <p className="research-hint">
            {packages.length} runtime package
            {packages.length === 1 ? "" : "s"} registered on the server.
          </p>
        )}
      </div>
    </section>
  );
};

export default ResearchEnrollment;
