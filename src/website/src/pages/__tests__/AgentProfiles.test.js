import React from "react";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import * as api from "../../utils/api";
import AgentProfiles from "../AgentProfiles";

jest.mock("../../utils/api");

const CONNECTION = {
  connection_id: "c1",
  label: "primary-provider",
  models: ["model-a", "model-b"],
  is_active: true,
  ready: true,
};

const PROFILE = {
  profile_id: "p1",
  name: "arm-a",
  model: "model-a",
  framework_version: "code4me2-agent",
  connection: CONNECTION,
  tools_json: "[]",
  approval_policy: "per_step",
  max_steps: 15,
  temperature: null,
  max_context_tokens: null,
  is_active: true,
  release_id: "rel-1",
  release_version: "1.2.0",
  verified: true,
  supported_platforms: [{ os: "linux", arch: "x64" }],
};

const CATALOGUE = [
  {
    release_id: "rel-1",
    version: "1.2.0",
    agent_id: "code4me2-agent",
    distribution_mode: "PACKAGED",
    qualification_status: "QUALIFIED",
    supported_platforms: [{ os: "linux", arch: "x64" }],
    verified_approval_options: null,
    is_byoa: false,
  },
  {
    release_id: "rel-2",
    version: "2.0.0",
    agent_id: "code4me2-agent",
    distribution_mode: "PACKAGED",
    qualification_status: "QUALIFIED",
    supported_platforms: [{ os: "linux", arch: "x64" }],
    verified_approval_options: null,
    is_byoa: false,
  },
];

beforeEach(() => {
  jest.clearAllMocks();
  api.getAgentProfiles.mockResolvedValue({ ok: true, data: [PROFILE] });
  api.getAgentAvailableTools.mockResolvedValue({
    ok: true,
    data: { tools: [], frameworks: [] },
  });
  api.getReleaseCatalogue.mockResolvedValue({ ok: true, data: CATALOGUE });
  api.getProviderConnections.mockResolvedValue({ ok: true, data: [CONNECTION] });
  api.updateAgentProfile.mockResolvedValue({ ok: true, data: {} });
  api.createAgentProfile.mockResolvedValue({ ok: true, data: {} });
});

const renderPage = async (user = { is_admin: true }) => {
  render(<AgentProfiles user={user} />);
  await screen.findByText("arm-a");
};

test("edits the connection/release fields and posts only the allowed payload", async () => {
  await renderPage();

  fireEvent.click(screen.getByRole("button", { name: /^edit$/i }));

  // Derived, server-computed read-only state is displayed.
  expect(screen.getAllByText("Verified").length).toBeGreaterThan(0);
  expect(screen.getAllByText(/1\.2\.0/).length).toBeGreaterThan(0);
  expect(screen.getAllByText("linux/x64").length).toBeGreaterThan(0);

  fireEvent.change(screen.getByLabelText(/Registered release/i), {
    target: { value: "rel-2" },
  });
  fireEvent.click(screen.getByRole("button", { name: /save changes/i }));

  await waitFor(() => expect(api.updateAgentProfile).toHaveBeenCalled());
  const [profileId, payload] = api.updateAgentProfile.mock.calls[0];
  expect(profileId).toBe("p1");
  expect(payload.connection_id).toBe("c1");
  expect(payload.release_id).toBe("rel-2");
  // The retired provider/BYOA fields are never sent (backend forbids them).
  expect(payload.base_url).toBeUndefined();
  expect(payload.api_key_ref).toBeUndefined();
  expect(payload.distribution_mode).toBeUndefined();
  expect(payload.agent_package).toBeUndefined();
  expect(payload.agent_command).toBeUndefined();
});

test("clones a profile with a different release as a create", async () => {
  await renderPage();

  fireEvent.click(screen.getByRole("button", { name: /^clone$/i }));

  expect(screen.getByLabelText(/Profile name/i).value).toBe("arm-a-copy");

  fireEvent.change(screen.getByLabelText(/Registered release/i), {
    target: { value: "rel-2" },
  });
  fireEvent.click(screen.getByRole("button", { name: /create profile/i }));

  await waitFor(() => expect(api.createAgentProfile).toHaveBeenCalled());
  const payload = api.createAgentProfile.mock.calls[0][0];
  expect(payload.name).toBe("arm-a-copy");
  expect(payload.release_id).toBe("rel-2");
  expect(payload.connection_id).toBe("c1");
  expect(api.updateAgentProfile).not.toHaveBeenCalled();
});

