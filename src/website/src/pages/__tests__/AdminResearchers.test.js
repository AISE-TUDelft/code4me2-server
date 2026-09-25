/**
 * Admin account view: real `api.js` with a stubbed `fetch`. Asserts the request
 * method/path/body of the researcher toggle and the loading/empty/error states.
 */
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import AdminResearchers from "../AdminResearchers";

const renderPage = () =>
  render(
    <MemoryRouter>
      <AdminResearchers />
    </MemoryRouter>,
  );

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
  renderPage();
  expect(screen.getByRole("status")).toHaveTextContent(/loading accounts/i);
});

test("renders an explicit empty state", async () => {
  renderPage();
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

  renderPage();
  expect(await screen.findByText("researcher@local.dev")).toBeInTheDocument();
  expect(screen.getByText("Verified")).toBeInTheDocument();

  fireEvent.click(
    screen.getByRole("switch", { name: /researcher access for researcher@local\.dev/i }),
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

  renderPage();
  await screen.findByText("researcher@local.dev");
  fireEvent.click(
    screen.getByRole("switch", { name: /researcher access for researcher@local\.dev/i }),
  );

  expect(
    (await screen.findAllByText(/admin privileges required/i)).length,
  ).toBeGreaterThan(0);
  expect(screen.getByText("researcher@local.dev")).toBeInTheDocument();
});

test("sends search and filters to the server and shows enrollment study links", async () => {
  const enrolled = {
    ...ACCOUNT,
    user_id: "u-2",
    email: "participant@local.dev",
    name: "Participant",
    enrollments: [
      {
        study_id: "s-1",
        study_name: "Pilot study",
        study_status: "ACTIVE",
        status: "ACTIVE",
      },
    ],
  };
  global.fetch = jest.fn(() =>
    Promise.resolve(jsonResponse(200, { researchers: [enrolled], total: 1, limit: 50, offset: 0 })),
  );

  renderPage();
  const link = await screen.findByRole("link", { name: "Pilot study" });
  expect(link).toHaveAttribute("href", "/research/studies/s-1");
  expect(screen.getByText("Active")).toBeInTheDocument();
  // Participant codes and arms are never shown on the account list.
  expect(screen.queryByText(/P-[A-Z0-9]+/)).not.toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("Filter by role"), { target: { value: "participant" } });
  fireEvent.change(screen.getByLabelText("Filter by enrollment"), { target: { value: "enrolled" } });
  fireEvent.change(screen.getByLabelText("Search accounts"), { target: { value: "partic" } });

  await waitFor(() => {
    const listing = requests()
      .filter((r) => r.method === "GET" && r.url.includes("/api/research/researchers?"))
      .map((r) => r.url);
    expect(
      listing.some(
        (url) => url.includes("role=participant") && url.includes("enrollment=enrolled") && url.includes("q=partic"),
      ),
    ).toBe(true);
  });
});

test("administrators have research access by role and revoking asks for confirmation", async () => {
  const admin = { ...ACCOUNT, user_id: "u-3", email: "admin@local.dev", is_admin: true };
  const researcher = { ...ACCOUNT, user_id: "u-4", email: "r@local.dev", can_research: true };
  global.fetch = jest.fn(() =>
    Promise.resolve(jsonResponse(200, { researchers: [admin, researcher], total: 2 })),
  );
  const confirm = jest.spyOn(window, "confirm").mockReturnValue(false);

  renderPage();
  await screen.findByText("admin@local.dev");
  expect(screen.getByText("Included with admin")).toBeInTheDocument();
  expect(screen.queryByRole("switch", { name: /admin@local\.dev/i })).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("switch", { name: /researcher access for r@local\.dev/i }));
  expect(confirm).toHaveBeenCalled();
  // Declining the confirmation sends no write.
  expect(requests().some((r) => r.method === "PUT")).toBe(false);
  confirm.mockRestore();
});

test("summary tiles only render when the server reports real totals", async () => {
  global.fetch = jest.fn(() => Promise.resolve(jsonResponse(200, { researchers: [ACCOUNT] })));
  renderPage();
  await screen.findByText("researcher@local.dev");
  // "Research access granted" is the researchers tile detail (tiles only).
  expect(screen.queryByText("Research access granted")).not.toBeInTheDocument();
});
