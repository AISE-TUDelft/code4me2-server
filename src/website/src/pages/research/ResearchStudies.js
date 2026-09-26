import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  applyStudyDefaultBudget,
  cloneResearchStudy,
  createResearchStudy,
  engageResearchKillSwitch,
  getAgentProfiles,
  getCurrentUser,
  getStudyAnalyticsSummary,
  getStudyBudget,
  getStudyParticipants,
  listResearchStudies,
  releaseResearchKillSwitch,
  stopResearchStudy,
  updateResearchStudyMetadata,
  updateStudyBudget,
} from "../../utils/api";
import Icon from "../../components/common/Icon";
import { isResearcher } from "../../components/layout/AppShell";
import { CopyButton, EmptyState, Loading, PageHeader, TabPanel, Tabs } from "../../components/common/ui";
import { formatDate, formatNumber } from "../../utils/format";
import StudyCreateForm from "./StudyCreateForm";
import StudyOverview from "./StudyOverview";
import StudyParticipants from "./StudyParticipants";
import StudyAnalytics from "./StudyAnalytics";
import StudySettings from "./StudySettings";
import { STATUS_LABELS, armsForStudy, statusClass } from "./studyUtils";
import "./research.css";
import "./ResearchStudies.css";

const TABS = ["overview", "participants", "analytics", "settings"];
const LIST_COLLAPSED_KEY = "code4me.research.studies.listCollapsed";

const STATUS_FILTERS = [
  { value: "", label: "All" },
  { value: "ACTIVE", label: "Active" },
  { value: "DRAFT", label: "Draft" },
  { value: "STUDY_STOPPED", label: "Stopped" },
];

const METADATA_ERRORS = {
  STUDY_METADATA_LOCKED: "Study metadata is locked after consent.",
  STUDY_STOPPED: "Stopped studies cannot be edited.",
  PROFILE_LOCKED: "The selected profile is locked.",
  PROFILE_NOT_ALLOWED: "You are not allowed to use that profile.",
};

// Typed failures of the budget mutations (Settings → Participant budgets).
const BUDGET_ERRORS = {
  STUDY_STOPPED: "Stopped studies cannot be edited.",
  STUDY_NOT_METERED: "Budgets do not apply: no selected profile runs Goose or the built-in agent.",
  IDEMPOTENCY_KEY_REUSED: "That request was already submitted; refresh to see its result.",
  REASON_REQUIRED: "A reason of 1–500 characters is required.",
};

// Create/clone failures that belong on the form's budget field.
const CREATE_BUDGET_CODES = ["BUDGET_REQUIRED", "BUDGET_INVALID", "BUDGET_PRICE_MISSING"];

const scheduleText = (study) => {
  if (!study.starts_at && !study.ends_at) return "Open-ended";
  const start = study.starts_at ? formatDate(study.starts_at) : "Now";
  const end = study.ends_at ? formatDate(study.ends_at) : "no end";
  return `${start} – ${end}`;
};

