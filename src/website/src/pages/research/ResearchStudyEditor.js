import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  createResearchDraft,
  getAgentDistributions,
  getCurrentUser,
  getResearchDraft,
  getResearchRevision,
  publishResearchDraft,
  supersedeResearchRevision,
  validateResearchProtocol,
} from "../../utils/api";
import {
  ASSIGNMENT_STRATEGIES,
  ASSIGNMENT_UNITS,
  COMPLETION_POLICIES,
  RETENTION_ACTIONS,
  SCHEDULE_KINDS,
  TELEMETRY_FIELD_CLASSES,
  buildProtocolFromForm,
  emptyCondition,
  emptyProtocolForm,
  errorsForPrefix,
  formatDateTime,
  indexErrorsByField,
  protocolToForm,
} from "./protocol";
import "./research.css";
import "./ResearchStudyEditor.css";

const FieldErrors = ({ path, index }) => {
  const items = errorsForPrefix(index, path);
  if (items.length === 0) return null;
  return (
    <ul className="research-field-errors" role="alert">
      {items.map((item, position) => (
        <li key={`${item.code}-${item.field}-${position}`}>
          <span className="research-severity">{item.severity || "ERROR"}</span>
          <code>{item.field}</code> {item.message}
        </li>
      ))}
    </ul>
  );
};

const VerifiedBadge = ({ verified }) => (
  <span
    className={`research-status research-status-${
      verified ? "published" : "retired"
    }`}
  >
    {verified ? "VERIFIED" : "UNVERIFIED"}
  </span>
);

const formatPlatforms = (platforms) => {
  if (!Array.isArray(platforms) || platforms.length === 0) return "none declared";
  return platforms
    .map((platform) => `${platform.os || "?"}/${platform.arch || "?"}`)
    .join(", ");
};

// One-line release identity for the picker option text. BYOA distributions have
// no registry release, so they show their participant-installed identity.
const distributionReleaseLabel = (distribution = {}) => {
  if (distribution.distribution_mode === "BYOA_EXTERNAL") {
    return `BYOA · ${
      distribution.agent_command ||
      distribution.agent_package ||
      "participant-installed"
    }`;
  }
  const release = distribution.release_id || "unresolved";
  const version = distribution.release_version || "?";
  return `${release} · v${version}`;
};

// Read-only resolved detail for the selected distribution. `verified` and the
// supported platforms come straight from the API; the client never derives or
// recomputes them. Platform is informational only and is never a pick.
const DistributionDetail = ({ distribution }) => {
  if (!distribution) return null;
  const isByoa = distribution.distribution_mode === "BYOA_EXTERNAL";
  return (
    <div className="research-distribution-detail">
      <div className="research-distribution-detail-head">
        <VerifiedBadge verified={distribution.verified === true} />
        <span className="research-group-label">{distribution.name}</span>
      </div>
      <dl className="research-metrics">
        <div>
          <dt>Resolved release</dt>
          <dd>{distribution.release_id || (isByoa ? "—" : "unresolved")}</dd>
        </div>
        <div>
          <dt>Release version</dt>
          <dd>{distribution.release_version || "—"}</dd>
        </div>
        <div>
          <dt>Distribution mode</dt>
          <dd>{distribution.distribution_mode || "PACKAGED"}</dd>
        </div>
        {isByoa && (
          <div>
            <dt>Agent identity</dt>
            <dd>
              {distribution.agent_command ||
                distribution.agent_package ||
                "participant-installed"}
            </dd>
          </div>
        )}
        <div>
          <dt>Supported platforms</dt>
          <dd>{formatPlatforms(distribution.supported_platforms)}</dd>
        </div>
      </dl>
      <p className="research-hint">
        Platform is never part of the pick: if the pinned release does not
        publish an artifact for the participant's platform, enrollment fails
        closed.
      </p>
    </div>
  );
};

