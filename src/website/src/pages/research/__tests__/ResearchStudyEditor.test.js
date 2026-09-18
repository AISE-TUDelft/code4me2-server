import React from "react";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import * as api from "../../../utils/api";
import ResearchStudyEditor from "../ResearchStudyEditor";

jest.mock("../../../utils/api");

const STUDY_ID = "11111111-1111-1111-1111-111111111111";

const DIST_VERIFIED = {
  distribution_id: "aaaaaaaa-1111-1111-1111-111111111111",
  name: "arm-a",
  distribution_mode: "PACKAGED",
  release_id: "rel-1",
  release_version: "1.2.0",
  verified: true,
  supported_platforms: [{ os: "linux", arch: "x64" }],
  agent_package: null,
  agent_command: null,
  agent_command_args: [],
};

const DIST_UNVERIFIED = {
  distribution_id: "bbbbbbbb-2222-2222-2222-222222222222",
  name: "arm-b",
  distribution_mode: "BYOA_EXTERNAL",
  release_id: null,
  release_version: null,
  verified: false,
  supported_platforms: [],
  agent_package: null,
  agent_command: "goose",
  agent_command_args: [],
};

const renderEditor = () =>
  render(
    <MemoryRouter
      initialEntries={[`/research/studies/${STUDY_ID}/editor`]}
    >
      <Routes>
        <Route
          path="/research/studies/:studyId/editor"
          element={<ResearchStudyEditor />}
        />
      </Routes>
    </MemoryRouter>,
  );

const addCondition = async () => {
  fireEvent.click(screen.getByRole("button", { name: /add condition/i }));
  // The single distribution picker only exists once a condition is added.
  const select = await screen.findByLabelText(/^Distribution/);
  // Option values only exist once the distributions have loaded; selecting
  // before that would not actually change the controlled select.
  await screen.findByRole("option", { name: /arm-a/ });
  return select;
};

const selectDistribution = (distributionId) =>
  fireEvent.change(screen.getByLabelText(/^Distribution/), {
    target: { value: distributionId },
  });

beforeEach(() => {
  jest.clearAllMocks();
  api.getCurrentUser.mockResolvedValue({
    ok: true,
    user: { is_admin: true, email: "admin@example.com" },
  });
  api.getAgentDistributions.mockResolvedValue({
    ok: true,
    data: [DIST_VERIFIED, DIST_UNVERIFIED],
  });
  api.getResearchDraft.mockResolvedValue({ ok: false, error: "not found" });
  api.getResearchRevision.mockResolvedValue({ ok: false, error: "not found" });
  api.validateResearchProtocol.mockResolvedValue({
    ok: true,
    valid: true,
    errors: [],
    warnings: [],
  });
  api.createResearchDraft.mockResolvedValue({ ok: true, data: { draft_id: "d1" } });
  api.publishResearchDraft.mockResolvedValue({ ok: true, data: { revision: {} } });
  api.supersedeResearchRevision.mockResolvedValue({
    ok: true,
    data: { revision: {} },
  });
});

test("renders one distribution picker and never asks for a digest", async () => {
  renderEditor();

  expect(
    await screen.findByRole("heading", { name: /new study protocol draft/i }),
  ).toBeInTheDocument();

  const select = await addCondition();
  // Exactly one pick: the distribution. No release id, digest, package or
  // distribution-mode input for a researcher.
  expect(select).toBeInTheDocument();
  expect(screen.queryByLabelText(/Artifact digest/i)).not.toBeInTheDocument();
  expect(screen.queryByLabelText(/Registered release/i)).not.toBeInTheDocument();
  expect(screen.queryByLabelText(/Release ID/i)).not.toBeInTheDocument();
  expect(screen.queryByLabelText(/Agent command/i)).not.toBeInTheDocument();
  expect(screen.queryByLabelText(/^Distribution mode/i)).not.toBeInTheDocument();
  expect(screen.queryByLabelText(/Agent profile/i)).not.toBeInTheDocument();
});

