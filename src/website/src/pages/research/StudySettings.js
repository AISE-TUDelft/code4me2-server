import React, { useEffect, useState } from "react";
import Icon from "../../components/common/Icon";
import { Badge, Card } from "../../components/common/ui";
import { formatDateTime } from "../../utils/format";

/**
 * Editable study metadata (until the first consent), administrator
 * operations (kill switch) and the terminal stop action.
 */
const StudySettings = ({ study, isAdmin, isBusy, onSaveMetadata, onStop, onKillSwitch }) => {
  const [metadata, setMetadata] = useState({ name: study.name || "", description: study.description || "" });
  const [killSwitchReason, setKillSwitchReason] = useState("");

  useEffect(() => {
    setMetadata({ name: study.name || "", description: study.description || "" });
  }, [study.study_id, study.name, study.description]);

  const stopped = study.research_status === "STUDY_STOPPED";
  const locked = Boolean(study.consent_locked_at) || stopped;
  const switchEngaged = Boolean(study.kill_switch?.switch_id) && study.kill_switch.status !== "RELEASED";

  return (
    <div className="ui-stack">
      {!stopped ? (
        <Card
          as="form"
          title="Study details"
          subtitle={
            locked
              ? "Locked: the first participant has consented, so the name and description they agreed to stay fixed."
              : "Shown to participants when they review the study. Editable until the first participant consents."
          }
          onSubmit={(event) => {
            event.preventDefault();
            onSaveMetadata(metadata);
          }}
          footer={
            <button type="submit" className="primary-button" disabled={isBusy || locked || !metadata.name.trim()}>
              Save metadata
            </button>
          }
        >
          <div className="ui-field">
            <label className="ui-label" htmlFor="study-settings-name">
              Name
            </label>
            <input
              id="study-settings-name"
              className="ui-input"
              value={metadata.name}
              onChange={(event) => setMetadata({ ...metadata, name: event.target.value })}
              disabled={isBusy || locked}
            />
          </div>
          <div className="ui-field">
            <label className="ui-label" htmlFor="study-settings-description">
              Description
            </label>
            <textarea
              id="study-settings-description"
              className="ui-textarea"
              value={metadata.description}
              onChange={(event) => setMetadata({ ...metadata, description: event.target.value })}
              rows={3}
              disabled={isBusy || locked}
            />
          </div>
          {locked ? (
            <p className="ui-hint profile-locked">
              <Icon name="lock" size={13} />
              Metadata locked {study.consent_locked_at ? `since ${formatDateTime(study.consent_locked_at)}` : ""}.
            </p>
          ) : null}
        </Card>
      ) : (
        <Card title="Study details">
          <p className="research-hint">Stopped studies cannot be edited. Clone the study to run it again.</p>
        </Card>
      )}

      {isAdmin && !stopped ? (
        <Card
          title={
            <span className="ui-row">
              <Icon name="shield" size={16} />
              Operations
              {switchEngaged ? <Badge tone="danger">Kill switch engaged</Badge> : null}
            </span>
          }
          subtitle="Administrator only. The kill switch pauses telemetry intake and agent use for this study until it is released."
        >
          <div className="research-actions ui-row">
            {!switchEngaged ? (
              <input
                className="ui-input"
                style={{ flex: "1 1 280px", maxWidth: 420 }}
                aria-label="Kill switch reason"
                value={killSwitchReason}
                onChange={(event) => setKillSwitchReason(event.target.value)}
                placeholder="Reason (required)"
                disabled={isBusy}
              />
            ) : null}
            <button
              type="button"
              className={switchEngaged ? "secondary-button" : "danger-button"}
              onClick={async () => {
                // Keep the typed reason when the request fails or is cancelled.
                if (await onKillSwitch(killSwitchReason.trim())) setKillSwitchReason("");
              }}
              disabled={isBusy || (!switchEngaged && !killSwitchReason.trim())}
            >
              {switchEngaged ? "Release kill switch" : "Engage kill switch"}
            </button>
          </div>
        </Card>
      ) : null}
      {isAdmin && study.kill_switch ? (
        <p className="research-hint">
          Kill switch: {study.kill_switch.status || "RELEASED"}. Reason: {study.kill_switch.reason || "Not provided"}
        </p>
      ) : null}

      {!stopped ? (
        <Card className="study-danger-zone" title="Stop the study" subtitle="Permanent: ends collection for every participant.">
          <div className="ui-row-between">
            <p className="ui-muted" style={{ margin: 0, maxWidth: 620 }}>
              Stopping is terminal. Enrollments close, new joins are refused, and all research data collected so far is
              retained for analysis. You can clone a stopped study to run it again.
            </p>
            <button type="button" className="danger-button" onClick={onStop} disabled={isBusy}>
              <Icon name="power" size={15} />
              Stop study
            </button>
          </div>
        </Card>
      ) : null}
    </div>
  );
};

export default StudySettings;
