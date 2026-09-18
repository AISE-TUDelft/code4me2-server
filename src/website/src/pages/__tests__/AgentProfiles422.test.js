/**
 * A5 regression: a 422 from the agent-profile endpoints must not crash the page
 * and must surface a readable message. Uses the real `api.js` (no module mock)
 * with a stubbed `fetch`, which is what previously returned a raw `detail`
 * object/array straight into JSX.
 */
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import AgentProfiles from "../AgentProfiles";

const PROFILE = {
  profile_id: "p1",
  name: "arm-a",
  model: "model-a",
  framework_version: "code4me2-agent",
  connection: {
    connection_id: "c1",
    label: "primary",
    models: ["model-a"],
    is_active: true,
    ready: true,
  },
  tools_json: "[]",
  approval_policy: "per_step",
  max_steps: 15,
  temperature: null,
  max_context_tokens: null,
  is_active: true,
  release_id: "rel-1",
  release_version: "1.2.0",
  verified: true,
  supported_platforms: [],
};

const jsonResponse = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  statusText: status === 422 ? "Unprocessable Entity" : "OK",
  json: async () => body,
});

let submitBody;

beforeEach(() => {
  submitBody = null;
  global.fetch = jest.fn((url, options = {}) => {
    const target = String(url);
    const method = options.method || "GET";
    if (method === "POST" && target.includes("/api/agent/profiles")) {
      return Promise.resolve(jsonResponse(422, submitBody));
    }
    if (target.includes("/api/agent/profiles")) {
      return Promise.resolve(jsonResponse(200, { profiles: [PROFILE] }));
    }
    if (target.includes("/provider-connections")) {
      return Promise.resolve(
        jsonResponse(200, { connections: [PROFILE.connection] }),
      );
    }
    if (target.includes("/api/research/agents/distributions")) {
      return Promise.resolve(
        jsonResponse(200, {
          distributions: [
            {
              distribution_id: "d1",
              name: "arm-a",
              release_id: "rel-1",
              release_version: "1.2.0",
              verified: true,
            },
          ],
        }),
      );
    }
    if (target.includes("/available-tools")) {
      return Promise.resolve(jsonResponse(200, { tools: [], frameworks: [] }));
    }
    return Promise.resolve(jsonResponse(200, {}));
  });
});

const renderAndSubmit = async () => {
  render(<AgentProfiles user={{ is_admin: true }} />);
  await screen.findByText("arm-a");
  // Wait for the connection and release catalogues before interacting.
  await screen.findByRole("option", { name: /primary/ });
  await screen.findByRole("option", { name: /rel-1/ });

  fireEvent.change(screen.getByLabelText(/Profile name/i), {
    target: { value: "new-arm" },
  });
  fireEvent.change(screen.getByLabelText(/Provider connection/i), {
    target: { value: "c1" },
  });
  fireEvent.change(screen.getByLabelText(/^Model$/i), {
    target: { value: "model-a" },
  });
  fireEvent.change(screen.getByLabelText(/Registered release/i), {
    target: { value: "rel-1" },
  });
  fireEvent.click(screen.getByRole("button", { name: /create profile/i }));
  return waitFor(() =>
    expect(
      global.fetch.mock.calls.some(([, options]) => options && options.method === "POST"),
    ).toBe(true),
  );
};

test("a FastAPI 422 detail array renders a readable message without crashing", async () => {
  submitBody = {
    detail: [
      {
        type: "value_error",
        loc: ["body", "connection_id"],
        msg: "field required",
        input: null,
      },
    ],
  };

  await renderAndSubmit();

  await waitFor(() => {
    expect(screen.getAllByText(/field required/).length).toBeGreaterThan(0);
  });
  expect(screen.getAllByText(/connection_id/).length).toBeGreaterThan(0);
});

test("a custom {code,field,message} 422 renders its message without crashing", async () => {
  submitBody = {
    detail: {
      code: "MODEL_NOT_ALLOWED",
      field: "model",
      message: "model 'model-a' is not allowed by connection 'primary'",
    },
  };

  await renderAndSubmit();

  await waitFor(() => {
    expect(
      screen.getAllByText(
        /model 'model-a' is not allowed by connection 'primary'/,
      ).length,
    ).toBeGreaterThan(0);
  });
});
