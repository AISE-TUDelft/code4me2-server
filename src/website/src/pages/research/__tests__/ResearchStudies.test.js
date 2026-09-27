import React from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import ResearchStudies from "../ResearchStudies";
import * as api from "../../../utils/api";

jest.mock("../../../utils/api");

beforeEach(() => {
  jest.clearAllMocks();
  api.getCurrentUser.mockResolvedValue({ ok: true, user: { is_admin: false, can_research: true } });
  api.getStudyParticipants.mockResolvedValue({
    ok: true,
    data: { study_id: "study-1", arms: [], participants: [] },
  });
  api.getStudyAnalyticsSummary.mockResolvedValue({ ok: true, data: null });
});

const openSettings = () => fireEvent.click(screen.getByRole("tab", { name: /settings/i }));

const STUDY = {
  study_id: "study-1",
  name: "Pilot study",
  description: "Metadata only",
  research_status: "DRAFT",
  join_code: "JOIN1234",
  consent_locked_at: null,
  profile_selections: [
    { profile_id: "profile-1", name: "Code4Me", model: "model-a" },
  ],
};

const VALID_SESSION_POLICY = {
  idle_timeout_seconds: 600,
  resume_grace_seconds: 120,
  heartbeat_seconds: 30,
};

const renderPage = () =>
  render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={["/research/studies"]}>
      <ResearchStudies />
    </MemoryRouter>,
  );

test("renders lifecycle study list without revision controls", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [STUDY] });

  renderPage();

  expect(await screen.findByText("Pilot study")).toBeInTheDocument();
  const item = screen.getByRole("button", { name: /Pilot study/ });
  expect(within(item).getByText("Draft")).toBeInTheDocument();
  expect(screen.queryByText(/supersede|revision|publish draft/i)).not.toBeInTheDocument();
});

test("shows selected profiles as read-only study configuration", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [STUDY] });

  renderPage();
  fireEvent.click(await screen.findByText("Pilot study"));

  expect(await screen.findByText("Selected agent profiles")).toBeInTheDocument();
  expect(screen.getByText("Code4Me")).toBeInTheDocument();
  expect(screen.getByText("Profile selection is fixed after study creation.")).toBeInTheDocument();
});

test("lists participants with their assigned arm in the Participants tab", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [STUDY] });
  api.getStudyParticipants.mockResolvedValue({
    ok: true,
    data: {
      study_id: "study-1",
      arms: [{ profile_id: "profile-1", name: "Code4Me", model: "model-a", participants: 1 }],
      participants: [
        {
          enrollment_id: "e-1",
          participant_code: "P-ALPHA",
          status: "ACTIVE",
          enrolled_at: "2026-09-01T10:00:00Z",
          arm: { profile_id: "profile-1", name: "Code4Me", model: "model-a", framework_version: "code4me2-agent" },
          sessions: { total: 4, active: 1, session_seconds: 5400 },
          activity: { prompts: 12, tool_calls: 40, tool_failures: 3, errors: 2, last_event_at: "2026-09-10T09:04:00Z" },
          health: "IDLE",
        },
      ],
    },
  });

  renderPage();
  fireEvent.click(await screen.findByText("Pilot study"));
  // The overview needs only the summary; the list loads when its tab opens.
  await waitFor(() => expect(api.getStudyAnalyticsSummary).toHaveBeenCalledWith("study-1", {}));
  expect(api.getStudyParticipants).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("tab", { name: /participants/i }));
  await waitFor(() => expect(api.getStudyParticipants).toHaveBeenCalledWith("study-1"));

  const table = await screen.findByRole("table", { name: "Enrolled participants" });
  expect(within(table).getByText("P-ALPHA")).toBeInTheDocument();
  // The frozen arm is shown by name, not as an opaque profile id.
  expect(within(table).getByText("Code4Me")).toBeInTheDocument();
  expect(within(table).getByText("12")).toBeInTheDocument();
  expect(within(table).getByText("3 failed")).toBeInTheDocument();
  expect(screen.getByText("Study-local participant codes only; account identities never appear here.")).toBeInTheDocument();
});

test("opens a participant's telemetry dashboard in a drawer", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [STUDY] });
  api.getStudyParticipants.mockResolvedValue({
    ok: true,
    data: {
      arms: [],
      participants: [
        {
          enrollment_id: "e-1",
          participant_code: "P-ALPHA",
          status: "ACTIVE",
          arm: { profile_id: "profile-1", name: "Code4Me", model: "model-a" },
          sessions: { total: 1 },
          activity: { prompts: 2 },
          health: "ACTIVE",
        },
      ],
    },
  });
  api.getStudyParticipantDashboard.mockResolvedValue({
    ok: true,
    data: {
      enrollment_id: "e-1",
      participant_code: "P-ALPHA",
      status: "ACTIVE",
      health: "ACTIVE",
      activity: { prompts: 2 },
      metrics: { prompts: 2, tool_calls_per_prompt: 1.5, tool_failure_rate: 0.25 },
      daily: [{ date: "2026-09-10", prompts: 2, tool_calls: 3, errors: 0, ide_edits: 4, session_seconds: 600 }],
      tool_kinds: [{ tool_kind: "read", calls: 2, failures: 0 }, { tool_kind: "edit", calls: 1, failures: 1 }],
      tools: [],
      stop_reasons: [{ stop_reason: "end_turn", count: 1 }, { stop_reason: "cancelled", count: 1 }],
      permission_decisions: [{ decision: "allow", count: 1 }],
      sessions_list: [],
      turns: [
        { turn_id: "t-1", started_at: "2026-09-10T09:00:00Z", duration_seconds: 42, tool_calls: 3, tool_failures: 1, permission_requests: 1, stop_reason: "cancelled", cancelled: true },
      ],
      timeline: [{ occurred_at: "2026-09-10T09:00:00Z", event_type: "tool.failed", source: "acp", tool_name: "edit_file", tool_kind: "edit", status: "failed" }],
    },
  });

  renderPage();
  fireEvent.click(await screen.findByText("Pilot study"));
  fireEvent.click(screen.getByRole("tab", { name: /participants/i }));
  fireEvent.click(await screen.findByRole("button", { name: "Open dashboard for participant P-ALPHA" }));

  const drawer = await screen.findByRole("dialog", { name: "Participant P-ALPHA" });
  await waitFor(() => expect(api.getStudyParticipantDashboard).toHaveBeenCalledWith("study-1", "e-1"));
  expect(await within(drawer).findByText("Tool calls / prompt")).toBeInTheDocument();
  expect(within(drawer).getByText("25%")).toBeInTheDocument();
  expect(within(drawer).getByText("Cancelled by user")).toBeInTheDocument();
  expect(within(drawer).getByText("Tool failed")).toBeInTheDocument();
  // Metadata only: the timeline never renders prompt or code content.
  expect(within(drawer).getByText(/metadata only/i)).toBeInTheDocument();
});

