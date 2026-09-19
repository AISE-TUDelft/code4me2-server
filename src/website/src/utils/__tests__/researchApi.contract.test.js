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

test("getResearchStudyJoinCode reads the study-owned join code", async () => {
  global.fetch.mockResolvedValue(
    jsonResponse({
      study: {
        study_id: "s-1",
        join_code: "JOIN-AB12",
        research_status: "DRAFT",
      },
    }),
  );

  const result = await api.getResearchStudyJoinCode("s-1");

  expect(result).toMatchObject({
    ok: true,
    join_code: "JOIN-AB12",
    status: "DRAFT",
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
          research_status: "DRAFT",
        },
      ],
    }),
  );

  const result = await api.listResearchStudies();

  expect(result.ok).toBe(true);
  expect(result.data).toHaveLength(1);
  expect(result.data[0].research_status).toBe("DRAFT");
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

test("createResearchStudy sends the complete lifecycle payload and null dates", async () => {
  global.fetch.mockResolvedValue(jsonResponse({ study: { study_id: "s-1" } }, 201));

  await api.createResearchStudy({
    name: "Focus study",
    description: "A short study",
    startsAt: "",
    endsAt: "",
    telemetryPolicy: { redact: true },
    sessionPolicy: { max_minutes: 30 },
    profileIds: ["p-1"],
  });

  expect(global.fetch).toHaveBeenCalledWith(
    expect.stringContaining("/api/research/studies"),
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({
        name: "Focus study",
        description: "A short study",
        starts_at: null,
        ends_at: null,
        telemetry_policy: { redact: true },
        session_policy: { max_minutes: 30 },
        profile_ids: ["p-1"],
      }),
    }),
  );
});

test("cloneResearchStudy sends profile_ids so the clone is runnable", async () => {
  global.fetch.mockResolvedValue(jsonResponse({ study: { study_id: "s-2" } }, 201));

  await api.cloneResearchStudy("s-1", { profileIds: ["p-1", "p-2"] });

  expect(global.fetch).toHaveBeenCalledWith(
    expect.stringContaining("/api/research/studies/s-1/clone"),
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ profile_ids: ["p-1", "p-2"] }),
    }),
  );
});

test("getReleaseCatalogue reads the researcher-readable releases envelope", async () => {
  global.fetch.mockResolvedValue(
    jsonResponse({
      releases: [
        {
          release_id: "rel-1",
          version: "1.2.0",
          qualification_status: "QUALIFIED",
          distribution_mode: "PACKAGED",
        },
      ],
    }),
  );

  const result = await api.getReleaseCatalogue();

  expect(result.ok).toBe(true);
  expect(result.data[0]).toMatchObject({
    release_id: "rel-1",
    qualification_status: "QUALIFIED",
  });
});

test("researchRequest preserves typed study-stopped errors", async () => {
  global.fetch.mockResolvedValue(
    jsonResponse({ detail: { code: "STUDY_STOPPED", message: "Study has stopped" } }, 409),
  );

  const result = await api.resolveResearchJoinCode("JOIN-AB12");

  expect(result).toMatchObject({
    ok: false,
    code: "STUDY_STOPPED",
    status: 409,
    error: "Study has stopped",
    errors: [{ code: "STUDY_STOPPED" }],
  });
});

test("resolveResearchJoinCode maps study and consent contract fields", async () => {
  global.fetch.mockResolvedValue(
    jsonResponse({
      join_code: "JOIN-AB12",
      study: {
        study_id: "s-1",
        name: "Focus study",
        description: "A short study",
        research_status: "PUBLISHED",
        joinability: "OPEN",
        status: "ACTIVE",
      },
      consent: { text: "I agree to participate." },
    }),
  );

  const result = await api.resolveResearchJoinCode("JOIN-AB12");

  expect(result.data.study).toMatchObject({
    studyId: "s-1",
    name: "Focus study",
    description: "A short study",
    researchStatus: "PUBLISHED",
    joinability: "OPEN",
    status: "ACTIVE",
  });
  expect(result.data).toMatchObject({
    consentText: "I agree to participate.",
    policyText: "I agree to participate.",
  });
});

test("getStudyParticipantCoverage requests the study-scoped coverage endpoint", async () => {
  global.fetch.mockResolvedValue(
    jsonResponse({
      study_id: "s-1",
      coverage_version: "v1",
      population: "enrolled",
      participant_count: 1,
      participants: [
        {
          enrollment_id: "e-1",
          participant_code: "P-0001",
          status: "ACTIVE",
          assignment: {
            agent_profile_id: "p-1",
            strategy: "RANDOM_EQUAL",
            randomization_epoch: 1,
          },
          sessions: { total: 1, active: 1, terminal: 0 },
          events: {
            total: 2,
            by_event_type: { tool_call: 2 },
            by_source: { acp: 2 },
          },
        },
      ],
    }),
  );

  const result = await api.getStudyParticipantCoverage("s-1");

  expect(global.fetch).toHaveBeenCalledWith(
    expect.stringContaining(
      "/api/research/operations/participants/coverage?study_id=s-1",
    ),
    expect.objectContaining({ method: "GET" }),
  );
  expect(result.ok).toBe(true);
  expect(result.data.participants[0]).toMatchObject({
    participant_code: "P-0001",
    assignment: { strategy: "RANDOM_EQUAL", randomization_epoch: 1 },
    events: {
      by_event_type: { tool_call: 2 },
      by_source: { acp: 2 },
    },
  });
});

test("getStudyParticipantCoverage reports a 403 as forbidden", async () => {
  global.fetch.mockResolvedValue(
    jsonResponse({ detail: { code: "FORBIDDEN" } }, 403),
  );

  const result = await api.getStudyParticipantCoverage("s-1");

  expect(result).toMatchObject({
    ok: false,
    forbidden: true,
    code: "FORBIDDEN",
    status: 403,
  });
  expect(result.error).toMatch(/study owner or an administrator/i);
});

test("redeemResearchJoinCode preserves enrollment assignment and idempotency fields", async () => {
  global.fetch.mockResolvedValue(
    jsonResponse({
      enrollment_id: "e-1",
      study_id: "s-1",
      assignment_id: "a-1",
      agent_profile_id: "p-1",
      status: "ACTIVE",
      created: true,
      reused: false,
      handoff: { session_id: "session-1" },
    }),
  );

  const result = await api.redeemResearchJoinCode("JOIN-AB12", true);

  expect(result.data).toMatchObject({
    enrollment_id: "e-1",
    study_id: "s-1",
    assignment_id: "a-1",
    agent_profile_id: "p-1",
    status: "ACTIVE",
    created: true,
    reused: false,
    handoff: { session_id: "session-1" },
  });
});