test("creates the first profile from an empty-profile release catalogue", async () => {
  api.getAgentProfiles.mockResolvedValue({ ok: true, data: [] });

  render(<AgentProfiles user={{ can_research: true }} />);

  await screen.findByText(/No agent profiles found/i);
  await screen.findByRole("option", { name: /primary-provider/ });
  await screen.findByRole("option", { name: /rel-1/ });

  const releaseOption = screen.getByRole("option", {
    name: /rel-1.*QUALIFIED.*PACKAGED.*linux\/x64/,
  });
  expect(releaseOption).not.toBeDisabled();

  fireEvent.change(screen.getByLabelText(/Profile name/i), {
    target: { value: "first-arm" },
  });
  fireEvent.change(screen.getByLabelText(/Provider connection/i), {
    target: { value: "c1" },
  });
  fireEvent.change(screen.getByLabelText(/^Model$/i), {
    target: { value: "model-a" },
  });
  fireEvent.change(screen.getByLabelText(/Registered release/i), {
    target: { value: "rel-1" },
  });
  fireEvent.click(screen.getByRole("button", { name: /create profile/i }));

  await waitFor(() => expect(api.createAgentProfile).toHaveBeenCalled());
  const payload = api.createAgentProfile.mock.calls[0][0];
  expect(payload.release_id).toBe("rel-1");
  expect(payload.connection_id).toBe("c1");
});

test("the model select is constrained to the connection's allowed models", async () => {
  await renderPage();
  fireEvent.click(screen.getByRole("button", { name: /^edit$/i }));

  const model = screen.getByLabelText(/^Model$/i);
  const values = Array.from(model.querySelectorAll("option")).map(
    (option) => option.value,
  );
  expect(values).toEqual(expect.arrayContaining(["model-a", "model-b"]));
  expect(values).not.toContain("model-c");
});

test("switching connection resets a now-disallowed model", async () => {
  api.getProviderConnections.mockResolvedValue({
    ok: true,
    data: [
      CONNECTION,
      {
        connection_id: "c2",
        label: "secondary",
        models: ["model-c"],
        is_active: true,
        ready: true,
      },
    ],
  });
  await renderPage();
  fireEvent.click(screen.getByRole("button", { name: /^edit$/i }));

  fireEvent.change(screen.getByLabelText(/Provider connection/i), {
    target: { value: "c2" },
  });
  expect(screen.getByLabelText(/^Model$/i).value).toBe("");
});

test("a non-admin cannot select an unverified release", async () => {
  api.getReleaseCatalogue.mockResolvedValue({
    ok: true,
    data: [
      ...CATALOGUE,
      {
        release_id: "rel-unverified",
        version: "0.1.0",
        agent_id: "codex",
        distribution_mode: "BYOA_EXTERNAL",
        qualification_status: "UNQUALIFIED",
        supported_platforms: [],
        verified_approval_options: null,
        is_byoa: true,
      },
    ],
  });

  await renderPage({ can_research: true });

  const unverified = screen.getByRole("option", { name: /rel-unverified/i });
  expect(unverified).toBeDisabled();
});

test("an administrator may select an unverified release", async () => {
  api.getReleaseCatalogue.mockResolvedValue({
    ok: true,
    data: [
      ...CATALOGUE,
      {
        release_id: "rel-unverified",
        version: "0.1.0",
        agent_id: "codex",
        distribution_mode: "BYOA_EXTERNAL",
        qualification_status: "UNQUALIFIED",
        supported_platforms: [],
        verified_approval_options: null,
        is_byoa: true,
      },
    ],
  });

  await renderPage({ is_admin: true });

  const unverified = screen.getByRole("option", { name: /rel-unverified/i });
  expect(unverified).not.toBeDisabled();
});

test("approval options not verified for the release are disabled", async () => {
  api.getReleaseCatalogue.mockResolvedValue({
    ok: true,
    data: [
      {
        ...CATALOGUE[0],
        verified_approval_options: ["auto"],
      },
    ],
  });

  await renderPage({ is_admin: true });
  fireEvent.click(screen.getByRole("button", { name: /^edit$/i }));

  const perStep = screen.getByRole("option", { name: /Ask per step/i });
  const suggestion = screen.getByRole("option", { name: /Suggestion only/i });
  const auto = screen.getByRole("option", { name: /Auto-approve/i });
  expect(perStep).toBeDisabled();
  expect(suggestion).toBeDisabled();
  expect(auto).not.toBeDisabled();
});

