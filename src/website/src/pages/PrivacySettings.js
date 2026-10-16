import React, { useCallback, useEffect, useRef, useState } from "react";
import { deleteMyAccount, eraseMyData, getPrivacyStatus, setDataCollection } from "../utils/api";
import Icon from "../components/common/Icon";
import { Alert, Badge, Card, Loading, PageHeader } from "../components/common/ui";
import { formatDate, formatNumber } from "../utils/format";
import "./PrivacySettings.css";

const COLLECTING_TEXT =
  "Code4Me stores code context, completion and chat requests, usage telemetry and — if you take part in a study — study telemetry, as allowed by your plugin settings and the study's consent terms.";

const OPTED_OUT_TEXT =
  "Code4Me no longer stores your code, prompts, completion and chat requests, telemetry or study data. It keeps your account and sign-in sessions and, while you use the agent outside a study, content-free run records such as token counts.";

const ERASE_TEXT =
  "Permanently delete everything Code4Me has collected about you: completion and chat requests with their code context and telemetry, chat conversations, agent runs, and your study participation and study telemetry. Data collection is turned off. Your account stays, so you can keep using Code4Me.";

// The `stored_data` counts (and the `erased` summary), in display order.
const STORED_DATA = [
  { key: "queries", label: "Completion and chat requests" },
  { key: "chats", label: "Chat conversations" },
  { key: "agent_runs", label: "Agent runs" },
  { key: "study_enrollments", label: "Study enrollments" },
  { key: "study_events", label: "Study events" },
];

// The button each confirmation replaces while it is open.
const TRIGGER_IDS = {
  "opt-out": "privacy-collection-toggle",
  erase: "privacy-erase-start",
  delete: "privacy-delete-start",
};

const studyName = (study) => study.name || "Unnamed study";

const countSummary = (counts) =>
  STORED_DATA.map((item) => `${item.label}: ${formatNumber(counts[item.key])}`).join(" · ");

/**
 * Self-service privacy controls for the signed-in account: whether Code4Me
 * collects data, what it stores, erasing that data and deleting the account.
 * Every change is confirmed inline where it withdraws or deletes something,
 * and the page always shows the status the server returns (nothing is
 * optimistic).
 */
