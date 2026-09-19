import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
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
