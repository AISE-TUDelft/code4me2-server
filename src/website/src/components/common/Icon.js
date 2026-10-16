import React from "react";

// A small stroke icon set (24px grid, 2px stroke, round joins) so navigation
// and controls share one visual weight instead of mixed emoji glyphs.
const PATHS = {
  overview: [
    <rect key="a" x="3" y="3" width="7" height="9" rx="1.5" />,
    <rect key="b" x="14" y="3" width="7" height="5" rx="1.5" />,
    <rect key="c" x="14" y="12" width="7" height="9" rx="1.5" />,
    <rect key="d" x="3" y="16" width="7" height="5" rx="1.5" />,
  ],
  usage: [<path key="a" d="M3 3v18h18" />, <path key="b" d="M18 17V9M13 17V5M8 17v-3" />],
  models: [
    <rect key="a" x="4" y="4" width="16" height="16" rx="2" />,
    <rect key="b" x="9" y="9" width="6" height="6" />,
    <path key="c" d="M15 2v2M15 20v2M2 15h2M2 9h2M20 15h2M20 9h2M9 2v2M9 20v2" />,
  ],
  activity: [<path key="a" d="M22 12h-4l-3 9L9 3l-3 9H2" />],
  target: [
    <circle key="a" cx="12" cy="12" r="10" />,
    <circle key="b" cx="12" cy="12" r="6" />,
    <circle key="c" cx="12" cy="12" r="2" />,
  ],
  flask: [
    <path key="a" d="M9 3h6" />,
    <path key="b" d="M10 3v6.5L4.6 18.9A1.5 1.5 0 0 0 5.9 21h12.2a1.5 1.5 0 0 0 1.3-2.1L14 9.5V3" />,
    <path key="c" d="M7.5 15h9" />,
  ],
  sliders: [
    <path key="a" d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M2 14h4M10 8h4M18 16h4" />,
  ],
  userCheck: [
    <circle key="a" cx="9" cy="7" r="4" />,
    <path key="b" d="M3 21v-2a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v2" />,
    <path key="c" d="M16 11l2 2 4-4" />,
  ],
  users: [
    <path key="a" d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />,
    <circle key="b" cx="9" cy="7" r="4" />,
    <path key="c" d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" />,
  ],
  user: [
    <circle key="a" cx="12" cy="8" r="4" />,
    <path key="b" d="M20 21v-1a5 5 0 0 0-5-5H9a5 5 0 0 0-5 5v1" />,
  ],
  plug: [
    <path key="a" d="M12 22v-5M9 8V2M15 8V2" />,
    <path key="b" d="M18 8v4a5 5 0 0 1-5 5h-2a5 5 0 0 1-5-5V8Z" />,
  ],
  package: [
    <path key="a" d="M16.5 9.4 7.55 4.24" />,
    <path key="b" d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />,
    <path key="c" d="M3.27 6.96 12 12.01l8.73-5.05M12 22.08V12" />,
  ],
  config: [
    <path key="a" d="M20 7h-9M14 17H5" />,
    <circle key="b" cx="17" cy="17" r="3" />,
    <circle key="c" cx="7" cy="7" r="3" />,
  ],
  logout: [
    <path key="a" d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />,
    <path key="b" d="M16 17l5-5-5-5M21 12H9" />,
  ],
  login: [
    <path key="a" d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4" />,
    <path key="b" d="M10 17l5-5-5-5M15 12H3" />,
  ],
  plus: [<path key="a" d="M12 5v14M5 12h14" />],
  x: [<path key="a" d="M18 6 6 18M6 6l12 12" />],
  search: [<circle key="a" cx="11" cy="11" r="7" />, <path key="b" d="m20 20-3.5-3.5" />],
  copy: [
    <rect key="a" x="9" y="9" width="13" height="13" rx="2" />,
    <path key="b" d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />,
  ],
  check: [<path key="a" d="M20 6 9 17l-5-5" />],
  refresh: [
    <path key="a" d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />,
    <path key="b" d="M3 3v5h5" />,
    <path key="c" d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16" />,
    <path key="d" d="M16 16h5v5" />,
  ],
  chevronRight: [<path key="a" d="m9 18 6-6-6-6" />],
  chevronLeft: [<path key="a" d="m15 18-6-6 6-6" />],
  chevronDown: [<path key="a" d="m6 9 6 6 6-6" />],
  external: [
    <path key="a" d="M15 3h6v6M10 14 21 3" />,
    <path key="b" d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />,
  ],
  alert: [
    <circle key="a" cx="12" cy="12" r="10" />,
    <path key="b" d="M12 8v4M12 16h.01" />,
  ],
  checkCircle: [
    <path key="a" d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />,
    <path key="b" d="M22 4 12 14.01l-3-3" />,
  ],
  info: [
    <circle key="a" cx="12" cy="12" r="10" />,
    <path key="b" d="M12 16v-4M12 8h.01" />,
  ],
  menu: [<path key="a" d="M4 6h16M4 12h16M4 18h16" />],
  lock: [
    <rect key="a" x="4" y="11" width="16" height="10" rx="2" />,
    <path key="b" d="M8 11V7a4 4 0 0 1 8 0v4" />,
  ],
  pencil: [
    <path key="a" d="M12 20h9" />,
    <path key="b" d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" />,
  ],
  trash: [
    <path key="a" d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />,
    <path key="b" d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />,
  ],
  calendar: [
    <rect key="a" x="3" y="4" width="18" height="18" rx="2" />,
    <path key="b" d="M16 2v4M8 2v4M3 10h18" />,
  ],
  clock: [<circle key="a" cx="12" cy="12" r="10" />, <path key="b" d="M12 6v6l4 2" />],
  power: [<path key="a" d="M18.36 6.64a9 9 0 1 1-12.73 0" />, <path key="b" d="M12 2v10" />],
  shield: [<path key="a" d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />],
  key: [
    <circle key="a" cx="7.5" cy="15.5" r="5.5" />,
    <path key="b" d="m21 2-9.6 9.6M15.5 7.5l3 3L22 7l-3-3" />,
  ],
  terminal: [<path key="a" d="m4 17 6-6-6-6M12 19h8" />],
  message: [<path key="a" d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />],
  wrench: [
    <path key="a" d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />,
  ],
  filter: [<path key="a" d="M22 3H2l8 9.46V19l4 2v-8.54L22 3z" />],
  eye: [
    <path key="a" d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z" />,
    <circle key="b" cx="12" cy="12" r="3" />,
  ],
  sun: [
    <circle key="a" cx="12" cy="12" r="4" />,
    <path key="b" d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />,
  ],
  moon: [<path key="a" d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z" />],
  zap: [<path key="a" d="M13 2 3 14h9l-1 8 10-12h-9l1-8z" />],
  layers: [
    <path key="a" d="m12 2 10 5-10 5L2 7l10-5z" />,
    <path key="b" d="m2 17 10 5 10-5M2 12l10 5 10-5" />,
  ],
};

const Icon = ({ name, size = 16, className = "", title, strokeWidth = 2 }) => {
  const children = PATHS[name];
  if (!children) return null;
  return (
    <svg
      className={`ui-icon ${className}`.trim()}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden={title ? undefined : "true"}
      role={title ? "img" : undefined}
      focusable="false"
    >
      {title ? <title>{title}</title> : null}
      {children}
    </svg>
  );
};

export default Icon;