test("the analytics tab compares arms on participant-level metrics", async () => {
  const twoArms = {
    ...STUDY,
    profile_selections: [
      { profile_id: "profile-1", name: "Code4Me", model: "model-a", selection_order: 0 },
      { profile_id: "profile-2", name: "Codex", model: "model-b", selection_order: 1 },
    ],
  };
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [twoArms] });
  api.getStudyAnalyticsSummary.mockResolvedValue({
    ok: true,
    data: {
      totals: { participants_enrolled: 4, participants_with_telemetry: 3, participants_active: 4, prompts: 30, tool_calls: 90, tool_failures: 9, sessions: 6 },
      arms: [
        {
          profile_id: "profile-1",
          name: "Code4Me",
          metrics: { tool_calls_per_prompt: { n: 2, median: 3, p25: 2, p75: 4, mean: 3, values: [2, 4] } },
          tool_kinds: [{ tool_kind: "read", calls: 10 }],
          stop_reasons: [],
          permission_decisions: [],
          transitions: [],
          context: { cap_tokens: 16000, model_calls: 5, calls_with_prompt_tokens: 5, prompt_tokens_p50: 4000, prompt_tokens_p95: 9000, prompt_tokens_max: 12000, over_cap_calls: 0, over_cap_share: 0, coverage: "AVAILABLE" },
        },
        {
          profile_id: "profile-2",
          name: "Codex",
          metrics: { tool_calls_per_prompt: { n: 1, median: 1, p25: 1, p75: 1, mean: 1, values: [1] } },
          tool_kinds: [],
          stop_reasons: [],
          permission_decisions: [],
          transitions: [],
          context: { cap_tokens: 16000, model_calls: 0, coverage: "UNAVAILABLE" },
        },
      ],
      daily: [],
      tools: [],
      coverage: { usage_tokens: "PARTIAL" },
    },
  });

  renderPage();
  fireEvent.click(await screen.findByText("Pilot study"));
  fireEvent.click(screen.getByRole("tab", { name: /analytics/i }));

  expect(await screen.findByText("Arm comparison")).toBeInTheDocument();
  expect(screen.getByText("Tool calls per prompt")).toBeInTheDocument();
  // Codex signs in with ChatGPT and bypasses the metered relay.
  expect(screen.getByText("Not observable (Codex)")).toBeInTheDocument();
  // "All time" reuses the summary the overview loaded: one request, not two.
  expect(api.getStudyAnalyticsSummary).toHaveBeenCalledTimes(1);
  expect(api.getStudyAnalyticsSummary).toHaveBeenCalledWith("study-1", {});

  fireEvent.click(screen.getByRole("button", { name: "Last 7 days" }));
  await waitFor(() =>
    expect(api.getStudyAnalyticsSummary).toHaveBeenLastCalledWith(
      "study-1",
      expect.objectContaining({ start: expect.any(String), end: expect.any(String) }),
    ),
  );
});

test("a superseded analytics range never replaces the newer one", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [STUDY] });
  const summaryWith = (prompts) => ({
    ok: true,
    data: {
      totals: { participants_enrolled: 1, participants_with_telemetry: 1, participants_active: 1, prompts, tool_calls: 0, sessions: 1 },
      arms: [],
      daily: [],
      tools: [],
      coverage: {},
    },
  });
  let resolveSevenDays;
  api.getStudyAnalyticsSummary.mockImplementation((studyId, window) => {
    if (!window || !window.start) return Promise.resolve(summaryWith(1111));
    const days = (new Date(window.end) - new Date(window.start)) / 86400000 + 1;
    if (days === 7) return new Promise((resolve) => (resolveSevenDays = resolve));
    return Promise.resolve(summaryWith(3333));
  });

  renderPage();
  fireEvent.click(await screen.findByText("Pilot study"));
  fireEvent.click(screen.getByRole("tab", { name: /analytics/i }));
  expect(await screen.findByText("1,111")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Last 7 days" }));
  fireEvent.click(screen.getByRole("button", { name: "Last 30 days" }));
  expect(await screen.findByText("3,333")).toBeInTheDocument();

  // The slow 7-day response arrives last and is ignored.
  await act(async () => {
    resolveSevenDays(summaryWith(7777));
  });
  expect(screen.getByText("3,333")).toBeInTheDocument();
  expect(screen.queryByText("7,777")).not.toBeInTheDocument();
});

test("a custom range must not end before it starts", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [STUDY] });
  api.getStudyAnalyticsSummary.mockResolvedValue({
    ok: true,
    data: { totals: { prompts: 2, tool_calls: 0, sessions: 1 }, arms: [], daily: [], tools: [], coverage: {} },
  });

  renderPage();
  fireEvent.click(await screen.findByText("Pilot study"));
  fireEvent.click(screen.getByRole("tab", { name: /analytics/i }));
  fireEvent.click(await screen.findByRole("button", { name: "Custom" }));
  fireEvent.change(screen.getByLabelText("From date"), { target: { value: "2026-09-20" } });
  fireEvent.change(screen.getByLabelText("To date"), { target: { value: "2026-09-10" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));

  expect(await screen.findByText("The start date must be on or before the end date.")).toBeInTheDocument();
  expect(api.getStudyAnalyticsSummary).toHaveBeenCalledTimes(1);
});

