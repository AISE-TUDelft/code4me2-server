/**
 * Dashboard view routing: the archived analytics views and any view the role
 * may not open redirect to the user's home page; admin and research views
 * still render from their `?view=` deep links.
 */
import React from "react";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import Dashboard from "../Dashboard";

jest.mock("../../components/analytics/ConfigManagement", () => () => <div>CONFIGS-VIEW</div>);
jest.mock("../AgentProfiles", () => () => <div>AGENT-PROFILES-VIEW</div>);
jest.mock("../AdminResearchers", () => () => <div>ACCOUNTS-VIEW</div>);
jest.mock("../AdminConnections", () => () => <div>CONNECTIONS-VIEW</div>);
jest.mock("../AdminAgents", () => () => <div>AGENTS-VIEW</div>);

const ADMIN = { is_admin: true };
const RESEARCHER = { is_admin: false, can_research: true };
const PARTICIPANT = { is_admin: false, can_research: false };

const CurrentPath = () => {
  const location = useLocation();
  return <div data-testid="path">{`${location.pathname}${location.search}`}</div>;
};

const renderAt = (path, user) =>
  render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={[path]}>
      <Routes>
        <Route path="/dashboard" element={<Dashboard user={user} />} />
        <Route path="*" element={<div>HOME</div>} />
      </Routes>
      <CurrentPath />
    </MemoryRouter>,
  );

test.each(["overview", "usage", "models", "agents", "calibration"])(
  "the archived %s view redirects home",
  async (view) => {
    renderAt(`/dashboard?view=${view}&timeWindow=30d`, RESEARCHER);
    expect(await screen.findByText("HOME")).toBeInTheDocument();
    expect(screen.getByTestId("path")).toHaveTextContent(/^\/research\/studies$/);
  },
);

test("a bare /dashboard sends a participant to My studies", async () => {
  renderAt("/dashboard", PARTICIPANT);
  expect(await screen.findByText("HOME")).toBeInTheDocument();
  expect(screen.getByTestId("path")).toHaveTextContent(/^\/research\/my-studies$/);
});

test("a non-admin cannot open an admin view", async () => {
  renderAt("/dashboard?view=admin-researchers", RESEARCHER);
  expect(await screen.findByText("HOME")).toBeInTheDocument();
  expect(screen.queryByText("ACCOUNTS-VIEW")).not.toBeInTheDocument();
});

test.each([
  ["admin-researchers", ADMIN, "ACCOUNTS-VIEW"],
  ["admin-connections", ADMIN, "CONNECTIONS-VIEW"],
  ["admin-agents", ADMIN, "AGENTS-VIEW"],
  ["configs", ADMIN, "CONFIGS-VIEW"],
  ["agent-profiles", RESEARCHER, "AGENT-PROFILES-VIEW"],
])("the %s view still renders", (view, user, marker) => {
  renderAt(`/dashboard?view=${view}`, user);
  expect(screen.getByText(marker)).toBeInTheDocument();
});

test("a legacy hash link is rewritten to the query form", async () => {
  renderAt("/dashboard#configs", ADMIN);
  expect(screen.getByText("CONFIGS-VIEW")).toBeInTheDocument();
  expect(await screen.findByText("/dashboard?view=configs")).toBeInTheDocument();
});