const PrivacySettings = ({ onAccountDeleted }) => {
  const [privacy, setPrivacy] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  // One request at a time: "collection", "erase" or "delete".
  const [pending, setPending] = useState("");
  // The open inline confirmation ("opt-out", "erase" or "delete"). Only one is
  // open at a time, so one acknowledgement checkbox state is enough.
  const [confirming, setConfirming] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  // Outcomes are shown in the card whose action produced them.
  const [actionError, setActionError] = useState(null);
  const [collectionNotice, setCollectionNotice] = useState("");
  const [erased, setErased] = useState(null);
  const focusTarget = useRef(null);
  const lastConfirming = useRef("");

  const load = useCallback(async () => {
    setIsLoading(true);
    setLoadError("");
    const result = await getPrivacyStatus();
    if (result.ok) {
      setPrivacy(result.data);
    } else {
      setLoadError(result.error);
    }
    setIsLoading(false);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // A confirmation replaces its trigger button, so focus moves into it when it
  // opens and back to the trigger when it closes.
  useEffect(() => {
    const previous = lastConfirming.current;
    lastConfirming.current = confirming;
    if (confirming) {
      if (focusTarget.current) focusTarget.current.focus();
    } else if (previous) {
      const trigger = document.getElementById(TRIGGER_IDS[previous]);
      if (trigger) trigger.focus();
    }
  }, [confirming]);

  const openConfirmation = (kind) => {
    setConfirming(kind);
    setAcknowledged(false);
    setActionError(null);
  };

  const closeConfirmation = () => {
    setConfirming("");
    setAcknowledged(false);
  };

  const begin = (action) => {
    setPending(action);
    setActionError(null);
    setCollectionNotice("");
    setErased(null);
  };

  const changeCollection = async (enabled) => {
    const study = privacy.active_study;
    begin("collection");
    const result = await setDataCollection(enabled);
    if (result.ok) {
      setPrivacy(result.data);
      closeConfirmation();
      setCollectionNotice(
        enabled
          ? "Data collection is turned back on."
          : study
            ? `Data collection is turned off, and you have been withdrawn from the study “${studyName(study)}”.`
            : "Data collection is turned off.",
      );
    } else {
      setActionError({ action: "collection", message: result.error });
    }
    setPending("");
  };

  const erase = async () => {
    begin("erase");
    const result = await eraseMyData();
    if (result.ok) {
      setErased(result.data.erased || {});
      closeConfirmation();
      if (result.data.status) setPrivacy(result.data.status);
      else load();
    } else {
      setActionError({ action: "erase", message: result.error });
    }
    setPending("");
  };

  const deleteAccount = async () => {
    begin("delete");
    const result = await deleteMyAccount();
    if (result.ok) {
      // The account is gone and the server cleared the session cookies; the
      // controls stay disabled while the app signs out.
      if (onAccountDeleted) onAccountDeleted();
      return;
    }
    setActionError({ action: "delete", message: result.error });
    setPending("");
  };

  const busy = Boolean(pending);
  const collection = (privacy && privacy.data_collection) || {};
  const collecting = collection.enabled !== false;
  const activeStudy = (privacy && privacy.active_study) || null;
  const stored = (privacy && privacy.stored_data) || {};
  const deletion = (privacy && privacy.account_deletion) || {};
  const deletionAllowed = deletion.allowed !== false;

  const toggleCollection = () => {
    if (collecting && activeStudy) openConfirmation("opt-out");
    else changeCollection(!collecting);
  };

  const errorFor = (action) =>
    actionError && actionError.action === action ? <Alert tone="danger">{actionError.message}</Alert> : null;

  // Acknowledgement + final button for the two permanent deletions.
  const deletionConfirmation = ({ acknowledgement, label, pendingLabel, onConfirm, action }) => (
    <div className="privacy-confirm">
      <label className="ui-check">
        <input
          type="checkbox"
          ref={focusTarget}
          checked={acknowledged}
          onChange={(event) => setAcknowledged(event.target.checked)}
          disabled={busy}
        />
        <span className="ui-check-text">{acknowledgement}</span>
      </label>
      <div className="ui-row">
        <button type="button" className="danger-button is-solid" onClick={onConfirm} disabled={busy || !acknowledged}>
          <Icon name="trash" size={15} />
          {pending === action ? pendingLabel : label}
        </button>
        <button type="button" className="ghost-button" onClick={closeConfirmation} disabled={busy}>
          Cancel
        </button>
      </div>
    </div>
  );

  return (
    <section className="ui-page privacy-page" aria-labelledby="privacy-title">
      <PageHeader
        titleId="privacy-title"
        title="Privacy & data"
        description="Control whether Code4Me collects data about you, see what is stored, and erase it."
      />

      {!privacy && isLoading ? <Loading label="Loading your privacy settings…" /> : null}

      {!privacy && !isLoading && loadError ? (
        <div className="ui-stack-sm">
          <Alert tone="danger" title="Your privacy settings could not be loaded.">
            {loadError}
          </Alert>
          <div>
            <button type="button" className="secondary-button" onClick={load}>
              <Icon name="refresh" size={15} />
              Retry
            </button>
          </div>
        </div>
      ) : null}

      {privacy ? (
        <>
          <Card
            title="Data collection"
            actions={
              <>
                <Badge tone={collecting ? "info" : "neutral"} dot>
                  {collecting ? "Collecting" : "Opted out"}
                </Badge>
                {!collecting && collection.opted_out_at ? (
                  <span className="ui-subtle">since {formatDate(collection.opted_out_at)}</span>
                ) : null}
              </>
            }
          >
            <p className="privacy-text">{collecting ? COLLECTING_TEXT : OPTED_OUT_TEXT}</p>
            {collectionNotice ? <Alert tone="success">{collectionNotice}</Alert> : null}
            {errorFor("collection")}
            {confirming === "opt-out" && activeStudy ? (
              <div
                className="privacy-confirm"
                role="group"
                aria-labelledby="privacy-opt-out-text"
                tabIndex={-1}
                ref={focusTarget}
              >
                <p id="privacy-opt-out-text" className="privacy-confirm-text">
                  Turning off data collection also withdraws you from the study “{studyName(activeStudy)}”. Your study
                  data stays stored until you erase it.
                </p>
                <div className="ui-row">
                  <button
                    type="button"
                    className="danger-button is-solid"
                    onClick={() => changeCollection(false)}
                    disabled={busy}
                  >
                    {pending === "collection" ? "Turning off…" : "Withdraw and turn off"}
                  </button>
                  <button type="button" className="ghost-button" onClick={closeConfirmation} disabled={busy}>
                    Cancel
                  </button>
                </div>
              </div>
            ) : (
              <div className="ui-row">
                <button
                  id={TRIGGER_IDS["opt-out"]}
                  type="button"
                  className={collecting ? "secondary-button" : "primary-button"}
                  onClick={toggleCollection}
                  disabled={busy}
                >
                  <Icon name="power" size={15} />
                  {pending === "collection"
                    ? collecting
                      ? "Turning off…"
                      : "Turning on…"
                    : collecting
                      ? "Turn off data collection"
                      : "Turn data collection back on"}
                </button>
              </div>
            )}
          </Card>

          <Card title="Your stored data" subtitle="What Code4Me currently keeps that is linked to your account.">
            <dl className="ui-dl privacy-counts">
              {STORED_DATA.map((item) => (
                <div key={item.key}>
                  <dt>{item.label}</dt>
                  <dd>{formatNumber(stored[item.key])}</dd>
                </div>
              ))}
            </dl>
          </Card>

          <Card className="privacy-danger" title="Erase my data">
            <p className="privacy-text">{ERASE_TEXT}</p>
            <Alert tone="warning" live={false}>
              This cannot be undone. Code4Me keeps only an anonymous record that an erasure took place.
            </Alert>
            {erased ? (
              <Alert tone="success" title="Your data has been erased.">
                {countSummary(erased)}
              </Alert>
            ) : null}
            {errorFor("erase")}
            {confirming === "erase" ? (
              deletionConfirmation({
                acknowledgement: "I understand that my data will be permanently deleted.",
                label: "Erase my data",
                pendingLabel: "Erasing…",
                onConfirm: erase,
                action: "erase",
              })
            ) : (
              <div className="ui-row">
                <button
                  id={TRIGGER_IDS.erase}
                  type="button"
                  className="danger-button"
                  onClick={() => openConfirmation("erase")}
                  disabled={busy}
                >
                  <Icon name="trash" size={15} />
                  Erase my data…
                </button>
              </div>
            )}
          </Card>

          <Card className="privacy-danger" title="Delete account">
            <p className="privacy-text">
              Delete your account and everything Code4Me has collected about you. You will be signed out.
            </p>
            {!deletionAllowed ? (
              <Alert tone="warning" live={false}>
                {deletion.blocked_reason || "Your account cannot be deleted right now."}
              </Alert>
            ) : null}
            {errorFor("delete")}
            {confirming === "delete" && deletionAllowed ? (
              deletionConfirmation({
                acknowledgement: "I understand that my account and data will be permanently deleted.",
                label: "Delete my account",
                pendingLabel: "Deleting…",
                onConfirm: deleteAccount,
                action: "delete",
              })
            ) : (
              <div className="ui-row">
                <button
                  id={TRIGGER_IDS.delete}
                  type="button"
                  className="danger-button"
                  onClick={() => openConfirmation("delete")}
                  disabled={busy || !deletionAllowed}
                >
                  <Icon name="trash" size={15} />
                  Delete my account…
                </button>
              </div>
            )}
          </Card>
        </>
      ) : null}
    </section>
  );
};

export default PrivacySettings;
