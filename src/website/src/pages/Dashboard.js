import React, { useState, useEffect } from "react";
import ThemeToggle from "../components/common/ThemeToggle";
import VerificationBanner from "../components/common/VerificationBanner";
import AnalyticsNavigation from "../components/analytics/AnalyticsNavigation";
import OverviewDashboard from "../components/analytics/OverviewDashboard";
import UsageAnalytics from "../components/analytics/UsageAnalytics";
import ModelAnalytics from "../components/analytics/ModelAnalytics";
import AgentAnalytics from "../components/analytics/AgentAnalytics";
import CalibrationAnalytics from "../components/analytics/CalibrationAnalytics";
import StudyManagement from "../components/analytics/StudyManagement";
import ConfigManagement from "../components/analytics/ConfigManagement";
import AgentProfiles from "./AgentProfiles";
import AgentAssignments from "./AgentAssignments";
import "./Dashboard.css";

const BASE_VIEWS = new Set(["overview", "usage", "models", "agents", "calibration"]);

const isValidView = (view, user) => {
  if (BASE_VIEWS.has(view)) return true;
  if (view === "studies") return !!user?.is_admin;
  if (view === "configs") return !!user?.is_admin;
  if (view === "agent-profiles") return !!user?.is_admin;
  if (view === "agent-assignments") return !!user?.is_admin;
  return false;
};

const Dashboard = ({ user, onLogout }) => {
  const [activeView, setActiveView] = useState('overview');
  const [timeWindow, setTimeWindow] = useState("7d");
  const [isUrlInitialized, setIsUrlInitialized] = useState(false);

  // Initialize activeView/timeWindow from URL (query ?view=... or hash #studies)
  useEffect(() => {
    try {
      const url = new URL(window.location.href);
      let viewParam = url.searchParams.get('view') || (window.location.hash ? window.location.hash.slice(1) : null);
      if (viewParam) {
        viewParam = viewParam.toLowerCase();
        if (isValidView(viewParam, user)) {
          setActiveView(viewParam);
        }
      }
      const tw = url.searchParams.get('timeWindow') || url.searchParams.get('time_window');
      if (tw && ['7d','30d','90d'].includes(tw)) {
        setTimeWindow(tw);
      }
      setIsUrlInitialized(true);
    } catch (_) {
      // ignore URL parse errors
      setIsUrlInitialized(true);
    }
  }, [user]);

  // Keep URL in sync with current view and time window so users can deeplink
  useEffect(() => {
    if (!isUrlInitialized) return;
    try {
      const url = new URL(window.location.href);
      if (isValidView(activeView, user)) {
        url.searchParams.set('view', activeView);
      } else {
        url.searchParams.delete('view');
      }
      if (timeWindow) {
        url.searchParams.set('timeWindow', timeWindow);
      }
      // Preserve hash only if not being used for the view shortcut
      if (window.location.hash && window.location.hash.slice(1) !== activeView) {
        // keep existing hash
      } else {
        // set hash to current view for quick copy
        url.hash = '#' + activeView;
      }
      window.history.replaceState({}, '', url.toString());
    } catch (_) {
      // ignore URL update errors
    }
  }, [activeView, timeWindow, user, isUrlInitialized]);

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
        return <ModelAnalytics timeWindow={timeWindow} />;
      case 'agents':
        return <AgentAnalytics timeWindow={timeWindow} />;
      case 'calibration':
        return <CalibrationAnalytics />;
      case 'studies':
        return <StudyManagement user={user} />;
      case 'configs':
        return <ConfigManagement user={user} />;
      case 'agent-profiles':
        return <AgentProfiles />;
      case 'agent-assignments':
        return <AgentAssignments />;
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