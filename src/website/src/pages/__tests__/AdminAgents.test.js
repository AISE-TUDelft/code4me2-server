/**
 * Admin agent-catalogue view: real `api.js` with a stubbed `fetch`. Asserts the
 * multipart import request (manifest + archive bytes), the placeholder marker
 * for unbuilt platforms, and platform test results and permanent disable.
 */
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import AdminAgents from "../AdminAgents";

const jsonResponse = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  statusText: "OK",
  json: async () => body,
});

const RELEASE = {
  release_id: "code4me-agent-0.0.0-dev-abcd1234",
  agent_id: "code4me-agent",
  source_manifest_digest: "sha256:abc",
  status: "UNQUALIFIED",
  created_at: "2026-09-20T00:00:00+00:00",
};

// A producer manifest only declares platforms the build actually produced.
const MANIFEST = {
  manifest_version: 1,
  runtime_version: "0.0.0-dev",
  artifacts: [
    {
      runtime_id: "code4me-agent",
      version: "0.0.0-dev",
      platform: "macos",
      architecture: "arm64",
      archive: "code4me-agent-macos-arm64.zip",
      sha256: "74dd71f9c11e341f819d4d7417464a7de33d1a361103edf5e8a3124d5f92a9a5",
      size: 24542238,
    },
  ],
};

// A manifest still carrying an all-zero placeholder for an unbuilt platform is
// rejected wholesale by the server; the form must not offer to import it.
const PLACEHOLDER_MANIFEST = {
  ...MANIFEST,
  artifacts: [
    ...MANIFEST.artifacts,
    {
      runtime_id: "code4me-agent",
      version: "pending-release",
      platform: "windows",
      architecture: "x64",
      archive: "code4me-agent-windows-x64.zip",
      sha256: "0".repeat(64),
      size: 1,
    },
  ],
};

const requests = () =>
  global.fetch.mock.calls.map(([url, options = {}]) => {
    const body = options.body;
    let parsed;
    if (body instanceof FormData) {
      parsed = {
        form: true,
        manifest: body.get("manifest"),
        archives: body.getAll("archives").map((file) => file && file.name),
      };
    } else if (typeof body === "string") {
      parsed = JSON.parse(body);
    } else if (body !== undefined) {
      parsed = body;
    }
    return { url: String(url), method: options.method || "GET", body: parsed };
  });

const defaultFetch = (approvals) => (url, options = {}) => {
  const target = String(url);
  const method = options.method || "GET";
  if (target.includes("/api/research/agents/releases/import")) {
    return Promise.resolve(
      jsonResponse(201, {
        accepted: true,
        created: true,
        release: RELEASE,
        verified_artifacts: [
          {
            archive: "code4me-agent-macos-arm64.zip",
            platform: "macos-arm64",
            sha256: "sha256:74dd71f9c11e341f819d4d7417464a7de33d1a361103edf5e8a3124d5f92a9a5",
            size: 24542238,
            verified: true,
          },
        ],
      }),
    );
  }
  if (target.includes("/disable")) {
    approvals.push("disabled");
    return Promise.resolve(jsonResponse(200, { release: { ...RELEASE, status: "DISABLED" } }));
  }
  if (target.includes("/api/research/agents/releases/")) {
    const qualified = approvals.length > 0;
    return Promise.resolve(
      jsonResponse(200, {
        release: { ...RELEASE, status: qualified ? "QUALIFIED" : "UNQUALIFIED" },
        model: {
          ...RELEASE,
          version: "0.0.0-dev",
          qualification_status: qualified ? "DISABLED" : "QUALIFIED",
          adapter: { adapter_id: "a", version: "0.0.0-dev", digest: "sha256:adapter" },
          artifacts: [
            { os: "macos", arch: "arm64", sha256: "sha256:74dd", size: 24542238 },
          ],
          tests: [{os: "macos", arch: "arm64", self_check: "PASS", acp_initialize: "PASS", ran_at: "2026-09-21T00:00:00Z"}],
        },
      }),
    );
  }
  if (target.includes("/api/research/agents/releases")) {
    return Promise.resolve(jsonResponse(200, { releases: [RELEASE] }));
  }
  if (target.includes("/release-catalogue")) {
    return Promise.resolve(
      jsonResponse(200, {
        releases: [{ release_id: RELEASE.release_id, version: "0.0.0-dev" }],
      }),
    );
  }
  return Promise.resolve(jsonResponse(200, {}));
};

let approvals;

beforeEach(() => {
  approvals = [];
  global.fetch = jest.fn(defaultFetch(approvals));
  window.prompt = jest.fn(() => "looks good");
});

