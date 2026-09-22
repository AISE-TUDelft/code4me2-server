import React, { useEffect, useMemo, useState } from "react";
import {
  disableAgentRelease,
  importAgentReleaseUrl,
  getRegisteredRelease,
  getReleaseCatalogue,
  importAgentRelease,
  listRegisteredReleases,
} from "../utils/api";
import "./research/research.css";
import "./AdminPages.css";

const PLACEHOLDER_DIGEST = /^0{64}$/;
const PENDING_VERSION = "pending-release";

const normalizeOs = (value) => {
  const text = String(value || "").trim().toLowerCase();
  if (["macos", "darwin", "mac", "macosx"].includes(text)) return "macos";
  if (text.startsWith("win")) return "windows";
  if (text.startsWith("linux")) return "linux";
  return text || "?";
};

const normalizeArch = (value) => {
  const text = String(value || "").trim().toLowerCase();
  if (["arm64", "aarch64"].includes(text)) return "arm64";
  if (["x64", "x86_64", "amd64"].includes(text)) return "x64";
  return text || "?";
};

const platformKey = (os, arch) =>
  `${normalizeOs(os)}-${normalizeArch(arch)}`;

const bareDigest = (value) => {
  const text = String(value || "").trim().toLowerCase();
  return text.startsWith("sha256:") ? text.slice("sha256:".length) : text;
};

// A manifest entry is a placeholder when its digest is all-zero or it still
// carries the pending-release version. The server rejects such a manifest
// outright; the UI flags it before the operator submits.
const isPlaceholderArtifact = (artifact) =>
  PLACEHOLDER_DIGEST.test(bareDigest(artifact.sha256)) ||
  String(artifact.version || "").trim() === PENDING_VERSION;

const declaredBasename = (artifact) => {
  const archive = String(artifact.archive || "").trim();
  return archive.split(/[\\/]/).pop();
};

const parseManifest = (text) => {
  if (!text || !text.trim()) return { ok: false, error: "" };
  try {
    const parsed = JSON.parse(text);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return { ok: false, error: "The manifest must be a JSON object." };
    }
    return { ok: true, manifest: parsed };
  } catch (_) {
    return { ok: false, error: "Invalid JSON. Fix the manifest before importing." };
  }
};

