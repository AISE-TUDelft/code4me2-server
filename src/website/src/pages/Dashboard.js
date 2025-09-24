import React, { useState } from "react";
import ThemeToggle from "../components/common/ThemeToggle";
import VerificationBanner from "../components/common/VerificationBanner";
import AnalyticsNavigation from "../components/analytics/AnalyticsNavigation";
import OverviewDashboard from "../components/analytics/OverviewDashboard";
import UsageAnalytics from "../components/analytics/UsageAnalytics";
import ModelAnalytics from "../components/analytics/ModelAnalytics";
import StudyManagement from "../components/analytics/StudyManagement";
import "./Dashboard.css";

const Dashboard = ({ user, onLogout }) => {
  const [activeView, setActiveView] = useState('overview');
  const [timeWindow, setTimeWindow] = useState("7d");

  const renderActiveView = () => {
    switch (activeView) {
      case 'overview':
        return <OverviewDashboard 
          timeWindow={timeWindow} 
          onTimeWindowChange={setTimeWindow} 
        />;
      case 'usage':
        return <UsageAnalytics />;
      case 'models':
        return <ModelAnalytics />;
      case 'calibration':
        return (
          <div className="placeholder-view">
            <div className="placeholder-content">
              <h2>Model Calibration Analytics</h2>
              <p>Confidence calibration and reliability analysis coming soon.</p>
              <p>This section will include:</p>
              <ul>
                <li>Reliability diagrams</li>
                <li>Expected Calibration Error (ECE)</li>
                <li>Brier score analysis</li>
                <li>Confidence vs. accuracy correlations</li>
              </ul>
            </div>
          </div>
        );
      case 'studies':
        return <StudyManagement user={user} />;
      default:
        return <OverviewDashboard 
          timeWindow={timeWindow} 
          onTimeWindowChange={setTimeWindow} 
        />;
    }
  };

  return (
    <div className="dashboard-container">
      <header className="dashboard-header">
        <div className="header-content">
          <div className="header-left">
            <h1>Code4Me Analytics</h1>
            <span className="header-subtitle">
              {user?.is_admin ? 'Administrator Dashboard' : 'Personal Analytics'}
            </span>
          </div>
          
          <div className="header-right">
            <ThemeToggle />
            <div className="user-info">
              <span className="user-name">
                {user
                  ? `${user.name || user.email || "User"}`
                  : "Loading user..."}
              </span>
              {user?.is_admin && (
                <span className="admin-badge">Admin</span>
              )}
            </div>
            <button onClick={onLogout} className="logout-button">
              Logout
            </button>
          </div>
        </div>
      </header>

      <VerificationBanner user={user} />

      <div className="dashboard-content">
        <AnalyticsNavigation 
          activeView={activeView}
          onViewChange={setActiveView}
          user={user}
        />
        
        <main className="analytics-main">
          {renderActiveView()}
        </main>
      </div>
    </div>
  );
};

export default Dashboard;