const openReleaseDetail = async () => {
  await screen.findByText(RELEASE.release_id);
  fireEvent.click(screen.getAllByRole("button", { name: /^view$/i })[0]);
  return screen.findByLabelText("Platform tests");
};

test("lists releases with the catalogue version and derived status", async () => {
  render(<AdminAgents />);
  expect(await screen.findByText(RELEASE.release_id)).toBeInTheDocument();
  expect(screen.getByText("0.0.0-dev")).toBeInTheDocument();
  expect(screen.getByText("UNQUALIFIED")).toBeInTheDocument();
});

test("imports a manifest with its archive bytes and shows verified platforms", async () => {
  render(<AdminAgents />);
  await screen.findByText(RELEASE.release_id);

  fireEvent.change(screen.getByLabelText(/runtime manifest json/i), {
    target: { value: JSON.stringify(MANIFEST) },
  });

  // The declared archive must be attached before the form can submit.
  expect(screen.getByText(/awaiting upload/i)).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: /verify and import/i }).disabled,
  ).toBe(true);

  const archive = new File(["zip-bytes"], "code4me-agent-macos-arm64.zip", {
    type: "application/zip",
  });
  fireEvent.change(screen.getByLabelText(/agent archive files/i), {
    target: { files: [archive] },
  });

  fireEvent.click(screen.getByRole("button", { name: /verify and import/i }));

  await waitFor(() => {
    const post = requests().find((r) => r.url.includes("/agents/releases/import"));
    expect(post).toBeTruthy();
    expect(post.method).toBe("POST");
    expect(post.body.form).toBe(true);
    expect(JSON.parse(post.body.manifest)).toEqual(MANIFEST);
    expect(post.body.archives).toEqual(["code4me-agent-macos-arm64.zip"]);
  });

  expect(await screen.findByText(/accepted: true/i)).toBeInTheDocument();
  expect(await screen.findByText(/verified artifacts/i)).toBeInTheDocument();
});

test("a placeholder platform disables the import and is never accepted", async () => {
  render(<AdminAgents />);
  await screen.findByText(RELEASE.release_id);

  fireEvent.change(screen.getByLabelText(/runtime manifest json/i), {
    target: { value: JSON.stringify(PLACEHOLDER_MANIFEST) },
  });

  expect(
    screen.getByText(/placeholder — import will be rejected/i),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: /verify and import/i }).disabled,
  ).toBe(true);

  fireEvent.click(screen.getByRole("button", { name: /verify and import/i }));
  expect(
    requests().some((r) => r.url.includes("/agents/releases/import")),
  ).toBe(false);
});

test("refuses an import with invalid JSON before any request", async () => {
  render(<AdminAgents />);
  await screen.findByText(RELEASE.release_id);

  fireEvent.change(screen.getByLabelText(/runtime manifest json/i), {
    target: { value: "{ not json" },
  });
  fireEvent.click(screen.getByRole("button", { name: /verify and import/i }));

  expect(
    (await screen.findAllByText(/invalid json/i)).length,
  ).toBeGreaterThan(0);
  expect(
    requests().some((r) => r.url.includes("/agents/releases/import")),
  ).toBe(false);
});

test("disables a release permanently without an approval step", async () => {
  render(<AdminAgents />);
  await openReleaseDetail();
  expect(screen.queryByRole("button", { name: /mark tested/i })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /disable release/i }));
  await waitFor(() => expect(requests().some(r => r.method === "POST" && r.url.endsWith("/disable"))).toBe(true));
  await waitFor(() => expect(screen.getByRole("button", { name: /disable release/i })).toBeDisabled());
});

test("imports remote assets through the byte-verifying URL endpoint", async () => {
  render(<AdminAgents />);
  await screen.findByText(RELEASE.release_id);
  fireEvent.click(screen.getByLabelText(/import from release URLs/i));
  fireEvent.change(screen.getByLabelText("Manifest URL"), { target: { value: "https://github.com/org/repo/releases/download/v1/manifest.json" }});
  fireEvent.change(screen.getByLabelText(/Archive URLs/), { target: { value: "https://github.com/org/repo/releases/download/v1/agent.zip" }});
  fireEvent.click(screen.getByRole("button", { name: /verify and import/i }));
  await waitFor(() => expect(requests().find(r => r.url.endsWith("/import-url")).body).toEqual({
    manifest_url: "https://github.com/org/repo/releases/download/v1/manifest.json",
    archive_urls: ["https://github.com/org/repo/releases/download/v1/agent.zip"],
  }));
});