const AdminAgents = () => {
  const [releases, setReleases] = useState([]);
  const [versions, setVersions] = useState({});
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState("");
  const [fieldErrors, setFieldErrors] = useState([]);
  const [notice, setNotice] = useState("");

  const [selectedReleaseId, setSelectedReleaseId] = useState("");
  const [detail, setDetail] = useState(null);
  const [detailError, setDetailError] = useState("");
  const [isDisabling, setIsDisabling] = useState(false);

  const [manifestUrl, setManifestUrl] = useState("");
  const [archiveUrls, setArchiveUrls] = useState("");
  const [useUrls, setUseUrls] = useState(false);
  const [manifestText, setManifestText] = useState("");
  const [archiveFiles, setArchiveFiles] = useState([]);
  const [importResult, setImportResult] = useState(null);
  const [isImporting, setIsImporting] = useState(false);

  const loadReleases = async () => {
    setIsLoading(true);
    setError("");
    setFieldErrors([]);
    const response = await listRegisteredReleases();
    if (response.ok) {
      setReleases(Array.isArray(response.data) ? response.data : []);
    } else {
      setError(response.error);
      setFieldErrors(Array.isArray(response.errors) ? response.errors : []);
    }
    setIsLoading(false);
  };

  useEffect(() => {
    loadReleases();
    // The catalogue supplies the release version the compact list omits.
    getReleaseCatalogue().then((response) => {
      if (response && response.ok) {
        const next = {};
        (response.data || []).forEach((entry) => {
          next[entry.release_id] = entry.version || "";
        });
        setVersions(next);
      }
    });
  }, []);

  const openDetail = async (releaseId) => {
    setSelectedReleaseId(releaseId);
    setDetail(null);
    setDetailError("");
    const response = await getRegisteredRelease(releaseId);
    if (response.ok) {
      setDetail(response.model || response.release || null);
    } else {
      setDetailError(response.error);
    }
  };

  const parsedManifest = useMemo(
    () => parseManifest(manifestText),
    [manifestText],
  );

  const manifestArtifacts = useMemo(() => {
    if (!parsedManifest.ok) return [];
    const artifacts = parsedManifest.manifest.artifacts;
    return Array.isArray(artifacts) ? artifacts : [];
  }, [parsedManifest]);

  // A placeholder (all-zero digest / pending version) is a platform the build
  // did not produce; the server rejects the whole import for it. It must not be
  // uploaded, and the form stays disabled while one is present.
  const hasPlaceholder = useMemo(
    () => manifestArtifacts.some(isPlaceholderArtifact),
    [manifestArtifacts],
  );

  const requiredBasenames = useMemo(
    () =>
      manifestArtifacts
        .filter((artifact) => !isPlaceholderArtifact(artifact))
        .map(declaredBasename)
        .filter(Boolean),
    [manifestArtifacts],
  );

  const selectedBasenames = useMemo(
    () => archiveFiles.map((file) => file.name),
    [archiveFiles],
  );

  const missingArchives = useMemo(
    () => requiredBasenames.filter((name) => !selectedBasenames.includes(name)),
    [requiredBasenames, selectedBasenames],
  );

  const unexpectedArchives = useMemo(
    () => selectedBasenames.filter((name) => !requiredBasenames.includes(name)),
    [requiredBasenames, selectedBasenames],
  );

  const verifiedByArchive = useMemo(() => {
    const map = {};
    if (importResult && Array.isArray(importResult.verified_artifacts)) {
      importResult.verified_artifacts.forEach((entry) => {
        if (entry && entry.archive) map[entry.archive] = entry;
      });
    }
    return map;
  }, [importResult]);

  const handleImport = async (event) => {
    event.preventDefault();
    if (!useUrls && !parsedManifest.ok) {
      setError(parsedManifest.error || "Paste a runtime manifest first.");
      setFieldErrors([]);
      return;
    }
    setIsImporting(true);
    setError("");
    setFieldErrors([]);
    setNotice("");
    setImportResult(null);
    const response = useUrls
      ? await importAgentReleaseUrl({ manifest_url: manifestUrl, archive_urls: archiveUrls.split(/\s+/).filter(Boolean) })
      : await importAgentRelease({ manifest: manifestText, archives: archiveFiles });
    if (response.ok) {
      setImportResult(response.data);
      setNotice("Manifest accepted.");
      await loadReleases();
      const releaseId = response.data && response.data.release && response.data.release.release_id;
      if (releaseId) {
        await openDetail(releaseId);
      }
    } else {
      setError(response.error);
      setFieldErrors(Array.isArray(response.errors) ? response.errors : []);
    }
    setIsImporting(false);
  };

  const handleDisable = async () => {
    setIsDisabling(true);
    const response = await disableAgentRelease(selectedReleaseId);
    if (response.ok) {
      setNotice("Release permanently disabled.");
      await loadReleases();
      await openDetail(selectedReleaseId);
    } else {
      setError(response.error);
    }
    setIsDisabling(false);
  };

  return (
    <section className="admin-page" aria-labelledby="admin-agents-title">
      <header className="research-header">
        <div>
          <h2 id="admin-agents-title">Agent Catalogue</h2>
          <p>
            Import digest-pinned runtime releases from a producer manifest plus
            the exact archive bytes it declares. The server recomputes every
            digest and size. Releases with passing producer tests are ready to use.
          </p>
        </div>
        <button
          type="button"
          className="secondary-button"
          onClick={loadReleases}
          disabled={isLoading}
        >
          Refresh
        </button>
      </header>

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

      <section className="admin-section" aria-label="Releases">
        <h3>Releases</h3>
        {isLoading && (
          <p className="research-hint" role="status">
            Loading releases...
          </p>
        )}
        {!isLoading && releases.length === 0 && (
          <p className="research-hint">No registered releases yet.</p>
        )}
        {releases.length > 0 && (
          <table className="admin-table">
            <thead>
              <tr>
                <th scope="col">Release</th>
                <th scope="col">Agent</th>
                <th scope="col">Version</th>
                <th scope="col">Status</th>
                <th scope="col">Detail</th>
              </tr>
            </thead>
            <tbody>
              {releases.map((release) => (
                <tr key={release.release_id}>
                  <td>
                    <code>{release.release_id}</code>
                  </td>
                  <td>{release.agent_id}</td>
                  <td>{versions[release.release_id] || "—"}</td>
                  <td>
                    <span
                      className={
                        release.status === "QUALIFIED"
                          ? "admin-status-badge ready"
                          : "admin-status-badge not-ready"
                      }
                    >
                      {release.status}
                    </span>
                  </td>
                  <td>
                    <button
                      type="button"
                      className="secondary-button"
                      onClick={() => openDetail(release.release_id)}
                    >
                      View
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {detailError && (
          <p className="research-error" role="alert">
            {detailError}
          </p>
        )}
        {detail && (
          <div aria-label="Release detail">
            <h4>Release detail</h4>
            <dl className="research-metrics">
              <div>
                <dt>Release</dt>
                <dd>
                  <code>{detail.release_id}</code>
                </dd>
              </div>
              <div>
                <dt>Version</dt>
                <dd>{detail.version}</dd>
              </div>
              <div>
                <dt>Qualification</dt>
                <dd>{detail.qualification_status}</dd>
              </div>
              <div>
                <dt>Adapter</dt>
                <dd>
                  <code>{detail.adapter ? detail.adapter.digest : "—"}</code>
                </dd>
              </div>
            </dl>
            <table className="admin-table" aria-label="Platform tests">
              <thead><tr><th>Platform</th><th>Self-check</th><th>ACP initialize</th><th>Tested at</th></tr></thead>
              <tbody>{(detail.tests || []).map((test) => (
                <tr key={platformKey(test.os, test.arch)}>
                  <td>{platformKey(test.os, test.arch)}</td><td>{test.self_check}</td>
                  <td>{test.acp_initialize}</td><td>{test.ran_at}</td>
                </tr>
              ))}</tbody>
            </table>
            <p>Disabling permanently prevents this release from starting new runs.</p>
            <button type="button" className="secondary-button" onClick={handleDisable}
              disabled={isDisabling || detail.qualification_status === "DISABLED"}>
              Disable release
            </button>
          </div>
        )}
      </section>

      <form className="admin-section" onSubmit={handleImport}>
        <h3>Import a runtime manifest and its archives</h3>
        <label><input type="checkbox" checked={useUrls} onChange={(event) => setUseUrls(event.target.checked)} />Import from release URLs</label>
        {useUrls ? <div key="urls" className="admin-form-grid">
          <label>Manifest URL<input type="url" required value={manifestUrl} onChange={(event) => setManifestUrl(event.target.value)} /></label>
          <label>Archive URLs (one per line)<textarea required value={archiveUrls} onChange={(event) => setArchiveUrls(event.target.value)} /></label>
        </div> : <div key="uploads" className="admin-form-grid">
          <label className="admin-span">
            Manifest JSON file
            <input type="file" accept=".json,application/json" onChange={async (event) => {
              const file = event.target.files?.[0];
              if (file) setManifestText(await file.text());
            }} />
          </label>
          <label className="admin-span">
            Runtime manifest JSON
            <textarea
              aria-label="Runtime manifest JSON"
              value={manifestText}
              onChange={(event) => setManifestText(event.target.value)}
              rows={8}
              disabled={isImporting}
            />
          </label>
          <label className="admin-span">
            Agent archive files (one ZIP per declared platform)
            <input
              aria-label="Agent archive files"
              type="file"
              accept=".zip,application/zip"
              multiple
              onChange={(event) =>
                setArchiveFiles(Array.from(event.target.files || []))
              }
              disabled={isImporting}
            />
          </label>
        </div>}
        {!useUrls && parsedManifest.error && (
          <p className="research-error" role="alert">
            {parsedManifest.error}
          </p>
        )}
        {!useUrls && archiveFiles.length > 0 && missingArchives.length > 0 && (
          <p className="research-error" role="alert">
            Missing declared archive(s): {missingArchives.join(", ")}
          </p>
        )}
        {!useUrls && unexpectedArchives.length > 0 && (
          <p className="research-error" role="alert">
            Undeclared archive(s): {unexpectedArchives.join(", ")}
          </p>
        )}
        <div className="admin-actions">
          <button
            type="submit"
            className="primary-button"
            disabled={
              isImporting ||
              (!useUrls && (!parsedManifest.ok ||
              hasPlaceholder ||
              requiredBasenames.length === 0 ||
              missingArchives.length > 0 ||
              unexpectedArchives.length > 0))
            }
          >
            Verify and import
          </button>
        </div>

        {!useUrls && manifestArtifacts.length > 0 && (
          <div>
            <p className="admin-hint">
              Declared platforms. The server recomputes every digest and size;
              an import only succeeds with exactly these archives uploaded and
              matching.
            </p>
            <table className="admin-table" aria-label="Manifest platforms">
              <thead>
                <tr>
                  <th scope="col">Platform</th>
                  <th scope="col">Version</th>
                  <th scope="col">Declared SHA-256</th>
                  <th scope="col">Declared size</th>
                  <th scope="col">Verification</th>
                </tr>
              </thead>
              <tbody>
                {manifestArtifacts.map((artifact, index) => {
                  const placeholder = isPlaceholderArtifact(artifact);
                  const platform = `${normalizeOs(artifact.platform)}-${normalizeArch(artifact.architecture)}`;
                  const basename = declaredBasename(artifact);
                  const verified = verifiedByArchive[basename];
                  const uploaded = selectedBasenames.includes(basename);
                  return (
                    <tr key={`${platform}-${index}`}>
                      <td>{platform}</td>
                      <td>{artifact.version || "—"}</td>
                      <td>
                        <code>{artifact.sha256 || "—"}</code>
                      </td>
                      <td>{artifact.size || "—"}</td>
                      <td>
                        {placeholder ? (
                          <span className="admin-status-badge not-built">
                            Placeholder — import will be rejected
                          </span>
                        ) : verified ? (
                          <span className="admin-status-badge ready">
                            Verified {String(verified.sha256 || "").slice(0, 18)}…
                          </span>
                        ) : (
                          <span className="admin-status-badge declared">
                            {uploaded ? "Uploaded — not yet verified" : "Awaiting upload"}
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {importResult && (
          <div className="research-card" aria-label="Import result">
            <h4>Import result</h4>
            <p>
              accepted: {String(importResult.accepted)} · created:{" "}
              {String(importResult.created)} · release:{" "}
              <code>
                {importResult.release ? importResult.release.release_id : "—"}
              </code>
            </p>
            {(importResult.verified_artifacts || []).length > 0 && (
              <>
                <p className="admin-hint">Verified artifacts:</p>
                <ul>
                  {importResult.verified_artifacts.map((entry, index) => (
                    <li key={`${entry.archive}-${index}`}>
                      {entry.platform}: <code>{entry.archive}</code>{" "}
                      {entry.sha256} ({entry.size} bytes)
                    </li>
                  ))}
                </ul>
              </>
            )}
          </div>
        )}
      </form>
    </section>
  );
};

export default AdminAgents;
