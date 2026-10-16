import React from "react";
import { render, screen } from "@testing-library/react";
import AgentAnalytics from "../AgentAnalytics";
import * as api from "../../../utils/api";

jest.mock("../../../utils/api");

test("labels agent analytics as personal scope, not participant results", async () => {
  api.getAgentOverview.mockResolvedValue({ ok: true, data: {} });

  render(<AgentAnalytics timeWindow="7d" />);

  expect(
    await screen.findByText("Your own agent runs — personal scope"),
  ).toBeInTheDocument();
});
