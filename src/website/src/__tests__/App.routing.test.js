import React from "react";
import { render, screen } from "@testing-library/react";
import App from "../App";
import * as api from "../utils/api";

jest.mock("../utils/api");
// Isolate the route guards: the pages themselves have their own suites.
jest.mock("../pages/Dashboard", () => () => <div>DASHBOARD-PAGE</div>);
jest.mock("../components/auth/Auth", () => () => <div>LOGIN-PAGE</div>);
jest.mock("../pages/research/ResearchStudies", () => () => <div>STUDIES-PAGE</div>);
jest.mock("../pages/research/ResearchStudyEditor", () => () => (
  <div>EDITOR-PAGE</div>
));
jest.mock("../pages/research/ResearchJoin", () => () => <div>JOIN-PAGE</div>);

const STUDY_ID = "11111111-1111-1111-1111-111111111111";

const renderAt = (path) => {
  window.history.pushState({}, "", path);
  return render(<App />);
};

beforeEach(() => {
  jest.clearAllMocks();
  process.env.REACT_APP_GOOGLE_CLIENT_ID = "test-client-id";
  api.getCurrentUser.mockResolvedValue({
    ok: true,
    user: { email: "researcher@example.com", is_admin: false },
    config: {},
  });
  api.logoutUser.mockResolvedValue({ ok: true });
});

test("a non-admin authenticated user can reach the study editor route", async () => {
  renderAt(`/research/studies/${STUDY_ID}/editor`);

  expect(await screen.findByText("EDITOR-PAGE")).toBeInTheDocument();
  expect(screen.queryByText("DASHBOARD-PAGE")).not.toBeInTheDocument();
});

test("a non-admin authenticated user can reach the studies route", async () => {
  renderAt("/research/studies");

  expect(await screen.findByText("STUDIES-PAGE")).toBeInTheDocument();
});

test("an unauthenticated visitor is redirected to login", async () => {
  api.getCurrentUser.mockResolvedValue({ ok: false });
  renderAt(`/research/studies/${STUDY_ID}/editor`);

  expect(await screen.findByText("LOGIN-PAGE")).toBeInTheDocument();
  expect(screen.queryByText("EDITOR-PAGE")).not.toBeInTheDocument();
});

test("registered route tag count matches the route enumeration (ISSUE-007)", () => {
  // Pins the App.js route inventory: every <Route tag counted by
  // grep -cE '<Route[ >]|<Route$' (== 13) resolves to an intended surface:
  // /, /login, /signup, the signed-in shell layout, /dashboard, /research
  // (redirect), /research/studies, /research/studies/:studyId,
  // /research/studies/:studyId/editor, /research/my-studies, /research/join
  // (+ its index) and the catch-all.
  const fs = require("fs");
  const path = require("path");
  const source = fs.readFileSync(path.join(__dirname, "..", "App.js"), "utf8");
  const matches = source.match(/<Route[ >]|<Route$/gm) || [];
  expect(matches.length).toBe(13);
});

test("a study deep link renders the studies workspace", async () => {
  renderAt(`/research/studies/${STUDY_ID}`);

  expect(await screen.findByText("STUDIES-PAGE")).toBeInTheDocument();
});

test("a signed-in participant reaches My studies inside the application shell", async () => {
  renderAt("/research/my-studies");

  expect(await screen.findByText("JOIN-PAGE")).toBeInTheDocument();
  expect(screen.getByRole("navigation", { name: "Main navigation" })).toBeInTheDocument();
});

test("the join page stays reachable without a session", async () => {
  api.getCurrentUser.mockResolvedValue({ ok: false });
  renderAt("/research/join");

  expect(await screen.findByText("JOIN-PAGE")).toBeInTheDocument();
  expect(screen.queryByRole("navigation", { name: "Main navigation" })).not.toBeInTheDocument();
  expect(screen.queryByText("LOGIN-PAGE")).not.toBeInTheDocument();
});

test("only public profile fields are cached locally (never the auth token)", async () => {
  api.getCurrentUser.mockResolvedValue({
    ok: true,
    user: {
      email: "researcher@example.com",
      is_admin: false,
      auth_token: "live-session-token",
      password: "********",
    },
    config: {},
  });
  renderAt("/research/studies");

  expect(await screen.findByText("STUDIES-PAGE")).toBeInTheDocument();
  const stored = localStorage.getItem("user") || "";
  expect(stored).toContain("researcher@example.com");
  expect(stored).not.toContain("live-session-token");
  expect(stored).not.toContain("password");
});