test("shows the resolved release, version, verified badge and platforms read-only", async () => {
  renderEditor();
  await addCondition();

  selectDistribution(DIST_VERIFIED.distribution_id);

  // The verified badge is driven by the API's derived flag.
  expect(await screen.findByText("VERIFIED")).toBeInTheDocument();
  // Resolved release identity and version are surfaced read-only.
  expect(screen.getAllByText(/rel-1/).length).toBeGreaterThan(0);
  expect(screen.getAllByText(/1\.2\.0/).length).toBeGreaterThan(0);
  expect(screen.getByText("linux/x64")).toBeInTheDocument();
  // Platform is never an input.
  expect(screen.queryByLabelText(/platform/i)).not.toBeInTheDocument();
});

test("an unverified distribution is visible but not selectable for a non-admin", async () => {
  api.getCurrentUser.mockResolvedValue({
    ok: true,
    user: { is_admin: false, email: "researcher@example.com" },
  });
  renderEditor();
  const select = await addCondition();

  const unverified = within(select).getByRole("option", { name: /arm-b/ });
  // Visible…
  expect(unverified).toBeInTheDocument();
  // …but disabled, while the verified one stays selectable.
  expect(unverified).toBeDisabled();
  expect(within(select).getByRole("option", { name: /arm-a/ })).toBeEnabled();
  // …and explained.
  expect(
    screen.getByText(/cannot be selected by a researcher/i),
  ).toBeInTheDocument();
});

test("an admin may select an unverified distribution and sees the warning", async () => {
  renderEditor();
  await addCondition();

  selectDistribution(DIST_UNVERIFIED.distribution_id);

  expect(await screen.findByText("UNVERIFIED")).toBeInTheDocument();
  expect(
    screen.getByText(/publishing will proceed with a warning/i),
  ).toBeInTheDocument();
});

test("surfaces publish warnings from the publish response", async () => {
  api.publishResearchDraft.mockResolvedValue({
    ok: true,
    data: {
      revision: { revision_number: 1, protocol_digest: "abc123" },
      warnings: [
        {
          code: "DISTRIBUTION_UNVERIFIED",
          field: "conditions[0].distribution_id",
          message: "publishing with an unverified distribution",
          severity: "WARNING",
        },
      ],
    },
  });
  renderEditor();
  await addCondition();
  selectDistribution(DIST_UNVERIFIED.distribution_id);

  fireEvent.change(screen.getByLabelText(/^Name$/i), {
    target: { value: "Warning study" },
  });
  fireEvent.click(screen.getByRole("button", { name: /create draft/i }));
  await waitFor(() => expect(api.createResearchDraft).toHaveBeenCalled());

  const publishButton = screen.getByRole("button", { name: /publish draft/i });
  await waitFor(() => expect(publishButton).toBeEnabled());
  fireEvent.click(publishButton);

  await waitFor(() => expect(api.publishResearchDraft).toHaveBeenCalled());
  expect(
    await screen.findByText(/publishing with an unverified distribution/i),
  ).toBeInTheDocument();
  expect(
    screen.getAllByText("conditions[0].distribution_id").length,
  ).toBeGreaterThan(0);
});

test("renders field-level validation errors returned by the backend", async () => {
  api.validateResearchProtocol.mockResolvedValue({
    ok: false,
    valid: false,
    error: "conditions[0].distribution_id: distribution is unverified",
    errors: [
      {
        code: "DISTRIBUTION_UNVERIFIED",
        field: "conditions[0].distribution_id",
        message: "distribution is unverified",
        severity: "ERROR",
      },
    ],
    warnings: [],
  });
  renderEditor();
  await addCondition();
  selectDistribution(DIST_VERIFIED.distribution_id);

  fireEvent.click(screen.getByRole("button", { name: /^validate$/i }));

  const matches = await screen.findAllByText(/distribution is unverified/i);
  expect(matches.length).toBeGreaterThan(0);
  // The dotted field path is shown so the researcher can locate the problem.
  expect(
    screen.getAllByText("conditions[0].distribution_id").length,
  ).toBeGreaterThan(0);
});

test("shows a general API error state without validation errors", async () => {
  api.validateResearchProtocol.mockResolvedValue({
    ok: false,
    valid: false,
    error: "Network down",
    errors: [],
    warnings: [],
  });
  renderEditor();
  await addCondition();
  selectDistribution(DIST_VERIFIED.distribution_id);

  fireEvent.click(screen.getByRole("button", { name: /^validate$/i }));

  await waitFor(() =>
    expect(screen.getByRole("alert")).toHaveTextContent(/Network down/i),
  );
});
