import React, { useCallback, useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import {
  getMyResearchEnrollments,
  redeemResearchJoinCode,
  resolveResearchJoinCode,
} from "../../utils/api";
import Icon from "../../components/common/Icon";
import { Badge, Card, CopyButton, KpiTile, PageHeader } from "../../components/common/ui";
import { daysUntil, formatDate, formatDateTime, formatNumber, formatRelative, formatUsd } from "../../utils/format";
import { collectedClasses } from "./studyUtils";
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

const ENROLLMENT_STATUS = {
  ACTIVE: { label: "Active", tone: "success" },
  COMPLETED: { label: "Completed", tone: "info" },
  REVOKED: { label: "Withdrawn by the research team", tone: "danger" },
  STUDY_STOPPED: { label: "Study stopped", tone: "neutral" },
};

// Plain-language names for the telemetry classes a study may collect.
// Keyed by what a study's policy resolves to (see collectedClasses): what is
// actually kept, not the declared names.
const COLLECTION_LABELS = {
  EVENTS:
    "Which events happened and when, in the agent and in your IDE (for example files opened, edited and saved, and runs), with timings, counts, sizes and token usage",
  BEHAVIORAL:
    "Details of what the agent and your IDE did: tools run, approvals, runs and errors. Tool titles and error messages are kept; they can contain full command lines, file paths, search terms and URLs.",
  SYSTEM: "Measurements and system details: timings, token counts, edit sizes, exit codes, software versions and platform details",
  CODE_METADATA: "Which files you work in: file types and languages, and possibly file paths and symbol or repository names (never file contents)",
  CODE_METADATA_HASHED:
    "Which file types and languages you work in, stored as unsalted hashes (easy to reverse for common values; never file contents)",
  CONTENT: "Your prompts, the agent's responses and reasoning, tool arguments and output, and file contents",
};

const RUNTIME_NAMES = {
  "code4me2-agent": "Code4Me agent (built-in)",
  goose: "Goose (install on your machine)",
  codex: "Codex (install on your machine)",
};

const runtimeName = (runtime) => {
  if (!runtime) return null;
  return runtime.display_name || RUNTIME_NAMES[runtime.framework_version] || runtime.framework_version || null;
};

const needsOwnAgent = (runtime) =>
  Boolean(runtime && runtime.framework_version && runtime.framework_version !== "code4me2-agent");

// Who provides the model access. Goose in a study runs on the study's own
// provider key ("shared"); Codex always signs in with the participant's
// ChatGPT account. Without the flag (older servers) the wording stays generic.
const installText = (runtime) => {
  if (runtime.framework_version === "goose" && runtime.credentials === "shared") {
    return "Your study uses Goose, which you install yourself; follow the coordinator's instructions. The study provides the model access: you do not need your own provider account or API key.";
  }
  if (runtime.framework_version === "codex") {
    return "Your study uses Codex, which you install yourself. Follow the coordinator's instructions and sign in with your ChatGPT account.";
  }
  return "Your study uses an agent you install yourself. Follow the coordinator's instructions and sign in to it with your own account.";
};

const scheduleDetail = (study) => {
  if (!study || !study.ends_at) return "No end date set";
  const days = daysUntil(study.ends_at);
  if (days === null) return "";
  if (days < 0) return "Ended";
  if (days === 0) return "Ends today";
  return `${days} day${days === 1 ? "" : "s"} left`;
};

const SetupSteps = ({ runtime }) => {
  const steps = [
    {
      title: "Install the Code4Me plugin",
      text: "In IntelliJ IDEA open Settings › Plugins › Install Plugin from Disk and choose the Code4Me ZIP your study coordinator sent. Also install JetBrains AI Assistant.",
    },
    ...(needsOwnAgent(runtime)
      ? [
          {
            title: `Install ${runtime.framework_version === "goose" ? "Goose" : "Codex"}`,
            text: installText(runtime),
          },
        ]
      : []),
    {
      title: "Sign in inside the IDE",
      text: "Open Settings › Tools › Code4Me V2 and sign in with this account. The plugin finds your enrollment and activates the study for your project.",
    },
    {
      title: "Use the research agent",
      text: "Open AI Chat and pick “Code4Me Research Proxy”. Sessions started through any other agent are not part of the study.",
    },
  ];
  return (
    <ol className="participant-steps">
      {steps.map((step, index) => (
        <li key={step.title}>
          <span className="participant-step-number" aria-hidden="true">
            {index + 1}
          </span>
          <div>
            <strong>{step.title}</strong>
            <p>{step.text}</p>
          </div>
        </li>
      ))}
    </ol>
  );
};

const CurrentStudy = ({ enrollment }) => {
  const study = enrollment.study || {};
  const status = ENROLLMENT_STATUS[enrollment.status] || { label: enrollment.status || "Unknown", tone: "neutral" };
  const contentCaptured = study.collection?.content_capture === true;
  // Content is stored whenever the study captures it (with consent), declared or not.
  const classes = study.collection ? collectedClasses(study.collection) : [];
  const sessions = enrollment.sessions || {};
  const activity = enrollment.activity || {};
  const agent = runtimeName(enrollment.runtime);
  // Numbers only (no model, price or arm): null when the study does not
  // meter this participant's agent.
  const budget = enrollment.budget && typeof enrollment.budget === "object" ? enrollment.budget : null;
  return (
    <Card
      className="participant-current"
      title={
        <span className="ui-row">
          <Icon name="flask" size={17} />
          {study.name || "Your study"}
          <Badge tone={status.tone} dot>
            {status.label}
          </Badge>
        </span>
      }
      subtitle={study.description || null}
    >
      <div className="ui-kpis is-compact">
        <KpiTile label="Joined" value={formatDate(enrollment.enrolled_at)} detail={enrollment.consent_accepted_at ? `Consent given ${formatDateTime(enrollment.consent_accepted_at)}` : null} />
        <KpiTile label="Study ends" value={study.ends_at ? formatDate(study.ends_at) : "Open-ended"} detail={scheduleDetail(study)} />
        <KpiTile
          label="Sessions"
          value={formatNumber(sessions.total ?? null)}
          detail={sessions.last_activity_at ? `Last active ${formatRelative(sessions.last_activity_at)}` : "No IDE session yet"}
        />
        <KpiTile
          label="Your prompts"
          value={formatNumber(activity.prompts ?? null)}
          detail={activity.tool_calls !== undefined && activity.tool_calls !== null ? `${formatNumber(activity.tool_calls)} agent tool calls` : null}
        />
        {budget ? (
          <KpiTile
            label="Budget remaining"
            value={budget.exhausted ? "Exhausted" : formatUsd(budget.remaining)}
            detail={
              budget.exhausted
                ? "The model allowance the study provides for you is used up; ask the research team for a top-up."
                : `of ${formatUsd(budget.limit)} provided by the study${budget.warning ? " · running low" : ""}`
            }
          />
        ) : null}
      </div>

      <div className="ui-grid-2">
        <div className="participant-section">
          <h4 className="ui-section-title">Your study details</h4>
          <dl className="ui-dl participant-dl">
            {enrollment.participant_code ? (
              <div>
                <dt>Participant code</dt>
                <dd className="ui-row">
                  <code>{enrollment.participant_code}</code>
                  <CopyButton value={enrollment.participant_code} label="Copy participant code" />
                </dd>
              </div>
            ) : null}
            <div>
              <dt>Agent to use</dt>
              <dd>{agent || "Assigned when you first start the IDE session"}</dd>
            </div>
            <div>
              <dt>Study started</dt>
              <dd>{study.starts_at ? formatDate(study.starts_at) : "—"}</dd>
            </div>
            <div>
              <dt>Last activity recorded</dt>
              <dd>{activity.last_event_at ? formatDateTime(activity.last_event_at) : "—"}</dd>
            </div>
          </dl>
          <p className="ui-hint">
            Quote your participant code when you contact the research team. Which agent configuration you were
            assigned is kept blind on purpose.
          </p>
        </div>
        <div className="participant-section">
          <h4 className="ui-section-title">What this study collects</h4>
          {classes.length ? (
            <ul className="participant-collect">
              {classes.map((name) => (
                <li key={name}>
                  <Icon name={name === "CONTENT" ? "eye" : "check"} size={14} />
                  <span>
                    {COLLECTION_LABELS[name] || name}
                    {name === "CONTENT" ? <Badge tone="warning">Sensitive</Badge> : null}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="ui-hint">The collection policy is shown in the consent notice you accepted.</p>
          )}
          {classes.length && !contentCaptured ? (
            <p className="ui-hint">
              Your prompts, code and tool output are not collected as text
              {classes.includes("BEHAVIORAL")
                ? "; tool titles and error messages (above) can still quote commands."
                : "."}
            </p>
          ) : null}
        </div>
      </div>

      {enrollment.status === "ACTIVE" ? (
        <div className="participant-section">
          <h4 className="ui-section-title">Get set up</h4>
          <SetupSteps runtime={enrollment.runtime} />
        </div>
      ) : null}
    </Card>
  );
};

const PastStudies = ({ enrollments }) => (
  <Card title="Previous studies">
    <ul className="participant-history">
      {enrollments.map((enrollment) => {
        const status = ENROLLMENT_STATUS[enrollment.status] || { label: enrollment.status, tone: "neutral" };
        return (
          <li key={enrollment.enrollment_id}>
            <div className="ui-cell-stack">
              <span className="ui-cell-primary">{enrollment.study?.name || "Study"}</span>
              <span className="ui-subtle">
                Joined {formatDate(enrollment.enrolled_at)}
                {enrollment.study?.ends_at ? ` · ended ${formatDate(enrollment.study.ends_at)}` : ""}
              </span>
            </div>
            <Badge tone={status.tone}>{status.label}</Badge>
          </li>
        );
      })}
    </ul>
  </Card>
);

/**
 * Participant home ("My studies"): the current enrollment with its study
 * details, earlier studies, and the join-code flow. Assignment stays on the
 * study server and the participant never sees which arm they are in.
 */
const ResearchJoin = ({ user }) => {
  const location = useLocation();
  const joinFocused = location.pathname === SAFE_JOIN_PATH;
  const [joinCode, setJoinCode] = useState("");
  const [study, setStudy] = useState(null);
  const [acceptConsent, setAcceptConsent] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [handoff, setHandoff] = useState(null);
  const [isBusy, setIsBusy] = useState(false);
  const [enrollments, setEnrollments] = useState([]);
  const [joinOpen, setJoinOpen] = useState(false);

  const loadEnrollments = useCallback(async () => {
    if (!user) return;
    try {
      const result = await getMyResearchEnrollments();
      if (result && result.ok) setEnrollments(Array.isArray(result.data) ? result.data : []);
    } catch (_) {
      // The join flow works without the enrollment summary.
    }
  }, [user]);

  useEffect(() => {
    const pending = sessionStorage.getItem(RESEARCH_JOIN_INTENT_KEY);
    if (pending && pending.length <= 128) setJoinCode(pending);
  }, []);

  useEffect(() => {
    loadEnrollments();
  }, [loadEnrollments]);

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
      setAcceptConsent(false);
      setNotice(
        result.data.reused
          ? "You are already enrolled in this study."
          : "Enrollment complete. Assignment is managed by the study server. IntelliJ activation and bootstrap happen after enrollment.",
      );
      loadEnrollments();
    } else {
      if (result.status === 401) return rememberJoinIntent(code);
      if (["STUDY_STOPPED", "ALREADY_ENROLLED", "ACTIVE_ENROLLMENT_EXISTS"].includes(result.code)) clearJoinIntent();
      setError(errorMessage(result));
    }
    setIsBusy(false);
  };

  const cancelReview = () => {
    setStudy(null);
    setAcceptConsent(false);
    setNotice("");
    setError("");
  };

  const active = enrollments.find((enrollment) => enrollment.status === "ACTIVE");
  const past = enrollments.filter((enrollment) => enrollment !== active);
  // With an active enrollment the join form is secondary: one active study at a
  // time. A visit to /research/join always shows it (that is the intent).
  const showJoinForm = !active || joinFocused || joinOpen || Boolean(study) || Boolean(handoff) || Boolean(error);

  const joinCard = (
    <Card
      className="participant-join"
      title={
        <span className="ui-row">
          <Icon name="login" size={16} />
          {study ? "Review and join" : "Join a study"}
        </span>
      }
      subtitle={
        study
          ? "Read the study summary and consent notice, then join."
          : "Enter the join code you received from the research team."
      }
    >
      <form className="ui-form" onSubmit={study ? join : resolve}>
        <div className="ui-field">
          <label className="ui-label" htmlFor="research-join-code">
            Join code
          </label>
          <div className="ui-input-group">
            <input
              id="research-join-code"
              className="ui-input participant-join-code"
              value={joinCode}
              onChange={(event) => setJoinCode(event.target.value)}
              required
              disabled={isBusy || Boolean(study)}
              autoComplete="off"
              spellCheck={false}
              placeholder="e.g. 7F3A92C1"
            />
            {!study ? (
              <button type="submit" className="primary-button" disabled={isBusy || !joinCode.trim()}>
                Review study
              </button>
            ) : null}
          </div>
        </div>

        {study ? (
          <div className="participant-review">
            <div className="participant-review-head">
              <h3>{study.name || "Research study"}</h3>
              {study.description ? <p>{study.description}</p> : null}
            </div>
            {study.consentText ? (
              <div className="participant-consent" aria-label="Consent notice">
                <p>{study.consentText}</p>
              </div>
            ) : null}
            <p className="research-hint">
              Assignment happens on the study server. IntelliJ is only used for post-enrollment activation and
              bootstrap.
            </p>
            <label className="ui-check">
              <input
                type="checkbox"
                checked={acceptConsent}
                onChange={(event) => setAcceptConsent(event.target.checked)}
                disabled={isBusy}
              />
              <span className="ui-check-text">I accept the study consent notice.</span>
            </label>
            <div className="ui-row">
              <button type="submit" className="primary-button" disabled={isBusy || !joinCode.trim()}>
                Accept and join
              </button>
              <button type="button" className="ghost-button" onClick={cancelReview} disabled={isBusy}>
                Use a different code
              </button>
            </div>
          </div>
        ) : null}
      </form>
    </Card>
  );

  return (
    <section className="ui-page research-page participant-page" aria-labelledby="research-join-title">
      <PageHeader
        titleId="research-join-title"
        title={user ? "My studies" : "Join a research study"}
        description={
          user
            ? "The studies you take part in, what they collect, and how to get set up. Join a new study with the code from your research team."
            : "Enter your join code. You will be asked to sign in before you can review the consent notice."
        }
      />

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

      {handoff ? (
        <div
          className="research-handoff"
          aria-label="Enrollment handoff"
          data-enrollment-id={handoff.enrollment_id}
          data-assignment-id={handoff.assignment_id}
          data-agent-profile-id={handoff.agent_profile_id}
        >
          <Icon name="checkCircle" size={18} />
          <div>
            <p>Enrollment is ready for post-enrollment activation.</p>
            <span>{handoff.reused ? "Existing enrollment reused." : "New enrollment created."} Next, sign in to the Code4Me plugin in IntelliJ IDEA.</span>
          </div>
        </div>
      ) : null}

      {active ? <CurrentStudy enrollment={active} /> : null}

      {showJoinForm ? (
        joinCard
      ) : (
        <Card>
          <div className="ui-row-between">
            <p className="ui-muted" style={{ margin: 0 }}>
              You can take part in one active study at a time. Have a code for another study?
            </p>
            <button type="button" className="secondary-button" onClick={() => setJoinOpen(true)}>
              <Icon name="login" size={15} />
              Enter a join code
            </button>
          </div>
        </Card>
      )}

      {user && !active && !study && !handoff && past.length === 0 ? (
        <Card title="How taking part works">
          <SetupSteps runtime={null} />
        </Card>
      ) : null}

      {past.length > 0 ? <PastStudies enrollments={past} /> : null}
    </section>
  );
};

export default ResearchJoin;
