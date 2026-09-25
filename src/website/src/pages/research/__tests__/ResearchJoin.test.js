import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import ResearchJoin from "../ResearchJoin";
import * as api from "../../../utils/api";
import { RESEARCH_JOIN_INTENT_KEY } from "../ResearchJoin";

jest.mock("../../../utils/api");

const renderJoin = (props = {}, path = "/research/join") =>
  render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={[path]}>
      <ResearchJoin {...props} />
    </MemoryRouter>,
  );

test("resolves a study and joins through the browser consent flow", async () => {
  api.resolveResearchJoinCode.mockResolvedValue({
    ok: true,
    data: { study: { studyId: "study-1", name: "Pilot study", description: "A short study" }, consentText: "I agree." },
  });
  api.redeemResearchJoinCode.mockResolvedValue({ ok: true, data: { study_id: "study-1", status: "ACTIVE" } });

  renderJoin();
  fireEvent.change(screen.getByLabelText("Join code"), { target: { value: "JOIN1234" } });
  fireEvent.click(screen.getByRole("button", { name: "Review study" }));

  expect(await screen.findByText("Pilot study")).toBeInTheDocument();
  expect(screen.getByText("A short study")).toBeInTheDocument();
  expect(screen.getByText("I agree.")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Accept and join" }));

  await waitFor(() => {
    expect(api.redeemResearchJoinCode).toHaveBeenCalledWith("JOIN1234", true);
    expect(screen.getByRole("status")).toHaveTextContent(/enrollment complete/i);
  });
});

test("persists only a bounded join intent and redirects to login on 401", async () => {
  api.resolveResearchJoinCode.mockResolvedValue({ ok: false, status: 401, code: "AUTHENTICATION_REQUIRED" });
  const assign = jest.fn();
  Object.defineProperty(window, "location", { configurable: true, value: { assign } });
  renderJoin();
  fireEvent.change(screen.getByLabelText("Join code"), { target: { value: "JOIN1234" } });
  fireEvent.click(screen.getByRole("button", { name: "Review study" }));
  await waitFor(() => expect(assign).toHaveBeenCalledWith("/login"));
  expect(sessionStorage.getItem(RESEARCH_JOIN_INTENT_KEY)).toBe("JOIN1234");
  expect(assign.mock.calls[0][0]).not.toContain("JOIN1234");
});

test("restores pending code and clears it after successful reuse", async () => {
  sessionStorage.setItem(RESEARCH_JOIN_INTENT_KEY, "JOIN1234");
  api.resolveResearchJoinCode.mockResolvedValue({ ok: true, data: { study: { name: "Pilot study" }, consentText: "I agree." } });
  api.redeemResearchJoinCode.mockResolvedValue({ ok: true, data: { enrollment_id: "e-1", assignment_id: "a-1", agent_profile_id: "p-1", reused: true } });
  renderJoin();
  expect(screen.getByLabelText("Join code")).toHaveValue("JOIN1234");
  fireEvent.click(screen.getByRole("button", { name: "Review study" }));
  fireEvent.click(await screen.findByRole("checkbox"));
  fireEvent.click(await screen.findByRole("button", { name: "Accept and join" }));
  await waitFor(() => expect(api.redeemResearchJoinCode).toHaveBeenCalledWith("JOIN1234", true));
  expect(sessionStorage.getItem(RESEARCH_JOIN_INTENT_KEY)).toBeNull();
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/already enrolled/i));
});

test.each(["STUDY_STOPPED", "ALREADY_ENROLLED", "ACTIVE_ENROLLMENT_EXISTS", "CONSENT_REQUIRED"])("renders typed %s state", async (code) => {
  api.resolveResearchJoinCode.mockResolvedValue({ ok: false, code, status: code === "CONSENT_REQUIRED" ? 422 : 409, error: code });
  renderJoin();
  fireEvent.change(screen.getByLabelText("Join code"), { target: { value: "JOIN1234" } });
  fireEvent.click(screen.getByRole("button", { name: "Review study" }));
  expect(await screen.findByRole("alert")).toBeInTheDocument();
});

test("requires consent before joining", async () => {
  api.resolveResearchJoinCode.mockResolvedValue({
    ok: true,
    data: { study: { studyId: "study-1", name: "Pilot study" } },
  });

  renderJoin();
  fireEvent.change(screen.getByLabelText("Join code"), { target: { value: "JOIN1234" } });
  fireEvent.click(screen.getByRole("button", { name: "Review study" }));
  fireEvent.click(await screen.findByRole("button", { name: "Accept and join" }));

  expect(api.redeemResearchJoinCode).not.toHaveBeenCalled();
  expect(screen.getByRole("alert")).toHaveTextContent(/accept the consent/i);
});

const ACTIVE_ENROLLMENT = {
  enrollment_id: "e-1",
  participant_code: "P-7F3A",
  study_id: "s-1",
  status: "ACTIVE",
  enrolled_at: "2026-09-20T10:00:00Z",
  consent_accepted_at: "2026-09-20T10:00:00Z",
  study: {
    study_id: "s-1",
    name: "Context window pilot",
    description: "Compare two agent configurations.",
    research_status: "ACTIVE",
    starts_at: "2026-09-01T00:00:00Z",
    ends_at: "2099-01-01T00:00:00Z",
    collection: { allowed_field_classes: ["STRUCTURAL", "METRICS"], content_capture: false },
  },
  runtime: { framework_version: "goose", display_name: "Goose (install on your machine)" },
  sessions: { total: 3, active: 0, last_activity_at: "2026-09-23T12:00:00Z" },
  activity: { prompts: 12, tool_calls: 40, last_event_at: "2026-09-23T12:00:00Z" },
};