test("the telemetry presets reach the submitted policy", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [] });
  api.getAgentProfiles.mockResolvedValue({
    ok: true,
    data: [{ profile_id: "profile-1", name: "Code4Me", model: "model-a", is_active: true }],
  });
  api.createResearchStudy.mockResolvedValue({ ok: true, data: { study: STUDY } });

  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: "New study" }));
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Content study" } });
  fireEvent.click(screen.getByLabelText(/Everything — also collect prompts/));
  fireEvent.change(screen.getByLabelText("Session length"), { target: { value: "long" } });
  fireEvent.click(await screen.findByLabelText("Code4Me"));
  fireEvent.click(screen.getByRole("button", { name: "Create Draft study" }));

  await waitFor(() => expect(api.createResearchStudy).toHaveBeenCalled());
  const payload = api.createResearchStudy.mock.calls[0][0];
  // Content is only stored when the frozen policy carries content_capture.
  // …and keeps every metadata class, so structural fields survive ingestion.
  expect(payload.telemetryPolicy).toEqual({
    allowed_field_classes: ["STRUCTURAL", "METRICS", "DIAGNOSTICS", "CODE_METADATA", "CONTENT"],
    content_capture: true,
  });
  expect(payload.sessionPolicy).toEqual({ idle_timeout_seconds: 3600, resume_grace_seconds: 900, heartbeat_seconds: 60 });
});

test("custom telemetry offers the study categories and previews what they collect", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [] });
  api.getAgentProfiles.mockResolvedValue({
    ok: true,
    data: [{ profile_id: "profile-1", name: "Code4Me", model: "model-a", is_active: true }],
  });

  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: "New study" }));
  fireEvent.click(screen.getByLabelText(/Custom — choose which categories/));
  const categories = document.querySelector(".research-policy-classes");
  // The five study categories, never the runtime names.
  expect(within(categories).getAllByRole("checkbox")).toHaveLength(5);
  expect(
    within(categories).getByText("Stored at runtime: event records, activity details, system details, code metadata."),
  ).toBeInTheDocument();
  const warning = /cannot identify prompts, tool calls or approvals/;
  expect(within(categories).queryByText(warning)).not.toBeInTheDocument();

  // Usage only: no agent activity, so the dashboards would be empty.
  fireEvent.click(within(categories).getByLabelText("Agent and session structure (which events and tools ran)"));
  fireEvent.click(within(categories).getByLabelText("Errors and diagnostics"));
  expect(within(categories).getByText(warning)).toBeInTheDocument();
  expect(within(categories).getByText("Stored at runtime: event records, system details, code metadata.")).toBeInTheDocument();

  // Diagnostics enables all agent-activity metadata at runtime.
  fireEvent.click(within(categories).getByLabelText("Errors and diagnostics"));
  expect(within(categories).queryByText(warning)).not.toBeInTheDocument();

  // The sensitive category turns content capture on: the preview says so.
  const contentBox = within(categories).getByLabelText(
    "Prompts, model responses and reasoning, tool arguments and output, and file contents (sensitive)",
  );
  fireEvent.click(contentBox);
  expect(
    within(categories).getByText(
      "Stored at runtime: event records, activity details, system details, code metadata, content (prompts, responses, tool arguments and output, file contents).",
    ),
  ).toBeInTheDocument();
  // Turning the sensitive category off turns content capture off again.
  fireEvent.click(contentBox);
  expect(JSON.parse(screen.getByLabelText("Telemetry policy (JSON)").value).content_capture).toBeUndefined();
});

test("a raw JSON policy in runtime names previews what it stores without a false warning", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [] });
  api.getAgentProfiles.mockResolvedValue({ ok: true, data: [] });

  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: "New study" }));
  fireEvent.change(screen.getByLabelText("Telemetry policy (JSON)"), {
    target: { value: '{"allowed_field_classes":["SYSTEM","BEHAVIORAL"],"content_capture":true}' },
  });
  const categories = document.querySelector(".research-policy-classes");
  expect(
    within(categories).getByText(
      "Stored at runtime: event records, activity details, system details, hashed code metadata, content (prompts, responses, tool arguments and output, file contents).",
    ),
  ).toBeInTheDocument();
  expect(within(categories).queryByText(/cannot identify prompts, tool calls or approvals/)).not.toBeInTheDocument();

  // Toggling a metadata category keeps the policy's own content-capture flag.
  fireEvent.click(within(categories).getByLabelText("Usage and timings (tokens, durations, counts)"));
  expect(JSON.parse(screen.getByLabelText("Telemetry policy (JSON)").value).content_capture).toBe(true);

  // No preview, and read-only categories, while the JSON does not parse.
  fireEvent.change(screen.getByLabelText("Telemetry policy (JSON)"), { target: { value: "{" } });
  const invalid = within(document.querySelector(".research-policy-classes"));
  expect(invalid.queryByText(/^Stored at runtime/)).not.toBeInTheDocument();
  invalid.getAllByRole("checkbox").forEach((box) => expect(box).toBeDisabled());
});

test("the overview lists what a legacy SECRET-only policy actually stores", async () => {
  api.listResearchStudies.mockResolvedValue({
    ok: true,
    data: [{ ...STUDY, telemetry_policy: { allowed_field_classes: ["SECRET"] } }],
  });

  renderPage();
  fireEvent.click(await screen.findByText("Pilot study"));

  expect(await screen.findByText(/^Activity details: tools run/)).toBeInTheDocument();
  expect(screen.getByText(/^Event records: which agent and IDE events/)).toBeInTheDocument();
  expect(screen.getByText(/^Code metadata: file types and languages/)).toBeInTheDocument();
  expect(screen.getByText("Declared as SECRET.")).toBeInTheDocument();
});

