import React, { useEffect } from "react";
import { Navigate, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import ConfigManagement from "../components/analytics/ConfigManagement";
import AgentProfiles from "./AgentProfiles";
import AdminResearchers from "./AdminResearchers";
import AdminConnections from "./AdminConnections";
import AdminAgents from "./AdminAgents";
import { homePath } from "../components/layout/AppShell";
import "./Dashboard.css";

// The first-generation analytics views (overview, usage, models, agents,
// calibration) are archived: study analytics and the participant's own figures
// on My studies supersede them. Their components stay in components/analytics/
// but are not routed, so old links to them land on the user's home page.
const ADMIN_VIEWS = new Set(["configs", "admin-researchers", "admin-connections", "admin-agents"]);

export const isValidView = (view, user) => {
  if (ADMIN_VIEWS.has(view)) return !!user?.is_admin;
  if (view === "agent-profiles") return !!(user?.is_admin || user?.can_research);
  return false;
};

/**
 * Renders one admin/configuration view inside the application shell. The view
 * lives in the URL (`?view=…`), so the sidebar, browser history and copied
 * links all agree. A legacy `#view` hash is still honoured once and rewritten
 * to the query form; an unknown, forbidden or archived view redirects home.
 */
const Dashboard = ({ user }) => {
  const [searchParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();

  const requestedView = (searchParams.get("view") || location.hash.slice(1)).toLowerCase();
  const isAllowed = isValidView(requestedView, user);

  // Normalise the URL (legacy hash links, mixed-case views).
  useEffect(() => {
    if (!isAllowed || (!location.hash && searchParams.get("view") === requestedView)) return;
    navigate({ pathname: "/dashboard", search: `?view=${requestedView}`, hash: "" }, { replace: true });
  }, [isAllowed, requestedView, location.hash, searchParams, navigate]);

  if (!isAllowed) return <Navigate to={homePath(user)} replace />;

  switch (requestedView) {
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
    default:
      return null;
  }
};

export default Dashboard;
