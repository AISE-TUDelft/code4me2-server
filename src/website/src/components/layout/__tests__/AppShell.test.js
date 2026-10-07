import React from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import AppShell, { buildNavigation, homePath } from "../AppShell";
import { ThemeProvider } from "../../../context/ThemeContext";
import * as api from "../../../utils/api";

jest.mock("../../../utils/api");

beforeEach(() => {
  api.checkVerificationStatus.mockResolvedValue({ ok: true, verified: true });
});

const renderShell = (user, path = "/research/my-studies") =>
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

const ARCHIVED_VIEWS = ["Overview", "Usage", "Model performance", "Agent telemetry", "Calibration"];

test("participants see My studies and Privacy & data only", () => {
  expect(navLabels({ is_admin: false, can_research: false })).toEqual(["My studies", "Privacy & data"]);
});

test("no role sees the archived analytics views", () => {
  [{ is_admin: false, can_research: false }, { is_admin: false, can_research: true }, { is_admin: true }].forEach(
    (user) => {
      const labels = navLabels(user);
      ARCHIVED_VIEWS.forEach((label) => expect(labels).not.toContain(label));
      expect(buildNavigation(user).map((group) => group.id)).not.toContain("analytics");
    },
  );
});

test("researchers land on Studies, participants on My studies", () => {
  expect(homePath({ is_admin: true })).toBe("/research/studies");
  expect(homePath({ is_admin: false, can_research: true })).toBe("/research/studies");
  expect(homePath({ is_admin: false, can_research: false })).toBe("/research/my-studies");
  expect(homePath(null)).toBe("/research/my-studies");
});

test("the brand link goes to the user's home page", () => {
  renderShell({ is_admin: false, can_research: true });
  expect(screen.getByRole("link", { name: "Code4Me home" })).toHaveAttribute("href", "/research/studies");
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

test("every signed-in user gets Privacy & data as the last group", () => {
  [{ is_admin: false, can_research: false }, { is_admin: false, can_research: true }, { is_admin: true }].forEach(
    (user) => {
      const groups = buildNavigation(user);
      const account = groups[groups.length - 1];
      expect(account.label).toBe("Account");
      expect(account.items).toEqual([
        expect.objectContaining({ label: "Privacy & data", path: "/settings/privacy", icon: "shield" }),
      ]);
    },
  );
});

test("a participant sees Privacy & data in the sidebar and it marks its page", async () => {
  renderShell({ is_admin: false, can_research: false }, "/settings/privacy");
  const nav = screen.getByRole("navigation", { name: "Main navigation" });
  // findBy also lets the verification banner's check settle.
  const link = await within(nav).findByRole("link", { name: "Privacy & data" });
  expect(link).toHaveAttribute("href", "/settings/privacy");
  expect(link).toHaveAttribute("aria-current", "page");
  expect(screen.getByText("Account")).toBeInTheDocument();
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
