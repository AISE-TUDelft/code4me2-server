import React, { useState, useEffect } from "react";
import Chart from "../components/visualization/Chart";
import ThemeToggle from "../components/common/ThemeToggle";
import { getVisualizationData } from "../utils/api";
import "./Dashboard.css";

const Dashboard = ({ user, onLogout }) => {
  const [visualizationData, setVisualizationData] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState(null);
  console.log(user);

  useEffect(() => {
    const fetchData = async () => {
      try {
        setIsLoading(true);
        const response = await getVisualizationData();

        if (response.ok) {
          setVisualizationData(response.data);
        } else {
          setError(response.error || "Failed to load visualization data");
        }
      } catch (err) {
        console.error("Error fetching visualization data:", err);
        setError("An unexpected error occurred. Please try again.");
      } finally {
        setIsLoading(false);
      }
    };

    fetchData();
  }, []);

  return (
    <div className="dashboard-container">
      <header className="dashboard-header">
        <h1>Visualization Dashboard</h1>
        <div className="user-info">
          <ThemeToggle />
          <span>
            {user
              ? `Welcome, ${user.name || user.email || "User"}`
              : "Loading user..."}
          </span>
          <button onClick={onLogout} className="logout-button">
            Logout
          </button>
        </div>
      </header>

      <div className="dashboard-content">
        <div className="visualization-section">
          <h2>Grafana-like Visualizations</h2>
          <p>Interactive data visualizations and metrics dashboards.</p>

          {error && (
            <div className="error-message">
              {error}
              <button
                onClick={() => window.location.reload()}
                className="reload-button"
              >
                Reload
              </button>
            </div>
          )}

          <div className="visualization-grid">
            {isLoading && !visualizationData ? (
              // Show loading skeleton when data is being fetched
              Array.from({ length: 4 }).map((_, index) => (
                <div key={index} className="visualization-card loading">
                  <div className="loading-chart"></div>
                  <div className="loading-title"></div>
                </div>
              ))
            ) : (
              // Show actual charts when data is available
              <>
                <div className="visualization-card">
                  <Chart
                    data={visualizationData?.systemMetrics || []}
                    title="System Metrics"
                    color="#4285f4"
                  />
                </div>
                <div className="visualization-card">
                  <Chart
                    data={visualizationData?.performanceAnalytics || []}
                    title="Performance Analytics"
                    color="#34a853"
                  />
                </div>
                <div className="visualization-card">
                  <Chart
                    data={visualizationData?.userActivity || []}
                    title="User Activity"
                    color="#fbbc05"
                  />
                </div>
                <div className="visualization-card">
                  <Chart
                    data={visualizationData?.resourceUtilization || []}
                    title="Resource Utilization"
                    color="#ea4335"
                  />
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

export default Dashboard;