test("the overview shows undeclared code metadata as hashed, not absent", async () => {
  api.listResearchStudies.mockResolvedValue({
    ok: true,
    data: [{ ...STUDY, telemetry_policy: { allowed_field_classes: ["METRICS"] } }],
  });

  renderPage();
  fireEvent.click(await screen.findByText("Pilot study"));

  expect(await screen.findByText(/stored as unsalted hashes/)).toBeInTheDocument();
  expect(screen.queryByText(/^Activity details/)).not.toBeInTheDocument();
});

test("rejects an end date before the start date", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [] });
  api.getAgentProfiles.mockResolvedValue({
    ok: true,
    data: [{ profile_id: "profile-1", name: "Code4Me", model: "model-a", is_active: true }],
  });

  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: "New study" }));
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Dated" } });
  fireEvent.change(screen.getByLabelText("Starts at"), { target: { value: "2026-10-10T10:00" } });
  fireEvent.change(screen.getByLabelText("Ends at"), { target: { value: "2026-10-01T10:00" } });
  fireEvent.click(await screen.findByLabelText("Code4Me"));
  fireEvent.click(screen.getByRole("button", { name: "Create Draft study" }));

  expect(await screen.findByText("The end must be after the start.")).toBeInTheDocument();
  expect(api.createResearchStudy).not.toHaveBeenCalled();
});

test("creates a Draft study through the lifecycle API", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [] });
  api.getAgentProfiles.mockResolvedValue({
    ok: true,
    data: [{ profile_id: "profile-1", name: "Code4Me", model: "model-a", is_active: true }],
  });
  api.createResearchStudy.mockResolvedValue({ ok: true, data: { study: STUDY } });

  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: "New study" }));
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "New pilot" } });
  fireEvent.click(await screen.findByLabelText("Code4Me"));
  fireEvent.click(screen.getByRole("button", { name: "Create Draft study" }));

  await waitFor(() => expect(api.createResearchStudy).toHaveBeenCalledWith({
    name: "New pilot",
    description: "",
    startsAt: "",
    endsAt: "",
    telemetryPolicy: {},
    sessionPolicy: VALID_SESSION_POLICY,
    profileIds: ["profile-1"],
    // No metered arm selected: no budget, no warning threshold.
    defaultBudgetUsd: "",
    budgetWarningFraction: null,
  }));
});

test("selects agent profiles when creating a study", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [] });
  api.getAgentProfiles.mockResolvedValue({
    ok: true,
    data: [
      { profile_id: "profile-1", name: "Code4Me", model: "model-a", is_active: true },
      { profile_id: "profile-2", name: "Codex", model: "model-b", is_active: true },
    ],
  });
  api.createResearchStudy.mockResolvedValue({ ok: true, data: { study: STUDY } });

  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: "New study" }));
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Agent study" } });
  fireEvent.click(await screen.findByLabelText("Code4Me"));
  fireEvent.click(screen.getByRole("button", { name: "Create Draft study" }));

  await waitFor(() => expect(api.createResearchStudy).toHaveBeenCalledWith({
    name: "Agent study",
    description: "",
    startsAt: "",
    endsAt: "",
    telemetryPolicy: {},
    sessionPolicy: VALID_SESSION_POLICY,
    profileIds: ["profile-1"],
    defaultBudgetUsd: "",
    budgetWarningFraction: null,
  }));
});

test("defaults the session policy to a complete valid policy", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [] });

  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: "New study" }));

  const sessionPolicy = screen.getByLabelText("Session policy (JSON)");
  expect(JSON.parse(sessionPolicy.value)).toEqual(VALID_SESSION_POLICY);
  expect(screen.queryByText(/invalid json/i)).not.toBeInTheDocument();
});

test("keeps invalid policy JSON visible and disables create until it parses", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [] });
  api.getAgentProfiles.mockResolvedValue({
    ok: true,
    data: [{ profile_id: "profile-1", name: "Code4Me", model: "model-a", is_active: true }],
  });

  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: "New study" }));
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Broken policy" } });
  fireEvent.click(await screen.findByLabelText("Code4Me"));

  const sessionPolicy = screen.getByLabelText("Session policy (JSON)");
  fireEvent.change(sessionPolicy, { target: { value: '{"idle_timeout_seconds":' } });

  expect(sessionPolicy.value).toBe('{"idle_timeout_seconds":');
  expect(screen.getByText(/invalid json/i)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Create Draft study" })).toBeDisabled();

  fireEvent.change(sessionPolicy, { target: { value: JSON.stringify(VALID_SESSION_POLICY) } });
  expect(screen.queryByText(/invalid json/i)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Create Draft study" })).not.toBeDisabled();
});

test("surfaces typed policy errors from the server", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [] });
  api.getAgentProfiles.mockResolvedValue({
    ok: true,
    data: [{ profile_id: "profile-1", name: "Code4Me", model: "model-a", is_active: true }],
  });
  api.createResearchStudy.mockResolvedValue({
    ok: false,
    status: 422,
    code: "SESSION_POLICY_INVALID",
    error: "idle_timeout_seconds: Input should be greater than 0",
  });

  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: "New study" }));
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Bad policy" } });
  fireEvent.click(await screen.findByLabelText("Code4Me"));
  fireEvent.click(screen.getByRole("button", { name: "Create Draft study" }));

  expect(await screen.findByText(/SESSION_POLICY_INVALID/)).toBeInTheDocument();
});

