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
