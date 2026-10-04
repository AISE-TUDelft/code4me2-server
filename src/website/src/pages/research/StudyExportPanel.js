import React, { useState } from "react";
import Icon from "../../components/common/Icon";
import { Alert } from "../../components/common/ui";
import { downloadStudyExport } from "../../utils/api";
import { saveBlob } from "./studyUtils";

export const EXPORT_DATASETS = [
  { key: "participants", label: "Participants", help: "Arm, assignment strategy, consent version and answers." },
  { key: "participant_metrics", label: "Participant metrics", help: "The per-participant metrics behind the arm comparison." },
  { key: "sessions", label: "Research sessions", help: "IDE activity boundaries per participant." },
  { key: "chats", label: "Chats", help: "Each chat with its number, start, end and outcomes." },
  { key: "events", label: "Events", help: "Every retained telemetry event (the raw data)." },
];

export const EXPORT_EVENT_CATEGORIES = [
  { key: "conversation", label: "Prompts, replies and reasoning" },
  { key: "tools", label: "Tool calls" },
  { key: "approvals", label: "Approvals" },
  { key: "chat_lifecycle", label: "Chat lifecycle" },
  { key: "plans_usage", label: "Plans and token usage" },
  { key: "ide", label: "IDE activity" },
  { key: "errors", label: "Errors" },
  { key: "other", label: "Everything else" },
];

const EXPORT_ERRORS = {
  CONTENT_NOT_CAPTURED: "This study did not capture content, so there is none to export.",
  CONTENT_REQUIRES_JSONL: "Content is exported in the JSONL format only.",
  UNKNOWN_FILTER_VALUE: "A filtered arm or participant is not part of this study any more; clear the filters and retry.",
};

const toggled = (list, key) => (list.includes(key) ? list.filter((item) => item !== key) : [...list, key]);

/**
 * Raw data export: pick the datasets, format, event categories, date range
 * and filters; the server returns a ZIP with a manifest (study, assignment
 * formula, filters, row counts and a column dictionary).
 */
