import React, { useMemo, useRef, useState } from "react";
import { formatNumber } from "../../utils/format";

/*
 * Dependency-free chart components for the research dashboards.
 *
 * Conventions (see the data-viz method used across the site):
 *  - identity colours come from the validated --viz-N tokens, assigned by a
 *    fixed entity order (an arm keeps its colour when filters change);
 *  - thin marks, 4px rounded data ends, hairline grid, one axis;
 *  - every chart has a hover/focus tooltip AND a table view, so no value is
 *    reachable only by hovering;
 *  - text never wears the series colour (swatches carry identity).
 */

export const SERIES_COLORS = [
  "var(--viz-1)",
  "var(--viz-2)",
  "var(--viz-3)",
  "var(--viz-4)",
  "var(--viz-5)",
  "var(--viz-6)",
  "var(--viz-7)",
  "var(--viz-8)",
];

export const colorForIndex = (index) =>
  index >= 0 && index < SERIES_COLORS.length ? SERIES_COLORS[index] : "var(--viz-muted)";

const niceMax = (value) => {
  if (!value || value <= 0) return 1;
  const exponent = Math.floor(Math.log10(value));
  const base = 10 ** exponent;
  const fraction = value / base;
  const nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 2.5 ? 2.5 : fraction <= 5 ? 5 : 10;
  return nice * base;
};

const defaultFormat = (value) => formatNumber(value, { maximumFractionDigits: 2 });

/** Chart frame: title, optional legend, table-view toggle and the plot. */
export const ChartCard = ({ title, subtitle, legend, table, children, actions, className = "" }) => {
  const [showTable, setShowTable] = useState(false);
  return (
    <figure className={`ui-card viz-card ${className}`.trim()} style={{ margin: 0 }}>
      <div className="ui-card-body">
        <div className="viz-card-header">
          <div>
            <figcaption className="viz-title">{title}</figcaption>
            {subtitle ? <p className="viz-subtitle">{subtitle}</p> : null}
          </div>
          <div className="ui-row">
            {actions}
            {table ? (
              <button
                type="button"
                className="viz-table-toggle"
                onClick={() => setShowTable((value) => !value)}
                aria-pressed={showTable}
              >
                {showTable ? "Show chart" : "Show table"}
              </button>
            ) : null}
          </div>
        </div>
        {legend && !showTable ? legend : null}
        {showTable && table ? table : children}
      </div>
    </figure>
  );
};

export const Legend = ({ items }) => (
  <ul className="viz-legend" aria-label="Legend">
    {items.map((item) => (
      <li key={item.key || item.label}>
        <span className="viz-swatch" style={{ backgroundColor: item.color }} aria-hidden="true" />
        {item.label}
      </li>
    ))}
  </ul>
);

