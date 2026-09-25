/**
 * Admin provider-connection view: real `api.js` with a stubbed `fetch`. Asserts
 * the create request method/path/body, the server `ready` flag rendering, and
 * that only the secret *name* is ever rendered.
 */
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import AdminConnections from "../AdminConnections";

const SECRET_VALUE = "sk-live-do-not-render-1234567890";

const jsonResponse = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  statusText: status === 422 ? "Unprocessable Entity" : "OK",
  json: async () => body,
});

const CONNECTION = {
  connection_id: "c-1",
  label: "primary",
  base_url: "https://api.example.com/v1",
  secret_ref: "OPENAI_API_KEY",
  models: ["model-a", "model-b"],
  is_active: true,
  ready: true,
};

const requests = () => global.fetch.mock.calls.map(([url, options = {}]) => ({
  url: String(url),
  method: options.method || "GET",
  body: options.body ? JSON.parse(options.body) : undefined,
}));

beforeEach(() => {
  global.fetch = jest.fn(() =>
    Promise.resolve(jsonResponse(200, { connections: [CONNECTION] })),
  );
});

test("shows the loading state then the server readiness flag", async () => {
  render(<AdminConnections />);
  expect(screen.getByRole("status")).toHaveTextContent(/loading provider connections/i);
  expect(await screen.findByText("primary")).toBeInTheDocument();
  expect(screen.getByText(/ready — secret present in deployment/i)).toBeInTheDocument();
  expect(screen.getByText("OPENAI_API_KEY")).toBeInTheDocument();
  // Only the env-var name is rendered; a secret value would never be in the payload.
  expect(screen.queryByText(SECRET_VALUE)).not.toBeInTheDocument();
});

test("renders a not-ready badge when the deployment lacks the secret", async () => {
  global.fetch = jest.fn(() =>
    Promise.resolve(
      jsonResponse(200, { connections: [{ ...CONNECTION, ready: false }] }),
    ),
  );
  render(<AdminConnections />);
  expect(
    await screen.findByText(/not ready — secret missing or inactive/i),
  ).toBeInTheDocument();
});

test("creates a connection by POSTing the normalized payload", async () => {
  global.fetch = jest.fn((url, options = {}) => {
    if ((options.method || "GET") === "POST") {
      return Promise.resolve(jsonResponse(201, { connection: CONNECTION }));
    }
    return Promise.resolve(jsonResponse(200, { connections: [CONNECTION] }));
  });

  render(<AdminConnections />);
  await screen.findByText("primary");

  fireEvent.click(screen.getByRole("button", { name: /new connection/i }));
  fireEvent.change(screen.getByLabelText(/^Label$/i), {
    target: { value: "secondary" },
  });
  fireEvent.change(screen.getByLabelText(/base url/i), {
    target: { value: "https://second.example/v1" },
  });
  fireEvent.change(screen.getByLabelText(/secret name/i), {
    target: { value: "OPENAI_API_KEY" },
  });
  // Several models at once: a pasted list (new lines and commas) becomes chips.
  // A text input strips new lines from typed values, so pasting is the path.
  fireEvent.paste(screen.getByLabelText(/^Models/i), {
    clipboardData: { getData: () => "model-a\nmodel-b, model-c" },
  });
  expect(screen.getByRole("button", { name: "Remove model-b" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /create connection/i }));

  await waitFor(() => {
    const post = requests().find((r) => r.method === "POST");
    expect(post).toBeTruthy();
    expect(post.url).toContain("/api/research/provider-connections");
    expect(post.body).toEqual({
      label: "secondary",
      base_url: "https://second.example/v1",
      secret_ref: "OPENAI_API_KEY",
      models: ["model-a", "model-b", "model-c"],
      is_active: true,
    });
    // The secret value is never part of the outbound payload.
    expect(JSON.stringify(post.body)).not.toContain(SECRET_VALUE);
  });
});

