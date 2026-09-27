/**
 * Privacy & data page: real `api.js` with a stubbed `fetch`. Asserts the
 * method/path/body of every action, the inline confirmations and the
 * pending, error and retry states.
 */
import React from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import PrivacySettings from "../PrivacySettings";
import { formatDate } from "../../utils/format";

const jsonResponse = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  statusText: status >= 400 ? "Error" : "OK",
  json: async () => body,
});

const STATUS = {
  data_collection: { enabled: true, opted_out_at: null },
  active_study: null,
  stored_data: { queries: 42, chats: 3, agent_runs: 7, study_enrollments: 1, study_events: 240 },
  account_deletion: { allowed: true, blocked_reason: null },
};

const OPTED_OUT_AT = "2026-09-20T10:00:00Z";

const OPTED_OUT = { ...STATUS, data_collection: { enabled: false, opted_out_at: OPTED_OUT_AT } };

const apiPath = (url) => String(url).slice(String(url).indexOf("/api/"));

// Answers each request from a table keyed by "METHOD /api/path"; a value is a
// response or a function returning one (or a promise of one).
const serve = (routes) => {
  global.fetch = jest.fn((url, options = {}) => {
    const key = `${options.method || "GET"} ${apiPath(url)}`;
    const route = routes[key];
    if (!route) return Promise.reject(new Error(`Unexpected request: ${key}`));
    return Promise.resolve(typeof route === "function" ? route() : route);
  });
};

const requests = () =>
  global.fetch.mock.calls.map(([url, options = {}]) => ({
    method: options.method || "GET",
    path: apiPath(url),
    body: options.body ? JSON.parse(options.body) : undefined,
    credentials: options.credentials,
  }));

const writes = () => requests().filter((request) => request.method !== "GET");

// The "Your stored data" list as { label: count }, read through its term/definition roles.
const storedCounts = () => {
  const counts = screen.getAllByRole("definition").map((item) => item.textContent);
  return Object.fromEntries(screen.getAllByRole("term").map((item, index) => [item.textContent, counts[index]]));
};

test("shows whether data is collected and the stored data counts", async () => {
  serve({ "GET /api/user/privacy": jsonResponse(200, STATUS) });
  render(<PrivacySettings />);

  expect(screen.getByRole("status")).toHaveTextContent(/loading your privacy settings/i);
  expect(await screen.findByText("Collecting")).toBeInTheDocument();
  expect(screen.getByText(/^Code4Me stores code context, completion and chat requests/)).toBeInTheDocument();
  expect(storedCounts()).toEqual({
    "Completion and chat requests": "42",
    "Chat conversations": "3",
    "Agent runs": "7",
    "Study enrollments": "1",
    "Study events": "240",
  });
  expect(requests()).toEqual([
    { method: "GET", path: "/api/user/privacy", body: undefined, credentials: "include" },
  ]);
});

test("turning collection off without a study sends enabled: false straight away", async () => {
  serve({
    "GET /api/user/privacy": jsonResponse(200, STATUS),
    "PUT /api/user/privacy/collection": jsonResponse(200, OPTED_OUT),
  });
  render(<PrivacySettings />);

  fireEvent.click(await screen.findByRole("button", { name: "Turn off data collection" }));

  expect(await screen.findByText("Opted out")).toBeInTheDocument();
  expect(writes()).toEqual([
    { method: "PUT", path: "/api/user/privacy/collection", body: { enabled: false }, credentials: "include" },
  ]);
  expect(screen.getByRole("status")).toHaveTextContent("Data collection is turned off.");
  expect(screen.getByRole("button", { name: "Turn data collection back on" })).toBeEnabled();
});

