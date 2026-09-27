import React, { useCallback, useEffect } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import OverviewDashboard from "../components/analytics/OverviewDashboard";
import UsageAnalytics from "../components/analytics/UsageAnalytics";
import ModelAnalytics from "../components/analytics/ModelAnalytics";
import AgentAnalytics from "../components/analytics/AgentAnalytics";
import CalibrationAnalytics from "../components/analytics/CalibrationAnalytics";
import ConfigManagement from "../components/analytics/ConfigManagement";
import AgentProfiles from "./AgentProfiles";
import AdminResearchers from "./AdminResearchers";
import AdminConnections from "./AdminConnections";
import AdminAgents from "./AdminAgents";
import "./Dashboard.css";

const BASE_VIEWS = new Set(["overview", "usage", "models", "agents", "calibration"]);
const ADMIN_VIEWS = new Set(["configs", "admin-researchers", "admin-connections", "admin-agents"]);
const TIME_WINDOWS = ["7d", "30d", "90d"];

export const isValidView = (view, user) => {
  if (BASE_VIEWS.has(view)) return true;
  if (ADMIN_VIEWS.has(view)) return !!user?.is_admin;
  if (view === "agent-profiles") return !!(user?.is_admin || user?.can_research);
  return false;
};

/**
 * Renders one analytics/admin view inside the application shell. The view and
 * time window live in the URL (`?view=…&timeWindow=…`), so the sidebar,
 * browser history and copied links all agree. A legacy `#view` hash is still
 * honoured once and rewritten to the query form.
 */
const Dashboard = ({ user }) => {
  const [searchParams, setSearchParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();

  const requestedView = (searchParams.get("view") || location.hash.slice(1) || "overview").toLowerCase();
  const activeView = isValidView(requestedView, user) ? requestedView : "overview";
  const rawWindow = searchParams.get("timeWindow") || searchParams.get("time_window");
  const timeWindow = TIME_WINDOWS.includes(rawWindow) ? rawWindow : "7d";

  // Normalise the URL (legacy hash links, unknown or forbidden views).
  useEffect(() => {
    const needsRewrite =
      location.hash || searchParams.get("view") !== activeView || searchParams.get("timeWindow") !== timeWindow;
    if (!needsRewrite) return;
    const next = new URLSearchParams(searchParams);
    next.set("view", activeView);
    next.set("timeWindow", timeWindow);
    next.delete("time_window");
    navigate({ pathname: "/dashboard", search: `?${next.toString()}`, hash: "" }, { replace: true });
  }, [activeView, timeWindow, location.hash, searchParams, navigate]);

  const setTimeWindow = useCallback(
    (value) => {
      const next = new URLSearchParams(searchParams);
      next.set("timeWindow", value);
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  switch (activeView) {
    case "usage":
      return <UsageAnalytics />;
    case "models":
      return <ModelAnalytics timeWindow={timeWindow} />;
    case "agents":
      return <AgentAnalytics timeWindow={timeWindow} />;
    case "calibration":
      return <CalibrationAnalytics />;
    case "configs":
      return <ConfigManagement user={user} />;
    case "agent-profiles":
      return <AgentProfiles user={user} />;
    case "admin-researchers":
      return <AdminResearchers />;
    case "admin-connections":
      return <AdminConnections />;
    case "admin-agents":
      return <AdminAgents />;
    case "overview":
    default:
      return <OverviewDashboard timeWindow={timeWindow} onTimeWindowChange={setTimeWindow} />;
  }
};

export default Dashboard;
