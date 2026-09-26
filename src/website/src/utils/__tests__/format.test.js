import { formatDate, formatUsd, microToUsd, parseUsdInput, usdToMicro } from "../format";

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

describe("money (integer micro-USD, string arithmetic)", () => {
  test("formatUsd renders two decimals with grouping and half-up rounding", () => {
    expect(formatUsd(12500000)).toBe("$12.50");
    expect(formatUsd(1250000000)).toBe("$1,250.00");
    expect(formatUsd(0)).toBe("$0.00");
    expect(formatUsd("3120000")).toBe("$3.12");
    expect(formatUsd(3125000)).toBe("$3.13");
    // The carry crosses into the dollars.
    expect(formatUsd(999999)).toBe("$1.00");
    expect(formatUsd(-500000)).toBe("-$0.50");
  });

  test("formatUsd keeps four decimals below one cent so a tiny charge never reads as zero", () => {
    expect(formatUsd(4250)).toBe("$0.0043");
    expect(formatUsd(120)).toBe("$0.0001");
  });

  test("formatUsd shows a dash for missing or non-integer values", () => {
    expect(formatUsd(null)).toBe("—");
    expect(formatUsd(undefined)).toBe("—");
    expect(formatUsd("")).toBe("—");
    expect(formatUsd(1.5)).toBe("—");
    expect(formatUsd("abc")).toBe("—");
  });

  test("microToUsd is the plain decimal string for CSV cells", () => {
    expect(microToUsd(3120000)).toBe("3.12");
    expect(microToUsd(1250000000)).toBe("1250.00");
    expect(microToUsd(null)).toBeNull();
  });

  test("parseUsdInput normalizes typed amounts by copying digits, never multiplying floats", () => {
    expect(parseUsdInput("12.5")).toEqual({ ok: true, value: "12.50", micro: 12500000 });
    expect(parseUsdInput("$1,250")).toEqual({ ok: true, value: "1250.00", micro: 1250000000 });
    expect(parseUsdInput("0.0042")).toEqual({ ok: true, value: "0.0042", micro: 4200 });
    expect(parseUsdInput(".5")).toEqual({ ok: true, value: "0.50", micro: 500000 });
    expect(parseUsdInput("0.3").micro).toBe(300000);
    expect(parseUsdInput("19.99").micro).toBe(19990000);
    expect(parseUsdInput("007")).toEqual({ ok: true, value: "7.00", micro: 7000000 });
  });

  test("parseUsdInput rejects what the server rejects", () => {
    expect(parseUsdInput("")).toMatchObject({ ok: false });
    expect(parseUsdInput("abc")).toMatchObject({ ok: false });
    expect(parseUsdInput("-5")).toMatchObject({ ok: false });
    expect(parseUsdInput("1e3")).toMatchObject({ ok: false });
    expect(parseUsdInput(".")).toMatchObject({ ok: false });
    expect(parseUsdInput("0.1234567")).toMatchObject({ ok: false, error: expect.stringMatching(/6 decimal/) });
    expect(parseUsdInput("100000.01")).toMatchObject({ ok: false, error: expect.stringMatching(/exceed/) });
    expect(parseUsdInput("10000.01", { max: 10000 })).toMatchObject({ ok: false });
    expect(parseUsdInput("10000", { max: 10000 })).toMatchObject({ ok: true, micro: 10000000000 });
  });

  test("usdToMicro converts a valid amount and returns null otherwise", () => {
    expect(usdToMicro("12.50")).toBe(12500000);
    expect(usdToMicro("nope")).toBeNull();
  });
});
