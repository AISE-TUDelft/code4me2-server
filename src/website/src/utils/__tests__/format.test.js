import { formatDate } from "../format";

// The test runner's time zone is fixed at start-up, so the options passed to
// the formatter are checked rather than the rendered text.
test("a bare date is formatted as its UTC calendar day", () => {
  const format = jest.spyOn(Date.prototype, "toLocaleDateString");
  try {
    formatDate("2026-09-24");
    expect(format.mock.calls[0][1]).toEqual(expect.objectContaining({ timeZone: "UTC" }));
    // A timestamp is still shown on the viewer's local day.
    formatDate("2026-09-24T23:30:00Z");
    expect(format.mock.calls[1][1].timeZone).toBeUndefined();
  } finally {
    format.mockRestore();
  }
});

test("a bare date renders its own day", () => {
  expect(formatDate("2026-09-24")).toContain("24");
});
