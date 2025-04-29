import React from "react";
import "./Chart.css";

/**
 * A simple chart component that displays data in a line chart format
 * In a real application, you would use a proper charting library like Chart.js or D3.js
 *
 * @param {Object} props - Component props
 * @param {Array} props.data - Array of data points with timestamp and value
 * @param {string} props.title - Chart title
 * @param {string} props.color - Chart color (optional)
 */
const Chart = ({ data, title, color = "#4285f4" }) => {
  if (!data || data.length === 0) {
    return (
      <div className="chart-container">
        <h3>{title}</h3>
        <div className="chart-loading">Loading data...</div>
      </div>
    );
  }

  const values = data.map((point) => point.value);
  const maxValue = Math.max(...values);
  const minValue = Math.min(...values);
  const range = maxValue - minValue || 1; // Avoid division by zero

  const formatTimestamp = (timestamp) => {
    const date = new Date(timestamp);
    return date.toLocaleString();
  };

  return (
    <div className="chart-container">
      <h3>{title}</h3>
      <div className="chart">
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
              // Calculate height percentage based on value
              const heightPercent = ((point.value - minValue) / range) * 100;

              return (
                <div
                  key={index}
                  className="chart-bar"
                  style={{
                    height: `${heightPercent}%`,
                    backgroundColor: color,
                  }}
                  title={`${formatTimestamp(point.timestamp)}: ${point.value}`}
                />
              );
            })}
          </div>
        </div>

        {/* X-axis labels (show only a few for clarity) */}
        <div className="chart-x-labels">
          {data
            .filter((_, i) => i % Math.ceil(data.length / 5) === 0)
            .map((point, index) => (
              <span key={index}>
                {new Date(point.timestamp).toLocaleDateString()}
              </span>
            ))}
        </div>
      </div>
    </div>
  );
};

export default Chart;
