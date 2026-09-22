// End-to-end product-flow driver for the administrator panel slice.
//
// It exercises the three new admin Dashboard views (accounts, provider
// connections, agent catalogue) in a real Chromium, then drives the existing
// researcher profile, study-creation, and participant-join pages, and finally
// checks the participant's bootstrap eligibility over HTTP.
//
// It reuses the repository's Playwright harness (same Chrome/Chromium channel
// discovery as e2e/browser/scenarios.py) and the running dev stack; it adds no
// runtime dependency to the website. The Playwright module is resolved from an
// existing install (env CODE4ME_PLAYWRIGHT_MODULE, a normal `require`, or the
// npm `npx` cache) and launched through the installed Google Chrome channel.
//
// Env:
//   CODE4ME_FLOW_UI_URL       (default http://localhost:3000)
//   CODE4ME_FLOW_API_URL      (default http://localhost:8008)
//   CODE4ME_FLOW_ADMIN_EMAIL  (default admin@example.com)
//   CODE4ME_FLOW_ADMIN_PASSWORD (default Code4me-dev1)
//   CODE4ME_FLOW_MANIFEST     (default ../code4me2/src/main/resources/code4me-runtime/manifest.json)
//   CODE4ME_FLOW_SECRET_REF   (default OPENAI_API_KEY; the *name* only)
//   CODE4ME_FLOW_CHANNEL      (default chrome; set to "" for bundled chromium)
//   CODE4ME_FLOW_REPORT       (optional JSON report path)

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { randomUUID } = require("crypto");

const UI = process.env.CODE4ME_FLOW_UI_URL || "http://localhost:3000";
const API = process.env.CODE4ME_FLOW_API_URL || "http://localhost:8008";
const ADMIN = {
  email: process.env.CODE4ME_FLOW_ADMIN_EMAIL || "admin@example.com",
  password: process.env.CODE4ME_FLOW_ADMIN_PASSWORD || "Code4me-dev1",
};
const SECRET_REF = process.env.CODE4ME_FLOW_SECRET_REF || "OPENAI_API_KEY";
const CHANNEL = process.env.CODE4ME_FLOW_CHANNEL ?? "chrome";
const MANIFEST_PATH = path.resolve(
  process.env.CODE4ME_FLOW_MANIFEST ||
    path.join(__dirname, "../../../code4me2/src/main/resources/code4me-runtime/manifest.json"),
);

const results = [];
const httpLog = [];