const GOOSE_RELEASE = {
  release_id: "goose-1",
  version: "1.0.0",
  agent_id: "goose",
  distribution_mode: "BYOA_EXTERNAL",
  qualification_status: "QUALIFIED",
  supported_platforms: [],
  verified_approval_options: null,
  is_byoa: true,
  compatible_frameworks: ["goose"],
  configurable_fields: ["model", "max_steps", "approval_policy", "tools"],
  required_bindings_missing: [],
};

const gooseProfile = (overrides) => ({
  ...PROFILE,
  framework_version: "goose",
  release_id: "goose-1",
  release_version: "1.0.0",
  tools_json: '["read_file"]',
  ...overrides,
});

test("a locked setting is saved unset even when the stored profile carries it", async () => {
  api.getReleaseCatalogue.mockResolvedValue({ ok: true, data: [...CATALOGUE, GOOSE_RELEASE] });
  api.getAgentAvailableTools.mockResolvedValue({ ok: true, data: { tools: ["read_file", "write_file"] } });
  api.getAgentProfiles.mockResolvedValue({
    ok: true,
    data: [
      gooseProfile({ profile_id: "g1", name: "goose-clean" }),
      // Written before the runtime refused these settings for BYOA agents.
      gooseProfile({ profile_id: "g2", name: "goose-stale", max_context_tokens: 32000, temperature: 0.7 }),
    ],
  });
  render(<AgentProfiles user={{ is_admin: true }} />);
  await screen.findByText("goose-clean");
  await screen.findByRole("option", { name: /goose-1/ });

  // Same runtime and release as the profile opened before it.
  const editButtons = screen.getAllByRole("button", { name: /^edit$/i });
  fireEvent.click(editButtons[0]);
  await waitFor(() => expect(screen.getByLabelText(/Profile name/i).value).toBe("goose-clean"));
  expect(await screen.findByRole("checkbox", { name: "write_file" })).toBeInTheDocument();
  fireEvent.click(editButtons[1]);
  await waitFor(() => expect(screen.getByLabelText(/Profile name/i).value).toBe("goose-stale"));
  // The stale value is cleared in the form too, not only in the payload.
  await waitFor(() => expect(screen.getByLabelText(/Max context tokens/i).value).toBe(""));
  fireEvent.click(screen.getByRole("button", { name: /save changes/i }));

  await waitFor(() => expect(api.updateAgentProfile).toHaveBeenCalled());
  const [profileId, payload] = api.updateAgentProfile.mock.calls[0];
  expect(profileId).toBe("g2");
  expect(payload.max_context_tokens).toBeNull();
  expect(payload.temperature).toBeNull();
  expect(payload.tools_json).toBe('["read_file"]');
});

test("a failed tool list keeps the profile's tools instead of clearing them", async () => {
  api.getAgentProfiles.mockResolvedValue({ ok: true, data: [{ ...PROFILE, tools_json: '["read_file"]' }] });
  api.getAgentAvailableTools.mockResolvedValue({ ok: false, error: "Network error" });
  await renderPage();

  fireEvent.click(screen.getByRole("button", { name: /^edit$/i }));
  expect(await screen.findByText("The tools this runtime offers could not be loaded.")).toBeInTheDocument();
  expect(screen.getByRole("checkbox", { name: "read_file" })).toBeChecked();

  api.getAgentAvailableTools.mockResolvedValue({ ok: true, data: { tools: ["read_file", "write_file"] } });
  fireEvent.click(screen.getByRole("button", { name: /retry/i }));
  expect(await screen.findByRole("checkbox", { name: "write_file" })).not.toBeChecked();
  expect(screen.getByRole("checkbox", { name: "read_file" })).toBeChecked();

  fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
  await waitFor(() => expect(api.updateAgentProfile).toHaveBeenCalled());
  expect(api.updateAgentProfile.mock.calls[0][1].tools_json).toBe('["read_file"]');
});

