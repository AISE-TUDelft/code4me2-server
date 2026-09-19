import React from "react";
import { render, screen } from "@testing-library/react";
import StudyParticipantCoverage from "../StudyParticipantCoverage";
import * as api from "../../../utils/api";

jest.mock("../../../utils/api");

const COVERAGE = {
  ok: true,
  data: {
    study_id: "study-1",
    coverage_version: "v1",
    population: "enrolled",
    participant_count: 1,
    coverage: "AVAILABLE",
    participants: [
      {
        enrollment_id: "e-1",
        participant_code: "P-ALPHA",
        status: "ACTIVE",
        enrolled_at: "2026-09-01T10:00:00Z",
        updated_at: "2026-09-10T09:05:00Z",
        assignment: {
          assignment_id: "a-1",
          agent_profile_id: "11111111-2222-3333-4444-555555555555",
          strategy: "RANDOM_EQUAL",
          randomization_epoch: 3,
          profile_digest: "sha256:abc",
          status: "ACTIVE",
          assigned_at: "2026-09-01T10:00:00Z",
        },
        sessions: {
          total: 4,
          active: 1,
          terminal: 3,
          last_activity_at: "2026-09-10T09:00:00Z",
          last_heartbeat_at: "2026-09-10T09:04:00Z",
        },
        events: {
          total: 7,
          by_event_type: { tool_call: 4, session_heartbeat: 3 },
          by_source: { acp: 7 },
          last_occurred_at: "2026-09-10T09:04:00Z",
        },
      },
    ],
  },
};

test("renders study-local participant rows with the explicit scope label", async () => {
  api.getStudyParticipantCoverage.mockResolvedValue(COVERAGE);

  render(<StudyParticipantCoverage studyId="study-1" />);

  expect(api.getStudyParticipantCoverage).toHaveBeenCalledWith("study-1");
  expect(
    await screen.findByText(
      "Study participants — study-scoped, participant-local codes",
    ),
  ).toBeInTheDocument();
  expect(screen.getByText("P-ALPHA")).toBeInTheDocument();
  expect(screen.getByText("ACTIVE")).toBeInTheDocument();
  expect(screen.getByText(/RANDOM_EQUAL · epoch 3/)).toBeInTheDocument();
  expect(screen.getByText("4 total · 1 active · 3 terminal")).toBeInTheDocument();
  expect(screen.getByText(/Last activity:/)).toBeInTheDocument();
  expect(screen.getByText(/Last heartbeat:/)).toBeInTheDocument();
  expect(screen.getByText("7 events")).toBeInTheDocument();
  expect(
    screen.getByText("By type: tool_call: 4 · session_heartbeat: 3"),
  ).toBeInTheDocument();
  expect(screen.getByText("By source: acp: 7")).toBeInTheDocument();
});

test("shows a permission notice when the endpoint returns 403", async () => {
  api.getStudyParticipantCoverage.mockResolvedValue({
    ok: false,
    forbidden: true,
    status: 403,
    code: "FORBIDDEN",
    error:
      "Only the study owner or an administrator can view study participant coverage.",
  });

  render(<StudyParticipantCoverage studyId="study-1" />);

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(/only the study owner or an administrator/i);
  expect(screen.queryByText("P-ALPHA")).not.toBeInTheDocument();
});

test("reports an unavailable endpoint without failing the study details", async () => {
  api.getStudyParticipantCoverage.mockResolvedValue({
    ok: false,
    missing: true,
    status: 404,
    error: "Study participant coverage is not available on this server yet.",
  });

  render(<StudyParticipantCoverage studyId="study-1" />);

  expect(await screen.findByRole("alert")).toHaveTextContent(
    /not available on this server/i,
  );
});
