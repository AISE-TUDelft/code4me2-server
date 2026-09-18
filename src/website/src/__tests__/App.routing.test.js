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
