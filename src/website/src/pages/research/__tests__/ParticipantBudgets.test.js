import React from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import StudyParticipants from "../StudyParticipants";
import * as api from "../../../utils/api";

jest.mock("../../../utils/api");

const STUDY = {
  study_id: "study-1",
  name: "Pilot study",
  research_status: "ACTIVE",
  join_code: "JOIN1234",
  budget_policy: { metered: true, warning_fraction: 0.8 },
};

const ARMS = [
  { profile_id: "profile-1", name: "Goose arm", model: "model-a", framework_version: "goose", color: "#123456", index: 0 },
  { profile_id: "profile-2", name: "Codex arm", model: "gpt-5-codex", framework_version: "codex", color: "#654321", index: 1 },
];

const BUDGET = {
  unit: "USD",
  limit_micro_usd: 10000000,
  consumed_micro_usd: 3120000,
  reserved_micro_usd: 400000,
  remaining_micro_usd: 6480000,
  available_micro_usd: 6480000,
  limit_source: "STUDY_DEFAULT",
  call_count: 7,
  refused_count: 0,
  last_call_at: "2026-09-10T09:00:00Z",
  exhausted: false,
  exhausted_at: null,
  updated_at: null,
};

const row = (overrides) => ({
  enrollment_id: "e-1",
  participant_code: "P-ALPHA",
  status: "ACTIVE",
  enrolled_at: "2026-09-01T10:00:00Z",
  arm: { profile_id: "profile-1", name: "Goose arm", model: "model-a", framework_version: "goose" },
  sessions: { total: 1 },
  activity: { prompts: 2 },
  health: "ACTIVE",
  budget: BUDGET,
  ...overrides,
});

const PARTICIPANTS = [
  row(),
  row({
    enrollment_id: "e-2",
    participant_code: "P-BRAVO",
    budget: {
      ...BUDGET,
      consumed_micro_usd: 10000000,
      reserved_micro_usd: 0,
      remaining_micro_usd: 0,
      available_micro_usd: 0,
      exhausted: true,
      exhausted_at: "2026-09-11T09:00:00Z",
    },
  }),
  row({
    enrollment_id: "e-3",
    participant_code: "P-CODEX",
    arm: { profile_id: "profile-2", name: "Codex arm", model: "gpt-5-codex", framework_version: "codex" },
    budget: null,
  }),
];

const BALANCE = {
  ...BUDGET,
  limit_usd: "10.00",
  consumed_usd: "3.12",
  remaining_usd: "6.48",
  warning_fraction: 0.8,
  settled_prompt_tokens: 1200,
  settled_completion_tokens: 300,
};

const LEDGER_PAGE = {
  ok: true,
  data: {
    entries: [
      {
        reservation_id: "r-1",
        model: "model-a",
        entry_point: "goose_gateway",
        request_id: "req-1",
        research_session_id: null,
        state: "SETTLED",
        hold_micro_usd: 500000,
        charged_micro_usd: 120000,
        estimated_prompt_tokens: 800,
        output_cap_tokens: 1024,
        prompt_tokens: 700,
        completion_tokens: 150,
        cached_prompt_tokens: 0,
        usage_source: "provider",
        resolution_reason: null,
        upstream_status: 200,
        finish_reason: "stop",
        reserved_at: "2026-09-10T09:00:00Z",
        deadline_at: "2026-09-10T09:15:00Z",
        resolved_at: "2026-09-10T09:00:05Z",
      },
    ],
    next_cursor: "2026-09-10T09:00:00+00:00",
  },
};

