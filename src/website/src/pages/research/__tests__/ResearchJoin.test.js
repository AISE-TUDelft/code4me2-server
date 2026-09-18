import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import * as api from "../../../utils/api";
import ResearchJoin from "../ResearchJoin";

jest.mock("../../../utils/api");

const STUDY_ID = "11111111-1111-1111-1111-111111111111";
const REVISION_ID = "22222222-2222-2222-2222-222222222222";
const ENROLLMENT_ID = "44444444-4444-4444-4444-444444444444";

const renderPage = () =>
  render(
    <MemoryRouter initialEntries={["/research/join"]}>
      <ResearchJoin />
    </MemoryRouter>,
  );

const enterCode = (code = "JOIN-1") => {
  fireEvent.change(screen.getByLabelText(/join code/i), {
    target: { value: code },
  });
};

beforeEach(() => {
  jest.clearAllMocks();
  api.resolveResearchJoinCode.mockResolvedValue({
    ok: true,
    data: {
      study: { studyId: STUDY_ID, name: "Focus Study" },
      revision: { revisionId: REVISION_ID, status: "PUBLISHED" },
      policyText: "Metadata only.",
      study_id: STUDY_ID,
      revision_id: REVISION_ID,
      name: "Focus study",
      status: "PUBLISHED",
    },
  });
  api.getMyResearchEnrollments.mockResolvedValue({ ok: true, data: [] });
  api.redeemResearchJoinCode.mockResolvedValue({
    ok: true,
    data: {
      enrollment_id: ENROLLMENT_ID,
      study: { studyId: STUDY_ID, name: "Focus Study" },
      revision: { revisionId: REVISION_ID, status: "PUBLISHED" },
      policyText: "Metadata only.",
      study_id: STUDY_ID,
      revision_id: REVISION_ID,
      status: "ACTIVE",
    },
  });
});

test("resolves and redeems a join code, showing the enrollment status", async () => {
  renderPage();
  enterCode();
  fireEvent.click(screen.getByRole("button", { name: /check code/i }));

  expect(await screen.findByText("Focus study")).toBeInTheDocument();
  expect(api.resolveResearchJoinCode).toHaveBeenCalledWith("JOIN-1");

  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: /accept & join/i }));

  expect(await screen.findByText("ACTIVE")).toBeInTheDocument();
  expect(api.redeemResearchJoinCode).toHaveBeenCalledWith("JOIN-1", true);
});

test("shows an error for an invalid or expired join code", async () => {
  api.resolveResearchJoinCode.mockResolvedValue({
    ok: false,
    status: 410,
    error: "join code expired",
    errors: [],
  });
  renderPage();
  enterCode("EXPIRED");
  fireEvent.click(screen.getByRole("button", { name: /check code/i }));

  await waitFor(() =>
    expect(screen.getByRole("alert")).toHaveTextContent(/join code expired/i),
  );
  expect(screen.queryByText("Focus study")).not.toBeInTheDocument();
});

test("shows an error when redeeming fails after the code resolved", async () => {
  api.redeemResearchJoinCode.mockResolvedValue({
    ok: false,
    status: 410,
    error: "join code expired",
    errors: [],
  });
  renderPage();
  enterCode();
  fireEvent.click(screen.getByRole("button", { name: /check code/i }));
  await screen.findByText("Focus study");

  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: /accept & join/i }));

  await waitFor(() =>
    expect(screen.getByRole("alert")).toHaveTextContent(/join code expired/i),
  );
});

test("degrades gracefully when the join endpoints are unavailable", async () => {
  api.resolveResearchJoinCode.mockResolvedValue({
    ok: false,
    missing: true,
    error: "The join service is not available on this server yet.",
  });
  renderPage();
  enterCode();
  fireEvent.click(screen.getByRole("button", { name: /check code/i }));

  expect(await screen.findByRole("alert")).toHaveTextContent(
    /not available on this server yet/i,
  );
  // The form stays usable for a retry once the server exposes the endpoint.
  expect(screen.getByRole("button", { name: /check code/i })).toBeEnabled();
});

test("requires the join code before checking", async () => {
  renderPage();
  fireEvent.click(screen.getByRole("button", { name: /check code/i }));

  expect(await screen.findByRole("alert")).toHaveTextContent(
    /enter the join code/i,
  );
  expect(api.resolveResearchJoinCode).not.toHaveBeenCalled();
});

test("an active enrollment for the code shows its status instead of asking again", async () => {
  api.getMyResearchEnrollments.mockResolvedValue({
    ok: true,
    data: [
      {
        enrollment_id: ENROLLMENT_ID,
        study_id: STUDY_ID,
        study_revision_id: REVISION_ID,
        status: "ACTIVE",
      },
    ],
  });
  renderPage();
  enterCode();
  fireEvent.click(screen.getByRole("button", { name: /check code/i }));

  expect(await screen.findByText("ACTIVE")).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent(/already enrolled/i);
  // The policy is not requested again and nothing is redeemed.
  expect(
    screen.queryByRole("button", { name: /accept & join/i }),
  ).not.toBeInTheDocument();
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  expect(api.redeemResearchJoinCode).not.toHaveBeenCalled();
});

test("an active enrollment in another study explains and shows it without re-enrolling", async () => {
  const OTHER_STUDY = "99999999-9999-9999-9999-999999999999";
  const OTHER_REVISION = "88888888-8888-8888-8888-888888888888";
  api.getMyResearchEnrollments.mockResolvedValue({
    ok: true,
    data: [
      {
        enrollment_id: ENROLLMENT_ID,
        study_id: OTHER_STUDY,
        study_revision_id: OTHER_REVISION,
        status: "ACTIVE",
      },
    ],
  });
  renderPage();
  enterCode();
  fireEvent.click(screen.getByRole("button", { name: /check code/i }));

  expect(await screen.findByRole("alert")).toHaveTextContent(
    /already enrolled in another research study/i,
  );
  // The other enrollment is viewable, and the accept step is not shown.
  expect(
    screen.getByRole("heading", { name: /your current enrollment/i }),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: /accept & join/i }),
  ).not.toBeInTheDocument();
  expect(api.redeemResearchJoinCode).not.toHaveBeenCalled();
});