export const DataTable = ({ columns, rows, caption }) => (
  <div className="ui-table-wrap">
    <table className="ui-table">
      {caption ? <caption className="ui-visually-hidden">{caption}</caption> : null}
      <thead>
        <tr>
          {columns.map((column) => (
            <th key={column.key} className={column.numeric ? "is-num" : undefined} scope="col">
              {column.label}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row, index) => (
          <tr key={row.key || index}>
            {columns.map((column) => (
              <td key={column.key} className={column.numeric ? "is-num" : undefined}>
                {column.render ? column.render(row) : row[column.key]}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  </div>
);

/**
 * Horizontal bars for one series over nominal categories (every bar the same
 * hue — the length already encodes the value). Values sit at the bar end.
 */
export const BarList = ({ items, color = "var(--viz-1)", format = defaultFormat, emptyText = "No data yet." }) => {
  const max = Math.max(0, ...items.map((item) => Number(item.value) || 0));
  if (!items.length) return <p className="viz-empty">{emptyText}</p>;
  return (
    <ul className="viz-bars">
      {items.map((item) => {
        const value = Number(item.value) || 0;
        const width = max > 0 ? Math.max((value / max) * 100, value > 0 ? 1.5 : 0) : 0;
        return (
          <li key={item.key || item.label} className="viz-bar-row" title={`${item.label}: ${format(value)}`}>
            <span className="viz-bar-label" title={item.label}>
              {item.label}
            </span>
            <span className="viz-bar-track">
              <span className="viz-bar-fill" style={{ width: `${width}%`, backgroundColor: item.color || color, display: "block" }} />
            </span>
            <span className="viz-bar-value">
              {format(value)}
              {item.detail ? <span className="ui-subtle"> {item.detail}</span> : null}
            </span>
          </li>
        );
      })}
    </ul>
  );
};

/**
 * One 100% stacked bar per row (e.g. per arm) across a fixed category order.
 * Categories keep their colour slot regardless of which rows are shown.
 */
export const ShareBars = ({ rows, categories, format = defaultFormat, emptyText = "No data yet." }) => {
  const [hover, setHover] = useState(null);
  if (!rows.length || !categories.length) return <p className="viz-empty">{emptyText}</p>;
  return (
    <div className="viz-frame">
      <ul className="viz-bars">
        {rows.map((row) => {
          const total = categories.reduce((sum, category) => sum + (Number(row.values[category.key]) || 0), 0);
          return (
            <li key={row.key} className="viz-bar-row">
              <span className="viz-bar-label" title={row.label}>
                {row.swatch ? (
                  <span className="viz-swatch is-dot" style={{ backgroundColor: row.swatch, marginRight: 6 }} aria-hidden="true" />
                ) : null}
                {row.label}
              </span>
              <span className="viz-bar-track" style={{ height: 16 }}>
                {total > 0 ? (
                  <span className="viz-bar-stack">
                    {categories.map((category) => {
                      const value = Number(row.values[category.key]) || 0;
                      if (value <= 0) return null;
                      const share = value / total;
                      const isHover = hover && hover.row === row.key && hover.category === category.key;
                      return (
                        <span
                          key={category.key}
                          tabIndex={0}
                          role="img"
                          aria-label={`${row.label}, ${category.label}: ${format(value)} (${Math.round(share * 100)}%)`}
                          onPointerEnter={() => setHover({ row: row.key, category: category.key })}
                          onPointerLeave={() => setHover(null)}
                          onFocus={() => setHover({ row: row.key, category: category.key })}
                          onBlur={() => setHover(null)}
                          style={{
                            width: `${share * 100}%`,
                            backgroundColor: category.color,
                            filter: isHover ? "brightness(1.12)" : undefined,
                            outline: "none",
                          }}
                          title={`${category.label}: ${format(value)} (${Math.round(share * 100)}%)`}
                        />
                      );
                    })}
                  </span>
                ) : (
                  <span className="ui-subtle" style={{ fontSize: 12 }}>
                    none
                  </span>
                )}
              </span>
              <span className="viz-bar-value">{format(total)}</span>
            </li>
          );
        })}
      </ul>
      {hover ? (
        <p className="ui-hint" style={{ marginTop: 8 }} aria-live="polite">
          {(() => {
            const row = rows.find((item) => item.key === hover.row);
            const category = categories.find((item) => item.key === hover.category);
            if (!row || !category) return null;
            const total = categories.reduce((sum, item) => sum + (Number(row.values[item.key]) || 0), 0);
            const value = Number(row.values[category.key]) || 0;
            return `${row.label} · ${category.label}: ${format(value)} of ${format(total)} (${total ? Math.round((value / total) * 100) : 0}%)`;
          })()}
        </p>
      ) : null}
    </div>
  );
};

/**
 * Daily columns, optionally stacked by series (arms). The hovered day shows a
 * tooltip listing every series for that day.
 */
export const DailyColumns = ({ days, series, height = 180, format = defaultFormat, emptyText = "No activity in this period." }) => {
  const frameRef = useRef(null);
  const [hoverIndex, setHoverIndex] = useState(null);
  const width = 720;
  const padding = { top: 12, right: 8, bottom: 26, left: 40 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const totals = days.map((day) => series.reduce((sum, item) => sum + (Number(day.values[item.key]) || 0), 0));
  const max = niceMax(Math.max(0, ...totals));
  if (!days.length) return <p className="viz-empty">{emptyText}</p>;
  const slot = plotWidth / days.length;
  const barWidth = Math.max(2, Math.min(24, slot * 0.64));
  const ticks = [0, max / 2, max];
  const labelEvery = Math.max(1, Math.ceil(days.length / 8));
  const y = (value) => padding.top + plotHeight - (value / max) * plotHeight;
  const hovered = hoverIndex !== null ? days[hoverIndex] : null;

  return (
    <div className="viz-frame" ref={frameRef}>
      <svg className="viz-svg" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Daily activity chart">
        {ticks.map((tick) => (
          <g key={tick}>
            <line className={tick === 0 ? "viz-baseline" : "viz-gridline"} x1={padding.left} x2={width - padding.right} y1={y(tick)} y2={y(tick)} />
            <text x={padding.left - 6} y={y(tick) + 4} textAnchor="end">
              {formatNumber(tick, { maximumFractionDigits: 1 })}
            </text>
          </g>
        ))}
        {days.map((day, index) => {
          const x = padding.left + slot * index + (slot - barWidth) / 2;
          let running = 0;
          const segments = series
            .map((item) => ({ item, value: Number(day.values[item.key]) || 0 }))
            .filter((segment) => segment.value > 0);
          return (
            <g key={day.key} className={`viz-col${hoverIndex === index ? " is-hover" : ""}`}>
              {segments.map((segment, segmentIndex) => {
                const top = y(running + segment.value);
                const bottom = y(running);
                running += segment.value;
                const isTop = segmentIndex === segments.length - 1;
                // 2px surface gap between stacked segments.
                const gap = segmentIndex > 0 ? 2 : 0;
                const segmentHeight = Math.max(0, bottom - top - gap);
                return isTop ? (
                  <path
                    key={segment.item.key}
                    d={roundedTopRect(x, top, barWidth, segmentHeight, Math.min(4, barWidth / 2, segmentHeight))}
                    fill={segment.item.color}
                  />
                ) : (
                  <rect key={segment.item.key} x={x} y={top} width={barWidth} height={segmentHeight} fill={segment.item.color} />
                );
              })}
              {index % labelEvery === 0 ? (
                <text x={padding.left + slot * index + slot / 2} y={height - 8} textAnchor="middle">
                  {day.label}
                </text>
              ) : null}
              <rect
                className="viz-hit"
                x={padding.left + slot * index}
                y={padding.top}
                width={slot}
                height={plotHeight}
                tabIndex={0}
                aria-label={`${day.fullLabel || day.label}: ${series
                  .map((item) => `${item.label} ${format(Number(day.values[item.key]) || 0)}`)
                  .join(", ")}`}
                onPointerEnter={() => setHoverIndex(index)}
                onPointerLeave={() => setHoverIndex(null)}
                onFocus={() => setHoverIndex(index)}
                onBlur={() => setHoverIndex(null)}
              />
            </g>
          );
        })}
      </svg>
      {hovered ? (
        <div
          className="viz-tooltip"
          style={{
            left: `${((padding.left + slot * hoverIndex + slot / 2) / width) * 100}%`,
            top: `${(y(totals[hoverIndex]) / height) * 100}%`,
          }}
        >
          <span>{hovered.fullLabel || hovered.label}</span>
          {series.map((item) => (
            <div className="viz-tooltip-row" key={item.key}>
              <span className="viz-tooltip-key" style={{ backgroundColor: item.color }} />
              <strong style={{ display: "inline" }}>{format(Number(hovered.values[item.key]) || 0)}</strong>
              <span>{item.label}</span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
};

const roundedTopRect = (x, y, width, height, radius) => {
  if (height <= 0) return "";
  const r = Math.max(0, Math.min(radius, height));
  return [
    `M${x},${y + height}`,
    `V${y + r}`,
    `Q${x},${y} ${x + r},${y}`,
    `H${x + width - r}`,
    `Q${x + width},${y} ${x + width},${y + r}`,
    `V${y + height}`,
    "Z",
  ].join(" ");
};

/**
 * Participant-level strip plot: one row per arm, one dot per participant, a
 * median tick, and the median printed at the right. All rows share one scale.
 */
export const ArmStripPlot = ({ arms, format = defaultFormat, emptyText = "Not enough data yet." }) => {
  const [hover, setHover] = useState(null);
  const allValues = arms.flatMap((arm) => (Array.isArray(arm.values) ? arm.values : [])).filter((value) => value !== null && value !== undefined && !Number.isNaN(Number(value)));
  const max = niceMax(Math.max(0, ...allValues.map(Number)));
  const hasData = allValues.length > 0;
  const toPercent = (value) => (max > 0 ? (Number(value) / max) * 100 : 0);
  if (!hasData) return <p className="viz-empty">{emptyText}</p>;
  return (
    <div className="ui-stack-sm">
      {arms.map((arm) => {
        const values = (Array.isArray(arm.values) ? arm.values : []).filter((value) => value !== null && value !== undefined);
        return (
          <div key={arm.key} className="viz-strip-row">
            <div className="viz-strip-label">
              <strong title={arm.label}>
                <span className="viz-swatch is-dot" style={{ backgroundColor: arm.color, marginRight: 6 }} aria-hidden="true" />
                {arm.label}
              </strong>
              <small>n = {arm.n ?? values.length}</small>
            </div>
            <div className="viz-strip-track" style={{ position: "relative", height: 26 }}>
              <span className="viz-strip-axis" aria-hidden="true" />
              {arm.p25 !== null && arm.p25 !== undefined && arm.p75 !== null && arm.p75 !== undefined ? (
                <span
                  className="viz-strip-iqr"
                  style={{ left: `${toPercent(arm.p25)}%`, width: `${Math.max(0.5, toPercent(arm.p75) - toPercent(arm.p25))}%` }}
                  aria-hidden="true"
                />
              ) : null}
              {values.map((value, index) => {
                const key = `${arm.key}-${index}`;
                return (
                  <span
                    key={key}
                    className={`viz-strip-dot${hover === key ? " is-hover" : ""}`}
                    style={{ left: `${toPercent(value)}%`, backgroundColor: arm.color }}
                    tabIndex={0}
                    role="img"
                    aria-label={`${arm.label}: participant value ${format(value)}`}
                    title={format(value)}
                    onPointerEnter={() => setHover(key)}
                    onPointerLeave={() => setHover(null)}
                    onFocus={() => setHover(key)}
                    onBlur={() => setHover(null)}
                  />
                );
              })}
              {arm.median !== null && arm.median !== undefined ? (
                <span className="viz-strip-median" style={{ left: `${toPercent(arm.median)}%` }} title={`Median ${format(arm.median)}`} aria-hidden="true" />
              ) : null}
            </div>
            <span className="viz-strip-value" title="Median">
              {arm.median === null || arm.median === undefined ? "—" : format(arm.median)}
            </span>
          </div>
        );
      })}
      <div className="viz-strip-row" aria-hidden="true">
        <span />
        <div className="viz-strip-scale">
          <span>0</span>
          <span>{format(max / 2)}</span>
          <span>{format(max)}</span>
        </div>
        <span className="ui-subtle" style={{ fontSize: 11.5, textAlign: "right" }}>
          median
        </span>
      </div>
    </div>
  );
};

/**
 * Bullet chart for "is the context cap respected": p95 prompt tokens as a
 * bar, the max as a thin line, the arm's cap as a marker.
 */
export const CapBullet = ({ rows, format = (value) => formatNumber(value, { maximumFractionDigits: 0 }) }) => {
  const max = niceMax(Math.max(0, ...rows.flatMap((row) => [row.cap, row.p95, row.max].map((value) => Number(value) || 0))));
  const pct = (value) => `${Math.min(100, ((Number(value) || 0) / max) * 100)}%`;
  return (
    <ul className="viz-bars">
      {rows.map((row) => (
        <li key={row.key} className="viz-bar-row" style={{ gridTemplateColumns: "minmax(110px, 26%) minmax(0, 1fr) 150px" }}>
          <span className="viz-bar-label" title={row.label}>
            <span className="viz-swatch is-dot" style={{ backgroundColor: row.color, marginRight: 6 }} aria-hidden="true" />
            {row.label}
          </span>
          <span className="viz-cap-track" role="img" aria-label={`${row.label}: p95 ${format(row.p95)} tokens, max ${format(row.max)}, cap ${format(row.cap)}`}>
            {row.p95 !== null && row.p95 !== undefined ? (
              <span className="viz-cap-fill" style={{ width: pct(row.p95), backgroundColor: row.color }} />
            ) : null}
            {row.max !== null && row.max !== undefined ? <span className="viz-cap-max" style={{ left: pct(row.max) }} /> : null}
            {row.cap ? <span className="viz-cap-marker" style={{ left: pct(row.cap) }} title={`Cap ${format(row.cap)}`} /> : null}
          </span>
          <span className="viz-bar-value">{row.note}</span>
        </li>
      ))}
    </ul>
  );
};

/** Sequential-ramp heatmap for a small matrix (e.g. tool-kind transitions). */
export const Heatmap = ({ rowsLabel, columnsLabel, keys, cells, format = defaultFormat }) => {
  const max = Math.max(0, ...cells.map((cell) => Number(cell.value) || 0));
  const lookup = useMemo(() => {
    const map = new Map();
    cells.forEach((cell) => map.set(`${cell.from}→${cell.to}`, cell));
    return map;
  }, [cells]);
  if (!keys.length || max <= 0) return <p className="viz-empty">No transitions recorded yet.</p>;
  return (
    <div className="ui-table-wrap">
      <table className="viz-heatmap" aria-label={`${rowsLabel} to ${columnsLabel}`}>
        <thead>
          <tr>
            <th scope="col" className="viz-heatmap-corner">
              {rowsLabel} → {columnsLabel}
            </th>
            {keys.map((key) => (
              <th key={key} scope="col">
                {key}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {keys.map((from) => (
            <tr key={from}>
              <th scope="row">{from}</th>
              {keys.map((to) => {
                const cell = lookup.get(`${from}→${to}`);
                const value = cell ? Number(cell.value) || 0 : 0;
                const intensity = max > 0 ? value / max : 0;
                return (
                  <td
                    key={to}
                    title={cell ? `${from} → ${to}: ${format(value)}${cell.detail ? ` · ${cell.detail}` : ""}` : `${from} → ${to}: 0`}
                    style={{
                      backgroundColor: value > 0 ? `color-mix(in srgb, var(--viz-1) ${Math.round(12 + intensity * 78)}%, var(--card-background))` : undefined,
                      color: intensity > 0.55 ? "#ffffff" : undefined,
                    }}
                  >
                    {value > 0 ? format(value) : ""}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};