beforeEach(() => {
  // resetAllMocks (not clearAllMocks): a queued mockResolvedValueOnce page must
  // not leak from a test that never opened the drawer into the next one.
  jest.resetAllMocks();
  api.getStudyParticipantDashboard.mockResolvedValue({
    ok: true,
    data: {
      enrollment_id: "e-1",
      status: "ACTIVE",
      health: "ACTIVE",
      activity: { prompts: 2 },
      metrics: {},
      daily: [],
      tool_kinds: [],
      tools: [],
      stop_reasons: [],
      permission_decisions: [],
      sessions_list: [],
      turns: [],
      timeline: [],
    },
  });
  api.getEnrollmentBudget.mockResolvedValue({
    ok: true,
    data: { enrollment_id: "e-1", metered: true, balance: BALANCE, ledger_summary: { calls: 1 }, recent_adjustments: [] },
  });
  api.getEnrollmentBudgetLedger
    .mockResolvedValueOnce(LEDGER_PAGE)
    .mockResolvedValue({ ok: true, data: { entries: [], next_cursor: null } });
  api.getEnrollmentBudgetAdjustments.mockResolvedValue({ ok: true, data: { adjustments: [], next_cursor: null } });
});

const renderParticipants = (overrides = {}) =>
  render(
    <StudyParticipants
      study={STUDY}
      arms={ARMS}
      state={{ isLoading: false, error: "", data: { arms: [], participants: PARTICIPANTS } }}
      onReload={jest.fn()}
      {...overrides}
    />,
  );

test("lists spend and budget per participant with an Exhausted badge and filter", () => {
  renderParticipants();
  const table = screen.getByRole("table", { name: "Enrolled participants" });
  expect(within(table).getByRole("columnheader", { name: "Spent" })).toBeInTheDocument();
  expect(within(table).getByRole("columnheader", { name: "Budget" })).toBeInTheDocument();
  expect(within(table).getByText("$3.12 / $10.00")).toBeInTheDocument();
  expect(within(table).getByText("$0.40 held")).toBeInTheDocument();
  expect(within(table).getByText("Exhausted")).toBeInTheDocument();
  const meters = within(table).getAllByRole("meter", { name: "Budget used" });
  expect(meters).toHaveLength(2);
  expect(meters[0]).toHaveClass("is-ok");
  expect(meters[1]).toHaveClass("is-exhausted");
  // The reserved amount lives in the tooltip, not the cell text.
  expect(within(table).getByTitle(/\$0\.40 reserved for calls in flight/)).toBeInTheDocument();
  // An unmetered (Codex) row has no budget at all.
  expect(within(table).getAllByTitle("This arm is not metered")).toHaveLength(2);

  fireEvent.change(screen.getByLabelText("Filter by budget"), { target: { value: "EXHAUSTED" } });
  expect(within(table).queryByText("P-ALPHA")).not.toBeInTheDocument();
  expect(within(table).getByText("P-BRAVO")).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Filter by budget"), { target: { value: "REMAINING" } });
  expect(within(table).getByText("P-ALPHA")).toBeInTheDocument();
  expect(within(table).queryByText("P-BRAVO")).not.toBeInTheDocument();
  expect(within(table).queryByText("P-CODEX")).not.toBeInTheDocument();
});

test("the CSV export carries the budget columns in decimal USD", () => {
  const blobs = [];
  const OriginalBlob = global.Blob;
  global.Blob = class extends OriginalBlob {
    constructor(parts, options) {
      super(parts, options);
      blobs.push(parts.join(""));
    }
  };
  global.URL.createObjectURL = jest.fn(() => "blob:csv");
  global.URL.revokeObjectURL = jest.fn();
  const click = jest.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  try {
    renderParticipants();
    fireEvent.click(screen.getByRole("button", { name: /export csv/i }));
    const [header, alpha, bravo, codex] = blobs[0].trim().split("\n");
    expect(header).toMatch(/,spent_usd,budget_usd,reserved_usd,budget_exhausted_at$/);
    expect(alpha).toMatch(/,3\.12,10\.00,0\.40,$/);
    expect(bravo).toMatch(/,10\.00,10\.00,0\.00,2026-09-11T09:00:00Z$/);
    expect(codex).toMatch(/,,,,$/);
  } finally {
    global.Blob = OriginalBlob;
    click.mockRestore();
  }
});