test("editing a profile while another runtime's tool list is shown keeps its tools", async () => {
  api.getAgentProfiles.mockResolvedValue({ ok: true, data: [{ ...PROFILE, tools_json: '["read_file"]' }] });
  let builtInLists = 0;
  let releaseBuiltIn;
  api.getAgentAvailableTools.mockImplementation((framework) => {
    if (framework !== "code4me2-agent") return Promise.resolve({ ok: true, data: { tools: [] } });
    builtInLists += 1;
    const list = { ok: true, data: { tools: ["read_file", "write_file"] } };
    // The first (page load) list answers at once; the one for the edit waits.
    if (builtInLists === 1) return Promise.resolve(list);
    return new Promise((resolve) => (releaseBuiltIn = () => resolve(list)));
  });
  await renderPage();

  // The new-profile form first shows Codex, whose tool list is empty (locked).
  fireEvent.change(screen.getByLabelText("Agent runtime"), { target: { value: "codex" } });
  await waitFor(() => expect(api.getAgentAvailableTools).toHaveBeenLastCalledWith("codex"));
  expect(await screen.findByText("Codex manages its own tools; there is nothing to select.")).toBeInTheDocument();

  // Opening the built-in profile must not apply Codex's empty list to it, and
  // bulk controls wait for the built-in list.
  fireEvent.click(screen.getByRole("button", { name: /^edit$/i }));
  await waitFor(() => expect(builtInLists).toBe(2));
  expect(screen.getByText("Loading tools…")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Select all" })).not.toBeInTheDocument();
  await act(async () => releaseBuiltIn());
  expect(await screen.findByRole("checkbox", { name: "read_file" })).toBeChecked();
  expect(screen.getByRole("button", { name: "Select all" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
  await waitFor(() => expect(api.updateAgentProfile).toHaveBeenCalled());
  expect(api.updateAgentProfile.mock.calls[0][1].tools_json).toBe('["read_file"]');
});

test("bulk tool controls wait for the current runtime's list", async () => {
  api.getAgentProfiles.mockResolvedValue({ ok: true, data: [{ ...PROFILE, tools_json: '["read_file"]' }] });
  let builtInLists = 0;
  let releaseBuiltIn;
  api.getAgentAvailableTools.mockImplementation((framework) => {
    if (framework === "goose") return Promise.resolve({ ok: true, data: { tools: ["goose_shell", "goose_read"] } });
    if (framework !== "code4me2-agent") return Promise.resolve({ ok: true, data: { tools: [] } });
    builtInLists += 1;
    const list = { ok: true, data: { tools: ["read_file", "write_file"] } };
    if (builtInLists === 1) return Promise.resolve(list);
    return new Promise((resolve) => (releaseBuiltIn = () => resolve(list)));
  });
  await renderPage();

  fireEvent.change(screen.getByLabelText("Agent runtime"), { target: { value: "goose" } });
  expect(await screen.findByRole("checkbox", { name: "goose_shell" })).toBeInTheDocument();

  // While the built-in list loads, Goose's list must not drive "Select all".
  fireEvent.click(screen.getByRole("button", { name: /^edit$/i }));
  await waitFor(() => expect(builtInLists).toBe(2));
  expect(screen.queryByRole("button", { name: "Select all" })).not.toBeInTheDocument();
  expect(screen.getByText("1 selected")).toBeInTheDocument();
  await act(async () => releaseBuiltIn());
  expect(await screen.findByRole("button", { name: "Select all" })).toBeInTheDocument();
  expect(screen.getByText("1 / 2 selected")).toBeInTheDocument();
});

test("a metered profile whose model has no server price says so in the list", async () => {
  api.getReleaseCatalogue.mockResolvedValue({ ok: true, data: [...CATALOGUE, GOOSE_RELEASE] });
  api.getAgentProfiles.mockResolvedValue({
    ok: true,
    data: [
      gooseProfile({ profile_id: "g1", name: "goose-unpriced", model_priced: false }),
      // The built-in agent is metered too.
      { ...PROFILE, model_priced: false },
      // Codex is never metered (the server reports null).
      { ...PROFILE, profile_id: "p2", name: "codex-arm", framework_version: "codex", model_priced: null },
    ],
  });
  render(<AgentProfiles user={{ is_admin: true }} />);
  await screen.findByText("goose-unpriced");

  expect(screen.getAllByText("Price missing on the server")).toHaveLength(2);
});

test("the profile form warns when the selected model has no price on its connection", async () => {
  api.getProviderConnections.mockResolvedValue({
    ok: true,
    data: [
      {
        ...CONNECTION,
        model_prices: {
          "model-a": { input_usd_per_million: "0.50", output_usd_per_million: "1.50", cached_input_usd_per_million: null },
          "model-b": null,
        },
        pricing: { complete: false, missing_models: ["model-b"] },
      },
    ],
  });
  await renderPage();
  fireEvent.click(screen.getByRole("button", { name: /^edit$/i }));

  expect(screen.queryByText(/Price missing on the server for/)).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText(/^Model$/i), { target: { value: "model-b" } });
  expect(screen.getByText(/Price missing on the server for model-b/)).toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// Built-in runtime command and harness settings (decision D-01) and the 64k
// context default for new built-in profiles (decision D-03).
// ---------------------------------------------------------------------------

const HARNESSED = {
  ...PROFILE,
  profile_id: "p-h",
  name: "arm-harness",
  commands_allowlist: ["pytest", "git"],
  command_timeout_seconds: 300,
  harness_options: {
    self_review: false,
    prompt_profile: "anthropic",
    // An API-authored argument containing spaces must survive unrelated edits.
    verify_command: ["pytest", "-k", "slow and not flaky"],
  },
};

const fillNewProfile = () => {
  fireEvent.change(screen.getByLabelText(/Profile name/i), { target: { value: "new-arm" } });
  fireEvent.change(screen.getByLabelText(/Provider connection/i), { target: { value: "c1" } });
  fireEvent.change(screen.getByLabelText(/^Model$/i), { target: { value: "model-a" } });
  fireEvent.change(screen.getByLabelText(/Registered release/i), { target: { value: "rel-1" } });
};

const renderEmptyCatalogue = async () => {
  api.getAgentProfiles.mockResolvedValue({ ok: true, data: [] });
  render(<AgentProfiles user={{ is_admin: true }} />);
  await screen.findByText(/No agent profiles found/i);
  await screen.findByRole("option", { name: /primary-provider/ });
  await screen.findByRole("option", { name: /rel-1/ });
};

test("a new built-in profile pre-fills the recommended 64k context and omits untouched harness settings", async () => {
  await renderEmptyCatalogue();

  expect(screen.getByLabelText(/Max context tokens/i).value).toBe("64000");
  expect(screen.getByText(/64k or more is recommended for frontier models/)).toBeInTheDocument();
  expect(screen.getByLabelText("Command allowlist").value).toBe("");
  expect(screen.getByLabelText("Default command timeout (s)").value).toBe("");
  expect(screen.getByRole("group", { name: "Harness options" })).toBeInTheDocument();
  // Every switch shows its runtime default (on).
  expect(screen.getByRole("checkbox", { name: "Self-review" })).toBeChecked();
  expect(screen.getByRole("checkbox", { name: "Test output summary" })).toBeChecked();

  fillNewProfile();
  fireEvent.click(screen.getByRole("button", { name: /create profile/i }));

  await waitFor(() => expect(api.createAgentProfile).toHaveBeenCalled());
  const payload = api.createAgentProfile.mock.calls[0][0];
  expect(payload.max_context_tokens).toBe(64000);
  expect(payload).not.toHaveProperty("commands_allowlist");
  expect(payload).not.toHaveProperty("command_timeout_seconds");
  expect(payload).not.toHaveProperty("harness_options");
});

test("a new profile moved to Codex and back gets the recommended context again", async () => {
  await renderEmptyCatalogue();

  fireEvent.change(screen.getByLabelText("Agent runtime"), { target: { value: "codex" } });
  await waitFor(() => expect(screen.getByLabelText(/Max context tokens/i).value).toBe(""));
  fireEvent.change(screen.getByLabelText("Agent runtime"), { target: { value: "code4me2-agent" } });
  expect(screen.getByLabelText(/Max context tokens/i).value).toBe("64000");
  // Let the built-in runtime's tool list settle before the test ends.
  expect(await screen.findByText("This runtime exposes no selectable tools.")).toBeInTheDocument();
});

test("built-in profiles save the command allowlist, timeout and harness options", async () => {
  await renderPage();
  fireEvent.click(screen.getByRole("button", { name: /^edit$/i }));
  // An existing profile without a context override is not pre-filled.
  expect(screen.getByLabelText(/Max context tokens/i).value).toBe("");

  fireEvent.change(screen.getByLabelText("Command allowlist"), { target: { value: "pytest, git  gradlew" } });
  fireEvent.change(screen.getByLabelText("Default command timeout (s)"), { target: { value: "300" } });
  fireEvent.click(screen.getByRole("checkbox", { name: "Self-review" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "Parallel tools" }));
  // Ticking a switch back returns it to the runtime default (not stored).
  fireEvent.click(screen.getByRole("checkbox", { name: "Parallel tools" }));
  fireEvent.change(screen.getByLabelText("Prompt profile"), { target: { value: "anthropic" } });
  fireEvent.change(screen.getByLabelText("Verify command"), { target: { value: " pytest  -q tests " } });
  fireEvent.click(screen.getByRole("button", { name: /save changes/i }));

  await waitFor(() => expect(api.updateAgentProfile).toHaveBeenCalled());
  const [profileId, payload] = api.updateAgentProfile.mock.calls[0];
  expect(profileId).toBe("p1");
  expect(payload.commands_allowlist).toEqual(["pytest", "git", "gradlew"]);
  expect(payload.command_timeout_seconds).toBe(300);
  expect(payload.harness_options).toEqual({
    self_review: false,
    prompt_profile: "anthropic",
    verify_command: ["pytest", "-q", "tests"],
  });
  expect(payload.max_context_tokens).toBeNull();
});

test("an unchanged edit omits the stored harness settings", async () => {
  api.getAgentProfiles.mockResolvedValue({ ok: true, data: [HARNESSED] });
  render(<AgentProfiles user={{ is_admin: true }} />);
  await screen.findByText("arm-harness");

  fireEvent.click(screen.getByRole("button", { name: /^edit$/i }));
  expect(screen.getByLabelText("Command allowlist").value).toBe("pytest, git");
  expect(screen.getByLabelText("Default command timeout (s)").value).toBe("300");
  expect(screen.getByRole("checkbox", { name: "Self-review" })).not.toBeChecked();
  expect(screen.getByRole("checkbox", { name: "Loop guard" })).toBeChecked();
  expect(screen.getByLabelText("Prompt profile").value).toBe("anthropic");
  expect(screen.getByLabelText("Verify command").value).toBe("pytest -k slow and not flaky");

  fireEvent.change(screen.getByLabelText(/Max steps/i), { target: { value: "20" } });
  fireEvent.click(screen.getByRole("button", { name: /save changes/i }));

  await waitFor(() => expect(api.updateAgentProfile).toHaveBeenCalled());
  const payload = api.updateAgentProfile.mock.calls[0][1];
  expect(payload.max_steps).toBe(20);
  expect(payload).not.toHaveProperty("commands_allowlist");
  expect(payload).not.toHaveProperty("command_timeout_seconds");
  expect(payload).not.toHaveProperty("harness_options");
});

test("an edit sends only the changed harness settings and null for a cleared one", async () => {
  api.getAgentProfiles.mockResolvedValue({ ok: true, data: [HARNESSED] });
  render(<AgentProfiles user={{ is_admin: true }} />);
  await screen.findByText("arm-harness");

  fireEvent.click(screen.getByRole("button", { name: /^edit$/i }));
  fireEvent.change(screen.getByLabelText("Default command timeout (s)"), { target: { value: "" } });
  fireEvent.click(screen.getByRole("checkbox", { name: "Loop guard" }));
  fireEvent.click(screen.getByRole("button", { name: /save changes/i }));

  await waitFor(() => expect(api.updateAgentProfile).toHaveBeenCalled());
  const payload = api.updateAgentProfile.mock.calls[0][1];
  expect(payload).not.toHaveProperty("commands_allowlist");
  expect(payload.command_timeout_seconds).toBeNull();
  expect(payload.harness_options).toEqual({
    self_review: false,
    loop_guard: false,
    prompt_profile: "anthropic",
    verify_command: ["pytest", "-k", "slow and not flaky"],
  });
});

test("clearing every harness option sends null", async () => {
  api.getAgentProfiles.mockResolvedValue({
    ok: true,
    data: [{ ...HARNESSED, harness_options: { self_review: false } }],
  });
  render(<AgentProfiles user={{ is_admin: true }} />);
  await screen.findByText("arm-harness");

  fireEvent.click(screen.getByRole("button", { name: /^edit$/i }));
  fireEvent.change(screen.getByLabelText("Command allowlist"), { target: { value: "" } });
  // The stored explicit value stays explicit when ticked again.
  fireEvent.click(screen.getByRole("checkbox", { name: "Self-review" }));
  fireEvent.click(screen.getByRole("button", { name: /save changes/i }));

  await waitFor(() => expect(api.updateAgentProfile).toHaveBeenCalled());
  const payload = api.updateAgentProfile.mock.calls[0][1];
  expect(payload.commands_allowlist).toBeNull();
  expect(payload.harness_options).toEqual({ self_review: true });
});

test("clone carries the harness settings into the create payload", async () => {
  api.getAgentProfiles.mockResolvedValue({ ok: true, data: [HARNESSED] });
  render(<AgentProfiles user={{ is_admin: true }} />);
  await screen.findByText("arm-harness");

  fireEvent.click(screen.getByRole("button", { name: /^clone$/i }));
  expect(screen.getByLabelText(/Profile name/i).value).toBe("arm-harness-copy");
  fireEvent.click(screen.getByRole("button", { name: /create profile/i }));

  await waitFor(() => expect(api.createAgentProfile).toHaveBeenCalled());
  const payload = api.createAgentProfile.mock.calls[0][0];
  expect(payload.commands_allowlist).toEqual(["pytest", "git"]);
  expect(payload.command_timeout_seconds).toBe(300);
  expect(payload.harness_options).toEqual(HARNESSED.harness_options);
});

test("Goose and Codex profiles hide the harness settings and never send them", async () => {
  api.getReleaseCatalogue.mockResolvedValue({ ok: true, data: [...CATALOGUE, GOOSE_RELEASE] });
  api.getAgentAvailableTools.mockResolvedValue({ ok: true, data: { tools: ["read_file"] } });
  api.getAgentProfiles.mockResolvedValue({
    ok: true,
    // Written before the server refused these settings for BYOA agents.
    data: [gooseProfile({ profile_id: "g1", name: "goose-stale", commands_allowlist: ["git"], command_timeout_seconds: 30 })],
  });
  render(<AgentProfiles user={{ is_admin: true }} />);
  await screen.findByText("goose-stale");
  await screen.findByRole("option", { name: /goose-1/ });

  fireEvent.click(screen.getByRole("button", { name: /^edit$/i }));
  await waitFor(() => expect(screen.getByLabelText(/Profile name/i).value).toBe("goose-stale"));
  expect(screen.queryByLabelText("Command allowlist")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("Default command timeout (s)")).not.toBeInTheDocument();
  expect(screen.queryByRole("group", { name: "Harness options" })).not.toBeInTheDocument();
  expect(screen.queryByText(/64k or more is recommended/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /save changes/i }));

  await waitFor(() => expect(api.updateAgentProfile).toHaveBeenCalled());
  const payload = api.updateAgentProfile.mock.calls[0][1];
  expect(payload).not.toHaveProperty("commands_allowlist");
  expect(payload).not.toHaveProperty("command_timeout_seconds");
  expect(payload).not.toHaveProperty("harness_options");
});

test("invalid harness input is explained before any request", async () => {
  await renderPage();
  fireEvent.click(screen.getByRole("button", { name: /^edit$/i }));
  const save = () => fireEvent.click(screen.getByRole("button", { name: /save changes/i }));

  fireEvent.change(screen.getByLabelText("Command allowlist"), { target: { value: "git ./gradlew" } });
  save();
  expect(await screen.findByText(/"\.\/gradlew" is not a command name/)).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("Command allowlist"), { target: { value: "git, git" } });
  save();
  expect(await screen.findByText(/lists a command more than once/)).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("Command allowlist"), { target: { value: "git" } });
  fireEvent.change(screen.getByLabelText("Default command timeout (s)"), { target: { value: "601" } });
  save();
  expect(await screen.findByText(/whole number of seconds from 1 to 600/)).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("Default command timeout (s)"), { target: { value: "" } });
  fireEvent.change(screen.getByLabelText("Verify command"), { target: { value: "pytest -q" } });
  save();
  expect(await screen.findByText(/The verify command runs pytest; add it to the command allowlist/)).toBeInTheDocument();

  expect(api.updateAgentProfile).not.toHaveBeenCalled();
});