const StudyExportPanel = ({ study, filters = { arms: [], participants: [] }, onDone }) => {
  const contentCaptured = study?.telemetry_policy?.content_capture === true;
  const filterCount = filters.arms.length + filters.participants.length;
  const [datasets, setDatasets] = useState(EXPORT_DATASETS.map((dataset) => dataset.key));
  const [format, setFormat] = useState("csv");
  const [categories, setCategories] = useState([]);
  const [range, setRange] = useState({ start: "", end: "" });
  const [useFilters, setUseFilters] = useState(filterCount > 0);
  const [includeContent, setIncludeContent] = useState(false);
  const [state, setState] = useState({ busy: false, error: "", done: "" });

  const contentPossible = contentCaptured && format === "jsonl";
  const rangeInvalid = Boolean(range.start && range.end && range.start > range.end);

  const submit = async (event) => {
    event.preventDefault();
    if (!datasets.length || rangeInvalid) return;
    setState({ busy: true, error: "", done: "" });
    const result = await downloadStudyExport(study.study_id, {
      datasets,
      format,
      eventCategories: datasets.includes("events") ? categories : [],
      start: range.start || undefined,
      end: range.end || undefined,
      arms: useFilters ? filters.arms : [],
      participants: useFilters ? filters.participants : [],
      includeContent: contentPossible && includeContent,
    });
    if (result && result.ok) {
      saveBlob(result.filename, result.blob);
      setState({ busy: false, error: "", done: `Downloaded ${result.filename}.` });
      if (onDone) onDone();
    } else {
      setState({
        busy: false,
        error: EXPORT_ERRORS[result && result.code] || (result && result.error) || "The export could not be created.",
        done: "",
      });
    }
  };

  return (
    <form className="ui-stack study-export" onSubmit={submit} aria-label="Export study data">
      <fieldset className="ui-fieldset">
        <legend>Datasets</legend>
        {EXPORT_DATASETS.map((dataset) => (
          <label key={dataset.key} className="ui-check">
            <input
              type="checkbox"
              checked={datasets.includes(dataset.key)}
              onChange={() => setDatasets((current) => toggled(current, dataset.key))}
              disabled={state.busy}
            />
            <span className="ui-check-text">
              {dataset.label}
              <small>{dataset.help}</small>
            </span>
          </label>
        ))}
      </fieldset>

      <fieldset className="ui-fieldset">
        <legend>Format</legend>
        <label className="ui-check">
          <input type="radio" name="export-format" checked={format === "csv"} onChange={() => setFormat("csv")} disabled={state.busy} />
          <span className="ui-check-text">
            CSV
            <small>Spreadsheets, R and pandas. Events as metadata columns.</small>
          </span>
        </label>
        <label className="ui-check">
          <input type="radio" name="export-format" checked={format === "jsonl"} onChange={() => setFormat("jsonl")} disabled={state.busy} />
          <span className="ui-check-text">
            JSONL
            <small>One JSON object per line; events carry their full stored envelope.</small>
          </span>
        </label>
      </fieldset>

      {datasets.includes("events") ? (
        <fieldset className="ui-fieldset">
          <legend>Event categories</legend>
          <p className="ui-hint">None ticked exports every event.</p>
          {EXPORT_EVENT_CATEGORIES.map((category) => (
            <label key={category.key} className="ui-check">
              <input
                type="checkbox"
                checked={categories.includes(category.key)}
                onChange={() => setCategories((current) => toggled(current, category.key))}
                disabled={state.busy}
              />
              <span className="ui-check-text">{category.label}</span>
            </label>
          ))}
        </fieldset>
      ) : null}

      <fieldset className="ui-fieldset">
        <legend>Date range (UTC)</legend>
        <div className="ui-row">
          <input
            className="ui-input"
            type="date"
            value={range.start}
            onChange={(event) => setRange({ ...range, start: event.target.value })}
            aria-label="Export from date"
            disabled={state.busy}
          />
          <span className="ui-subtle">to</span>
          <input
            className="ui-input"
            type="date"
            value={range.end}
            onChange={(event) => setRange({ ...range, end: event.target.value })}
            aria-label="Export to date"
            disabled={state.busy}
          />
        </div>
        {rangeInvalid ? <p className="ui-field-error">The start must be on or before the end.</p> : null}
        <p className="ui-hint">Applies to events, metrics and chats; empty means all time.</p>
      </fieldset>

      {filterCount > 0 ? (
        <label className="ui-check">
          <input type="checkbox" checked={useFilters} onChange={(event) => setUseFilters(event.target.checked)} disabled={state.busy} />
          <span className="ui-check-text">
            Only the participants selected in the analytics filters
            <small>
              {filters.arms.length} arm{filters.arms.length === 1 ? "" : "s"}, {filters.participants.length} participant
              {filters.participants.length === 1 ? "" : "s"} selected.
            </small>
          </span>
        </label>
      ) : null}

      <label className="ui-check">
        <input
          type="checkbox"
          checked={contentPossible && includeContent}
          onChange={(event) => setIncludeContent(event.target.checked)}
          disabled={state.busy || !contentPossible}
        />
        <span className="ui-check-text">
          Include captured content
          <small>
            {contentCaptured
              ? "Prompts, reasoning, messages and tool data as stored. JSONL only; handle under your ethics approval."
              : "This study does not capture content."}
          </small>
        </span>
      </label>

      {state.error ? (
        <Alert tone="danger" live>
          {state.error}
        </Alert>
      ) : null}
      {state.done ? (
        <p className="research-notice" role="status">
          {state.done}
        </p>
      ) : null}
      <div className="ui-row">
        <button type="submit" className="primary-button" disabled={state.busy || !datasets.length || rangeInvalid}>
          <Icon name="external" size={15} />
          {state.busy ? "Preparing…" : "Download ZIP"}
        </button>
      </div>
    </form>
  );
};

export default StudyExportPanel;
