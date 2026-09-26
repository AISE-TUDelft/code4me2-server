import React from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import AppShell, { buildNavigation } from "../AppShell";
import { ThemeProvider } from "../../../context/ThemeContext";
import * as api from "../../../utils/api";

jest.mock("../../../utils/api");

beforeEach(() => {
  api.checkVerificationStatus.mockResolvedValue({ ok: true, verified: true });
});

const renderShell = (user, path = "/dashboard?view=overview") =>
  render(
    <ThemeProvider>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={[path]}>
        <AppShell user={user} onLogout={jest.fn()}>
          <div>PAGE</div>
        </AppShell>
      </MemoryRouter>
    </ThemeProvider>,
  );

const navLabels = (user) =>
  buildNavigation(user).flatMap((group) => group.items.map((item) => item.label));

test("participants see analytics and My studies only", () => {
  const labels = navLabels({ is_admin: false, can_research: false });
  expect(labels).toContain("My studies");
  expect(labels).not.toContain("Studies");
  expect(labels).not.toContain("Agent profiles");
  expect(labels).not.toContain("Accounts");
});

test("researchers get the research section", () => {
  const labels = navLabels({ is_admin: false, can_research: true });
  expect(labels).toEqual(expect.arrayContaining(["Studies", "Agent profiles", "My studies"]));
  expect(labels).not.toContain("Accounts");
});

test("administrators get the administration section and no legacy completion A/B screen", () => {
  const labels = navLabels({ is_admin: true });
  expect(labels).toEqual(
    expect.arrayContaining(["Accounts", "Provider connections", "Agent catalogue", "Config management"]),
  );
  expect(labels.join(" ")).not.toMatch(/completion a\/b|legacy/i);
});

test("marks the current page and links to the research routes", () => {
  renderShell({ is_admin: true, name: "Ada Admin" }, "/research/studies/abc?tab=analytics");
  const nav = screen.getByRole("navigation", { name: "Main navigation" });
  const studies = within(nav).getByRole("link", { name: "Studies" });
  expect(studies).toHaveAttribute("aria-current", "page");
  expect(studies).toHaveAttribute("href", "/research/studies");
  expect(within(nav).getByRole("link", { name: "Accounts" })).toHaveAttribute(
    "href",
    "/dashboard?view=admin-researchers",
  );
  expect(screen.getByText("Administrator")).toBeInTheDocument();
  expect(screen.getByText("PAGE")).toBeInTheDocument();
});

test("the join route highlights My studies", () => {
  renderShell({ is_admin: false }, "/research/join");
  const link = screen.getByRole("link", { name: "My studies" });
  expect(link).toHaveAttribute("aria-current", "page");
});

test("the mobile menu button toggles the navigation", () => {
  renderShell({ is_admin: false });
  const toggle = screen.getByRole("button", { name: "Open navigation" });
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(toggle);
  expect(screen.getByRole("button", { name: "Close navigation", expanded: true })).toBeInTheDocument();
});
