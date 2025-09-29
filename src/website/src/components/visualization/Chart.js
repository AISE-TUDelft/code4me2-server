import React, { useEffect, useRef, useState } from "react";
import "./Chart.css";

/**
 * A simple, dependency-free bar/line chart component with dynamic x-axis.
 * It supports time-series (timestamps) and categorical x values.
 *
 * Props:
 * - data: Array of data points
 * - title: Chart title
 * - color: Bar/line color (optional)
 * - xKey: field name for x values (default: "timestamp")
 * - valueKey: field name for y values (default: "value")
 * - xType: "time" | "category" | undefined (auto-detect if undefined)
 * - xLabelFormatter: optional function for formatting x labels/tooltips
 */
const Chart = ({
  data,
  title,
  color = "#4285f4",
  xKey = "timestamp",
  valueKey = "value",
  xType,
  xLabelFormatter,
}) => {
  // Responsive x-axis state
  const containerRef = useRef(null);
  const [containerWidth, setContainerWidth] = useState(0);
  const [labelStep, setLabelStep] = useState(1);
  const [maxCharsPerLabel, setMaxCharsPerLabel] = useState(12);


  // Determine x-axis type if not provided
  const sampleX = data[0]?.[xKey];
  const inferredTime = (() => {
    if (xType) return xType === "time";
    if (sampleX == null) return false;
    if (typeof sampleX === "number" && !Number.isNaN(sampleX)) return true; // epoch ms/seconds
    if (typeof sampleX === "string") {
      const t = Date.parse(sampleX);
      return !Number.isNaN(t);
    }
    return false;
  })();

  const safeData = Array.isArray(data) ? data : [];
  const values = safeData.map((point) => Number(point?.[valueKey] ?? 0));
  const maxValue = values.length ? Math.max(...values) : 0;
  const minValue = values.length ? Math.min(...values) : 0;
  const range = maxValue - minValue || 1; // Avoid division by zero

  const formatXForTooltip = (xVal) => {
    if (xLabelFormatter) return xLabelFormatter(xVal);
    if (inferredTime) {
      const d = typeof xVal === "number" ? new Date(xVal) : new Date(String(xVal));
      return d.toLocaleString();
    }
    return String(xVal);
  };

  const formatXForAxis = (xVal) => {
    if (xLabelFormatter) return xLabelFormatter(xVal);
    if (inferredTime) {
      const d = typeof xVal === "number" ? new Date(xVal) : new Date(String(xVal));
      return d.toLocaleDateString();
    }
    return String(xVal);
  };

  // Responsive label management
  const truncate = (s, max) => {
    const str = String(s ?? "");
    if (str.length <= max) return str;
    if (max <= 1) return str.slice(0, 1);
    return str.slice(0, Math.max(1, max - 1)) + "…";
  };

  useEffect(() => {
    const updateWidth = () => {
      if (!containerRef.current) return;
      const width = containerRef.current.clientWidth || 0;
      if (width) setContainerWidth(width);
    };

    const ro = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect?.width || 0;
      setContainerWidth(width);
    });

    // Wait for ref to be available
    if (containerRef.current) {
      ro.observe(containerRef.current);
      updateWidth();
    }

    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    // Derive dynamic label step and truncation length
    if (!data || data.length === 0) {
      setLabelStep(1);
      setMaxCharsPerLabel(12);
      return;
    }
    const width = containerWidth || 0;
    if (!width) {
      setLabelStep(Math.ceil(data.length / 5));
      setMaxCharsPerLabel(12);
      return;
    }
    const samples = data.slice(0, Math.min(10, data.length)).map((p) => {
      return String(formatXForAxis(p?.[xKey]));
    });
    const avgLen = samples.length ? samples.reduce((a, s) => a + s.length, 0) / samples.length : 8;
    const charWidth = 7; // approx px per char
    const padding = 12;
    const minLabelWidth = Math.max(50, Math.min(140, Math.round(avgLen * charWidth + padding)));
    const maxLabels = Math.max(2, Math.floor((width - 16) / minLabelWidth));
    const step = Math.max(1, Math.ceil(data.length / maxLabels));
    setLabelStep(step);

    const labelsShown = Math.ceil(data.length / step);
    const perLabelPx = (width - 16) / Math.max(1, labelsShown);
    const maxChars = Math.max(4, Math.floor(perLabelPx / charWidth) - 1);
    setMaxCharsPerLabel(Math.min(24, maxChars));
  }, [containerWidth, data, xKey, xLabelFormatter, inferredTime]);

  // Show placeholder while loading or when no data
  if (!data || data.length === 0) {
    return (
      <div className="chart-container">
        <h3>{title}</h3>
        <div className="chart-loading">Loading data...</div>
      </div>
    );
  }

  return (
    <div className="chart-container">
      <h3>{title}</h3>
      <div className="chart" ref={containerRef}>
        <div className="chart-grid">
          {/* Y-axis labels */}
          <div className="chart-y-labels">
            <span>{maxValue}</span>
            <span>{Math.round((maxValue + minValue) / 2)}</span>
            <span>{minValue}</span>
          </div>

          {/* Chart area */}
          <div className="chart-area">
            {data.map((point, index) => {
              const yVal = Number(point?.[valueKey] ?? 0);
              // Calculate height percentage based on value
              const heightPercent = ((yVal - minValue) / range) * 100;
              const xVal = point?.[xKey];

              return (
                <div
                  key={index}
                  className="chart-bar"
                  style={{
                    height: `${heightPercent}%`,
                    backgroundColor: color,
                  }}
                  title={`${formatXForTooltip(xVal)}: ${yVal}`}
                />
              );
            })}
          </div>
        </div>

        {/* X-axis labels (responsive sampling for clarity) */}
        <div className="chart-x-labels">
          {data
            .filter((_, i) => i % labelStep === 0)
            .map((point, index) => {
              const full = formatXForAxis(point?.[xKey]);
              return (
                <span
                  key={index}
                  title={full}
                  style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}
                >
                  {truncate(full, maxCharsPerLabel)}
                </span>
              );
            })}
        </div>
      </div>
    </div>
  );
};

export default Chart;