test("stops a study and opens the clone profile-selection step", async () => {
  const stopped = { ...STUDY, research_status: "STUDY_STOPPED" };
  api.listResearchStudies
    .mockResolvedValueOnce({ ok: true, data: [STUDY] })
    .mockResolvedValue({ ok: true, data: [stopped] });
  api.stopResearchStudy.mockResolvedValue({ ok: true, data: { study: stopped } });
  jest.spyOn(window, "confirm").mockReturnValue(true);

  renderPage();
  fireEvent.click(await screen.findByText("Pilot study"));
  openSettings();
  fireEvent.click(screen.getByRole("button", { name: "Stop study" }));

  await waitFor(() => expect(api.stopResearchStudy).toHaveBeenCalledWith("study-1"));
  fireEvent.click(await screen.findByRole("button", { name: "Clone as new Draft" }));

  expect(await screen.findByRole("heading", { name: "Clone study" })).toBeInTheDocument();
  expect(screen.getByText("Pilot study (copy)")).toBeInTheDocument();
  expect(screen.getByText(/participants, consent, assignments, telemetry data, join code, and study ID are not copied/i)).toBeInTheDocument();
  expect(api.cloneResearchStudy).not.toHaveBeenCalled();
  window.confirm.mockRestore();
});

test("clone submits reselected profiles and explains what is copied", async () => {
  const stopped = { ...STUDY, research_status: "STUDY_STOPPED" };
  const clone = { ...STUDY, study_id: "study-2", name: "Pilot study (copy)" };
  api.listResearchStudies
    .mockResolvedValueOnce({ ok: true, data: [stopped] })
    .mockResolvedValueOnce({ ok: true, data: [stopped, clone] });
  api.getAgentProfiles.mockResolvedValue({
    ok: true,
    data: [{ profile_id: "profile-1", name: "Code4Me", model: "model-a", is_active: true }],
  });
  api.cloneResearchStudy.mockResolvedValue({ ok: true, data: { study: clone } });

  renderPage();
  fireEvent.click(await screen.findByText("Pilot study"));
  fireEvent.click(screen.getByRole("button", { name: "Clone as new Draft" }));

  const submit = await screen.findByRole("button", { name: "Clone Draft study" });
  expect(submit).toBeDisabled();
  fireEvent.click(submit);
  expect(api.cloneResearchStudy).not.toHaveBeenCalled();

  fireEvent.click(screen.getByLabelText("Code4Me"));
  expect(submit).not.toBeDisabled();
  fireEvent.click(submit);

  await waitFor(() =>
    expect(api.cloneResearchStudy).toHaveBeenCalledWith("study-1", { profileIds: ["profile-1"] }),
  );
  expect(await screen.findByRole("heading", { name: "Pilot study (copy)" })).toBeInTheDocument();
  expect(await screen.findByText(/Clone created in Draft state with the selected agent profiles/i)).toBeInTheDocument();
});

test("shows the kill-switch control to admins and calls the operations API", async () => {
  api.getCurrentUser.mockResolvedValue({ ok: true, user: { is_admin: true } });
  api.listResearchStudies
    .mockResolvedValueOnce({ ok: true, data: [STUDY] })
    .mockResolvedValue({ ok: true, data: [STUDY] });
  api.engageResearchKillSwitch.mockResolvedValue({ ok: true, data: { switch_id: "switch-1" } });
  jest.spyOn(window, "confirm").mockReturnValue(true);

  renderPage();
  fireEvent.click(await screen.findByText("Pilot study"));
  openSettings();
  fireEvent.change(await screen.findByLabelText("Kill switch reason"), { target: { value: "Admin maintenance" } });
  fireEvent.click(screen.getByRole("button", { name: "Engage kill switch" }));

  await waitFor(() => expect(api.engageResearchKillSwitch).toHaveBeenCalledWith("study-1", "Admin maintenance"));
  window.confirm.mockRestore();
});

test("a deep link loads each dataset once, only for the tab it opens", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [STUDY] });
  render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={["/research/studies/study-1"]}>
      <Routes>
        <Route path="/research/studies/:studyId" element={<ResearchStudies />} />
      </Routes>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("tab", { name: /overview/i, selected: true })).toBeInTheDocument();
  await waitFor(() => expect(api.getStudyAnalyticsSummary).toHaveBeenCalledWith("study-1", {}));
  await act(async () => {});
  expect(api.getStudyAnalyticsSummary).toHaveBeenCalledTimes(1);
  expect(api.getStudyParticipants).not.toHaveBeenCalled();
});

test("returning to a study reloads its summary", async () => {
  const OTHER = { ...STUDY, study_id: "study-2", name: "Second study", join_code: "JOIN5678" };
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [STUDY, OTHER] });
  api.getStudyAnalyticsSummary.mockImplementation((studyId) =>
    Promise.resolve({
      ok: true,
      data: {
        totals: { prompts: studyId === "study-1" ? 432 : 123, tool_calls: 0, sessions: 1 },
        arms: [],
        daily: [],
        tools: [],
        coverage: {},
      },
    }),
  );
  let navigateTo;
  const Navigator = () => {
    navigateTo = useNavigate();
    return null;
  };
  render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={["/research/studies/study-1"]}>
      <Navigator />
      <Routes>
        <Route path="/research/studies" element={<ResearchStudies />} />
        <Route path="/research/studies/:studyId" element={<ResearchStudies />} />
      </Routes>
    </MemoryRouter>,
  );
  expect(await screen.findByText("432")).toBeInTheDocument();

  // Back to the list, then forward to the same study.
  act(() => navigateTo("/research/studies"));
  await waitFor(() => expect(screen.queryByText("432")).not.toBeInTheDocument());
  act(() => navigateTo("/research/studies/study-1"));
  expect(await screen.findByText("432")).toBeInTheDocument();

  // A → B on the Participants tab → A, then the overview.
  act(() => navigateTo("/research/studies/study-1?tab=participants"));
  act(() => navigateTo("/research/studies/study-2?tab=participants"));
  act(() => navigateTo("/research/studies/study-1?tab=participants"));
  fireEvent.click(await screen.findByRole("tab", { name: /overview/i }));
  expect(await screen.findByText("432")).toBeInTheDocument();
});

