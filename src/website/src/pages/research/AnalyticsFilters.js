import React, { useEffect, useMemo, useRef, useState } from "react";
import Icon from "../../components/common/Icon";

export const NO_FILTERS = { arms: [], participants: [] };

export const filterCount = (filters) => filters.arms.length + filters.participants.length;

const toggled = (list, id) => (list.includes(id) ? list.filter((item) => item !== id) : [...list, id]);

/**
 * Arm and participant filters for the study analytics (they combine: only
 * the selected participants of the selected arms). `options` is the summary's
 * `filter_options`; changes apply together with the Apply button.
 */
const AnalyticsFilters = ({ options, value, onApply, disabled = false }) => {
  const [draft, setDraft] = useState(value);
  const [query, setQuery] = useState("");
  // The panel floats over the page: close it once filters apply, so it never
  // covers the results (or the participant-dashboard button).
  const panel = useRef(null);
  const apply = (next) => {
    if (panel.current) panel.current.open = false;
    onApply(next);
  };
  useEffect(() => setDraft(value), [value]);

  const arms = useMemo(() => (Array.isArray(options?.arms) ? options.arms : []), [options]);
  const participants = Array.isArray(options?.participants) ? options.participants : [];
  const armName = useMemo(() => new Map(arms.map((arm) => [arm.profile_id, arm.name])), [arms]);
  const needle = query.trim().toLowerCase();
  const visible = participants.filter(
    (participant) => !needle || String(participant.participant_code || "").toLowerCase().includes(needle),
  );
  const active = filterCount(value);
  const dirty = JSON.stringify(draft) !== JSON.stringify(value);

  return (
    <details className="analytics-filters" ref={panel}>
      <summary className="secondary-button button-sm">
        <Icon name="filter" size={14} />
        {active ? `Filters (${active})` : "Filters"}
      </summary>
      <div className="analytics-filters-panel" role="group" aria-label="Analytics filters">
        <fieldset className="ui-fieldset">
          <legend>Arms</legend>
          {arms.length ? (
            arms.map((arm) => (
              <label key={arm.profile_id} className="ui-check">
                <input
                  type="checkbox"
                  checked={draft.arms.includes(arm.profile_id)}
                  onChange={() => setDraft((current) => ({ ...current, arms: toggled(current.arms, arm.profile_id) }))}
                  disabled={disabled}
                />
                <span className="ui-check-text">
                  {arm.name || arm.profile_id} <small>({arm.participants})</small>
                </span>
              </label>
            ))
          ) : (
            <p className="ui-hint">No arms.</p>
          )}
        </fieldset>
        <fieldset className="ui-fieldset">
          <legend>Participants</legend>
          <input
            className="ui-input"
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search participant code"
            aria-label="Search participants"
            disabled={disabled}
          />
          <div className="analytics-filter-list">
            {visible.map((participant) => (
              <label key={participant.enrollment_id} className="ui-check">
                <input
                  type="checkbox"
                  checked={draft.participants.includes(participant.enrollment_id)}
                  onChange={() =>
                    setDraft((current) => ({
                      ...current,
                      participants: toggled(current.participants, participant.enrollment_id),
                    }))
                  }
                  disabled={disabled}
                />
                <span className="ui-check-text">
                  {participant.participant_code}
                  <small> · {armName.get(participant.profile_id) || "no arm"}</small>
                </span>
              </label>
            ))}
            {!visible.length ? <p className="ui-hint">No participant matches.</p> : null}
          </div>
        </fieldset>
        <div className="ui-row">
          <button type="button" className="primary-button button-sm" onClick={() => apply(draft)} disabled={disabled || !dirty}>
            Apply filters
          </button>
          <button type="button" className="ghost-button button-sm" onClick={() => apply(NO_FILTERS)} disabled={disabled || !active}>
            Clear all
          </button>
        </div>
      </div>
    </details>
  );
};

export default AnalyticsFilters;