const ResearchStudies = () => {
  const params = useParams();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();

  const [studies, setStudies] = useState([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isUnavailable, setIsUnavailable] = useState(false);
  const [isForbidden, setIsForbidden] = useState(false);
  const [selectedStudyId, setSelectedStudyId] = useState(params.studyId || "");
  const [activeTab, setActiveTab] = useState(TABS.includes(searchParams.get("tab")) ? searchParams.get("tab") : "overview");
  const [isCreating, setIsCreating] = useState(false);
  // Non-null while the create form is acting as the clone submission step.
  const [cloneSource, setCloneSource] = useState(null);
  const [profiles, setProfiles] = useState([]);
  const [isBusy, setIsBusy] = useState(false);
  const [isAdmin, setIsAdmin] = useState(false);
  const [killSwitchId, setKillSwitchId] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [listQuery, setListQuery] = useState("");
  const [listStatus, setListStatus] = useState("");
  const [participantsState, setParticipantsState] = useState({ isLoading: false, error: "", data: null });
  // The participant-budget policy shown on the Settings tab (loaded when it opens).
  const [budgetState, setBudgetState] = useState({ isLoading: false, error: "", data: null });
  // A typed budget failure from create/clone, shown on the form's budget field.
  const [createBudgetError, setCreateBudgetError] = useState("");
  // Collapsing the study list gives dashboards the full width (remembered per browser).
  const [listCollapsed, setListCollapsed] = useState(() => {
    try {
      return localStorage.getItem(LIST_COLLAPSED_KEY) === "1";
    } catch (_) {
      return false;
    }
  });
  // The all-time analytics summary: the overview and the analytics tab's
  // "All time" range share it, so opening the tab does not refetch it.
  const [summaryState, setSummaryState] = useState({ isLoading: false, error: "", data: null });
  // Bumped by every study reload so the selected study's participants and
  // analytics refresh with it (not only when another study is selected).
  const [refreshToken, setRefreshToken] = useState(0);
  const selectedRef = useRef(selectedStudyId);
  selectedRef.current = selectedStudyId;
  // `navigate` changes identity on every navigation outside a data router; an
  // effect that listed it would re-run the account check after each URL sync.
  const navigateRef = useRef(navigate);
  navigateRef.current = navigate;
  // Only the latest request of each kind may update the page.
  const participantsRequest = useRef(0);
  const summaryRequest = useRef(0);
  const budgetRequest = useRef(0);
  // "<study>:<refresh token>" of the summary in summaryState (see below).
  const summaryKey = useRef("");

  const selectedStudy = useMemo(
    () => studies.find((study) => study.study_id === selectedStudyId) || null,
    [studies, selectedStudyId],
  );

  const loadStudies = useCallback(async (preferredStudyId) => {
    setIsLoading(true);
    const result = await listResearchStudies();
    if (result && result.ok) {
      setIsUnavailable(false);
      setIsForbidden(false);
      const nextStudies = Array.isArray(result.data) ? result.data : [];
      setStudies(nextStudies);
      const target = preferredStudyId || selectedRef.current;
      setSelectedStudyId(target || "");
      setRefreshToken((value) => value + 1);
    } else {
      setIsUnavailable(Boolean(result && result.missing));
      setIsForbidden(Boolean(result && (result.forbidden || result.status === 403)));
      setError((result && result.error) || "Research studies are unavailable.");
    }
    setIsLoading(false);
  }, []);

  // The account is resolved first: a participant who lands here (an old link,
  // a typed URL) gets the forbidden page from this one request instead of a
  // failing study list, profile list and analytics request each.
  useEffect(() => {
    let cancelled = false;
    Promise.resolve(getCurrentUser()).then((result) => {
      if (cancelled) return;
      const user = result && result.ok ? result.user : null;
      if (user && !isResearcher(user)) {
        // Participants have their own page; the forbidden state only shows
        // until the redirect lands.
        setIsForbidden(true);
        setIsLoading(false);
        navigateRef.current("/research/my-studies", { replace: true });
        return;
      }
      setIsAdmin(Boolean(user && user.is_admin === true));
      loadStudies();
      Promise.resolve(getAgentProfiles()).then((profilesResult) => {
        if (cancelled) return;
        if (profilesResult && profilesResult.ok) {
          setProfiles((profilesResult.data || []).filter((profile) => profile.is_active !== false));
        }
      });
    });
    return () => {
      cancelled = true;
    };
  }, [loadStudies]);

  // Follow browser navigation (back/forward) between study URLs, including
  // back to the bare list, and between tabs.
  useEffect(() => {
    const target = params.studyId || "";
    if (target !== selectedRef.current) setSelectedStudyId(target);
  }, [params.studyId]);

  const tabParam = searchParams.get("tab");
  useEffect(() => {
    setActiveTab(TABS.includes(tabParam) ? tabParam : "overview");
  }, [tabParam]);

  useEffect(() => {
    const persisted = selectedStudy?.kill_switch;
    const engaged = persisted?.switch_id && persisted.status !== "RELEASED";
    setKillSwitchId(engaged ? persisted.switch_id : "");
  }, [selectedStudy]);

  const loadParticipants = useCallback(async (studyId) => {
    if (!studyId) return;
    const requestId = participantsRequest.current + 1;
    participantsRequest.current = requestId;
    setParticipantsState((current) => ({ ...current, isLoading: true, error: "" }));
    const result = await getStudyParticipants(studyId);
    if (participantsRequest.current !== requestId || selectedRef.current !== studyId) return;
    if (result && result.ok) setParticipantsState({ isLoading: false, error: "", data: result.data });
    else setParticipantsState({ isLoading: false, error: (result && result.error) || "", data: null });
  }, []);

  const loadSummary = useCallback(async (studyId) => {
    if (!studyId) return;
    const requestId = summaryRequest.current + 1;
    summaryRequest.current = requestId;
    setSummaryState((current) => ({ ...current, isLoading: true, error: "" }));
    const result = await getStudyAnalyticsSummary(studyId, {});
    if (summaryRequest.current !== requestId || selectedRef.current !== studyId) return;
    if (result && result.ok) setSummaryState({ isLoading: false, error: "", data: result.data });
    else
      setSummaryState({
        isLoading: false,
        error: (result && result.error) || "Study analytics could not be loaded.",
        data: null,
      });
  }, []);

  const loadBudget = useCallback(async (studyId) => {
    if (!studyId) return;
    const requestId = budgetRequest.current + 1;
    budgetRequest.current = requestId;
    setBudgetState((current) => ({ ...current, isLoading: true, error: "" }));
    const result = await getStudyBudget(studyId);
    if (budgetRequest.current !== requestId || selectedRef.current !== studyId) return;
    if (result && result.ok) setBudgetState({ isLoading: false, error: "", data: result.data });
    else
      setBudgetState({
        isLoading: false,
        error: (result && result.error) || "The participant budgets could not be loaded.",
        data: null,
      });
  }, []);

  // A different study starts from a clean slate…
  useEffect(() => {
    setParticipantsState({ isLoading: false, error: "", data: null });
    setSummaryState({ isLoading: false, error: "", data: null });
    setBudgetState({ isLoading: false, error: "", data: null });
    summaryKey.current = "";
  }, [selectedStudyId]);

  // …and each tab loads only what it shows: both requests read every
  // telemetry event of the study, so they are not fetched speculatively.
  // The participants list is refetched whenever its tab opens (enrollment
  // changes at any time); the summary once per study and reload, shared by
  // the overview and the analytics tab's "All time" range.
  // Nothing loads before the study list does (a deep link would otherwise
  // fetch twice: once on mount and again when the list bumps the token).
  const showsParticipants = activeTab === "participants";
  const needsSummary = activeTab === "overview" || activeTab === "analytics";
  const studyReady = Boolean(selectedStudy);
  useEffect(() => {
    if (studyReady && showsParticipants) loadParticipants(selectedStudyId);
  }, [studyReady, selectedStudyId, refreshToken, showsParticipants, loadParticipants]);
  useEffect(() => {
    if (!studyReady || !needsSummary) return;
    const key = `${selectedStudyId}:${refreshToken}`;
    if (summaryKey.current === key) return;
    summaryKey.current = key;
    loadSummary(selectedStudyId);
  }, [studyReady, selectedStudyId, refreshToken, needsSummary, loadSummary]);
  // The budget policy is refetched whenever the Settings tab opens and after
  // every study reload (participant counts change as they join).
  const showsSettings = activeTab === "settings";
  useEffect(() => {
    if (studyReady && showsSettings) loadBudget(selectedStudyId);
  }, [studyReady, selectedStudyId, refreshToken, showsSettings, loadBudget]);

  const syncUrl = (studyId, tab, replace = false) => {
    const search = tab && tab !== "overview" ? `?tab=${tab}` : "";
    navigate(studyId ? `/research/studies/${encodeURIComponent(studyId)}${search}` : "/research/studies", { replace });
  };

  const openStudy = (study) => {
    setSelectedStudyId(study.study_id);
    setNotice("");
    setError("");
    syncUrl(study.study_id, activeTab);
  };

  const changeTab = (tab) => {
    setActiveTab(tab);
    syncUrl(selectedStudyId, tab, true);
  };

  const toggleList = () => {
    setListCollapsed((value) => {
      const next = !value;
      try {
        localStorage.setItem(LIST_COLLAPSED_KEY, next ? "1" : "0");
      } catch (_) {
        // Storage may be unavailable; the toggle still works for this visit.
      }
      return next;
    });
  };

  const openCreateForm = () => {
    setCloneSource(null);
    setIsCreating(true);
    setError("");
    setNotice("");
    setCreateBudgetError("");
  };

  const closeCreateForm = () => {
    setIsCreating(false);
    setCloneSource(null);
    setCreateBudgetError("");
  };

  // Opens the create form prefilled from the stopped source. The clone
  // endpoint copies the stored configuration and accepts only new profile
  // selections, so the copied fields are shown for reference.
  const openCloneForm = () => {
    if (!selectedStudy) return;
    setCloneSource(selectedStudy);
    setIsCreating(true);
    setError("");
    setNotice("");
    setCreateBudgetError("");
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const handleCreate = async (form) => {
    if (cloneSource && form.profileIds.length === 0) {
      setError("Select at least one agent profile; a clone without profile selections cannot be joined.");
      return;
    }
    const source = cloneSource;
    setIsBusy(true);
    setError("");
    setNotice("");
    setCreateBudgetError("");
    const result = source
      ? await cloneResearchStudy(source.study_id, {
          profileIds: form.profileIds,
          // Omitted, the clone keeps the source study's default budget.
          ...(form.defaultBudgetUsd ? { defaultBudgetUsd: form.defaultBudgetUsd } : {}),
        })
      : await createResearchStudy(form);
    if (result && result.ok) {
      setIsCreating(false);
      setCloneSource(null);
      const created = result.data?.study || result.data;
      if (source) {
        setNotice(
          "Clone created in Draft state with the selected agent profiles. Name, description, schedule, telemetry policy, and session policy were copied; participants, consent, assignments, telemetry data, join code, and study ID were not.",
        );
      } else {
        setNotice("Study created in Draft state.");
      }
      setActiveTab("overview");
      await loadStudies(created?.study_id);
      if (created?.study_id) syncUrl(created.study_id, "overview");
    } else if (result && CREATE_BUDGET_CODES.includes(result.code)) {
      // A typed budget failure belongs on the budget field, not the banner.
      setCreateBudgetError(result.error || "The default budget per participant is invalid.");
    } else if (result && (result.code === "SESSION_POLICY_INVALID" || result.code === "TELEMETRY_POLICY_INVALID")) {
      setError(`${result.code}: ${result.error || "invalid policy"}`);
    } else {
      setError((result && result.error) || (source ? "Study could not be cloned." : "Study could not be created."));
    }
    setIsBusy(false);
  };

  const handleMetadataSave = async (metadata) => {
    if (!selectedStudy) return;
    setIsBusy(true);
    setError("");
    setNotice("");
    const result = await updateResearchStudyMetadata(selectedStudy.study_id, metadata);
    if (result && result.ok) {
      setNotice("Study metadata updated.");
      await loadStudies();
    } else {
      setError(METADATA_ERRORS[result && result.code] || (result && result.error) || "Study metadata could not be updated.");
    }
    setIsBusy(false);
  };

  const handleStop = async () => {
    if (
      !selectedStudy ||
      !window.confirm(
        "Stop this study permanently? This is terminal, closes collection, and retains existing research data without deleting it.",
      )
    ) {
      return;
    }
    setIsBusy(true);
    setError("");
    setNotice("");
    const result = await stopResearchStudy(selectedStudy.study_id);
    if (result && result.ok) {
      setNotice("Study stopped. Collection is revoked and retained data remains available.");
      await loadStudies();
    } else {
      setError((result && result.error) || "Study could not be stopped.");
    }
    setIsBusy(false);
  };

  // Resolves true only when the switch was engaged or released.
  const handleKillSwitch = async (reason) => {
    if (!selectedStudy) return false;
    if (selectedStudy.research_status === "STUDY_STOPPED") {
      setError("This study is stopped permanently; releasing a switch cannot reopen it.");
      return false;
    }
    const persistedSwitchId = selectedStudy.kill_switch?.switch_id;
    const persistedEngaged = persistedSwitchId && selectedStudy.kill_switch?.status !== "RELEASED";
    const activeSwitchId = persistedEngaged ? persistedSwitchId : killSwitchId;
    const confirmText = `Engage the kill switch for "${selectedStudy.name || "this study"}"? Reason: ${reason}`;
    if (!activeSwitchId && (!reason || !window.confirm(confirmText))) {
      return false;
    }
    setIsBusy(true);
    setError("");
    const result = activeSwitchId
      ? await releaseResearchKillSwitch(activeSwitchId)
      : await engageResearchKillSwitch(selectedStudy.study_id, reason);
    const succeeded = Boolean(result && result.ok);
    if (succeeded) {
      setKillSwitchId(activeSwitchId ? "" : result.data.switch_id || "");
      setNotice(activeSwitchId ? "Kill switch released." : "Kill switch engaged.");
      await loadStudies(selectedStudy.study_id);
    } else {
      setError((result && result.error) || "Kill switch operation failed.");
    }
    setIsBusy(false);
    return succeeded;
  };

  const handleBudgetSave = async (changes) => {
    if (!selectedStudy) return false;
    setIsBusy(true);
    setError("");
    setNotice("");
    const result = await updateStudyBudget(selectedStudy.study_id, changes);
    const succeeded = Boolean(result && result.ok);
    if (succeeded) {
      setBudgetState({ isLoading: false, error: "", data: result.data });
      setNotice("Participant budget defaults saved.");
      // The study payload carries the policy too (overview, create-form clone prefill).
      await loadStudies(selectedStudy.study_id);
    } else {
      setError(BUDGET_ERRORS[result && result.code] || (result && result.error) || "The budget could not be saved.");
    }
    setIsBusy(false);
    return succeeded;
  };

  // Resolves true when the default was applied (the reason input is cleared).
  const handleApplyDefault = async ({ reason, idempotencyKey }) => {
    if (!selectedStudy) return false;
    setIsBusy(true);
    setError("");
    setNotice("");
    const result = await applyStudyDefaultBudget(selectedStudy.study_id, { reason, idempotencyKey });
    const succeeded = Boolean(result && result.ok);
    if (succeeded) {
      const applied = Number(result.data?.applied) || 0;
      const skipped = Number(result.data?.skipped) || 0;
      setNotice(
        `Applied the default budget to ${applied} participant${applied === 1 ? "" : "s"}${
          skipped ? ` (${skipped} left unchanged)` : ""
        }.`,
      );
      await loadBudget(selectedStudy.study_id);
    } else {
      setError(BUDGET_ERRORS[result && result.code] || (result && result.error) || "The default budget could not be applied.");
    }
    setIsBusy(false);
    return succeeded;
  };

  const filteredStudies = useMemo(() => {
    const needle = listQuery.trim().toLowerCase();
    return studies.filter((study) => {
      if (listStatus && (study.research_status || "DRAFT") !== listStatus) return false;
      if (!needle) return true;
      return [study.name, study.description, study.join_code]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(needle));
    });
  }, [studies, listQuery, listStatus]);

  const arms = useMemo(
    () =>
      selectedStudy
        ? armsForStudy(selectedStudy, participantsState.data?.arms || summaryState.data?.arms || [])
        : [],
    [selectedStudy, participantsState.data, summaryState.data],
  );

  const participantCount = Array.isArray(participantsState.data?.participants)
    ? participantsState.data.participants.length
    : selectedStudy?.enrollment_count ?? null;

  const tabs = [
    { id: "overview", label: "Overview", icon: "overview" },
    { id: "participants", label: "Participants", icon: "users", count: participantCount ?? undefined },
    { id: "analytics", label: "Analytics", icon: "usage" },
    { id: "settings", label: "Settings", icon: "config" },
  ];

  if (isForbidden) {
    return (
      <section className="ui-page research-page" aria-labelledby="research-studies-title">
        <PageHeader titleId="research-studies-title" title="Research studies" />
        <p className="research-error" role="alert">
          You do not have permission to view research studies.
        </p>
        <p className="ui-muted">
          Ask an administrator to grant research access to your account. Looking for a study you joined? Open{" "}
          <a href="/research/my-studies">My studies</a>.
        </p>
      </section>
    );
  }

  return (
    <section className="ui-page research-page" aria-labelledby="research-studies-title">
      <PageHeader
        titleId="research-studies-title"
        title="Research studies"
        description="Create a study once, share its join code, follow enrollment and telemetry per arm, and stop it when collection ends."
        actions={
          <button type="button" className="primary-button" onClick={openCreateForm} disabled={isBusy}>
            <Icon name="plus" size={15} />
            New study
          </button>
        }
      />

      {isUnavailable ? (
        <p className="research-error" role="alert">
          The research control plane is unavailable on this server.
        </p>
      ) : null}
      {error && !isUnavailable ? (
        <p className="research-error" role="alert">
          {error}
        </p>
      ) : null}
      {notice ? (
        <p className="research-notice" role="status">
          {notice}
        </p>
      ) : null}

      {isCreating ? (
        <StudyCreateForm
          key={cloneSource ? `clone-${cloneSource.study_id}` : "create"}
          profiles={profiles}
          cloneSource={cloneSource}
          isBusy={isBusy}
          onSubmit={handleCreate}
          onCancel={closeCreateForm}
          budgetError={createBudgetError}
        />
      ) : null}

      <div className={`research-layout${listCollapsed && selectedStudy ? " is-list-collapsed" : ""}`}>
        <section className="ui-card research-card study-list-panel" aria-label="Study list">
          <div className="research-card-header study-list-header">
            <h3 className="ui-card-title">
              Studies <span className="ui-subtle">{studies.length ? `(${studies.length})` : ""}</span>
            </h3>
            <div className="ui-row">
              <button type="button" className="secondary-button button-sm" onClick={() => loadStudies()} disabled={isBusy}>
                <Icon name="refresh" size={14} />
                Refresh
              </button>
              {selectedStudy ? (
                <button
                  type="button"
                  className="icon-button"
                  onClick={toggleList}
                  aria-label="Hide the study list"
                  title="Hide the study list"
                >
                  <Icon name="chevronLeft" size={16} />
                </button>
              ) : null}
            </div>
          </div>
          <div className="study-list-filters">
            <div className="ui-search">
              <Icon name="search" size={15} />
              <input
                className="ui-input"
                type="search"
                value={listQuery}
                onChange={(event) => setListQuery(event.target.value)}
                placeholder="Search studies"
                aria-label="Search studies"
              />
            </div>
            <div className="ui-segmented study-status-filter" role="group" aria-label="Filter by status">
              {STATUS_FILTERS.map((option) => (
                <button
                  key={option.value || "all"}
                  type="button"
                  aria-pressed={listStatus === option.value}
                  className={listStatus === option.value ? "is-active" : undefined}
                  onClick={() => setListStatus(option.value)}
                >
                  {option.label}
                </button>
              ))}
            </div>
          </div>
          {isLoading && studies.length === 0 ? (
            <div className="study-list-empty">
              <Loading label="Loading research studies..." />
            </div>
          ) : !isUnavailable && studies.length === 0 ? (
            <EmptyState
              icon="flask"
              title="No research studies yet."
              action={
                <button type="button" className="primary-button" onClick={openCreateForm}>
                  <Icon name="plus" size={15} />
                  Create your first study
                </button>
              }
            >
              A study bundles agent profiles (arms), a telemetry policy and a join code for participants.
            </EmptyState>
          ) : filteredStudies.length === 0 ? (
            <EmptyState icon="search" title="No studies match" />
          ) : (
            <ul className="research-list">
              {filteredStudies.map((study) => {
                const selected = study.study_id === selectedStudyId;
                return (
                  <li key={study.study_id}>
                    <button
                      type="button"
                      className={`research-list-item${selected ? " is-selected" : ""}`}
                      onClick={() => openStudy(study)}
                      aria-current={selected ? "true" : undefined}
                    >
                      <span className="research-list-item-top">
                        <strong>{study.name}</strong>
                        <span className={statusClass(study.research_status)}>
                          {STATUS_LABELS[study.research_status] || study.research_status || "Draft"}
                        </span>
                      </span>
                      <small className="research-list-item-description">{study.description || "No description"}</small>
                      <span className="research-list-item-meta">
                        <span>
                          <Icon name="users" size={13} />
                          {formatNumber(study.enrollment_count ?? 0)} enrolled
                        </span>
                        <span>
                          <Icon name="layers" size={13} />
                          {(study.profile_selections || []).length} arm{(study.profile_selections || []).length === 1 ? "" : "s"}
                        </span>
                        <span>
                          <Icon name="calendar" size={13} />
                          {formatDate(study.created_at)}
                        </span>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </section>

        {selectedStudy ? (
          <section className="ui-card research-card study-detail" aria-label="Study details">
            <div className="study-detail-header">
              <div className="study-detail-heading">
                <div className="ui-row">
                  <h3 className="study-detail-title">{selectedStudy.name}</h3>
                  <span className={statusClass(selectedStudy.research_status)}>
                    {STATUS_LABELS[selectedStudy.research_status] || selectedStudy.research_status || "Draft"}
                  </span>
                </div>
                {selectedStudy.description ? <p className="study-detail-description">{selectedStudy.description}</p> : null}
              </div>
              <div className="ui-page-actions">
                {listCollapsed ? (
                  <button type="button" className="secondary-button button-sm" onClick={toggleList}>
                    <Icon name="chevronRight" size={14} />
                    All studies
                  </button>
                ) : null}
                {selectedStudy.research_status === "STUDY_STOPPED" ? (
                  <button type="button" className="primary-button" onClick={openCloneForm} disabled={isBusy}>
                    <Icon name="copy" size={15} />
                    Clone as new Draft
                  </button>
                ) : null}
              </div>
            </div>

            <dl className="study-detail-facts">
              <div>
                <dt>Status</dt>
                <dd>{STATUS_LABELS[selectedStudy.research_status] || selectedStudy.research_status || "Draft"}</dd>
              </div>
              <div className="study-join-code">
                <dt>Join code</dt>
                <dd>
                  <code>{selectedStudy.join_code || "Not available"}</code>
                </dd>
                {selectedStudy.join_code ? <CopyButton value={selectedStudy.join_code} label="Copy join code" /> : null}
              </div>
              <div>
                <dt>Participants</dt>
                <dd>
                  {selectedStudy.enrollment_count ?? "—"}
                  {selectedStudy.active_enrollment_count !== undefined && selectedStudy.active_enrollment_count !== null
                    ? ` (${selectedStudy.active_enrollment_count} active)`
                    : ""}
                </dd>
              </div>
              <div>
                <dt>Schedule</dt>
                <dd>{scheduleText(selectedStudy)}</dd>
              </div>
            </dl>

            <Tabs tabs={tabs} active={activeTab} onChange={changeTab} label="Study sections" idPrefix="study-tab" />

            <TabPanel id={activeTab} idPrefix="study-tab">
              {activeTab === "overview" ? (
                <StudyOverview
                  study={selectedStudy}
                  arms={arms}
                  participantsData={participantsState.data}
                  onReloadSummary={() => loadSummary(selectedStudy.study_id)}
                  summaryError={summaryState.error}
                  summary={summaryState.data}
                  onOpenTab={changeTab}
                />
              ) : null}
              {activeTab === "participants" ? (
                <StudyParticipants
                  study={selectedStudy}
                  arms={arms}
                  state={participantsState}
                  onReload={() => loadParticipants(selectedStudy.study_id)}
                />
              ) : null}
              {activeTab === "analytics" ? (
                <StudyAnalytics
                  key={selectedStudy.study_id}
                  study={selectedStudy}
                  allTime={summaryState}
                  onReloadAllTime={() => loadSummary(selectedStudy.study_id)}
                  refreshToken={refreshToken}
                />
              ) : null}
              {activeTab === "settings" ? (
                <StudySettings
                  key={selectedStudy.study_id}
                  study={selectedStudy}
                  isAdmin={isAdmin}
                  isBusy={isBusy}
                  onSaveMetadata={handleMetadataSave}
                  onStop={handleStop}
                  onKillSwitch={handleKillSwitch}
                  budget={budgetState}
                  onReloadBudget={() => loadBudget(selectedStudy.study_id)}
                  onSaveBudget={handleBudgetSave}
                  onApplyDefault={handleApplyDefault}
                />
              ) : null}
            </TabPanel>
          </section>
        ) : studies.length > 0 ? (
          <section className="ui-card study-detail study-detail-placeholder" aria-label="No study selected">
            <EmptyState icon="flask" title="Select a study">
              Pick a study on the left to see its participants, analytics and settings.
            </EmptyState>
          </section>
        ) : null}
      </div>
    </section>
  );
};

export default ResearchStudies;