test("the drawer's Adjust budget action opens the form; a failed submit keeps its idempotency key so the retry replays", async () => {
  const onReload = jest.fn();
  api.adjustEnrollmentBudget
    .mockResolvedValueOnce({ ok: false, status: null, error: "Failed to adjust participant budget", errors: [] })
    .mockResolvedValueOnce({
      ok: true,
      status: 201,
      data: {
        adjustment: { kind: "TOP_UP", delta_micro_usd: 5000000, limit_before_micro_usd: 10000000, limit_after_micro_usd: 15000000 },
        replayed: false,
        balance: { ...BALANCE, limit_micro_usd: 15000000, remaining_micro_usd: 11480000 },
      },
    });

  renderParticipants({ onReload });
  fireEvent.click(screen.getByRole("button", { name: "Open dashboard for participant P-ALPHA" }));
  const drawer = await screen.findByRole("dialog", { name: "Participant P-ALPHA" });
  await waitFor(() => expect(api.getEnrollmentBudget).toHaveBeenCalledWith("study-1", "e-1"));
  expect(api.getEnrollmentBudgetLedger).toHaveBeenCalledWith("study-1", "e-1", { limit: 20, cursor: undefined });

  // Balance, ledger and history come from the budget endpoints.
  expect(await within(drawer).findByText("$6.48")).toBeInTheDocument();
  expect(within(drawer).getByText("Study default")).toBeInTheDocument();
  expect(within(drawer).getByRole("meter", { name: "Budget used" })).toHaveAttribute("aria-valuetext", "$3.12 spent of $10.00");
  const ledger = within(drawer).getByRole("table", { name: "Metered model calls" });
  expect(within(ledger).getByText("Settled")).toBeInTheDocument();
  expect(within(ledger).getByText("$0.12")).toBeInTheDocument();
  expect(within(drawer).getByText(/No adjustments yet/)).toBeInTheDocument();

  // "Load more" follows the server's cursor.
  fireEvent.click(within(drawer).getByRole("button", { name: "Load more" }));
  await waitFor(() =>
    expect(api.getEnrollmentBudgetLedger).toHaveBeenLastCalledWith("study-1", "e-1", {
      limit: 20,
      cursor: "2026-09-10T09:00:00+00:00",
    }),
  );
  await waitFor(() => expect(within(drawer).queryByRole("button", { name: "Load more" })).not.toBeInTheDocument());

  expect(within(drawer).queryByRole("form", { name: "Adjust budget" })).not.toBeInTheDocument();
  fireEvent.click(within(drawer).getByRole("button", { name: "Adjust budget" }));
  const form = within(drawer).getByRole("form", { name: "Adjust budget" });
  const submit = within(form).getByRole("button", { name: "Top up budget" });
  expect(submit).toBeDisabled();
  fireEvent.change(within(form).getByLabelText("Amount to add (USD)"), { target: { value: "5" } });
  fireEvent.change(within(form).getByLabelText("Reason"), { target: { value: "Extra week" } });
  expect(within(form).getByText(/new limit \$15\.00/)).toBeInTheDocument();
  expect(submit).not.toBeDisabled();
  fireEvent.click(submit);

  await waitFor(() => expect(api.adjustEnrollmentBudget).toHaveBeenCalledTimes(1));
  expect(await within(form).findByRole("alert")).toHaveTextContent(/Failed to adjust/);
  // The same unchanged request goes out with the same key: the server replays, never double-applies.
  fireEvent.click(within(form).getByRole("button", { name: "Top up budget" }));
  await waitFor(() => expect(api.adjustEnrollmentBudget).toHaveBeenCalledTimes(2));
  const [first, second] = api.adjustEnrollmentBudget.mock.calls.map((call) => call[2]);
  expect(api.adjustEnrollmentBudget.mock.calls[0].slice(0, 2)).toEqual(["study-1", "e-1"]);
  expect(first).toEqual({
    kind: "TOP_UP",
    amountUsd: "5.00",
    reason: "Extra week",
    idempotencyKey: expect.stringMatching(/^[A-Za-z0-9_-]{8,128}$/),
  });
  expect(second.idempotencyKey).toBe(first.idempotencyKey);

  expect(await within(drawer).findByText("Topped up by $5.00; the limit is now $15.00.")).toBeInTheDocument();
  expect(onReload).toHaveBeenCalled();
  expect(within(drawer).queryByRole("form", { name: "Adjust budget" })).not.toBeInTheDocument();
  expect(within(drawer).getByText("$11.48")).toBeInTheDocument();
});

