import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import ResearchStudies from "../ResearchStudies";
import * as api from "../../../utils/api";

jest.mock("../../../utils/api");

beforeEach(() => {
  api.getCurrentUser.mockResolvedValue({ ok: true, user: { is_admin: false } });
});

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
    <MemoryRouter initialEntries={["/research/studies"]}>
      <ResearchStudies />
    </MemoryRouter>,
  );

test("renders lifecycle study list without revision controls", async () => {
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [STUDY] });

  renderPage();

  expect(await screen.findByText("Pilot study")).toBeInTheDocument();
  expect(screen.getByText("Draft")).toBeInTheDocument();
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
  fireEvent.change(screen.getByLabelText("Kill switch reason"), { target: { value: "Admin maintenance" } });
  fireEvent.click(screen.getByRole("button", { name: "Engage kill switch" }));

  await waitFor(() => expect(api.engageResearchKillSwitch).toHaveBeenCalledWith("study-1", "Admin maintenance"));
  window.confirm.mockRestore();
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
