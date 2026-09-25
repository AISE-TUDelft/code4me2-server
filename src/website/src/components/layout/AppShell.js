import React, { useEffect, useMemo, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import Icon from "../common/Icon";
import { useTheme } from "../../context/ThemeContext";
import VerificationBanner from "../common/VerificationBanner";
import "./AppShell.css";

export const isResearcher = (user) => Boolean(user?.is_admin || user?.can_research);

// One navigation model for every signed-in page. Dashboard views keep their
// `?view=` deep links; research surfaces are real routes.
export const buildNavigation = (user) => {
  const groups = [
    {
      id: "analytics",
      label: "Analytics",
      items: [
        { id: "overview", view: "overview", label: "Overview", icon: "overview" },
        { id: "usage", view: "usage", label: "Usage", icon: "usage" },
        { id: "models", view: "models", label: "Model performance", icon: "models" },
        { id: "agents", view: "agents", label: "Agent telemetry", icon: "activity" },
        { id: "calibration", view: "calibration", label: "Calibration", icon: "target" },
      ],
    },
  ];
  if (isResearcher(user)) {
    groups.push({
      id: "research",
      label: "Research",
      items: [
        { id: "studies", path: "/research/studies", label: "Studies", icon: "flask" },
        { id: "agent-profiles", view: "agent-profiles", label: "Agent profiles", icon: "sliders" },
      ],
    });
  }
  groups.push({
    id: "participation",
    label: "Participation",
    items: [{ id: "my-studies", path: "/research/my-studies", label: "My studies", icon: "userCheck" }],
  });
  if (user?.is_admin) {
    groups.push({
      id: "admin",
      label: "Administration",
      items: [
        { id: "admin-researchers", view: "admin-researchers", label: "Accounts", icon: "users" },
        { id: "admin-connections", view: "admin-connections", label: "Provider connections", icon: "plug" },
        { id: "admin-agents", view: "admin-agents", label: "Agent catalogue", icon: "package" },
        { id: "configs", view: "configs", label: "Config management", icon: "config" },
      ],
    });
  }
  return groups;
};

const itemHref = (item) => (item.view ? `/dashboard?view=${item.view}` : item.path);

const currentView = (location) => {
  const params = new URLSearchParams(location.search);
  const fromQuery = params.get("view");
  if (fromQuery) return fromQuery.toLowerCase();
  if (location.hash) return location.hash.slice(1).toLowerCase();
  return "overview";
};

const isItemActive = (item, location) => {
  if (item.view) {
    return location.pathname === "/dashboard" && currentView(location) === item.view;
  }
  if (item.path === "/research/my-studies") {
    return location.pathname === "/research/my-studies" || location.pathname === "/research/join";
  }
  return location.pathname === item.path || location.pathname.startsWith(`${item.path}/`);
};

const initials = (user) => {
  const source = (user?.name || user?.email || "?").trim();
  const parts = source.split(/[\s@._-]+/).filter(Boolean);
  const letters = parts.length > 1 ? parts[0][0] + parts[1][0] : source.slice(0, 2);
  return letters.toUpperCase();
};

const roleLabel = (user) => {
  if (user?.is_admin) return "Administrator";
  if (user?.can_research) return "Researcher";
  return "Participant";
};

const ThemeSwitch = () => {
  const { theme, toggleTheme } = useTheme();
  const next = theme === "light" ? "dark" : "light";
  return (
    <button
      type="button"
      className="icon-button"
      onClick={toggleTheme}
      aria-label={`Switch to ${next} mode`}
      title={`Switch to ${next} mode`}
    >
      <Icon name={theme === "light" ? "moon" : "sun"} size={17} />
    </button>
  );
};

const AppShell = ({ user, onLogout, children }) => {
  const location = useLocation();
  const [navOpen, setNavOpen] = useState(false);
  const groups = useMemo(() => buildNavigation(user), [user]);

  // Close the mobile navigation after every route change.
  useEffect(() => {
    setNavOpen(false);
  }, [location.pathname, location.search]);

  return (
    <div className={`shell${navOpen ? " is-nav-open" : ""}`}>
      <a className="shell-skip" href="#shell-main">
        Skip to content
      </a>
      <header className="shell-header">
        <button
          type="button"
          className="icon-button shell-menu-button"
          onClick={() => setNavOpen((open) => !open)}
          aria-label={navOpen ? "Close navigation" : "Open navigation"}
          aria-expanded={navOpen}
          aria-controls="shell-sidebar"
        >
          <Icon name={navOpen ? "x" : "menu"} size={18} />
        </button>
        <Link to="/dashboard" className="shell-brand" aria-label="Code4Me home">
          <span className="shell-logo" aria-hidden="true">
            C4
          </span>
          <span className="shell-brand-text">
            <span className="shell-brand-name">Code4Me</span>
            <span className="shell-brand-sub">Research platform</span>
          </span>
        </Link>
        <div className="shell-header-actions">
          <ThemeSwitch />
          <div className="shell-user" title={user?.email || ""}>
            <span className="shell-avatar" aria-hidden="true">
              {initials(user)}
            </span>
            <span className="shell-user-text">
              <span className="shell-user-name user-name">{user ? user.name || user.email : "Loading user…"}</span>
              <span className="shell-user-role">{roleLabel(user)}</span>
            </span>
          </div>
          <button type="button" className="ghost-button shell-logout" onClick={onLogout}>
            <Icon name="logout" size={16} />
            <span>Log out</span>
          </button>
        </div>
      </header>

      <div className="shell-body">
        <nav id="shell-sidebar" className="shell-sidebar" aria-label="Main navigation">
          <div className="shell-sidebar-inner">
          {groups.map((group) => (
            <div className="shell-nav-group" key={group.id}>
              <p className="shell-nav-heading">{group.label}</p>
              <ul>
                {group.items.map((item) => {
                  const active = isItemActive(item, location);
                  return (
                    <li key={item.id}>
                      <Link
                        to={itemHref(item)}
                        className={`shell-nav-link${active ? " is-active" : ""}`}
                        aria-current={active ? "page" : undefined}
                        data-nav-id={item.id}
                      >
                        <Icon name={item.icon} size={17} />
                        <span>{item.label}</span>
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
          </div>
        </nav>
        {/* Mouse/touch affordance only; the header toggle is the accessible control. */}
        <div className="shell-scrim" aria-hidden="true" onClick={() => setNavOpen(false)} />

        <main id="shell-main" className="shell-main">
          <VerificationBanner user={user} />
          <div className="shell-content">{children}</div>
        </main>
      </div>
    </div>
  );
};

export default AppShell;
