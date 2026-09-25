import React, { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { Drawer, Switch, TabPanel, Tabs } from "../ui";

const DrawerHarness = () => {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Open details
      </button>
      <Drawer open={open} title="Participant P-1" onClose={() => setOpen(false)}>
        <a href="#timeline">Timeline</a>
        <button type="button">Export</button>
        {/* Not tabbable: must not become the end of the focus wrap. */}
        <button type="button" tabIndex={-1}>
          Inactive tab
        </button>
        <div hidden>
          <button type="button">Hidden action</button>
        </div>
        <fieldset disabled>
          <button type="button">Locked action</button>
        </fieldset>
      </Drawer>
    </>
  );
};

test("the drawer keeps focus inside and returns it to the opener", () => {
  render(<DrawerHarness />);
  const opener = screen.getByRole("button", { name: "Open details" });
  opener.focus();
  fireEvent.click(opener);

  const close = screen.getByRole("button", { name: "Close" });
  const last = screen.getByRole("button", { name: "Export" });
  expect(close).toHaveFocus();

  // Tab from the last control wraps to the first, Shift+Tab from the first to the last.
  last.focus();
  fireEvent.keyDown(document, { key: "Tab" });
  expect(close).toHaveFocus();
  fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
  expect(last).toHaveFocus();

  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(opener).toHaveFocus();
});

const TABS = [
  { id: "overview", label: "Overview" },
  { id: "participants", label: "Participants" },
  { id: "analytics", label: "Analytics" },
];

const TabsHarness = () => {
  const [active, setActive] = useState("overview");
  return (
    <>
      <Tabs tabs={TABS} active={active} onChange={setActive} idPrefix="t" />
      <TabPanel id={active} idPrefix="t">
        {active}
      </TabPanel>
    </>
  );
};

test("tabs point only at the rendered panel and support arrow, Home and End keys", () => {
  render(<TabsHarness />);
  const overview = screen.getByRole("tab", { name: "Overview" });
  expect(overview).toHaveAttribute("aria-controls", "t-panel-overview");
  expect(screen.getByRole("tab", { name: "Analytics" })).not.toHaveAttribute("aria-controls");
  expect(screen.getByRole("tabpanel")).toHaveAttribute("id", "t-panel-overview");

  fireEvent.keyDown(overview, { key: "End" });
  expect(screen.getByRole("tab", { name: "Analytics" })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("tab", { name: "Analytics" })).toHaveFocus();
  fireEvent.keyDown(screen.getByRole("tab", { name: "Analytics" }), { key: "ArrowRight" });
  expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
  fireEvent.keyDown(screen.getByRole("tab", { name: "Overview" }), { key: "ArrowLeft" });
  expect(screen.getByRole("tab", { name: "Analytics" })).toHaveAttribute("aria-selected", "true");
  fireEvent.keyDown(screen.getByRole("tab", { name: "Analytics" }), { key: "Home" });
  expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("tabpanel")).toHaveAttribute("aria-labelledby", "t-overview");
});

test("a named switch keeps a stable name and exposes its state through aria-checked", () => {
  render(
    <Switch
      checked
      ariaLabel="Availability"
      label="Active"
      description="Researchers can select it."
      onChange={() => {}}
    />,
  );
  const control = screen.getByRole("switch", { name: "Availability" });
  expect(control).toBeChecked();
  expect(control).toHaveAccessibleDescription("Researchers can select it.");
  // The visible on/off text is state, not a second label.
  expect(screen.getByText("Active")).toHaveAttribute("aria-hidden", "true");
});

test("an unnamed switch is labelled by its visible text", () => {
  render(<Switch checked={false} label="Active" onChange={() => {}} />);
  expect(screen.getByRole("switch", { name: "Active" })).not.toBeChecked();
});
