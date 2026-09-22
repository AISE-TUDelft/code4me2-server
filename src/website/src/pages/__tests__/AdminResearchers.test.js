/**
 * Admin account view: real `api.js` with a stubbed `fetch`. Asserts the request
 * method/path/body of the researcher toggle and the loading/empty/error states.
 */
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import AdminResearchers from "../AdminResearchers";

const jsonResponse = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  statusText: status === 403 ? "Forbidden" : "OK",
  json: async () => body,
});

const ACCOUNT = {
  user_id: "u-1",
  email: "researcher@local.dev",
  name: "Researcher",
  is_admin: false,
  can_research: false,
  verified: true,
};

beforeEach(() => {
  global.fetch = jest.fn(() => Promise.resolve(jsonResponse(200, { researchers: [] })));
});

const requests = () => global.fetch.mock.calls.map(([url, options = {}]) => ({
  url: String(url),
  method: options.method || "GET",
  body: options.body ? JSON.parse(options.body) : undefined,
}));

test("shows the loading state before accounts resolve", () => {
  global.fetch = jest.fn(() => new Promise(() => {}));
  render(<AdminResearchers />);
  expect(screen.getByRole("status")).toHaveTextContent(/loading accounts/i);
});

test("renders an explicit empty state", async () => {
  render(<AdminResearchers />);
  expect(await screen.findByText("No accounts found.")).toBeInTheDocument();
  expect(requests().some((r) => r.method === "GET" && r.url.includes("/api/research/researchers"))).toBe(true);
});

test("lists every account and toggles researcher access with a real PUT", async () => {
  global.fetch = jest.fn((url, options = {}) => {
    if ((options.method || "GET") === "PUT") {
      return Promise.resolve(jsonResponse(200, { user: { ...ACCOUNT, can_research: true } }));
    }
    return Promise.resolve(jsonResponse(200, { researchers: [ACCOUNT] }));
  });

  render(<AdminResearchers />);
  expect(await screen.findByText("researcher@local.dev")).toBeInTheDocument();
  expect(screen.getByText("Verified")).toBeInTheDocument();

  fireEvent.click(
    screen.getByRole("checkbox", { name: /researcher access for researcher@local\.dev/i }),
  );

  await waitFor(() => {
    const put = requests().find((r) => r.method === "PUT");
    expect(put).toBeTruthy();
    expect(put.url).toContain("/api/research/researchers/u-1");
    expect(put.body).toEqual({ can_research: true });
  });
  // A successful toggle refetches rather than trusting optimistic state.
  await waitFor(() =>
    expect(requests().filter((r) => r.method === "GET").length).toBeGreaterThanOrEqual(2),
  );
});

test("surfaces a readable error and keeps the row when the toggle fails", async () => {
  global.fetch = jest.fn((url, options = {}) => {
    if ((options.method || "GET") === "PUT") {
      return Promise.resolve(
        jsonResponse(403, {
          detail: { code: "ADMIN_REQUIRED", message: "Admin privileges required" },
        }),
      );
    }
    return Promise.resolve(jsonResponse(200, { researchers: [ACCOUNT] }));
  });

  render(<AdminResearchers />);
  await screen.findByText("researcher@local.dev");
  fireEvent.click(
    screen.getByRole("checkbox", { name: /researcher access for researcher@local\.dev/i }),
  );

  expect(
    (await screen.findAllByText(/admin privileges required/i)).length,
  ).toBeGreaterThan(0);
  expect(screen.getByText("researcher@local.dev")).toBeInTheDocument();
});