test("a failed kill switch keeps the typed reason", async () => {
  api.getCurrentUser.mockResolvedValue({ ok: true, user: { is_admin: true } });
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [STUDY] });
  api.engageResearchKillSwitch.mockResolvedValue({ ok: false, error: "Operations service unavailable." });
  jest.spyOn(window, "confirm").mockReturnValue(true);

  renderPage();
  fireEvent.click(await screen.findByText("Pilot study"));
  openSettings();
  fireEvent.change(await screen.findByLabelText("Kill switch reason"), { target: { value: "Leak investigation" } });
  fireEvent.click(screen.getByRole("button", { name: "Engage kill switch" }));

  expect(await screen.findByText("Operations service unavailable.")).toBeInTheDocument();
  expect(screen.getByLabelText("Kill switch reason")).toHaveValue("Leak investigation");
  window.confirm.mockRestore();
});

test("a kept kill-switch reason does not carry over to another study", async () => {
  const OTHER = { ...STUDY, study_id: "study-2", name: "Second study", join_code: "JOIN5678" };
  api.getCurrentUser.mockResolvedValue({ ok: true, user: { is_admin: true } });
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [STUDY, OTHER] });
  api.engageResearchKillSwitch.mockResolvedValue({ ok: false, error: "Operations service unavailable." });
  jest.spyOn(window, "confirm").mockReturnValue(true);

  renderPage();
  fireEvent.click(await screen.findByText("Pilot study"));
  openSettings();
  fireEvent.change(await screen.findByLabelText("Kill switch reason"), { target: { value: "Leak investigation" } });
  fireEvent.click(screen.getByRole("button", { name: "Engage kill switch" }));
  expect(await screen.findByText("Operations service unavailable.")).toBeInTheDocument();
  expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('"Pilot study"'));

  fireEvent.click(screen.getByText("Second study"));
  await waitFor(() => expect(screen.getByRole("heading", { name: "Second study" })).toBeInTheDocument());
  expect(screen.getByLabelText("Kill switch reason")).toHaveValue("");
  window.confirm.mockRestore();
});

test("browser navigation follows the study and tab in the URL", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [STUDY] });
  let navigateTo;
  const Navigator = () => {
    navigateTo = useNavigate();
    return null;
  };
  render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={["/research/studies/study-1?tab=participants"]}>
      <Navigator />
      <Routes>
        <Route path="/research/studies" element={<ResearchStudies />} />
        <Route path="/research/studies/:studyId" element={<ResearchStudies />} />
      </Routes>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("tab", { name: /participants/i, selected: true })).toBeInTheDocument();
  act(() => navigateTo("/research/studies/study-1?tab=analytics"));
  expect(await screen.findByRole("tab", { name: /analytics/i, selected: true })).toBeInTheDocument();
  act(() => navigateTo("/research/studies"));
  await waitFor(() => expect(screen.queryByRole("tab", { name: /analytics/i })).not.toBeInTheDocument());
});

test("releases a persisted engaged kill switch after refresh", async () => {
  const engaged = { ...STUDY, kill_switch: { switch_id: "switch-1", status: "ENGAGED", reason: "Maintenance" } };
  const released = { ...engaged, kill_switch: { switch_id: "switch-1", status: "RELEASED", reason: "Maintenance" } };
  api.getCurrentUser.mockResolvedValue({ ok: true, user: { is_admin: true } });
  api.listResearchStudies
    .mockResolvedValueOnce({ ok: true, data: [engaged] })
    .mockResolvedValueOnce({ ok: true, data: [released] });
  api.releaseResearchKillSwitch.mockResolvedValue({ ok: true, data: {} });

  renderPage();
  fireEvent.click(await screen.findByText("Pilot study"));
  openSettings();
  fireEvent.click(await screen.findByRole("button", { name: "Release kill switch" }));

  await waitFor(() => expect(api.releaseResearchKillSwitch).toHaveBeenCalledWith("switch-1"));
  expect(await screen.findByRole("button", { name: "Engage kill switch" })).toBeInTheDocument();
  expect(screen.getByText(/Kill switch: RELEASED/)).toBeInTheDocument();
});

test("renders safe unavailable counts and hides terminal controls for stopped studies", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [{ ...STUDY, research_status: "STUDY_STOPPED", enrollment_count: null, active_enrollment_count: undefined, assignment_count: undefined, active_session_count: null, collection_status: null }] });

  renderPage();
  fireEvent.click(await screen.findByText("Pilot study"));

  expect(screen.getByText("Unavailable (Unavailable active)")).toBeInTheDocument();
  expect(screen.getAllByText("Unavailable", { exact: true })).toHaveLength(2);
  expect(screen.queryByRole("button", { name: "Stop study" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Clone as new Draft" })).toBeInTheDocument();
});

test("a profile-less clone is shown as not joinable", async () => {
  const profileLessClone = {
    ...STUDY,
    study_id: "study-2",
    name: "Pilot study (copy)",
    profile_selections: [],
  };
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [profileLessClone] });

  renderPage();
  fireEvent.click(await screen.findByText("Pilot study (copy)"));

  expect(screen.getByText(/cannot be joined/i)).toBeInTheDocument();
});

// ---- Participant budgets ----------------------------------------------------

const GOOSE_PROFILE = {
  profile_id: "profile-goose",
  name: "Goose arm",
  model: "openai/gpt-4o-mini",
  framework_version: "goose",
  model_priced: true,
  is_active: true,
};
const CODEX_PROFILE = {
  profile_id: "profile-codex",
  name: "Codex arm",
  model: "gpt-5-codex",
  framework_version: "codex",
  model_priced: null,
  is_active: true,
};

const BUDGET = {
  study_id: "study-1",
  metered: true,
  metered_profile_ids: ["profile-1"],
  default_budget_micro_usd: 10000000,
  default_budget_usd: "10.00",
  warning_fraction: 0.8,
  updated_at: null,
  updated_by: null,
  editable: true,
  participants: { total: 3, on_default: 1, on_old_default: 2, custom: 0, exhausted: 1 },
  metered_spend_micro_usd: 3120000,
  metered_spend_usd: "3.12",
  metered_calls: 7,
  reserved_micro_usd: 0,
  pricing: { complete: true, missing: [] },
};

