import React, { useEffect, useState } from "react";
import { listAccounts, setResearcherEnabled } from "../utils/api";
import "./AdminPages.css";

/**
 * Admin-only account list. Every account is shown (not only the already-enabled
 * researchers) because this is where an administrator enables researcher
 * access. The toggle is not optimistic: a successful PUT triggers a refetch so
 * the row always reflects server state.
 */
const AdminResearchers = () => {
  const [accounts, setAccounts] = useState([]);
  const [isLoading, setIsLoading] = useState(true);
  const [busyUserId, setBusyUserId] = useState("");
  const [error, setError] = useState("");
  const [fieldErrors, setFieldErrors] = useState([]);
  const [notice, setNotice] = useState("");

  const loadAccounts = async () => {
    setIsLoading(true);
    setError("");
    setFieldErrors([]);
    const response = await listAccounts({ limit: 100 });
    if (response.ok) {
      setAccounts(Array.isArray(response.data) ? response.data : []);
    } else {
      setError(response.error);
      setFieldErrors(Array.isArray(response.errors) ? response.errors : []);
    }
    setIsLoading(false);
  };

  useEffect(() => {
    loadAccounts();
    // Initial load only; the toggle explicitly refetches after a successful write.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const toggleResearcher = async (account, enabled) => {
    setBusyUserId(account.user_id);
    setError("");
    setFieldErrors([]);
    setNotice("");
    const response = await setResearcherEnabled(account.user_id, enabled);
    if (response.ok) {
      setNotice(
        `${account.email} is ${enabled ? "now enabled" : "no longer enabled"} for research.`,
      );
      await loadAccounts();
    } else {
      setError(response.error);
      setFieldErrors(Array.isArray(response.errors) ? response.errors : []);
    }
    setBusyUserId("");
  };

  return (
    <section className="admin-page" aria-labelledby="admin-researchers-title">
      <header className="research-header">
        <div>
          <h2 id="admin-researchers-title">Accounts</h2>
          <p>
            Every account, newest first. Enable the “Researcher” flag for an
            account that should own private profiles and create studies. Account
            administration happens out of band; this view can only grant or
            revoke researcher access.
          </p>
        </div>
        <button
          type="button"
          className="secondary-button"
          onClick={loadAccounts}
          disabled={isLoading}
        >
          Refresh
        </button>
      </header>

      {isLoading && (
        <p className="research-hint" role="status">
          Loading accounts...
        </p>
      )}
      {error && (
        <p className="research-error" role="alert">
          {error}
        </p>
      )}
      {fieldErrors.length > 0 && (
        <ul className="research-error" role="alert">
          {fieldErrors.map((item, position) => (
            <li key={`${item.field || "error"}-${position}`}>
              {item.field ? `${item.field}: ` : ""}
              {item.message || item.code || "invalid value"}
            </li>
          ))}
        </ul>
      )}
      {notice && (
        <p className="research-notice" role="status">
          {notice}
        </p>
      )}

      {!isLoading && !error && accounts.length === 0 && (
        <p className="research-hint">No accounts found.</p>
      )}

      {accounts.length > 0 && (
        <div className="research-card">
          <table className="admin-table">
            <thead>
              <tr>
                <th scope="col">Email</th>
                <th scope="col">Name</th>
                <th scope="col">Admin</th>
                <th scope="col">Verified</th>
                <th scope="col">Researcher</th>
              </tr>
            </thead>
            <tbody>
              {accounts.map((account) => (
                <tr key={account.user_id}>
                  <td>{account.email}</td>
                  <td>{account.name || "—"}</td>
                  <td>{account.is_admin ? "Yes" : "No"}</td>
                  <td>{account.verified ? "Yes" : "No"}</td>
                  <td>
                    <label className="admin-toggle">
                      <input
                        type="checkbox"
                        aria-label={`Researcher access for ${account.email}`}
                        checked={Boolean(account.can_research)}
                        disabled={busyUserId === account.user_id}
                        onChange={(event) =>
                          toggleResearcher(account, event.target.checked)
                        }
                      />
                      {account.can_research ? "Enabled" : "Disabled"}
                    </label>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
};

export default AdminResearchers;
