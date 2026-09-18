import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import * as api from "../../../utils/api";
import ResearchEnrollment from "../ResearchEnrollment";

jest.mock("../../../utils/api");

const STUDY_ID = "11111111-1111-1111-1111-111111111111";
const REVISION_ID = "22222222-2222-2222-2222-222222222222";
const ENROLLMENT_ID = "44444444-4444-4444-4444-444444444444";
const STUDY_JOIN_CODE = "STUDY-CODE-1";

const DIST_PACKAGED_ID = "aaaaaaaa-0000-0000-0000-000000000001";
const DIST_BYOA_ID = "bbbbbbbb-0000-0000-0000-000000000002";

const DISTRIBUTIONS = [
  {
    distribution_id: DIST_PACKAGED_ID,
    name: "arm-a",
    distribution_mode: "PACKAGED",
    release_id: "rel-1",
    release_version: "1.2.0",
    verified: true,
  },
  {
    distribution_id: DIST_BYOA_ID,
    name: "goose-arm",
    distribution_mode: "BYOA_EXTERNAL",
    release_id: null,
    release_version: null,
    agent_command: "goose",
    agent_package: null,
    verified: true,
  },
];

const PROTOCOL = {
  conditions: [
    {
      condition_id: "arm-a",
      name: "arm-a",
      distribution_id: DIST_PACKAGED_ID,
      declared_overrides: { agent_profile: "arm-a" },
      resolved_distribution: {
        distribution_id: DIST_PACKAGED_ID,
        distribution_mode: "PACKAGED",
        release_id: "rel-1",
        agent_id: "arm-a",
        version: "1.2.0",
        artifact_digest: "sha256:deadbeef",
        verified: true,
      },
    },
    {
      condition_id: "goose-arm",
      name: "goose-arm",
      distribution_id: DIST_BYOA_ID,
      declared_overrides: { agent_profile: "goose-arm" },
      resolved_distribution: {
        distribution_id: DIST_BYOA_ID,
        distribution_mode: "BYOA_EXTERNAL",
        release_id: null,
        agent_id: "goose-arm",
        agent_command: "goose",
        verified: true,
      },
    },
  ],
};

const renderPage = () =>
  render(
    <MemoryRouter
      initialEntries={[
        `/research/enrollment?study_id=${STUDY_ID}&revision_id=${REVISION_ID}`,
      ]}
    >
      <ResearchEnrollment />
    </MemoryRouter>,
  );

beforeEach(() => {
  jest.clearAllMocks();
  api.getAgentDistributions.mockResolvedValue({
    ok: true,
    data: DISTRIBUTIONS,
  });
  api.getResearchPackages.mockResolvedValue({ ok: true, data: [] });
  api.getResearchRevision.mockResolvedValue({
    ok: true,
    revision: {
      revision_id: REVISION_ID,
      revision_number: 1,
      status: "PUBLISHED",
      protocol_digest: "deadbeefcafebabe",
      published_at: "2026-01-02T03:04:05Z",
    },
    protocol: PROTOCOL,
  });
  api.getResearchStudyJoinCode.mockResolvedValue({
    ok: true,
    join_code: STUDY_JOIN_CODE,
    revision_id: REVISION_ID,
    status: "PUBLISHED",
  });
  api.requestResearchEnrollment.mockResolvedValue({
    ok: true,
    data: {
      created: true,
      enrollment: {
        enrollment_id: ENROLLMENT_ID,
        status: "ACTIVE",
        participant_code: "P-0001",
        eligible: true,
      },
    },
  });
  api.getResearchEnrollment.mockResolvedValue({
    ok: true,
    data: { enrollment_id: ENROLLMENT_ID, status: "ACTIVE" },
  });
});

