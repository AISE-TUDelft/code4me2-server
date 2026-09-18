import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import ResearchJoin from "../ResearchJoin";
import * as api from "../../../utils/api";
import { RESEARCH_JOIN_INTENT_KEY } from "../ResearchJoin";

jest.mock("../../../utils/api");

test("resolves a study and joins through the browser consent flow", async () => {
  api.resolveResearchJoinCode.mockResolvedValue({
    ok: true,
    data: { study: { studyId: "study-1", name: "Pilot study", description: "A short study" }, consentText: "I agree." },
  });
  api.redeemResearchJoinCode.mockResolvedValue({ ok: true, data: { study_id: "study-1", status: "ACTIVE" } });

  render(<ResearchJoin />);
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
  render(<ResearchJoin />);
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
  render(<ResearchJoin />);
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
  render(<ResearchJoin />);
  fireEvent.change(screen.getByLabelText("Join code"), { target: { value: "JOIN1234" } });
  fireEvent.click(screen.getByRole("button", { name: "Review study" }));
  expect(await screen.findByRole("alert")).toBeInTheDocument();
});

test("requires consent before joining", async () => {
  api.resolveResearchJoinCode.mockResolvedValue({
    ok: true,
    data: { study: { studyId: "study-1", name: "Pilot study" } },
  });

  render(<ResearchJoin />);
  fireEvent.change(screen.getByLabelText("Join code"), { target: { value: "JOIN1234" } });
  fireEvent.click(screen.getByRole("button", { name: "Review study" }));
  fireEvent.click(await screen.findByRole("button", { name: "Accept and join" }));

  expect(api.redeemResearchJoinCode).not.toHaveBeenCalled();
  expect(screen.getByRole("alert")).toHaveTextContent(/accept the consent/i);
});