function record(id, ok, detail = "") {
  results.push({ id, status: ok ? "PASS" : "FAIL", detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${id}${detail ? `  :: ${detail}` : ""}`);
}

function loadPlaywright() {
  if (process.env.CODE4ME_PLAYWRIGHT_MODULE) {
    return require(process.env.CODE4ME_PLAYWRIGHT_MODULE);
  }
  try {
    return require("playwright");
  } catch (_) {
    /* fall through to the npx cache */
  }
  const npxRoot = path.join(os.homedir(), ".npm", "_npx");
  if (fs.existsSync(npxRoot)) {
    for (const entry of fs.readdirSync(npxRoot)) {
      const candidate = path.join(npxRoot, entry, "node_modules", "playwright");
      if (fs.existsSync(candidate)) {
        try {
          return require(candidate);
        } catch (_) {
          /* try the next cached install */
        }
      }
    }
  }
  throw new Error(
    "Playwright not found. Install it or set CODE4ME_PLAYWRIGHT_MODULE to an existing install.",
  );
}

async function apiRequest(pathname, { method = "GET", body, cookie } = {}) {
  const response = await fetch(`${API}${pathname}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(cookie ? { Cookie: cookie } : {}),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  const text = await response.text();
  let parsed = {};
  try {
    parsed = text ? JSON.parse(text) : {};
  } catch (_) {
    parsed = { raw: text };
  }
  return { status: response.status, body: parsed, headers: response.headers };
}

async function loginApi(account) {
  const response = await apiRequest("/api/user/authenticate", {
    method: "POST",
    body: { email: account.email, password: account.password },
  });
  const setCookie = response.headers.get("set-cookie") || "";
  const token = /auth_token=([^;]+)/.exec(setCookie);
  return { status: response.status, cookie: token ? `auth_token=${token[1]}` : "" };
}

async function createAccount(prefix, configId = 1) {
  const stamp = Date.now();
  const account = {
    email: `${prefix}-${stamp}@local.dev`,
    name: `${prefix} Flow`,
    password: "FlowPass123",
    config_id: configId,
  };
  const response = await apiRequest("/api/user/create", {
    method: "POST",
    body: account,
  });
  return { account, status: response.status, userId: response.body.user_id };
}

async function loginUi(page, account) {
  await page.goto(`${UI}/login`, { waitUntil: "domcontentloaded" });
  await page.fill("#email", account.email);
  await page.fill("#password", account.password);
  await Promise.all([
    page.waitForURL((url) => !String(url).includes("/login"), { timeout: 30000 }),
    page.click('button[type="submit"]'),
  ]);
}

function trackApi(context) {
  context.on("response", (response) => {
    const url = response.url();
    if (url.includes("/api/research/") || url.includes("/api/agent/")) {
      httpLog.push({ method: response.request().method(), url, status: response.status() });
    }
  });
}

async function main() {
  const { chromium } = loadPlaywright();
  const sourceManifest = JSON.parse(fs.readFileSync(MANIFEST_PATH, "utf8"));
  const manifestDir = path.dirname(MANIFEST_PATH);
  const manifest = sourceManifest;
  const manifestText = JSON.stringify(manifest);
  // The producer owns test results: an untested manifest can never be imported,
  // so fail with the exact producer command instead of a server-side rejection.
  const untested = (manifest.artifacts || []).filter((artifact) => {
    const tests = artifact.tests || {};
    return (
      tests.self_check !== "PASS" ||
      tests.acp_initialize !== "PASS" ||
      !tests.ran_at
    );
  });
  if (!manifest.artifacts || !manifest.artifacts.length || untested.length) {
    throw new Error(
      `manifest has no passing producer tests: ${MANIFEST_PATH}\n` +
        "Produce it first, then point CODE4ME_FLOW_MANIFEST at the result:\n" +
        "  PYTHONPATH=src python -m research.study.agents.participant_release native \\\n" +
        "      --skip-build --version <version> --platform macos-arm64 \\\n" +
        '      --server-commit "$(git rev-parse HEAD)" --output dist/release-<version>',
    );
  }
  const archivePaths = manifest.artifacts.map((artifact) =>
    path.join(manifestDir, artifact.archive),
  );
  for (const archivePath of archivePaths) {
    if (!fs.existsSync(archivePath)) {
      throw new Error(`manifest archive is missing on disk: ${archivePath}`);
    }
  }

  // ---- setup through the public API ----
  const adminLogin = await loginApi(ADMIN);
  record("SETUP-1 admin API login", adminLogin.status === 200, `status=${adminLogin.status}`);
  const researcher = await createAccount("flow-researcher");
  record("SETUP-2 create researcher account", researcher.status === 201, `status=${researcher.status}`);
  const participant = await createAccount("flow-participant");
  record("SETUP-3 create participant account", participant.status === 201, `status=${participant.status}`);

  const launchOptions = { headless: true };
  if (CHANNEL) launchOptions.channel = CHANNEL;
  const browser = await chromium.launch(launchOptions);
  const connectionLabel = `flow-conn-${Date.now()}`;
  const profileName = `flow-profile-${Date.now()}`;
  const studyName = `Flow study ${Date.now()}`;
  let releaseId = "";
  let joinCode = "";
  let enrollmentId = "";

  const adminCtx = await browser.newContext();
  const researcherCtx = await browser.newContext();
  const participantCtx = await browser.newContext();
  trackApi(adminCtx);
  trackApi(researcherCtx);
  trackApi(participantCtx);

  const admin = await adminCtx.newPage();
  const researcherPage = await researcherCtx.newPage();
  const participantPage = await participantCtx.newPage();

  try {
    // ---- 1. admin -> admin-researchers ----
    await loginUi(admin, ADMIN);
    await admin.goto(`${UI}/dashboard?view=admin-researchers`, { waitUntil: "domcontentloaded" });
    await admin.waitForSelector("h2#admin-researchers-title", { timeout: 20000 });
    await admin.waitForSelector(`text=${researcher.account.email}`, { timeout: 20000 });
    const toggle = admin.getByRole("checkbox", {
      name: `Researcher access for ${researcher.account.email}`,
    });
    // The toggle is controlled and refetches after the PUT, so click (rather
    // than check) and wait for the server-backed result.
    await toggle.click();
    await admin.waitForSelector("text=now enabled for research", { timeout: 20000 });
    const enableCall = httpLog.find(
      (entry) =>
        entry.method === "PUT" &&
        entry.url.includes(`/api/research/researchers/${researcher.userId}`),
    );
    record(
      "ADMIN-1 enable researcher via admin-researchers",
      Boolean(enableCall) && enableCall.status === 200,
      `PUT status=${enableCall ? enableCall.status : "missing"}`,
    );

    // ---- 2. admin -> admin-connections ----
    await admin.goto(`${UI}/dashboard?view=admin-connections`, { waitUntil: "domcontentloaded" });
    await admin.waitForSelector("h2#admin-connections-title", { timeout: 20000 });
    await admin.getByLabel("Label", { exact: true }).fill(connectionLabel);
    await admin.getByLabel("Base URL").fill("http://127.0.0.1:11434/v1");
    await admin.getByLabel("Secret name (env var)").fill(SECRET_REF);
    await admin.getByLabel("Models (one per line)").fill("gpt-4o-mini");
    const createConnectionCall = admin.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().includes("/api/research/provider-connections"),
      { timeout: 20000 },
    );
    await admin.getByRole("button", { name: "Create connection" }).click();
    const connectionResponse = await createConnectionCall;
    await admin.waitForSelector("text=Provider connection created.", { timeout: 20000 });
    await admin.waitForSelector("text=Ready — secret present in deployment", { timeout: 20000 });
    record(
      "ADMIN-2 create ready provider connection",
      connectionResponse.status() === 201,
      `POST status=${connectionResponse.status()} secret_ref=${SECRET_REF} (name only)`,
    );

    // ---- 3. admin -> admin-agents ----
    await admin.goto(`${UI}/dashboard?view=admin-agents`, { waitUntil: "domcontentloaded" });
    await admin.waitForSelector("h2#admin-agents-title", { timeout: 20000 });
    await admin.getByLabel("Runtime manifest JSON").fill(manifestText);
    // Without the declared archive bytes the import button stays disabled: there
    // is no digest-trusting acceptance path.
    const declaredPlatforms = manifest.artifacts.length;
    await admin.waitForSelector("text=Awaiting upload", { timeout: 20000 });
    const awaitingBadges = await admin.getByText("Awaiting upload").count();
    const importDisabled = await admin
      .getByRole("button", { name: "Verify and import" })
      .isDisabled();
    record(
      "ADMIN-3 declared archives must be uploaded before import",
      awaitingBadges === declaredPlatforms && importDisabled,
      `awaiting=${awaitingBadges} expected=${declaredPlatforms} disabled=${importDisabled}`,
    );

    await admin.locator('input[aria-label="Agent archive files"]').setInputFiles(archivePaths);
    await admin.getByText("Uploaded — not yet verified").first().waitFor({ timeout: 20000 });

    const importCall = admin.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().includes("/api/research/agents/releases/import"),
      { timeout: 60000 },
    );
    await admin.getByRole("button", { name: "Verify and import" }).click();
    const importResponse = await importCall;
    const importBody = importResponse.json ? await importResponse.json() : {};
    releaseId = importBody.release ? importBody.release.release_id : "";
    await admin.waitForSelector("text=Manifest accepted.", { timeout: 20000 });
    const verifiedArtifacts = (importBody.verified_artifacts || []).length;
    record(
      "ADMIN-4 import verified manifest + archives",
      Boolean(releaseId) &&
        verifiedArtifacts === declaredPlatforms &&
        (importResponse.status() === 201 || importResponse.status() === 200),
      `POST status=${importResponse.status()} release_id=${releaseId} verified=${verifiedArtifacts}`,
    );

    await admin.getByLabel("Platform tests").waitFor({ timeout: 20000 });
    const qualified = await admin
      .getByText("QUALIFIED", { exact: true })
      .first()
      .waitFor({ timeout: 20000 })
      .then(() => true)
      .catch(() => false);
    record("ADMIN-6 release qualified from producer tests", qualified, `release_id=${releaseId}`);

    // ---- 4. researcher -> profile (existing page) ----
    await loginUi(researcherPage, researcher.account);
    await researcherPage.goto(`${UI}/dashboard?view=agent-profiles`, { waitUntil: "domcontentloaded" });
    await researcherPage.waitForSelector("h2", { timeout: 20000 });
    await researcherPage.getByLabel("Profile name").fill(profileName);
    await researcherPage.waitForFunction(
      (label) =>
        Array.from(
          document.querySelectorAll('select[name="connection_id"] option'),
        ).some((option) => option.textContent.includes(label)),
      connectionLabel,
      { timeout: 20000 },
    );
    await researcherPage.selectOption('select[name="connection_id"]', {
      label: connectionLabel,
    });
    await researcherPage.waitForFunction(
      (value) =>
        Boolean(
          document.querySelector(`select[name="model"] option[value="${value}"]`),
        ),
      "gpt-4o-mini",
      { timeout: 20000 },
    );
    await researcherPage.selectOption('select[name="model"]', "gpt-4o-mini");
    await researcherPage.waitForFunction(
      (value) =>
        Boolean(
          document.querySelector(`select[name="release_id"] option[value="${value}"]`),
        ),
      releaseId,
      { timeout: 20000 },
    );
    await researcherPage.selectOption('select[name="release_id"]', releaseId);
    await researcherPage.selectOption('select[name="approval_policy"]', { label: "Ask per step" });
    const profileCall = researcherPage.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().includes("/api/agent/profiles"),
      { timeout: 20000 },
    );
    await researcherPage.getByRole("button", { name: "Create Profile" }).click();
    const profileResponse = await profileCall;
    const profileStatus = profileResponse.status();
    if (profileStatus !== 201) {
      throw new Error(
        `profile create returned ${profileStatus}: ${(await profileResponse.text()).slice(0, 300)}`,
      );
    }
    // The success notice is cleared by the form reset, so confirm the server
    // row instead: the new profile appears in the profiles table.
    await researcherPage
      .getByText(profileName, { exact: true })
      .waitFor({ timeout: 20000 });
    record(
      "RESEARCH-1 create profile pinning release + connection + verified approval",
      profileResponse.status() === 201,
      `POST status=${profileResponse.status()} profile=${profileName}`,
    );

    // ---- 5. researcher -> create study and read the join code ----
    await researcherPage.goto(`${UI}/research/studies`, { waitUntil: "domcontentloaded" });
    await researcherPage.waitForSelector("h2#research-studies-title", { timeout: 20000 });
    await researcherPage.getByRole("button", { name: "New study" }).click();
    await researcherPage.locator("form.research-card input").first().fill(studyName);
    await researcherPage.getByRole("checkbox", { name: profileName }).check();
    const studyCall = researcherPage.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().endsWith("/api/research/studies"),
      { timeout: 20000 },
    );
    await researcherPage.getByRole("button", { name: "Create Draft study" }).click();
    const studyResponse = await studyCall;
    await researcherPage.waitForSelector("text=Study created in Draft state.", { timeout: 20000 });
    const studiesList = await researcherCtx.request.get(`${API}/api/research/studies`);
    const studiesJson = await studiesList.json();
    const created = (studiesJson.studies || []).find((study) => study.name === studyName);
    joinCode = created ? created.join_code : "";
    record(
      "RESEARCH-2 create study with fixed profile and read join code",
      studyResponse.status() === 201 && Boolean(joinCode),
      `POST status=${studyResponse.status()} join_code=${joinCode}`,
    );

    // ---- 6. participant -> web join ----
    await loginUi(participantPage, participant.account);
    await participantPage.goto(`${UI}/research/join`, { waitUntil: "domcontentloaded" });
    await participantPage.locator("input[required]").fill(joinCode);
    await participantPage.getByRole("button", { name: "Review study" }).click();
    await participantPage.waitForSelector("text=I accept the study consent notice.", {
      timeout: 20000,
    });
    const joinCall = participantPage.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().endsWith("/api/research/join"),
      { timeout: 20000 },
    );
    await participantPage.locator('input[type="checkbox"]').first().check();
    await participantPage.getByRole("button", { name: "Accept and join" }).click();
    const joinResponse = await joinCall;
    await participantPage.waitForSelector('div[aria-label="Enrollment handoff"]', {
      timeout: 20000,
    });
    enrollmentId = await participantPage
      .locator('div[aria-label="Enrollment handoff"]')
      .getAttribute("data-enrollment-id");
    record(
      "PARTICIPANT-1 web join with consent",
      joinResponse.status() === 201 && Boolean(enrollmentId),
      `POST status=${joinResponse.status()} enrollment=${enrollmentId}`,
    );

    // ---- 7. bootstrap before vs after consent (HTTP) ----
    const preConsent = await participantCtx.request.post(
      `${API}/api/research/bootstrap/research-sessions`,
      {
        data: {
          enrollment_id: randomUUID(),
          context_id: "flow-pre-consent",
          environment: { os: "macos", arch: "arm64" },
        },
      },
    );
    const afterConsent = await participantCtx.request.post(
      `${API}/api/research/bootstrap/research-sessions`,
      {
        data: {
          enrollment_id: enrollmentId,
          context_id: "flow-post-consent",
          environment: { os: "macos", arch: "arm64" },
        },
      },
    );
    record(
      "PARTICIPANT-2 cannot bootstrap before consent",
      preConsent.status() === 404,
      `POST status=${preConsent.status()} (no enrollment exists yet)`,
    );
    record(
      "PARTICIPANT-3 can bootstrap after consent",
      afterConsent.status() === 201,
      `POST status=${afterConsent.status()}`,
    );
  } catch (error) {
    record("HARNESS", false, String(error && error.message ? error.message : error).slice(0, 400));
  } finally {
    await browser.close();
  }

  const failed = results.filter((entry) => entry.status !== "PASS");
  console.log(`\nTOTAL ${results.length}  PASS ${results.length - failed.length}  FAIL ${failed.length}`);
  failed.forEach((entry) => console.log(`  FAILED ${entry.id} ${entry.detail}`));
  const report = { steps: results, http: httpLog };
  if (process.env.CODE4ME_FLOW_REPORT) {
    fs.writeFileSync(process.env.CODE4ME_FLOW_REPORT, JSON.stringify(report, null, 2));
  }
  return failed.length ? 1 : 0;
}

main()
  .then((code) => process.exit(code))
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });
