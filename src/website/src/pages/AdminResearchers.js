import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { listAccounts, listResearchStudies, setResearcherEnabled } from "../utils/api";
import Icon from "../components/common/Icon";
import {
  Alert,
  Badge,
  Card,
  EmptyState,
  FieldErrors,
  KpiTile,
  Loading,
  PageHeader,
  Switch,
} from "../components/common/ui";
import { formatDate, formatNumber } from "../utils/format";
import "./AdminPages.css";

const PAGE_SIZE = 50;

const ROLE_OPTIONS = [
  { value: "all", label: "All roles" },
  { value: "admin", label: "Administrators" },
  { value: "researcher", label: "Researchers" },
  { value: "participant", label: "Participants" },
];

const ENROLLMENT_OPTIONS = [
  { value: "any", label: "Any enrollment" },
  { value: "enrolled", label: "In an active study" },
  { value: "not_enrolled", label: "Not in an active study" },
];

const ENROLLMENT_TONES = {
  ACTIVE: "success",
  COMPLETED: "info",
  REVOKED: "danger",
  STUDY_STOPPED: "neutral",
};

const ENROLLMENT_LABELS = {
  ACTIVE: "Active",
  COMPLETED: "Completed",
  REVOKED: "Revoked",
  STUDY_STOPPED: "Study stopped",
};

const initials = (account) => {
  const source = (account.name || account.email || "?").trim();
  const parts = source.split(/[\s@._-]+/).filter(Boolean);
  return (parts.length > 1 ? parts[0][0] + parts[1][0] : source.slice(0, 2)).toUpperCase();
};

const RoleBadges = ({ account }) => (
  <div className="ui-row">
    {account.is_admin ? <Badge tone="violet">Admin</Badge> : null}
    {account.can_research && !account.is_admin ? <Badge tone="primary">Researcher</Badge> : null}
    {!account.is_admin && !account.can_research ? <Badge tone="neutral">Participant</Badge> : null}
  </div>
);

const StudyCell = ({ enrollments }) => {
  const list = Array.isArray(enrollments) ? enrollments : [];
  if (list.length === 0) return <span className="ui-subtle">Not enrolled</span>;
  return (
    <ul className="account-studies">
      {list.slice(0, 3).map((enrollment, index) => (
        <li key={`${enrollment.study_id}-${index}`}>
          <Link
            to={`/research/studies/${encodeURIComponent(enrollment.study_id)}`}
            className="account-study-link"
            title={`Open ${enrollment.study_name || "study"}`}
          >
            {enrollment.study_name || "Unnamed study"}
          </Link>
          <Badge tone={ENROLLMENT_TONES[enrollment.status] || "neutral"}>
            {ENROLLMENT_LABELS[enrollment.status] || enrollment.status || "Unknown"}
          </Badge>
        </li>
      ))}
      {list.length > 3 ? <li className="ui-subtle">+{list.length - 3} more</li> : null}
    </ul>
  );
};

/**
 * Admin-only account list. Every account is shown (not only researchers)
 * because this is where research access is granted. Writes are not
 * optimistic: a successful change triggers a refetch so the row always
 * reflects server state.
 */
const AdminResearchers = () => {
  const [accounts, setAccounts] = useState([]);
  const [total, setTotal] = useState(0);
  const [summary, setSummary] = useState(null);
  const [studies, setStudies] = useState([]);
  const [filters, setFilters] = useState({ q: "", role: "all", enrollment: "any", studyId: "" });
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [isLoading, setIsLoading] = useState(true);
  const [hasLoaded, setHasLoaded] = useState(false);
  const [busyUserId, setBusyUserId] = useState("");
  const [error, setError] = useState("");
  const [fieldErrors, setFieldErrors] = useState([]);
  const [notice, setNotice] = useState("");
  const requestId = useRef(0);

  const loadAccounts = useCallback(async (activeFilters, activeOffset) => {
    const current = ++requestId.current;
    setIsLoading(true);
    setError("");
    setFieldErrors([]);
    const response = await listAccounts({ limit: PAGE_SIZE, offset: activeOffset, ...activeFilters });
    if (current !== requestId.current) return;
    if (response.ok) {
      const rows = Array.isArray(response.data) ? response.data : [];
      setAccounts(rows);
      setTotal(typeof response.total === "number" ? response.total : rows.length);
    } else {
      setError(response.error);
      setFieldErrors(Array.isArray(response.errors) ? response.errors : []);
    }
    setIsLoading(false);
    setHasLoaded(true);
  }, []);

  // Headline counts come from the same filtered endpoint (limit=1 → total).
  const loadSummary = useCallback(async () => {
    const [all, researchers, admins, enrolled] = await Promise.all([
      listAccounts({ limit: 1 }),
      listAccounts({ limit: 1, role: "researcher" }),
      listAccounts({ limit: 1, role: "admin" }),
      listAccounts({ limit: 1, enrollment: "enrolled" }),
    ]);
    const counted = [all, researchers, admins, enrolled];
    if (counted.every((result) => result && result.ok && typeof result.total === "number")) {
      setSummary({
        total: all.total,
        researchers: researchers.total,
        admins: admins.total,
        enrolled: enrolled.total,
      });
    }
  }, []);

  useEffect(() => {
    loadAccounts(filters, offset);
  }, [filters, offset, loadAccounts]);

  useEffect(() => {
    loadSummary();
    listResearchStudies().then((result) => {
      if (result && result.ok) setStudies(Array.isArray(result.data) ? result.data : []);
    });
  }, [loadSummary]);

  // Debounce free-text search so typing does not fire a request per key.
  useEffect(() => {
    const handle = setTimeout(() => {
      setFilters((current) => (current.q === search ? current : { ...current, q: search }));
      setOffset(0);
    }, 300);
    return () => clearTimeout(handle);
  }, [search]);

  const updateFilter = (key, value) => {
    setFilters((current) => ({ ...current, [key]: value }));
    setOffset(0);
  };

  const resetFilters = () => {
    setSearch("");
    setFilters({ q: "", role: "all", enrollment: "any", studyId: "" });
    setOffset(0);
  };

  const filtersActive =
    filters.q || filters.role !== "all" || filters.enrollment !== "any" || filters.studyId;

  const toggleResearcher = async (account, enabled) => {
    if (
      !enabled &&
      !window.confirm(
        `Revoke research access from ${account.email}? They will no longer be able to create agent profiles or studies. Their existing studies are kept.`,
      )
    ) {
      return;
    }
    setBusyUserId(account.user_id);
    setError("");
    setFieldErrors([]);
    setNotice("");
    const response = await setResearcherEnabled(account.user_id, enabled);
    if (response.ok) {
      setNotice(
        enabled
          ? `Granted research access to ${account.email}.`
          : `Revoked research access from ${account.email}.`,
      );
      await Promise.all([loadAccounts(filters, offset), loadSummary()]);
    } else {
      setError(response.error);
      setFieldErrors(Array.isArray(response.errors) ? response.errors : []);
    }
    setBusyUserId("");
  };

  const studyOptions = useMemo(
    () =>
      [...studies]
        .filter((study) => study && study.study_id)
        .sort((a, b) => String(a.name || "").localeCompare(String(b.name || ""))),
    [studies],
  );

  const rangeStart = total === 0 ? 0 : offset + 1;
  const rangeEnd = Math.min(offset + accounts.length, total);

  return (
    <section className="ui-page admin-page" aria-labelledby="admin-researchers-title">
      <PageHeader
        titleId="admin-researchers-title"
        title="Accounts"
        description="Every account on this server, newest first. Grant research access to let an account create agent profiles and run studies; participants join studies with a join code and need no access change."
        actions={
          <button
            type="button"
            className="secondary-button"
            onClick={() => {
              loadAccounts(filters, offset);
              loadSummary();
            }}
            disabled={isLoading}
          >
            <Icon name="refresh" size={15} />
            Refresh
          </button>
        }
      />

      {summary ? (
        <div className="ui-kpis">
          <KpiTile label="Accounts" value={formatNumber(summary.total)} />
          <KpiTile label="Researchers" value={formatNumber(summary.researchers)} detail="Research access granted" />
          <KpiTile label="Administrators" value={formatNumber(summary.admins)} detail="Full access" />
          <KpiTile label="In an active study" value={formatNumber(summary.enrolled)} detail="Active enrollment" />
        </div>
      ) : null}

      {error ? (
        <p className="research-error" role="alert">
          {error}
        </p>
      ) : null}
      <FieldErrors errors={fieldErrors} />
      {notice ? (
        <p className="research-notice" role="status">
          {notice}
        </p>
      ) : null}

      <Card className="admin-accounts-card">
        <div className="ui-toolbar" role="search">
          <div className="ui-search">
            <Icon name="search" size={15} />
            <input
              className="ui-input"
              type="search"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search by name or email"
              aria-label="Search accounts"
            />
          </div>
          <select
            className="ui-select"
            value={filters.role}
            onChange={(event) => updateFilter("role", event.target.value)}
            aria-label="Filter by role"
          >
            {ROLE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <select
            className="ui-select"
            value={filters.enrollment}
            onChange={(event) => updateFilter("enrollment", event.target.value)}
            aria-label="Filter by enrollment"
          >
            {ENROLLMENT_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <select
            className="ui-select"
            value={filters.studyId}
            onChange={(event) => updateFilter("studyId", event.target.value)}
            aria-label="Filter by study"
          >
            <option value="">All studies</option>
            {studyOptions.map((study) => (
              <option key={study.study_id} value={study.study_id}>
                {study.name || study.study_id}
              </option>
            ))}
          </select>
          {filtersActive ? (
            <button type="button" className="ghost-button" onClick={resetFilters}>
              Clear filters
            </button>
          ) : null}
          <span className="ui-toolbar-meta" aria-live="polite">
            {hasLoaded ? `${formatNumber(total)} account${total === 1 ? "" : "s"}` : ""}
          </span>
        </div>

        {isLoading && !hasLoaded ? <Loading label="Loading accounts..." /> : null}

        {hasLoaded && !error && accounts.length === 0 ? (
          <EmptyState icon="users" title={filtersActive ? "No matching accounts" : "No accounts found."}>
            {filtersActive ? "Try a different search or clear the filters." : null}
          </EmptyState>
        ) : null}

        {accounts.length > 0 ? (
          <div className={`ui-table-wrap${isLoading ? " is-refreshing" : ""}`}>
            <table className="ui-table admin-accounts-table">
              <thead>
                <tr>
                  <th scope="col">Account</th>
                  <th scope="col">Role</th>
                  <th scope="col">Email</th>
                  <th scope="col">Study</th>
                  <th scope="col">Joined</th>
                  <th scope="col">Research access</th>
                </tr>
              </thead>
              <tbody>
                {accounts.map((account) => (
                  <tr key={account.user_id}>
                    <td>
                      <div className="account-identity">
                        <span className="account-avatar" aria-hidden="true">
                          {initials(account)}
                        </span>
                        <div className="ui-cell-stack">
                          <span className="ui-cell-primary">{account.name || "—"}</span>
                          <span className="ui-subtle account-email">{account.email}</span>
                        </div>
                      </div>
                    </td>
                    <td>
                      <RoleBadges account={account} />
                    </td>
                    <td>
                      {account.verified ? (
                        <Badge tone="success">Verified</Badge>
                      ) : (
                        <Badge tone="warning">Unverified</Badge>
                      )}
                    </td>
                    <td>
                      <StudyCell enrollments={account.enrollments} />
                    </td>
                    <td className="ui-nowrap ui-muted">{formatDate(account.joined_at)}</td>
                    <td>
                      {account.is_admin ? (
                        <span className="account-access-admin" title="Administrators always have research access">
                          <Icon name="shield" size={14} />
                          Included with admin
                        </span>
                      ) : (
                        <Switch
                          checked={Boolean(account.can_research)}
                          disabled={busyUserId === account.user_id}
                          ariaLabel={`Researcher access for ${account.email}`}
                          label={account.can_research ? "Granted" : "Not granted"}
                          onChange={(checked) => toggleResearcher(account, checked)}
                        />
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}

        {total > PAGE_SIZE ? (
          <div className="ui-table-footer">
            <span>
              Showing {formatNumber(rangeStart)}–{formatNumber(rangeEnd)} of {formatNumber(total)}
            </span>
            <div className="ui-row">
              <button
                type="button"
                className="secondary-button button-sm"
                onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}
                disabled={offset === 0 || isLoading}
              >
                <Icon name="chevronLeft" size={14} />
                Previous
              </button>
              <button
                type="button"
                className="secondary-button button-sm"
                onClick={() => setOffset((value) => value + PAGE_SIZE)}
                disabled={offset + PAGE_SIZE >= total || isLoading}
              >
                Next
                <Icon name="chevronRight" size={14} />
              </button>
            </div>
          </div>
        ) : null}
      </Card>

      <Alert tone="info" live={false}>
        Participant study codes and assigned agent profiles are intentionally not shown here, so
        this list cannot be used to de-pseudonymize study data. Open a study to see its
        participants.
      </Alert>
    </section>
  );
};

export default AdminResearchers;
