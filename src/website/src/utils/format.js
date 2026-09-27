// Display formatting shared by the research/admin views. A missing value is
// always rendered as an em dash (never as 0), so "not measured" and "zero"
// stay distinguishable.

export const DASH = "—";

const isMissing = (value) => value === null || value === undefined || value === "";

export const formatNumber = (value, { maximumFractionDigits = 1 } = {}) => {
  if (isMissing(value) || Number.isNaN(Number(value))) return DASH;
  return Number(value).toLocaleString(undefined, { maximumFractionDigits });
};

// 1,284 / 12.9K / 4.2M for tiles where width matters.
export const formatCompact = (value) => {
  if (isMissing(value) || Number.isNaN(Number(value))) return DASH;
  const number = Number(value);
  if (Math.abs(number) < 10000) return number.toLocaleString(undefined, { maximumFractionDigits: 1 });
  return new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(number);
};

export const formatPercent = (value, digits = 0) => {
  if (isMissing(value) || Number.isNaN(Number(value))) return DASH;
  return `${(Number(value) * 100).toFixed(digits)}%`;
};

export const parseDate = (value) => {
  if (isMissing(value)) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
};

const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

export const formatDate = (value) => {
  const date = parseDate(value);
  if (!date) return isMissing(value) ? DASH : String(value);
  // A bare YYYY-MM-DD is a calendar day (parsed as UTC midnight): show that
  // day in every time zone rather than the local day it falls on.
  const options = { year: "numeric", month: "short", day: "numeric" };
  if (typeof value === "string" && DATE_ONLY.test(value)) options.timeZone = "UTC";
  return date.toLocaleDateString(undefined, options);
};

export const formatDateTime = (value) => {
  const date = parseDate(value);
  if (!date) return isMissing(value) ? DASH : String(value);
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
};

// "Sep 23, 12:17" (the year only when it differs from the current one).
export const formatShortDateTime = (value, now = new Date()) => {
  const date = parseDate(value);
  if (!date) return isMissing(value) ? DASH : String(value);
  return date.toLocaleString(undefined, {
    ...(date.getFullYear() === now.getFullYear() ? {} : { year: "numeric" }),
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
};

export const formatRelative = (value, now = new Date()) => {
  const date = parseDate(value);
  if (!date) return DASH;
  const seconds = Math.round((date.getTime() - now.getTime()) / 1000);
  const abs = Math.abs(seconds);
  const units = [
    ["year", 31536000],
    ["month", 2592000],
    ["week", 604800],
    ["day", 86400],
    ["hour", 3600],
    ["minute", 60],
  ];
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  for (const [unit, size] of units) {
    if (abs >= size) return rtf.format(Math.round(seconds / size), unit);
  }
  return rtf.format(seconds, "second");
};

// Seconds → "42s", "3m 05s", "1h 12m", "2d 4h".
export const formatDuration = (seconds) => {
  if (isMissing(seconds) || Number.isNaN(Number(seconds))) return DASH;
  const total = Math.max(0, Math.round(Number(seconds)));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes}m ${String(total % 60).padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h ${String(minutes % 60).padStart(2, "0")}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
};

// Whole days from now until the date (negative once it has passed).
export const daysUntil = (value, now = new Date()) => {
  const date = parseDate(value);
  if (!date) return null;
  return Math.ceil((date.getTime() - now.getTime()) / 86400000);
};

export const humanize = (value) => {
  if (isMissing(value)) return DASH;
  const text = String(value).replace(/[_.]+/g, " ").trim().toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
};

// ---- Money ----------------------------------------------------------------
//
// Budgets travel as integer micro-USD in API payloads and as decimal strings
// ("12.50") in requests. Everything below works on digit strings, never on a
// float, so display and request values are exact.

export const MICRO_PER_USD = 1000000;

// Integer micro-USD (number or numeric string) → sign, whole dollars and the
// six fractional digits; null when it is not an integer amount.
const microParts = (micro) => {
  if (isMissing(micro)) return null;
  if (typeof micro === "number" && !Number.isInteger(micro)) return null;
  const match = /^(-?)(\d+)$/.exec(String(micro).trim());
  if (!match) return null;
  const digits = match[2].replace(/^0+(?=\d)/, "").padStart(7, "0");
  return {
    negative: match[1] === "-" && /[1-9]/.test(digits),
    whole: digits.slice(0, -6),
    frac: digits.slice(-6),
  };
};

// Half-up rounding of "whole.frac" to `keep` fractional digits, with carry.
const roundDigits = (whole, frac, keep) => {
  const digits = `${whole}${frac.slice(0, keep)}`.split("");
  if ((frac.charAt(keep) || "0") >= "5") {
    let index = digits.length - 1;
    while (index >= 0 && digits[index] === "9") {
      digits[index] = "0";
      index -= 1;
    }
    if (index < 0) digits.unshift("1");
    else digits[index] = String(Number(digits[index]) + 1);
  }
  const joined = digits.join("");
  return [joined.slice(0, joined.length - keep).replace(/^0+(?=\d)/, "") || "0", joined.slice(joined.length - keep)];
};

const group = (whole) => whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");

/**
 * "$12.50" from integer micro-USD; four decimals below one cent ("$0.0042")
 * so a tiny charge never reads as zero; an em dash when missing.
 */
export const formatUsd = (microUsd, { symbol = "$", grouping = true } = {}) => {
  const parts = microParts(microUsd);
  if (!parts) return DASH;
  const subCent = parts.whole === "0" && parts.frac.startsWith("00") && parts.frac !== "000000";
  const [whole, frac] = roundDigits(parts.whole, parts.frac, subCent ? 4 : 2);
  return `${parts.negative ? "-" : ""}${symbol}${grouping ? group(whole) : whole}.${frac}`;
};

// Plain decimal string ("12.50") for CSV cells and requests; null when missing.
export const microToUsd = (microUsd) => {
  const text = formatUsd(microUsd, { symbol: "", grouping: false });
  return text === DASH ? null : text;
};

/**
 * Parse typed USD ("12.5", "$1,250", "0.0042") into a normalized decimal
 * string (at least two decimals) and integer micro-USD. Mirrors the server's
 * limits: not negative, at most `maxDecimals` places, at most `max` dollars.
 */
export const parseUsdInput = (text, { max = 100000, maxDecimals = 6 } = {}) => {
  const raw = String(text ?? "").trim().replace(/^\$\s*/, "").replace(/,/g, "");
  if (!raw) return { ok: false, error: "Enter an amount in USD." };
  const match = /^(\d+)?(?:\.(\d*))?$/.exec(raw);
  if (!match || (match[1] === undefined && !match[2])) {
    return { ok: false, error: "Enter a number such as 12.50." };
  }
  const whole = (match[1] || "0").replace(/^0+(?=\d)/, "");
  const frac = match[2] || "";
  if (frac.length > maxDecimals) {
    return { ok: false, error: `Use at most ${maxDecimals} decimal places.` };
  }
  // Exact: an integer far below 2^53 once the maximum is enforced.
  const micro = Number(`${whole}${frac.padEnd(6, "0")}`);
  if (micro > max * MICRO_PER_USD) {
    return { ok: false, error: `The amount must not exceed $${group(String(max))}.` };
  }
  return { ok: true, value: `${whole}.${frac.length < 2 ? frac.padEnd(2, "0") : frac}`, micro };
};

// Integer micro-USD for a typed amount, or null when it does not parse.
export const usdToMicro = (text) => {
  const parsed = parseUsdInput(text);
  return parsed.ok ? parsed.micro : null;
};