test("My studies shows the current study, what it collects and setup steps", async () => {
  api.getMyResearchEnrollments.mockResolvedValue({ ok: true, data: [ACTIVE_ENROLLMENT] });

  renderJoin({ user: { email: "p@example.com" } }, "/research/my-studies");

  expect(await screen.findByText("Context window pilot")).toBeInTheDocument();
  expect(screen.getByText("P-7F3A")).toBeInTheDocument();
  expect(screen.getByText("Goose (install on your machine)")).toBeInTheDocument();
  expect(screen.getByText("Install Goose")).toBeInTheDocument();
  expect(screen.getByText(/Your prompts, code and tool output are not collected/i)).toBeInTheDocument();
  // Participants stay blind to their arm: no profile names or models.
  expect(screen.queryByText(/profile/i)).not.toBeInTheDocument();
  // One active study at a time: the join form is behind a button.
  expect(screen.queryByLabelText("Join code")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /enter a join code/i }));
  expect(screen.getByLabelText("Join code")).toBeInTheDocument();
});

test("My studies lists content capture whenever the study stores content", async () => {
  // Content is stored with the flag and consent even when CONTENT is not declared.
  const capturing = {
    ...ACTIVE_ENROLLMENT,
    study: {
      ...ACTIVE_ENROLLMENT.study,
      collection: { allowed_field_classes: ["STRUCTURAL", "METRICS"], content_capture: true },
    },
  };
  api.getMyResearchEnrollments.mockResolvedValue({ ok: true, data: [capturing] });

  renderJoin({ user: { email: "p@example.com" } }, "/research/my-studies");

  expect(
    await screen.findByText("Your prompts, the agent's responses and reasoning, tool arguments and output, and file contents"),
  ).toBeInTheDocument();
  expect(screen.getByText("Sensitive")).toBeInTheDocument();
  expect(screen.queryByText(/are not collected/i)).not.toBeInTheDocument();
});

const withCollection = (collection) => ({
  ...ACTIVE_ENROLLMENT,
  study: { ...ACTIVE_ENROLLMENT.study, collection },
});

const EVENTS =
  "Which events happened and when, in the agent and in your IDE (for example files opened, edited and saved, and runs), with timings, counts, sizes and token usage";
const ACTIVITY =
  "Details of what the agent and your IDE did: tools run, approvals, runs and errors. Tool titles and error messages are kept; they can contain full command lines, file paths, search terms and URLs.";
const USAGE = "Measurements and system details: timings, token counts, edit sizes, exit codes, software versions and platform details";
const FILES =
  "Which files you work in: file types and languages, and possibly file paths and symbol or repository names (never file contents)";
const FILES_HASHED =
  "Which file types and languages you work in, stored as unsalted hashes (easy to reverse for common values; never file contents)";

test("My studies lists what the policy actually collects, not the declared names", async () => {
  // DIAGNOSTICS enables all agent-activity metadata at runtime, not only errors.
  api.getMyResearchEnrollments.mockResolvedValue({
    ok: true,
    data: [withCollection({ allowed_field_classes: ["METRICS", "DIAGNOSTICS"], content_capture: false })],
  });
  renderJoin({ user: { email: "p@example.com" } }, "/research/my-studies");

  expect(await screen.findByText(ACTIVITY)).toBeInTheDocument();
  expect(screen.getByText(USAGE)).toBeInTheDocument();
  // Event records are kept whatever the policy.
  expect(screen.getByText(EVENTS)).toBeInTheDocument();
  // Code metadata was not declared: it is still stored, as hashes.
  expect(screen.getByText(FILES_HASHED)).toBeInTheDocument();
  expect(screen.queryByText(FILES)).not.toBeInTheDocument();
});

test.each([
  ["runtime names and SECRET", ["SYSTEM", "BEHAVIORAL", "SECRET"], [EVENTS, ACTIVITY, USAGE, FILES_HASHED]],
  ["a legacy SECRET-only policy", ["SECRET"], [EVENTS, ACTIVITY, USAGE, FILES]],
  ["both vocabularies", ["STRUCTURAL", "SYSTEM", "BEHAVIORAL", "CODE_METADATA"], [EVENTS, ACTIVITY, USAGE, FILES]],
])("My studies explains %s without raw or repeated names", async (_label, classes, expected) => {
  api.getMyResearchEnrollments.mockResolvedValue({
    ok: true,
    data: [withCollection({ allowed_field_classes: classes, content_capture: false })],
  });
  renderJoin({ user: { email: "p@example.com" } }, "/research/my-studies");

  expect(await screen.findByText(expected[0])).toBeInTheDocument();
  for (const label of expected) expect(screen.getAllByText(label)).toHaveLength(1);
  expect(screen.getAllByRole("listitem").filter((item) => item.closest(".participant-collect"))).toHaveLength(expected.length);
  expect(screen.queryByText(/SECRET|SYSTEM|BEHAVIORAL|STRUCTURAL/)).not.toBeInTheDocument();
});

test("the join route always shows the join form, even with an active study", async () => {
  api.getMyResearchEnrollments.mockResolvedValue({ ok: true, data: [ACTIVE_ENROLLMENT] });

  renderJoin({ user: { email: "p@example.com" } }, "/research/join");

  expect(await screen.findByText("Context window pilot")).toBeInTheDocument();
  expect(screen.getByLabelText("Join code")).toBeInTheDocument();
});

test("earlier studies are listed with their final status", async () => {
  api.getMyResearchEnrollments.mockResolvedValue({
    ok: true,
    data: [{ ...ACTIVE_ENROLLMENT, status: "COMPLETED" }],
  });

  renderJoin({ user: { email: "p@example.com" } }, "/research/my-studies");

  expect(await screen.findByText("Previous studies")).toBeInTheDocument();
  expect(screen.getByText("Completed")).toBeInTheDocument();
});
