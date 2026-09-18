// Real-module API contract tests: import the production `api` module and stub
// only `fetch`, so the request/response shape mapping is exercised for real
// (component-mock-only tests cannot catch a flat-vs-nested response bug).
import * as api from "../api";

const jsonResponse = (body, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

beforeEach(() => {
  jest.restoreAllMocks();
  global.fetch = jest.fn();
  // The module wraps window.fetch at load time; make sure the stub is the one
  // the request helper resolves.
  window.fetch = global.fetch;
});

test("getResearchStudyJoinCode reads the nested revision summary", async () => {
  global.fetch.mockResolvedValue(
    jsonResponse({
      study_id: "s-1",
      join_code: "JOIN-AB12",
      revision: {
        revision_id: "r-1",
        revision_number: 3,
        status: "PUBLISHED",
        protocol_digest: "abcdef01",
      },
    }),
  );

  const result = await api.getResearchStudyJoinCode("s-1");

  expect(result).toMatchObject({
    ok: true,
    join_code: "JOIN-AB12",
    revision_id: "r-1",
    revision_number: 3,
    status: "PUBLISHED",
    protocol_digest: "abcdef01",
  });
});

test("listResearchStudies returns the studies array from the index envelope", async () => {
  global.fetch.mockResolvedValue(
    jsonResponse({
      studies: [
        {
          study_id: "s-1",
          name: "Focus study",
          join_code: "JOIN-AB12",
          latest_revision: { revision_number: 2, status: "PUBLISHED" },
        },
      ],
    }),
  );

  const result = await api.listResearchStudies();

  expect(result.ok).toBe(true);
  expect(result.data).toHaveLength(1);
  expect(result.data[0].latest_revision.status).toBe("PUBLISHED");
});

test("listResearchStudies surfaces a 403 as a typed researcher error", async () => {
  global.fetch.mockResolvedValue(
    jsonResponse({ detail: { code: "RESEARCHER_REQUIRED" } }, 403),
  );

  const result = await api.listResearchStudies();

  expect(result.ok).toBe(false);
  expect(result.forbidden).toBe(true);
  expect(result.error).toMatch(/not enabled for research/i);
});