test("editing the request after a failure mints a new key, and Set new limit previews the limit", async () => {
  api.adjustEnrollmentBudget.mockResolvedValue({ ok: false, status: 409, code: "STUDY_STOPPED", error: "stopped" });

  renderParticipants();
  fireEvent.click(screen.getByRole("button", { name: "Open dashboard for participant P-ALPHA" }));
  const drawer = await screen.findByRole("dialog", { name: "Participant P-ALPHA" });
  await within(drawer).findByText("$6.48");
  fireEvent.click(within(drawer).getByRole("button", { name: "Adjust budget" }));
  const form = within(drawer).getByRole("form", { name: "Adjust budget" });

  fireEvent.click(within(form).getByRole("button", { name: "Set new limit" }));
  fireEvent.change(within(form).getByLabelText("New limit (USD)"), { target: { value: "2" } });
  fireEvent.change(within(form).getByLabelText("Reason"), { target: { value: "Wind down" } });
  expect(within(form).getByText(/Current limit \$10\.00 → new limit \$2\.00/)).toBeInTheDocument();
  expect(within(form).getByText(/A limit below what is already spent stops further calls/)).toBeInTheDocument();
  fireEvent.click(within(form).getByRole("button", { name: "Save new limit" }));

  await waitFor(() => expect(api.adjustEnrollmentBudget).toHaveBeenCalledTimes(1));
  expect(await within(form).findByRole("alert")).toHaveTextContent(/The study has been stopped/);
  fireEvent.change(within(form).getByLabelText("Reason"), { target: { value: "Wind down now" } });
  fireEvent.click(within(form).getByRole("button", { name: "Save new limit" }));
  await waitFor(() => expect(api.adjustEnrollmentBudget).toHaveBeenCalledTimes(2));
  const [first, second] = api.adjustEnrollmentBudget.mock.calls.map((call) => call[2]);
  expect(first).toMatchObject({ kind: "SET_LIMIT", amountUsd: "2.00", reason: "Wind down" });
  expect(second.reason).toBe("Wind down now");
  expect(second.idempotencyKey).not.toBe(first.idempotencyKey);
});

test("a stopped study offers no budget adjustment", async () => {
  renderParticipants({ study: { ...STUDY, research_status: "STUDY_STOPPED" } });
  fireEvent.click(screen.getByRole("button", { name: "Open dashboard for participant P-ALPHA" }));
  const drawer = await screen.findByRole("dialog", { name: "Participant P-ALPHA" });

  expect(await within(drawer).findByText("$6.48")).toBeInTheDocument();
  expect(within(drawer).queryByRole("button", { name: "Adjust budget" })).not.toBeInTheDocument();
});

test("an unmetered participant has no budget panel", async () => {
  renderParticipants();
  fireEvent.click(screen.getByRole("button", { name: "Open dashboard for participant P-CODEX" }));
  await screen.findByRole("dialog", { name: "Participant P-CODEX" });
  await waitFor(() => expect(api.getStudyParticipantDashboard).toHaveBeenCalledWith("study-1", "e-3"));

  expect(api.getEnrollmentBudget).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "Adjust budget" })).not.toBeInTheDocument();
});
