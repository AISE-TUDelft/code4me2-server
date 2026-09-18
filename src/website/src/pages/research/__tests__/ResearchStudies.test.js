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
    profileIds: ["profile-1"],
  }));
});

test("stops a study and exposes clone for stopped studies", async () => {
  const stopped = { ...STUDY, research_status: "STUDY_STOPPED" };
  api.listResearchStudies
    .mockResolvedValueOnce({ ok: true, data: [STUDY] })
    .mockResolvedValue({ ok: true, data: [stopped] });
  api.stopResearchStudy.mockResolvedValue({ ok: true, data: { study: stopped } });
  api.cloneResearchStudy.mockResolvedValue({ ok: true, data: { study: { ...STUDY, study_id: "study-2" } } });
  jest.spyOn(window, "confirm").mockReturnValue(true);

  renderPage();
  fireEvent.click(await screen.findByText("Pilot study"));
  fireEvent.click(screen.getByRole("button", { name: "Stop study" }));

  await waitFor(() => expect(api.stopResearchStudy).toHaveBeenCalledWith("study-1"));
  fireEvent.click(await screen.findByRole("button", { name: "Clone as new Draft" }));
  await waitFor(() => expect(api.cloneResearchStudy).toHaveBeenCalledWith("study-1"));
  window.confirm.mockRestore();
});

test("shows the kill-switch control to admins and calls the operations API", async () => {
  api.getCurrentUser.mockResolvedValue({ ok: true, user: { is_admin: true } });
  api.listResearchStudies.mockResolvedValue({ ok: true, data: [STUDY] });
  api.engageResearchKillSwitch.mockResolvedValue({ ok: true, data: { switch_id: "switch-1" } });

  renderPage();
  fireEvent.click(await screen.findByText("Pilot study"));
  fireEvent.click(screen.getByRole("button", { name: "Engage kill switch" }));

  await waitFor(() => expect(api.engageResearchKillSwitch).toHaveBeenCalledWith("study-1", "Admin maintenance"));
});