test("shows the study-scoped join code, instructions and packaged/BYOA split", async () => {
  renderPage();

  // Revision details and the study join code auto-load from the URL.
  expect(await screen.findByText(/Revision #1/i)).toBeInTheDocument();
  expect(await screen.findByText(STUDY_JOIN_CODE)).toBeInTheDocument();
  expect(screen.getByText(/Enter the join code/i)).toBeInTheDocument();
  expect(screen.getByText(/JetBrains Marketplace/i)).toBeInTheDocument();
  // arm-a is the packaged runtime; goose-arm is BYOA.
  expect(screen.getByText("Packaged")).toBeInTheDocument();
  expect(screen.getByText("BYOA")).toBeInTheDocument();
  // The frozen resolved_distribution is the source of truth: the packaged
  // release pin and the BYOA agent command come from it, not the deleted
  // per-condition release/profile fields.
  expect(screen.getByText("rel-1")).toBeInTheDocument();
  expect(screen.getByText("goose")).toBeInTheDocument();
  expect(api.getResearchStudyJoinCode).toHaveBeenCalledWith(STUDY_ID);
});

test("classifies an unresolved draft condition from the distribution registry", async () => {
  // A draft that has not been published has no frozen resolved_distribution,
  // so classification falls back to the registry entry named by distribution_id.
  api.getResearchRevision.mockResolvedValue({
    ok: true,
    revision: {
      revision_id: REVISION_ID,
      revision_number: 1,
      status: "DRAFT",
      protocol_digest: "deadbeefcafebabe",
      published_at: null,
    },
    protocol: {
      conditions: [
        {
          condition_id: "arm-a",
          name: "arm-a",
          distribution_id: DIST_PACKAGED_ID,
          declared_overrides: {},
        },
        {
          condition_id: "goose-arm",
          name: "goose-arm",
          distribution_id: DIST_BYOA_ID,
          declared_overrides: {},
        },
      ],
    },
  });
  renderPage();

  await screen.findByText(/Revision #1/i);
  expect(screen.getByText("Packaged")).toBeInTheDocument();
  expect(screen.getByText("BYOA")).toBeInTheDocument();
  expect(screen.getByText("rel-1")).toBeInTheDocument();
  expect(screen.getByText("goose")).toBeInTheDocument();
  expect(api.getAgentDistributions).toHaveBeenCalled();
});

test("copies the study join code", async () => {
  const writeText = jest.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText },
    configurable: true,
  });
  renderPage();

  await screen.findByText(STUDY_JOIN_CODE);
  fireEvent.click(screen.getByRole("button", { name: /copy code/i }));

  await waitFor(() => expect(writeText).toHaveBeenCalledWith(STUDY_JOIN_CODE));
  expect(await screen.findByText(/copied/i)).toBeInTheDocument();
});

test("enrolls the signed-in account on request", async () => {
  renderPage();

  await screen.findByText(STUDY_JOIN_CODE);
  fireEvent.click(screen.getByRole("button", { name: /enroll this account/i }));

  expect(await screen.findByText(/Account enrollment/i)).toBeInTheDocument();
  expect(await screen.findByText("ACTIVE")).toBeInTheDocument();
  expect(api.requestResearchEnrollment).toHaveBeenCalledWith(
    STUDY_ID,
    REVISION_ID,
  );
});

test("falls back to the per-account enrollment id when the join-code endpoint is missing", async () => {
  api.getResearchStudyJoinCode.mockResolvedValue({
    ok: false,
    missing: true,
    error: "missing",
  });
  renderPage();

  await screen.findByText(/Revision #1/i);
  expect(
    await screen.findByText(/does not expose the study join-code endpoint yet/i),
  ).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: /enroll this account/i }));
  // The per-account enrollment id becomes the only shareable value.
  expect(await screen.findByText(ENROLLMENT_ID)).toBeInTheDocument();
});

test("shows an API error when the study join code cannot be loaded", async () => {
  api.getResearchStudyJoinCode.mockResolvedValue({
    ok: false,
    error: "join code rejected",
    errors: [],
  });
  renderPage();

  fireEvent.click(screen.getByRole("button", { name: /get join code/i }));

  await waitFor(() =>
    expect(screen.getByRole("alert")).toHaveTextContent(/join code rejected/i),
  );
});

test("notes when the runtime package registry is unavailable", async () => {
  api.getResearchPackages.mockResolvedValue({
    ok: false,
    error: "packages down",
  });
  renderPage();

  expect(
    await screen.findByText(/Runtime package registry unavailable/i),
  ).toBeInTheDocument();
});
