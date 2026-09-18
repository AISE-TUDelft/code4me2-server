import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import ResearchJoin from "../ResearchJoin";
import * as api from "../../../utils/api";

jest.mock("../../../utils/api");

test("resolves a study and joins through the browser consent flow", async () => {
  api.resolveResearchJoinCode.mockResolvedValue({
    ok: true,
    data: { study: { studyId: "study-1", name: "Pilot study" } },
  });
  api.redeemResearchJoinCode.mockResolvedValue({ ok: true, data: { study_id: "study-1", status: "ACTIVE" } });

  render(<ResearchJoin />);
  fireEvent.change(screen.getByLabelText("Join code"), { target: { value: "JOIN1234" } });
  fireEvent.click(screen.getByRole("button", { name: "Review study" }));

  expect(await screen.findByText("Pilot study")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Accept and join" }));

  await waitFor(() => {
    expect(api.redeemResearchJoinCode).toHaveBeenCalledWith("JOIN1234", true);
    expect(screen.getByRole("status")).toHaveTextContent(/joined the study/i);
  });
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
