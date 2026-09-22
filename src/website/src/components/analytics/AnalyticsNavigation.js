import React from 'react';
import { Link } from 'react-router-dom';
import './AnalyticsNavigation.css';

const AnalyticsNavigation = ({ activeView, onViewChange, user }) => {
  const navigationItems = [
    {
      id: 'overview',
      label: 'Dashboard Overview', 
      icon: '📊',
      description: 'Key metrics and trends'
    },
    {
      id: 'usage',
      label: 'Usage Analytics',
      icon: '📈', 
      description: 'Query patterns and user behavior'
    },
    {
      id: 'models',
      label: 'Model Performance',
      icon: '🤖',
      description: 'AI model comparison and quality metrics'
    },
    {
      id: 'agents',
      label: 'Agent Telemetry',
      icon: '⚙️',
      description: 'Runs, tools, latency, failures and outcomes'
    },
    {
      id: 'calibration',
      label: 'Model Calibration',
      icon: '🎯',
      description: 'Confidence and reliability analysis'
    }
  ];

  // Administrator-only navigation items.
  if (user?.is_admin) {
    navigationItems.push({
      id: 'studies',
      label: 'Completion A/B (legacy)',
      icon: '🔬',
      description: 'Legacy completion study management',
      adminOnly: true
    });
    navigationItems.push({
      id: 'configs',
      label: 'Config Management',
      icon: '🧩',
      description: 'Create and manage server/config modules',
      adminOnly: true
    });
    navigationItems.push({
      id: 'admin-researchers',
      label: 'Accounts',
      icon: '👤',
      description: 'List accounts and grant researcher access',
      adminOnly: true
    });
    navigationItems.push({
      id: 'admin-connections',
      label: 'Provider Connections',
      icon: '🔌',
      description: 'Manage provider endpoints and secret names',
      adminOnly: true
    });
    navigationItems.push({
      id: 'admin-agents',
      label: 'Agent Catalogue',
      icon: '📦',
      description: 'Import tested releases and disable versions',
      adminOnly: true
    });
  }

  // Researcher surfaces: administrators and administrator-enabled researchers.
  if (user?.is_admin || user?.can_research) {
    navigationItems.push({
      id: 'agent-profiles',
      label: 'Agent Profiles',
      icon: '🛠️',
      description: 'Agent runtime, connection, tools and policy variants',
      researcherOnly: true
    });
  }

  return (
    <nav className="analytics-navigation">
      <div className="nav-header">
        <h2>Analytics</h2>
        {user?.is_admin ? (
          <span className="admin-badge">Admin View</span>
        ) : user?.can_research ? (
          <span className="admin-badge">Researcher View</span>
        ) : null}
      </div>
      
      <div className="nav-items">
        {navigationItems.map(item => (
          <button
            key={item.id}
            className={`nav-item ${activeView === item.id ? 'active' : ''} ${item.adminOnly ? 'admin-only' : ''}`}
            onClick={() => onViewChange(item.id)}
            title={item.description}
          >
            <span className="nav-icon">{item.icon}</span>
            <div className="nav-content">
              <span className="nav-label">{item.label}</span>
              <span className="nav-description">{item.description}</span>
            </div>
            {item.adminOnly && (
              <span className="admin-indicator">👑</span>
            )}
          </button>
        ))}

        {/* Participant entry point into the research platform: any signed-in
            account can redeem a study join code from here. */}
        <Link
          to="/research/join"
          className="nav-item research-join-link"
          title="Enter a study join code to enroll"
        >
          <span className="nav-icon" aria-hidden="true">🎟️</span>
          <div className="nav-content">
            <span className="nav-label">Join a Study</span>
            <span className="nav-description">Enter a study join code</span>
          </div>
        </Link>

        {/* Researcher control plane lives on its own routes (see App.js) so the
            immutable-revision workflow is not folded into the analytics views. */}
        {(user?.is_admin || user?.can_research) && (
          <Link
            to="/research/studies"
            className="nav-item research-launch-link"
            title="Create and manage research studies"
          >
            <span className="nav-icon" aria-hidden="true">🧪</span>
            <div className="nav-content">
              <span className="nav-label">Research Control Plane</span>
              <span className="nav-description">
                Study lifecycle, profiles and enrollment
              </span>
            </div>
          </Link>
        )}
      </div>
    </nav>
  );
};

export default AnalyticsNavigation;