test("the create form asks for a budget only for metered arms and sends it as a decimal string", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [] });
  api.getAgentProfiles.mockResolvedValue({ ok: true, data: [GOOSE_PROFILE, CODEX_PROFILE] });
  api.createResearchStudy.mockResolvedValue({ ok: true, data: { study: STUDY } });

  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: "New study" }));
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Metered pilot" } });

  // Codex only: nothing to budget or price.
  fireEvent.click(await screen.findByLabelText("Codex arm"));
  expect(screen.queryByLabelText("Default budget per participant (USD)")).not.toBeInTheDocument();
  expect(screen.getByText(/signs in with the participant's ChatGPT account and is not metered/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Create Draft study" })).not.toBeDisabled();

  // A Goose arm spends from the shared key: the budget becomes required.
  fireEvent.click(screen.getByLabelText("Goose arm"));
  const budget = screen.getByLabelText("Default budget per participant (USD)");
  const submit = screen.getByRole("button", { name: "Create Draft study" });
  expect(submit).toBeDisabled();
  fireEvent.change(budget, { target: { value: "abc" } });
  expect(budget).toHaveAttribute("aria-invalid", "true");
  expect(submit).toBeDisabled();
  fireEvent.change(budget, { target: { value: "0" } });
  expect(screen.getByText("The budget must be greater than zero.")).toBeInTheDocument();
  expect(submit).toBeDisabled();
  fireEvent.change(budget, { target: { value: "12.5" } });
  fireEvent.change(screen.getByLabelText("Warn participants at (% of budget used)"), { target: { value: "90" } });
  expect(submit).not.toBeDisabled();
  fireEvent.click(submit);

  await waitFor(() => expect(api.createResearchStudy).toHaveBeenCalled());
  expect(api.createResearchStudy.mock.calls[0][0]).toMatchObject({
    profileIds: ["profile-codex", "profile-goose"],
    defaultBudgetUsd: "12.50",
    budgetWarningFraction: 0.9,
  });
});

test("an unpriced metered model blocks study creation until an administrator prices it", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [] });
  api.getAgentProfiles.mockResolvedValue({ ok: true, data: [{ ...GOOSE_PROFILE, model_priced: false }] });

  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: "New study" }));
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Unpriced" } });
  fireEvent.click(await screen.findByLabelText("Goose arm"));
  fireEvent.change(screen.getByLabelText("Default budget per participant (USD)"), { target: { value: "10" } });

  expect(screen.getByText("Price missing")).toBeInTheDocument();
  expect(screen.getByText("A selected model has no price on the server.")).toBeInTheDocument();
  expect(screen.getByText(/ask an administrator to price this model/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Create Draft study" })).toBeDisabled();
});

test("a typed budget error from the server lands on the budget field", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [] });
  api.getAgentProfiles.mockResolvedValue({ ok: true, data: [GOOSE_PROFILE] });
  api.createResearchStudy.mockResolvedValue({
    ok: false,
    status: 422,
    code: "BUDGET_PRICE_MISSING",
    error:
      "these profiles' models have no budget price on their provider connection; ask an administrator to price them first: Goose arm (openai/gpt-4o-mini)",
    errors: [{ field: "default_budget_usd", code: "BUDGET_PRICE_MISSING", message: "no price" }],
  });

  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: "New study" }));
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Server says no" } });
  fireEvent.click(await screen.findByLabelText("Goose arm"));
  const budget = screen.getByLabelText("Default budget per participant (USD)");
  fireEvent.change(budget, { target: { value: "10" } });
  fireEvent.click(screen.getByRole("button", { name: "Create Draft study" }));

  expect(await screen.findByRole("alert")).toHaveTextContent(/ask an administrator to price them first/);
  expect(budget).toHaveAttribute("aria-invalid", "true");
  expect(budget).toHaveAccessibleDescription(/ask an administrator to price them first/);
  // The banner is not used for a field error.
  expect(screen.getAllByRole("alert")).toHaveLength(1);
});

test("a clone prefills the source study's default budget for its metered arms", async () => {
  const stopped = {
    ...STUDY,
    research_status: "STUDY_STOPPED",
    budget_policy: { metered: true, default_budget_micro_usd: 20000000, default_budget_usd: "20.00", warning_fraction: 0.8 },
  };
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [stopped] });
  api.getAgentProfiles.mockResolvedValue({ ok: true, data: [GOOSE_PROFILE] });
  api.cloneResearchStudy.mockResolvedValue({ ok: true, data: { study: { ...STUDY, study_id: "study-2" } } });

  renderPage();
  fireEvent.click(await screen.findByText("Pilot study"));
  fireEvent.click(screen.getByRole("button", { name: "Clone as new Draft" }));
  fireEvent.click(await screen.findByLabelText("Goose arm"));

  const budget = screen.getByLabelText("Default budget per participant (USD)");
  expect(budget).toHaveValue("20.00");
  // The warning threshold is copied by the server, so the clone form has no field for it.
  expect(screen.queryByLabelText("Warn participants at (% of budget used)")).not.toBeInTheDocument();
  fireEvent.change(budget, { target: { value: "25" } });
  fireEvent.click(screen.getByRole("button", { name: "Clone Draft study" }));

  await waitFor(() =>
    expect(api.cloneResearchStudy).toHaveBeenCalledWith("study-1", { profileIds: ["profile-goose"], defaultBudgetUsd: "25.00" }),
  );
});