const ResearchStudyEditor = () => {
  const navigate = useNavigate();
  const params = useParams();
  const [searchParams] = useSearchParams();
  const routeStudyId = params.studyId || "";
  const draftIdFromUrl = searchParams.get("draft_id") || "";
  const revisionIdFromUrl = searchParams.get("revision_id") || "";

  // The "New study" flow passes the name/description it minted through the URL
  // so the researcher does not retype them; an existing draft/revision load
  // replaces this initial state entirely.
  const [form, setForm] = useState(() => ({
    ...emptyProtocolForm(routeStudyId),
    name: searchParams.get("name") || "",
    description: searchParams.get("description") || "",
  }));
  const [distributions, setDistributions] = useState([]);
  const [distributionsUnavailable, setDistributionsUnavailable] = useState(false);
  const [isAdmin, setIsAdmin] = useState(false);
  const [mode, setMode] = useState("new");
  const [draftId, setDraftId] = useState("");
  const [revision, setRevision] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isBusy, setIsBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [validationErrors, setValidationErrors] = useState([]);
  const [warnings, setWarnings] = useState([]);
  const [capabilityDraft, setCapabilityDraft] = useState("");
  const [capabilityState, setCapabilityState] = useState("SUPPORTED");

  const fieldErrorIndex = useMemo(
    () => indexErrorsByField(validationErrors),
    [validationErrors],
  );

  const distributionsById = useMemo(() => {
    const index = {};
    distributions.forEach((distribution) => {
      if (distribution && distribution.distribution_id) {
        index[distribution.distribution_id] = distribution;
      }
    });
    return index;
  }, [distributions]);

  // The caller's admin flag decides whether an unverified distribution is
  // selectable. It is read from the session, never guessed from the list.
  useEffect(() => {
    let cancelled = false;
    Promise.resolve(getCurrentUser())
      .then((response) => {
        if (!cancelled && response && response.ok && response.user) {
          setIsAdmin(response.user.is_admin === true);
        }
      })
      .catch(() => {
        /* Not signed in / unavailable: treat as a non-admin (fail closed). */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // A distribution IS the single thing a condition picks. It exposes no secret
  // and carries the server-derived `verified` flag and supported platforms.
  useEffect(() => {
    let cancelled = false;
    Promise.resolve(getAgentDistributions())
      .then((response) => {
        if (cancelled) return;
        if (response && response.ok) {
          setDistributions(Array.isArray(response.data) ? response.data : []);
          setDistributionsUnavailable(false);
        } else {
          setDistributions([]);
          setDistributionsUnavailable(true);
        }
      })
      .catch(() => {
        if (!cancelled) setDistributionsUnavailable(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);


  // Load an existing draft or a revision to supersede. Draft/revision content
  // is mapped back into the form; publishing always produces a new revision.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      setIsLoading(true);
      if (draftIdFromUrl) {
        const response = await getResearchDraft(draftIdFromUrl);
        if (cancelled) return;
        if (response.ok) {
          setForm(protocolToForm(response.protocol, response.study_id || routeStudyId));
          setDraftId(response.draft_id);
          setMode("draft");
        } else {
          setError(response.error);
        }
      } else if (revisionIdFromUrl) {
        const response = await getResearchRevision(revisionIdFromUrl);
        if (cancelled) return;
        if (response.ok) {
          setForm(protocolToForm(response.protocol, routeStudyId));
          setRevision(response.revision);
          setMode("supersede");
        } else {
          setError(response.error);
        }
      }
      if (!cancelled) setIsLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [draftIdFromUrl, revisionIdFromUrl, routeStudyId]);

  const setField = useCallback((name, value) => {
    setForm((current) => ({ ...current, [name]: value }));
  }, []);

  const handleChange = (event) => {
    const { name, value, type, checked } = event.target;
    setField(name, type === "checkbox" ? checked : value);
  };

  const handleTelemetryToggle = (fieldClass) => {
    setForm((current) => {
      const selected = new Set(current.telemetryClasses);
      if (selected.has(fieldClass)) {
        selected.delete(fieldClass);
      } else {
        selected.add(fieldClass);
      }
      return { ...current, telemetryClasses: Array.from(selected) };
    });
  };

  const handleAddCondition = () => {
    setForm((current) => ({
      ...current,
      conditions: [...current.conditions, emptyCondition()],
    }));
  };

  const updateCondition = (index, name, value) => {
    setForm((current) => {
      const conditions = current.conditions.map((condition, position) =>
        position === index ? { ...condition, [name]: value } : condition,
      );
      return { ...current, conditions };
    });
  };

  const removeCondition = (index) => {
    setForm((current) => ({
      ...current,
      conditions: current.conditions.filter((_, position) => position !== index),
    }));
  };

  // Selecting a distribution is the ONLY pick a condition makes. Everything
  // else (release, artifact, mode, verified state) is resolved server-side and
  // shown read-only. A non-admin may not select an unverified distribution:
  // the option is disabled, and this guard keeps programmatic changes honest.
  const handleSelectDistribution = (index, distributionId) => {
    const distribution = distributionsById[distributionId] || null;
    if (distribution && !isAdmin && distribution.verified !== true) {
      setError(
        `"${distribution.name}" is unverified and cannot be selected by a researcher. Ask an administrator to verify the release.`,
      );
      return;
    }
    setForm((current) => ({
      ...current,
      conditions: current.conditions.map((condition, position) =>
        position === index
          ? {
              ...condition,
              distributionId,
              // Preview the identity from the pick; the researcher may rename.
              conditionId:
                condition.conditionId ||
                (distribution ? distribution.name : ""),
              name: condition.name || (distribution ? distribution.name : ""),
            }
          : condition,
      ),
    }));
  };

  const addCapability = () => {
    const capability = capabilityDraft.trim();
    if (!capability) return;
    setForm((current) => ({
      ...current,
      requiredCapabilities: [
        ...current.requiredCapabilities,
        { capability, requireState: capabilityState },
      ],
    }));
    setCapabilityDraft("");
  };

  const removeCapability = (index) => {
    setForm((current) => ({
      ...current,
      requiredCapabilities: current.requiredCapabilities.filter(
        (_, position) => position !== index,
      ),
    }));
  };

  const buildProtocol = () => buildProtocolFromForm(form);

  const ensureStudyId = () => {
    if (!form.studyId.trim()) {
      setError("A study ID (UUID) is required before validating or saving.");
      return false;
    }
    return true;
  };

  const ensureConditionDistributions = () => {
    const missing = form.conditions.findIndex(
      (condition) => !(condition.distributionId || "").trim(),
    );
    if (missing === -1) {
      // A non-admin must not author from an unverified distribution. The picker
      // disables those options; this guards a loaded draft that already pins one.
      if (!isAdmin) {
        const unverified = form.conditions.findIndex((condition) => {
          const distribution = distributionsById[condition.distributionId];
          return distribution && distribution.verified !== true;
        });
        if (unverified !== -1) {
          setError(
            `Condition ${unverified + 1} uses an unverified distribution; a researcher may only author from a verified distribution.`,
          );
          return false;
        }
      }
      return true;
    }
    setError(`Condition ${missing + 1} needs a distribution.`);
    return false;
  };

  const applyValidation = (result) => {
    const errors = Array.isArray(result.errors) ? result.errors : [];
    if (result.valid !== false && errors.length === 0) {
      setValidationErrors([]);
      return true;
    }
    setValidationErrors(errors);
    setError(result.error || "The protocol failed validation.");
    return false;
  };

  const handleValidate = async () => {
    if (!ensureStudyId()) return;
    if (!ensureConditionDistributions()) return;
    setIsBusy(true);
    setError("");
    setNotice("");
    setValidationErrors([]);
    setWarnings([]);
    const result = await validateResearchProtocol(buildProtocol());
    if (applyValidation(result)) {
      setWarnings(Array.isArray(result.warnings) ? result.warnings : []);
      setNotice("Protocol is valid and publishable.");
    }
    setIsBusy(false);
  };

  const handleCreateDraft = async () => {
    if (!ensureStudyId()) return;
    if (!ensureConditionDistributions()) return;
    if (!form.name.trim()) {
      setError("A study name is required to create a draft.");
      return;
    }
    setIsBusy(true);
    setError("");
    setNotice("");
    setValidationErrors([]);
    setWarnings([]);
    const result = await createResearchDraft({
      studyId: form.studyId.trim(),
      name: form.name.trim(),
      protocol: buildProtocol(),
    });
    if (result.ok) {
      setDraftId(result.data.draft_id);
      setMode("draft");
      setNotice(`Draft ${result.data.draft_id} saved.`);
    } else {
      setValidationErrors(result.errors || []);
      setError(result.error);
    }
    setIsBusy(false);
  };

  const handlePublish = async () => {
    if (!ensureStudyId()) return;
    if (!ensureConditionDistributions()) return;
    setIsBusy(true);
    setError("");
    setNotice("");
    setValidationErrors([]);
    setWarnings([]);
    const protocol = buildProtocol();
    let result;
    if (mode === "supersede" && revision) {
      result = await supersedeResearchRevision(revision.revision_id, {
        protocol,
      });
    } else if (draftId) {
      result = await publishResearchDraft(draftId, { protocol });
    } else {
      setIsBusy(false);
      setError("Create a draft (or open a revision) before publishing.");
      return;
    }

    if (result.ok) {
      const published = result.data.revision;
      // An admin publishing an unverified distribution is allowed but surfaced.
      setWarnings(Array.isArray(result.data.warnings) ? result.data.warnings : []);
      setNotice(
        `Published revision #${published ? published.revision_number : "?"} (${
          published ? published.protocol_digest : ""
        }).`,
      );
    } else if (result.status === 409) {
      setError(
        "Publication conflict: a newer revision was published concurrently. Reload the study and retry.",
      );
    } else {
      setValidationErrors(result.errors || []);
      setError(result.error);
    }
    setIsBusy(false);
  };

  const hasUnverifiedDistributions = distributions.some(
    (distribution) => distribution.verified !== true,
  );

  if (isLoading) {
    return (
      <section className="research-page">
        <p className="research-empty">Loading protocol…</p>
      </section>
    );
  }

  return (
    <section className="research-page" aria-labelledby="research-editor-title">
      <div className="research-header">
        <div>
          <h2 id="research-editor-title">
            {mode === "supersede"
              ? "Author successor revision"
              : mode === "draft"
                ? "Edit draft"
                : "New study protocol draft"}
          </h2>
          <p>
            Each condition picks one agent distribution. The release, artifact
            and verification state are resolved server-side; a verified
            distribution is required for a researcher to publish.
          </p>
          {revision && (
            <p className="research-hint">
              Supersedes revision #{revision.revision_number} (
              {revision.protocol_digest}) published{" "}
              {formatDateTime(revision.published_at)}.
            </p>
          )}
        </div>
        <button
          type="button"
          className="secondary-button"
          onClick={() =>
            navigate(
              `/research/studies?study_id=${encodeURIComponent(form.studyId)}`,
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

      {warnings.length > 0 && (
        <div className="research-card research-warnings" role="status">
          <h3>Warnings</h3>
          <p className="research-hint">
            Publishing succeeded. These advisories do not block publication.
          </p>
          <ul>
            {warnings.map((warning, index) => (
              <li key={`${warning.code || "warning"}-${index}`}>
                {warning.field ? (
                  <>
                    <code>{warning.field}</code>{" "}
                  </>
                ) : null}
                {warning.message || "Unverified distribution."}
              </li>
            ))}
          </ul>
        </div>
      )}

      <form
        className="research-editor"
        onSubmit={(event) => event.preventDefault()}
      >
        <fieldset className="research-card">
          <legend>Study metadata</legend>
          <label>
            Study ID (UUID)
            <input
              name="studyId"
              value={form.studyId}
              onChange={handleChange}
              disabled={isBusy}
              placeholder="00000000-0000-0000-0000-000000000000"
            />
          </label>
          <FieldErrors path="study_id" index={fieldErrorIndex} />
          <label>
            Name
            <input
              name="name"
              value={form.name}
              onChange={handleChange}
              disabled={isBusy}
              placeholder="Adaptive agent study"
            />
          </label>
          <FieldErrors path="metadata.name" index={fieldErrorIndex} />
          <label>
            Description
            <textarea
              name="description"
              value={form.description}
              onChange={handleChange}
              disabled={isBusy}
              rows={2}
            />
          </label>
          <label>
            Owner
            <input
              name="owner"
              value={form.owner}
              onChange={handleChange}
              disabled={isBusy}
            />
          </label>
        </fieldset>

        <fieldset className="research-card">
          <legend>Schedule</legend>
          <label>
            Kind
            <select
              name="scheduleKind"
              value={form.scheduleKind}
              onChange={handleChange}
              disabled={isBusy}
            >
              {SCHEDULE_KINDS.map((kind) => (
                <option key={kind.value} value={kind.value}>
                  {kind.label}
                </option>
              ))}
            </select>
          </label>
          {form.scheduleKind === "ROLLING" ? (
            <>
              <label>
                Duration (seconds)
                <input
                  name="durationSeconds"
                  type="number"
                  min="1"
                  value={form.durationSeconds}
                  onChange={handleChange}
                  disabled={isBusy}
                />
              </label>
              <FieldErrors
                path="schedule.duration_seconds"
                index={fieldErrorIndex}
              />
            </>
          ) : (
            <>
              <label>
                Starts at
                <input
                  name="startAt"
                  type="datetime-local"
                  value={form.startAt}
                  onChange={handleChange}
                  disabled={isBusy}
                />
              </label>
              <FieldErrors path="schedule.start_at" index={fieldErrorIndex} />
              <label>
                Ends at (optional)
                <input
                  name="endAt"
                  type="datetime-local"
                  value={form.endAt}
                  onChange={handleChange}
                  disabled={isBusy}
                />
              </label>
              <FieldErrors path="schedule.end_at" index={fieldErrorIndex} />
            </>
          )}
        </fieldset>

        <fieldset className="research-card">
          <legend>Enrollment</legend>
          <label>
            Capacity (blank = inherit, “unknown” = unresolved)
            <input
              name="capacity"
              value={form.capacity}
              onChange={handleChange}
              disabled={isBusy}
              placeholder="e.g. 50 or unknown"
            />
          </label>
          <label>
            Allow re-entry
            <select
              name="allowReentry"
              value={form.allowReentry}
              onChange={handleChange}
              disabled={isBusy}
            >
              <option value="">Inherit server default</option>
              <option value="true">Allow</option>
              <option value="false">Do not allow</option>
            </select>
          </label>
        </fieldset>

        <fieldset className="research-card">
          <legend>Assignment</legend>
          <label>
            Unit
            <select
              name="assignmentUnit"
              value={form.assignmentUnit}
              onChange={handleChange}
              disabled={isBusy}
            >
              {ASSIGNMENT_UNITS.map((unit) => (
                <option key={unit.value} value={unit.value}>
                  {unit.label}
                </option>
              ))}
            </select>
          </label>
          <FieldErrors path="assignment.unit" index={fieldErrorIndex} />
          <label>
            Strategy
            <select
              name="strategy"
              value={form.strategy}
              onChange={handleChange}
              disabled={isBusy}
            >
              {ASSIGNMENT_STRATEGIES.map((strategy) => (
                <option key={strategy.value} value={strategy.value}>
                  {strategy.label}
                </option>
              ))}
            </select>
          </label>
          <FieldErrors path="assignment.strategy" index={fieldErrorIndex} />
          {form.strategy === "STRATIFIED" && (
            <label>
              Stratum keys (comma separated)
              <input
                name="stratification"
                value={form.stratification}
                onChange={handleChange}
                disabled={isBusy}
                placeholder="os, experience"
              />
            </label>
          )}
          <FieldErrors path="assignment.stratification" index={fieldErrorIndex} />
          <p className="research-hint">
            Assignments are always sticky: an enrollment is never re-randomized
            for the same revision. Reallocation, if ever required, must be
            defined by a successor revision.
          </p>
        </fieldset>

        <fieldset className="research-card">
          <legend>Conditions</legend>
          <div className="research-actions">
            <button
              type="button"
              className="secondary-button"
              onClick={handleAddCondition}
              disabled={isBusy}
            >
              Add condition
            </button>
          </div>
          {distributionsUnavailable && (
            <p className="research-hint">
              The distribution registry is unavailable on this server;
              conditions cannot be authored right now.
            </p>
          )}
          <FieldErrors path="conditions" index={fieldErrorIndex} />
          {form.conditions.length === 0 && (
            <p className="research-empty">
              Add at least one condition. Each condition picks exactly one agent
              distribution.
            </p>
          )}
          {form.conditions.map((condition, index) => {
            const selected = distributionsById[condition.distributionId] || null;
            return (
              <div className="research-condition" key={`condition-${index}`}>
                <h4>
                  {index + 1}.{" "}
                  {condition.name || selected?.name || "Unnamed condition"}
                </h4>
                <div className="research-condition-grid">
                  <label>
                    Condition ID
                    <input
                      value={condition.conditionId}
                      onChange={(event) =>
                        updateCondition(index, "conditionId", event.target.value)
                      }
                      disabled={isBusy}
                    />
                  </label>
                  <label>
                    Weight
                    <input
                      type="number"
                      min="0"
                      step="0.1"
                      value={condition.weight}
                      onChange={(event) =>
                        updateCondition(index, "weight", event.target.value)
                      }
                      disabled={isBusy}
                    />
                  </label>
                  <label>
                    Distribution
                    <select
                      value={condition.distributionId}
                      onChange={(event) =>
                        handleSelectDistribution(index, event.target.value)
                      }
                      disabled={isBusy}
                    >
                      <option value="">Select a distribution…</option>
                      {condition.distributionId && !selected && (
                        <option value={condition.distributionId}>
                          {condition.distributionId} (not registered)
                        </option>
                      )}
                      {distributions.map((distribution) => (
                        <option
                          key={distribution.distribution_id}
                          value={distribution.distribution_id}
                          disabled={!isAdmin && distribution.verified !== true}
                        >
                          {distribution.name} ·{" "}
                          {distributionReleaseLabel(distribution)} ·{" "}
                          {distribution.verified ? "verified" : "unverified"}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Adapter version
                    <input
                      value={condition.adapterVersion}
                      onChange={(event) =>
                        updateCondition(
                          index,
                          "adapterVersion",
                          event.target.value,
                        )
                      }
                      disabled={isBusy}
                    />
                  </label>
                  <label>
                    Model override
                    <input
                      value={condition.model}
                      onChange={(event) =>
                        updateCondition(index, "model", event.target.value)
                      }
                      disabled={isBusy}
                    />
                  </label>
                </div>
                <DistributionDetail distribution={selected} />
                {!isAdmin && hasUnverifiedDistributions && (
                  <p className="research-hint">
                    Unverified distributions are listed but cannot be selected
                    by a researcher; ask an administrator to verify the release.
                  </p>
                )}
                {isAdmin && selected && selected.verified !== true && (
                  <p className="research-hint research-condition-hint">
                    This distribution is unverified. Publishing will proceed
                    with a warning.
                  </p>
                )}
                {!selected && !condition.distributionId && (
                  <p className="research-hint research-condition-hint">
                    Pick one distribution. The server resolves its release,
                    artifact and verification state.
                  </p>
                )}
                <FieldErrors
                  path={`conditions[${index}]`}
                  index={fieldErrorIndex}
                />
                <button
                  type="button"
                  className="danger-button"
                  onClick={() => removeCondition(index)}
                  disabled={isBusy}
                >
                  Remove condition
                </button>
              </div>
            );
          })}
        </fieldset>

        <fieldset className="research-card">
          <legend>Session policy</legend>
          <label>
            Idle timeout (seconds)
            <input
              name="idleTimeoutSeconds"
              type="number"
              min="0"
              value={form.idleTimeoutSeconds}
              onChange={handleChange}
              disabled={isBusy}
            />
          </label>
          <label>
            Resume grace (seconds)
            <input
              name="resumeGraceSeconds"
              type="number"
              min="0"
              value={form.resumeGraceSeconds}
              onChange={handleChange}
              disabled={isBusy}
            />
          </label>
          <label>
            Heartbeat (seconds)
            <input
              name="heartbeatSeconds"
              type="number"
              min="0"
              value={form.heartbeatSeconds}
              onChange={handleChange}
              disabled={isBusy}
            />
          </label>
        </fieldset>

        <fieldset className="research-card">
          <legend>Telemetry policy</legend>
          <span className="research-group-label">Allowed field classes</span>
          {TELEMETRY_FIELD_CLASSES.map((fieldClass) => (
            <label key={fieldClass.value} className="research-checkbox">
              <input
                type="checkbox"
                checked={form.telemetryClasses.includes(fieldClass.value)}
                onChange={() => handleTelemetryToggle(fieldClass.value)}
                disabled={isBusy}
              />
              {fieldClass.label}
            </label>
          ))}
          <FieldErrors
            path="telemetry_policy.allowed_field_classes"
            index={fieldErrorIndex}
          />
        </fieldset>

        <fieldset className="research-card">
          <legend>Privacy policy</legend>
          <label>
            Retention action
            <select
              name="retentionAction"
              value={form.retentionAction}
              onChange={handleChange}
              disabled={isBusy}
            >
              {RETENTION_ACTIONS.map((action) => (
                <option key={action.value} value={action.value}>
                  {action.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Retention days
            <input
              name="retentionDays"
              type="number"
              min="0"
              value={form.retentionDays}
              onChange={handleChange}
              disabled={isBusy}
            />
          </label>
        </fieldset>

        

        <fieldset className="research-card">
          <legend>Environment requirements</legend>
          <label>
            Expected ACP protocol version
            <input
              name="expectedProtocolVersion"
              value={form.expectedProtocolVersion}
              onChange={handleChange}
              disabled={isBusy}
              placeholder="1"
            />
          </label>
          <FieldErrors
            path="environment_requirements.expected_protocol_version"
            index={fieldErrorIndex}
          />
          <label>
            Host kind (blank = inherit, “unknown” = unresolved)
            <input
              name="hostKind"
              value={form.hostKind}
              onChange={handleChange}
              disabled={isBusy}
              placeholder="e.g. intellij or unknown"
            />
          </label>
          <span className="research-group-label">Required capabilities</span>
          <div className="research-inline-add">
            <input
              value={capabilityDraft}
              onChange={(event) => setCapabilityDraft(event.target.value)}
              placeholder="capability id"
              aria-label="Required capability id"
              disabled={isBusy}
            />
            <select
              value={capabilityState}
              onChange={(event) => setCapabilityState(event.target.value)}
              aria-label="Required capability state"
              disabled={isBusy}
            >
              <option value="SUPPORTED">SUPPORTED</option>
              <option value="UNSUPPORTED">UNSUPPORTED</option>
              <option value="UNKNOWN">UNKNOWN</option>
            </select>
            <button
              type="button"
              className="secondary-button"
              onClick={addCapability}
              disabled={isBusy}
            >
              Add
            </button>
          </div>
          {form.requiredCapabilities.length > 0 && (
            <ul className="research-capability-list">
              {form.requiredCapabilities.map((capability, index) => (
                <li key={`${capability.capability}-${index}`}>
                  <code>{capability.capability}</code>{" "}
                  <span className="research-severity">
                    {capability.requireState}
                  </span>
                  <button
                    type="button"
                    className="danger-button"
                    onClick={() => removeCapability(index)}
                    disabled={isBusy}
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          )}
          <FieldErrors
            path="environment_requirements.required_capabilities"
            index={fieldErrorIndex}
          />
        </fieldset>

        <fieldset className="research-card">
          <legend>Completion</legend>
          <label>
            Policy
            <select
              name="completionPolicy"
              value={form.completionPolicy}
              onChange={handleChange}
              disabled={isBusy}
            >
              {COMPLETION_POLICIES.map((policy) => (
                <option key={policy.value} value={policy.value}>
                  {policy.label}
                </option>
              ))}
            </select>
          </label>
          {form.completionPolicy === "TARGET_CAPACITY" && (
            <label>
              Target enrollments
              <input
                name="targetEnrollments"
                type="number"
                min="1"
                value={form.targetEnrollments}
                onChange={handleChange}
                disabled={isBusy}
              />
            </label>
          )}
          <FieldErrors
            path="completion.target_enrollments"
            index={fieldErrorIndex}
          />
        </fieldset>

        <div className="research-card research-editor-actions">
          <button
            type="button"
            className="secondary-button"
            onClick={handleValidate}
            disabled={isBusy}
          >
            Validate
          </button>
          {mode !== "supersede" && (
            <button
              type="button"
              className="secondary-button"
              onClick={handleCreateDraft}
              disabled={isBusy}
            >
              {mode === "new" ? "Create draft" : "Save as new draft"}
            </button>
          )}
          <button
            type="button"
            className="primary-button"
            onClick={handlePublish}
            disabled={isBusy || (mode !== "supersede" && !draftId)}
          >
            {mode === "supersede" ? "Publish successor revision" : "Publish draft"}
          </button>
        </div>
      </form>

      <details className="research-card research-preview">
        <summary>Protocol JSON preview</summary>
        <pre>{JSON.stringify(buildProtocol(), null, 2)}</pre>
      </details>
    </section>
  );
};

export default ResearchStudyEditor;