test("renders a typed create error without a raw detail object", async () => {
  global.fetch = jest.fn((url, options = {}) => {
    if ((options.method || "GET") === "POST") {
      return Promise.resolve(
        jsonResponse(422, {
          detail: {
            code: "SECRET_REF_INVALID",
            field: "secret_ref",
            message: "secret_ref must be the NAME of an environment variable",
          },
        }),
      );
    }
    return Promise.resolve(jsonResponse(200, { connections: [] }));
  });

  render(<AdminConnections />);
  await screen.findByText("No provider connections yet.");

  fireEvent.click(screen.getByRole("button", { name: /new connection/i }));
  fireEvent.change(screen.getByLabelText(/^Label$/i), {
    target: { value: "bad" },
  });
  fireEvent.change(screen.getByLabelText(/base url/i), {
    target: { value: "https://second.example/v1" },
  });
  fireEvent.change(screen.getByLabelText(/secret name/i), {
    target: { value: "OPENAI_API_KEY" },
  });
  // Typed but not yet added with "+": still part of the submitted list.
  fireEvent.change(screen.getByLabelText(/^Models/i), {
    target: { value: "model-a" },
  });
  fireEvent.click(screen.getByRole("button", { name: /create connection/i }));

  expect(
    (await screen.findAllByText(/must be the NAME of an environment variable/i)).length,
  ).toBeGreaterThan(0);
});

test("the add button adds one model and the pending text is kept in the payload", async () => {
  global.fetch = jest.fn((url, options = {}) => {
    if ((options.method || "GET") === "POST") {
      return Promise.resolve(jsonResponse(201, { connection: CONNECTION }));
    }
    return Promise.resolve(jsonResponse(200, { connections: [] }));
  });

  render(<AdminConnections />);
  await screen.findByText("No provider connections yet.");
  fireEvent.click(screen.getByRole("button", { name: /new connection/i }));
  fireEvent.change(screen.getByLabelText(/^Label$/i), { target: { value: "third" } });
  fireEvent.change(screen.getByLabelText(/base url/i), { target: { value: "https://third.example/v1" } });
  fireEvent.change(screen.getByLabelText(/secret name/i), { target: { value: "THIRD_KEY" } });

  const models = screen.getByLabelText(/^Models/i);
  fireEvent.change(models, { target: { value: "model-x" } });
  fireEvent.click(screen.getByRole("button", { name: "Add model" }));
  expect(screen.getByRole("button", { name: "Remove model-x" })).toBeInTheDocument();
  fireEvent.change(models, { target: { value: "model-y" } });
  fireEvent.click(screen.getByRole("button", { name: /create connection/i }));

  await waitFor(() => {
    const post = requests().find((r) => r.method === "POST");
    expect(post).toBeTruthy();
    expect(post.body.models).toEqual(["model-x", "model-y"]);
  });
});

test("the card switch deactivates a connection with a full update", async () => {
  global.fetch = jest.fn((url, options = {}) => {
    if ((options.method || "GET") === "PUT") {
      return Promise.resolve(jsonResponse(200, { connection: { ...CONNECTION, is_active: false } }));
    }
    return Promise.resolve(jsonResponse(200, { connections: [CONNECTION] }));
  });

  render(<AdminConnections />);
  await screen.findByText("primary");
  fireEvent.click(screen.getByRole("switch", { name: /primary active/i }));

  await waitFor(() => {
    const put = requests().find((r) => r.method === "PUT");
    expect(put).toBeTruthy();
    expect(put.url).toContain("/api/research/provider-connections/c-1");
    expect(put.body).toEqual({
      label: "primary",
      base_url: "https://api.example.com/v1",
      secret_ref: "OPENAI_API_KEY",
      models: ["model-a", "model-b"],
      is_active: false,
    });
  });
});

test("deleting a connection in use warns with the profile count", async () => {
  global.fetch = jest.fn(() =>
    Promise.resolve(jsonResponse(200, { connections: [{ ...CONNECTION, profile_count: 2 }] })),
  );
  const confirm = jest.spyOn(window, "confirm").mockReturnValue(false);
  render(<AdminConnections />);
  await screen.findByText("primary");
  expect(screen.getByText("2 agent profiles")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: /delete/i }));
  expect(confirm.mock.calls[0][0]).toMatch(/used by 2 agent profiles/i);
  expect(requests().some((r) => r.method === "DELETE")).toBe(false);
  confirm.mockRestore();
});
