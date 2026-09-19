import React from "react";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import AnalyticsNavigation from "../AnalyticsNavigation";
import StudyManagement from "../StudyManagement";
import * as api from "../../../utils/api";

jest.mock("../../../utils/api");

beforeEach(() => {
  jest.clearAllMocks();
  api.getStudies.mockResolvedValue({ ok: true, data: { studies: [] } });
});

test("admin navigation labels the legacy completion A/B flow distinctly from the research control plane", () => {
  render(
    <MemoryRouter>
      <AnalyticsNavigation activeView="studies" onViewChange={() => {}} user={{ is_admin: true }} />
    </MemoryRouter>,
  );

  expect(screen.getByText("Completion A/B (legacy)")).toBeInTheDocument();
  expect(screen.getByText("Research Control Plane")).toBeInTheDocument();
  // The old ambiguous label must not come back.
  expect(screen.queryByText("A/B Testing")).not.toBeInTheDocument();
});

test("legacy completion study management names itself as legacy completion", async () => {
  render(<StudyManagement user={{ is_admin: true }} />);

  expect(
    await screen.findByRole("heading", {
      name: "Legacy completion study management",
    }),
  ).toBeInTheDocument();
  expect(screen.queryByText(/A\/B Testing & Study Management/)).not.toBeInTheDocument();
});