test("the settings tab loads the budget policy and saves a new default after the consent lock", async () => {
  api.listResearchStudies.mockResolvedValue({
    ok: true,
    data: [{ ...STUDY, research_status: "ACTIVE", consent_locked_at: "2026-09-02T10:00:00Z" }],
  });
  api.getStudyBudget.mockResolvedValue({ ok: true, data: BUDGET });
  api.updateStudyBudget.mockResolvedValue({
    ok: true,
    data: { ...BUDGET, default_budget_micro_usd: 12500000, default_budget_usd: "12.50", warning_fraction: 0.9 },
  });

  renderPage();
  fireEvent.click(await screen.findByText("Pilot study"));
  await waitFor(() => expect(api.getStudyAnalyticsSummary).toHaveBeenCalled());
  expect(api.getStudyBudget).not.toHaveBeenCalled();
  openSettings();
  await waitFor(() => expect(api.getStudyBudget).toHaveBeenCalledWith("study-1"));

  const input = await screen.findByLabelText("Default budget per participant (USD)");
  expect(input).toHaveValue("10.00");
  expect(input).not.toBeDisabled();
  expect(screen.getByText("$3.12")).toBeInTheDocument();
  expect(screen.getByText("1 exhausted")).toBeInTheDocument();
  // Metadata is locked after the first consent; the budget is not.
  expect(screen.getByLabelText("Name")).toBeDisabled();
  const save = screen.getByRole("button", { name: "Save budget defaults" });
  expect(save).toBeDisabled();
  fireEvent.change(input, { target: { value: "12.5" } });
  fireEvent.change(screen.getByLabelText("Warn participants at (% of budget used)"), { target: { value: "90" } });
  expect(save).not.toBeDisabled();
  fireEvent.click(save);

  await waitFor(() =>
    expect(api.updateStudyBudget).toHaveBeenCalledWith("study-1", { defaultBudgetUsd: "12.50", warningFraction: 0.9 }),
  );
  expect(await screen.findByText("Participant budget defaults saved.")).toBeInTheDocument();
});

test("applying a new default asks for a reason, confirms and sends a fresh idempotency key", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [{ ...STUDY, research_status: "ACTIVE" }] });
  api.getStudyBudget.mockResolvedValue({ ok: true, data: BUDGET });
  api.applyStudyDefaultBudget.mockResolvedValue({
    ok: true,
    data: { applied: 2, skipped: 1, default_budget_micro_usd: 10000000, default_budget_usd: "10.00" },
  });
  jest.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValue(true);

  renderPage();
  fireEvent.click(await screen.findByText("Pilot study"));
  openSettings();
  const apply = await screen.findByRole("button", { name: "Apply new default to 2 participants still on the old default" });
  expect(apply).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Reason for applying the default"), { target: { value: "Term budget raised" } });
  expect(apply).not.toBeDisabled();

  // Declined: nothing is sent.
  fireEvent.click(apply);
  expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("$10.00"));
  expect(api.applyStudyDefaultBudget).not.toHaveBeenCalled();

  fireEvent.click(apply);
  await waitFor(() => expect(api.applyStudyDefaultBudget).toHaveBeenCalledTimes(1));
  const [studyId, payload] = api.applyStudyDefaultBudget.mock.calls[0];
  expect(studyId).toBe("study-1");
  expect(payload.reason).toBe("Term budget raised");
  expect(payload.idempotencyKey).toMatch(/^[A-Za-z0-9_-]{8,128}$/);
  expect(await screen.findByText("Applied the default budget to 2 participants (1 left unchanged).")).toBeInTheDocument();
  expect(screen.getByLabelText("Reason for applying the default")).toHaveValue("");
  window.confirm.mockRestore();
});

test("a study without metered arms explains why there is no budget to set", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [STUDY] });
  api.getStudyBudget.mockResolvedValue({ ok: true, data: { ...BUDGET, metered: false, metered_profile_ids: [] } });

  renderPage();
  fireEvent.click(await screen.findByText("Pilot study"));
  openSettings();

  expect(
    await screen.findByText(/signs in with the participant's ChatGPT account and is not metered, so there is nothing to budget/),
  ).toBeInTheDocument();
  expect(screen.queryByLabelText("Default budget per participant (USD)")).not.toBeInTheDocument();
});

test("a stopped study shows its budget read-only", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [{ ...STUDY, research_status: "STUDY_STOPPED" }] });
  api.getStudyBudget.mockResolvedValue({ ok: true, data: { ...BUDGET, editable: false } });

  renderPage();
  fireEvent.click(await screen.findByText("Pilot study"));
  openSettings();

  const input = await screen.findByLabelText("Default budget per participant (USD)");
  expect(input).toBeDisabled();
  expect(screen.getByRole("button", { name: "Save budget defaults" })).toBeDisabled();
  expect(screen.queryByRole("button", { name: /Apply new default/ })).not.toBeInTheDocument();
});

test("the overview shows the metered spend from the analytics totals", async () => {
  api.listResearchStudies.mockResolvedValue({
    ok: true,
    data: [
      {
        ...STUDY,
        budget_policy: { metered: true, default_budget_micro_usd: 10000000, default_budget_usd: "10.00", warning_fraction: 0.8 },
      },
    ],
  });
  api.getStudyAnalyticsSummary.mockResolvedValue({
    ok: true,
    data: {
      totals: { prompts: 2, tool_calls: 0, sessions: 1, metered_spend_micro_usd: 3120000, metered_calls: 7 },
      arms: [],
      daily: [],
      tools: [],
      coverage: {},
    },
  });

  renderPage();
  fireEvent.click(await screen.findByText("Pilot study"));

  expect(await screen.findByText("$3.12")).toBeInTheDocument();
  expect(screen.getByText("7 model calls")).toBeInTheDocument();
  expect(screen.getByText("$10.00 (warn at 80%)")).toBeInTheDocument();
});

test("the overview says Codex arms are not metered", async () => {
  api.listResearchStudies.mockResolvedValue({
    ok: true,
    data: [
      {
        ...STUDY,
        budget_policy: { metered: false, default_budget_micro_usd: 0, default_budget_usd: "0.00", warning_fraction: 0.8 },
      },
    ],
  });

  renderPage();
  fireEvent.click(await screen.findByText("Pilot study"));

  expect(await screen.findByText("Codex arms are not metered")).toBeInTheDocument();
  expect(screen.queryByText("Budget per participant")).not.toBeInTheDocument();
});
