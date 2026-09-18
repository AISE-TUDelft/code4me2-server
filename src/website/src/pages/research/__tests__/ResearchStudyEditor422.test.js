import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
// Use the real api module (not the auto-mock) so the 422 parsing path is the
// one under test; only `fetch` is stubbed.
import ResearchStudyEditor from "../ResearchStudyEditor";

const STUDY_ID = "11111111-1111-1111-1111-111111111111";

const FIELD_ERRORS = [
  {
    code: "SCHEDULE_INVALID",
    field: "schedule.ends_at",
    message: "the study end date must be after the start date",
    severity: "ERROR",
  },
  {
    code: "ENVIRONMENT_PROTOCOL_VERSION_MISSING",
    field: "environment_requirements.expected_protocol_version",
    message: "an expected ACP protocol version is required",
    severity: "ERROR",
  },
];

const jsonResponse = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  statusText: status === 422 ? "Unprocessable Entity" : "OK",
  json: async () => body,
  text: async () => JSON.stringify(body),
});

const renderEditor = () =>
  render(
    <MemoryRouter initialEntries={[`/research/studies/${STUDY_ID}/editor`]}>
      <Routes>
        <Route
          path="/research/studies/:studyId/editor"
          element={<ResearchStudyEditor />}
        />
      </Routes>
    </MemoryRouter>,
  );

const mockValidateBody = (body) => {
  global.fetch = jest.fn((url) => {
    const target = String(url);
    if (target.includes("/api/agent/profiles")) {
      return Promise.resolve(jsonResponse(200, { profiles: [] }));
    }
    if (target.includes("/api/research/studies/drafts/validate")) {
      return Promise.resolve(jsonResponse(422, body));
    }
    return Promise.reject(new Error(`Unexpected fetch: ${target}`));
  });
};

const validateAndAssertFieldErrors = async () => {
  renderEditor();
  await screen.findByRole("heading", { name: /new study protocol draft/i });
  fireEvent.click(screen.getByRole("button", { name: /^validate$/i }));

  await waitFor(() =>
    expect(
      screen.getAllByText(/study end date must be after/i).length,
    ).toBeGreaterThan(0),
  );

  // Each typed field error is shown next to its field and in the summary.
  expect(screen.getAllByText("schedule.ends_at").length).toBeGreaterThan(0);
  expect(
    screen.getAllByText("environment_requirements.expected_protocol_version")
      .length,
  ).toBeGreaterThan(0);
  expect(
    screen.getAllByText(/an expected ACP protocol version is required/i).length,
  ).toBeGreaterThan(0);

  // The raw HTTP status must never be the only thing shown.
  expect(
    screen.queryByText(/422 unprocessable entity/i),
  ).not.toBeInTheDocument();
};

beforeEach(() => {
  // Silence the expected JSDOM "not wrapped in act" React warnings.
  jest.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  console.error.mockRestore();
  delete global.fetch;
});

test("a 422 detail array renders the field messages", async () => {
  mockValidateBody({ detail: FIELD_ERRORS });
  await validateAndAssertFieldErrors();
});

test("a 422 valid/errors body (the validate endpoint shape) renders field messages", async () => {
  // The /studies/drafts/validate endpoint returns {valid, errors}, not {detail}.
  // Collapsing that to "422 Unprocessable Entity" is the regression under test.
  mockValidateBody({ valid: false, errors: FIELD_ERRORS });
  await validateAndAssertFieldErrors();
});
