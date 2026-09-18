import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import * as api from "../../../utils/api";
import ResearchStudies from "../ResearchStudies";

jest.mock("../../../utils/api");

const STUDY_ID = "11111111-1111-1111-1111-111111111111";

const REVISION = {
  revision_id: "22222222-2222-2222-2222-222222222222",
  study_id: STUDY_ID,
  revision_number: 1,
  status: "PUBLISHED",
  protocol_digest: "abcdef0123456789",
  supersedes_revision_id: null,
  published_at: "2026-01-02T03:04:05Z",
  created_at: "2026-01-02T03:04:05Z",
};

const DRAFT = {
  draft_id: "33333333-3333-3333-3333-333333333333",
  study_id: STUDY_ID,
  name: "My protocol",
  schema_version: "1",
};

const STUDY = {
  study_id: STUDY_ID,
  name: "Focus study",
  join_code: "JOIN-AB12",
  latest_revision: {
    revision_id: "22222222-2222-2222-2222-222222222222",
    revision_number: 2,
    status: "PUBLISHED",
    protocol_digest: "abcdef0123456789",
  },
};

const renderPage = () =>
  render(
    <MemoryRouter initialEntries={["/research/studies"]}>
      <ResearchStudies />
    </MemoryRouter>,
  );

const loadStudy = async () => {
  fireEvent.change(screen.getByLabelText(/Study ID \(UUID\)/i), {
    target: { value: STUDY_ID },
  });
  fireEvent.click(screen.getByRole("button", { name: /load study/i }));
};

beforeEach(() => {
  jest.clearAllMocks();
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [] });
  api.listResearchRevisions.mockResolvedValue({ ok: true, data: [REVISION] });
  api.listResearchDrafts.mockResolvedValue({ ok: true, data: [DRAFT] });
  api.getResearchDraft.mockResolvedValue({
    ok: true,
    draft_id: DRAFT.draft_id,
    study_id: STUDY_ID,
    name: DRAFT.name,
    protocol: { schema_version: "1" },
  });
  api.getResearchExposures.mockResolvedValue({
    ok: true,
    data: [
      {
        condition_id: "arm-a",
        assigned_count: 3,
        exposed_count: 2,
        non_exposure_count: 1,
        exposure_rate: 0.6667,
        coverage: "AVAILABLE",
      },
    ],
  });
  api.getResearchEnrollmentCoverage.mockResolvedValue({
    ok: true,
    data: {
      total_enrollments: 3,
      coverage: "AVAILABLE",
    },
  });
  api.validateResearchProtocol.mockResolvedValue({
    ok: true,
    valid: true,
    errors: [],
  });
  api.publishResearchDraft.mockResolvedValue({ ok: true, data: {} });
  api.retireResearchRevision.mockResolvedValue({ ok: true, data: {} });
});

test("renders immutable revisions, drafts and derived counts", async () => {
  renderPage();
  await loadStudy();

  expect(
    await screen.findByText(/My protocol/, {}, { timeout: 3000 }),
  ).toBeInTheDocument();

  // Revision digest + status are surfaced.
  expect(screen.getByTitle(REVISION.revision_id)).toBeInTheDocument();
  expect(screen.getByText("PUBLISHED")).toBeInTheDocument();

  // Derived counts load for the selected revision.
  fireEvent.click(screen.getByRole("button", { name: /view counts/i }));

  expect(await screen.findByText("arm-a")).toBeInTheDocument();
  // Coverage state appears for both exposures and enrollment coverage.
  expect(screen.getAllByText("AVAILABLE").length).toBeGreaterThan(0);
  expect(api.getResearchExposures).toHaveBeenCalledWith(
    STUDY_ID,
    REVISION.revision_id,
  );
  // Enrollment total (3) also appears in the exposure assignment count.
  expect(screen.getAllByText("3").length).toBeGreaterThan(0);
});

test("renders the study index with join code and revision status", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [STUDY] });
  renderPage();

  expect(await screen.findByText("Focus study")).toBeInTheDocument();
  // Study-scoped join code and latest revision status are surfaced per study.
  expect(screen.getByText("JOIN-AB12")).toBeInTheDocument();
  expect(screen.getByText("PUBLISHED")).toBeInTheDocument();
  expect(screen.getByText("#2")).toBeInTheDocument();

  // The index is the primary entry point: Open loads that study's revisions.
  fireEvent.click(screen.getByRole("button", { name: /^open$/i }));
  await waitFor(() =>
    expect(api.listResearchRevisions).toHaveBeenCalledWith(STUDY_ID),
  );
});

test("shows an API error state when revisions fail to load", async () => {
  api.listResearchRevisions.mockResolvedValue({
    ok: false,
    error: "revisions exploded",
    errors: [],
  });
  renderPage();
  await loadStudy();

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(/revisions exploded/);
});

test("mints a study server-side so no UUID has to be pasted", async () => {
  api.createResearchStudy.mockResolvedValue({
    ok: true,
    data: {
      study: {
        study_id: "44444444-4444-4444-8444-444444444444",
        name: "My new study",
      },
    },
  });
  renderPage();

  fireEvent.click(
    await screen.findByRole("button", { name: /^new study$/i }),
  );
  fireEvent.change(await screen.findByLabelText(/^Name$/i), {
    target: { value: "My new study" },
  });
  fireEvent.click(screen.getByRole("button", { name: /create study/i }));

  await waitFor(() =>
    expect(api.createResearchStudy).toHaveBeenCalledWith({
      name: "My new study",
      description: null,
    }),
  );
});

test("surfaces a create-study failure instead of navigating", async () => {
  api.createResearchStudy.mockResolvedValue({
    ok: false,
    error: "not allowed",
  });
  renderPage();

  fireEvent.click(
    await screen.findByRole("button", { name: /^new study$/i }),
  );
  fireEvent.change(await screen.findByLabelText(/^Name$/i), {
    target: { value: "Denied study" },
  });
  fireEvent.click(screen.getByRole("button", { name: /create study/i }));

  await waitFor(() =>
    expect(screen.getByRole("alert")).toHaveTextContent(/not allowed/i),
  );
});

test("notes when the study index endpoint is unavailable", async () => {
  api.listResearchStudies.mockResolvedValue({
    ok: false,
    missing: true,
    error: "missing",
  });
  renderPage();

  expect(
    await screen.findByText(/does not expose a study index/i),
  ).toBeInTheDocument();
});

test("publishing a draft surfaces a conflict without inventing success", async () => {
  api.publishResearchDraft.mockResolvedValue({
    ok: false,
    status: 409,
    error: "conflict",
    errors: [],
  });
  renderPage();
  await loadStudy();

  const publishButton = await screen.findByRole("button", { name: /^publish$/i });
  fireEvent.click(publishButton);

  await waitFor(() =>
    expect(screen.getByRole("alert")).toHaveTextContent(/conflict/i),
  );
});