test("with an active study, turning collection off asks to confirm the withdrawal first", async () => {
  serve({
    "GET /api/user/privacy": jsonResponse(200, { ...STATUS, active_study: { study_id: "s-1", name: "Pilot study" } }),
    "PUT /api/user/privacy/collection": jsonResponse(200, OPTED_OUT),
  });
  render(<PrivacySettings />);

  fireEvent.click(await screen.findByRole("button", { name: "Turn off data collection" }));
  const confirmation = screen.getByRole("group", { name: /withdraws you from the study/ });
  expect(confirmation).toHaveTextContent(
    "Turning off data collection also withdraws you from the study “Pilot study”. Your study data stays stored until you erase it.",
  );
  expect(confirmation).toHaveFocus();
  expect(writes()).toEqual([]);

  // Cancel sends nothing and brings the button (and focus) back.
  fireEvent.click(within(confirmation).getByRole("button", { name: "Cancel" }));
  expect(screen.queryByRole("group")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Turn off data collection" })).toHaveFocus();
  expect(writes()).toEqual([]);

  fireEvent.click(screen.getByRole("button", { name: "Turn off data collection" }));
  fireEvent.click(screen.getByRole("button", { name: "Withdraw and turn off" }));

  expect(await screen.findByText("Opted out")).toBeInTheDocument();
  expect(writes()).toEqual([
    { method: "PUT", path: "/api/user/privacy/collection", body: { enabled: false }, credentials: "include" },
  ]);
  expect(screen.getByRole("status")).toHaveTextContent("you have been withdrawn from the study “Pilot study”");
});

test("an opted-out account sees since when, and turning collection back on sends enabled: true", async () => {
  serve({
    "GET /api/user/privacy": jsonResponse(200, OPTED_OUT),
    "PUT /api/user/privacy/collection": jsonResponse(200, STATUS),
  });
  render(<PrivacySettings />);

  expect(await screen.findByText("Opted out")).toBeInTheDocument();
  expect(screen.getByText(`since ${formatDate(OPTED_OUT_AT)}`)).toBeInTheDocument();
  expect(screen.getByText(/^Code4Me no longer stores your code, prompts/)).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Turn data collection back on" }));

  expect(await screen.findByText("Collecting")).toBeInTheDocument();
  expect(writes()).toEqual([
    { method: "PUT", path: "/api/user/privacy/collection", body: { enabled: true }, credentials: "include" },
  ]);
  expect(screen.getByRole("status")).toHaveTextContent("Data collection is turned back on.");
});

test("a failed change shows the error and keeps the current status", async () => {
  serve({
    "GET /api/user/privacy": jsonResponse(200, STATUS),
    "PUT /api/user/privacy/collection": jsonResponse(401, { detail: "Authentication required" }),
  });
  render(<PrivacySettings />);

  fireEvent.click(await screen.findByRole("button", { name: "Turn off data collection" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("Authentication required");
  expect(screen.getByText("Collecting")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Turn off data collection" })).toBeEnabled();
});

test("erasing needs the acknowledgement, then posts and shows what was erased", async () => {
  let finishErase;
  serve({
    "GET /api/user/privacy": jsonResponse(200, STATUS),
    "POST /api/user/privacy/erase": () =>
      new Promise((resolve) => {
        finishErase = resolve;
      }),
  });
  render(<PrivacySettings />);

  fireEvent.click(await screen.findByRole("button", { name: "Erase my data…" }));
  const acknowledgement = screen.getByRole("checkbox", {
    name: "I understand that my data will be permanently deleted.",
  });
  expect(acknowledgement).toHaveFocus();
  expect(screen.getByRole("button", { name: "Erase my data" })).toBeDisabled();

  // Cancel resets the acknowledgement.
  fireEvent.click(acknowledgement);
  expect(screen.getByRole("button", { name: "Erase my data" })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  fireEvent.click(screen.getByRole("button", { name: "Erase my data…" }));
  expect(screen.getByRole("checkbox", { name: /my data will be permanently deleted/ })).not.toBeChecked();
  expect(screen.getByRole("button", { name: "Erase my data" })).toBeDisabled();
  expect(writes()).toEqual([]);

  fireEvent.click(screen.getByRole("checkbox", { name: /my data will be permanently deleted/ }));
  fireEvent.click(screen.getByRole("button", { name: "Erase my data" }));

  // One action at a time while the request is pending.
  expect(await screen.findByRole("button", { name: "Erasing…" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Delete my account…" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Turn off data collection" })).toBeDisabled();
  expect(writes()).toEqual([
    { method: "POST", path: "/api/user/privacy/erase", body: undefined, credentials: "include" },
  ]);

  finishErase(
    jsonResponse(200, {
      erased: { queries: 42, chats: 3, agent_runs: 7, study_enrollments: 1, study_events: 240 },
      status: {
        ...OPTED_OUT,
        stored_data: { queries: 0, chats: 0, agent_runs: 0, study_enrollments: 0, study_events: 0 },
      },
    }),
  );

  const summary = await screen.findByRole("status");
  expect(summary).toHaveTextContent("Your data has been erased.");
  expect(summary).toHaveTextContent("Completion and chat requests: 42");
  expect(summary).toHaveTextContent("Study events: 240");
  // The page shows the status the server returned.
  expect(screen.getByText("Opted out")).toBeInTheDocument();
  expect(Object.values(storedCounts())).toEqual(["0", "0", "0", "0", "0"]);
  expect(screen.getByRole("button", { name: "Erase my data…" })).toBeEnabled();
});

test("account deletion stays disabled while it is blocked and shows why", async () => {
  const reason = "Your account owns research studies. Transfer or delete them first.";
  serve({
    "GET /api/user/privacy": jsonResponse(200, { ...STATUS, account_deletion: { allowed: false, blocked_reason: reason } }),
  });
  render(<PrivacySettings />);

  expect(await screen.findByText(reason)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Delete my account…" })).toBeDisabled();
});

test("deleting the account sends DELETE and hands over to onAccountDeleted", async () => {
  const onAccountDeleted = jest.fn();
  serve({
    "GET /api/user/privacy": jsonResponse(200, STATUS),
    "DELETE /api/user/delete?delete_data=true": jsonResponse(200, { message: "User deleted successfully" }),
  });
  render(<PrivacySettings onAccountDeleted={onAccountDeleted} />);

  fireEvent.click(await screen.findByRole("button", { name: "Delete my account…" }));
  expect(screen.getByRole("button", { name: "Delete my account" })).toBeDisabled();
  fireEvent.click(
    screen.getByRole("checkbox", { name: "I understand that my account and data will be permanently deleted." }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Delete my account" }));

  await waitFor(() => expect(onAccountDeleted).toHaveBeenCalledTimes(1));
  expect(writes()).toEqual([
    { method: "DELETE", path: "/api/user/delete?delete_data=true", body: undefined, credentials: "include" },
  ]);
});

test("a refused deletion (409) shows the server's message and keeps the account", async () => {
  const onAccountDeleted = jest.fn();
  serve({
    "GET /api/user/privacy": jsonResponse(200, STATUS),
    "DELETE /api/user/delete?delete_data=true": jsonResponse(409, {
      message: "This account owns research studies or agent profiles.",
    }),
  });
  render(<PrivacySettings onAccountDeleted={onAccountDeleted} />);

  fireEvent.click(await screen.findByRole("button", { name: "Delete my account…" }));
  fireEvent.click(screen.getByRole("checkbox", { name: /account and data will be permanently deleted/ }));
  fireEvent.click(screen.getByRole("button", { name: "Delete my account" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("This account owns research studies or agent profiles.");
  expect(onAccountDeleted).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "Delete my account" })).toBeEnabled();
});

test("a failed load offers a retry", async () => {
  const responses = [jsonResponse(500, { detail: "Database unavailable" }), jsonResponse(200, STATUS)];
  serve({ "GET /api/user/privacy": () => responses.shift() });
  render(<PrivacySettings />);

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Your privacy settings could not be loaded.");
  expect(alert).toHaveTextContent("Database unavailable");
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));

  expect(await screen.findByText("Collecting")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(requests().filter((request) => request.method === "GET")).toHaveLength(2);
});
