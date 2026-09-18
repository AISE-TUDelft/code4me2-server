import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import ResearchStudyEditor from "../ResearchStudyEditor";
import * as api from "../../../utils/api";

jest.mock("../../../utils/api");

const STUDY = {
  study_id: "study-1",
  name: "Pilot study",
  description: "Before consent",
  research_status: "DRAFT",
  join_code: "JOIN1234",
  consent_locked_at: null,
};

const renderEditor = () =>
  render(
    <MemoryRouter initialEntries={["/research/studies/study-1/editor"]}>
      <Routes>
        <Route path="/research/studies/:studyId/editor" element={<ResearchStudyEditor />} />
      </Routes>
    </MemoryRouter>,
  );

test("edits metadata through the lifecycle endpoint", async () => {
  api.getResearchStudy.mockResolvedValue({ ok: true, data: STUDY });
  api.updateResearchStudyMetadata.mockResolvedValue({ ok: true, data: { study: { ...STUDY, name: "Updated" } } });

  renderEditor();
  const name = await screen.findByLabelText("Name");
  fireEvent.change(name, { target: { value: "Updated" } });
  fireEvent.click(screen.getByRole("button", { name: "Save metadata" }));

  await waitFor(() => expect(api.updateResearchStudyMetadata).toHaveBeenCalledWith("study-1", {
    name: "Updated",
    description: "Before consent",
  }));
});

test("locks metadata after consent", async () => {
  api.getResearchStudy.mockResolvedValue({ ok: true, data: { ...STUDY, consent_locked_at: "2026-09-18T10:00:00Z" } });

  renderEditor();
  expect(await screen.findByLabelText("Name")).toBeDisabled();
  expect(screen.getByRole("button", { name: "Save metadata" })).toBeDisabled();
  expect(screen.getByText(/metadata is locked/i)).toBeInTheDocument();
});
