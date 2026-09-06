import React from 'react';
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
      id: 'calibration',
      label: 'Model Calibration',
      icon: '🎯',
      description: 'Confidence and reliability analysis'
    }
  ];

  // Add admin-only navigation items
  if (user?.is_admin) {
    navigationItems.push({
      id: 'studies',
      label: 'A/B Testing',
      icon: '🔬',
      description: 'Experiment management and evaluation',
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
      id: 'agent-profiles',
      label: 'Agent Profiles',
      icon: '🛠️',
      description: 'Agent runtime, provider, tools and policy variants',
      adminOnly: true
    });
    navigationItems.push({
      id: 'agent-assignments',
      label: 'Agent Assignments',
      icon: '🎲',
      description: 'A/B bucket distribution and manual pins',
      adminOnly: true
    });
  }

  return (
    <nav className="analytics-navigation">
      <div className="nav-header">
        <h2>Analytics</h2>
        {user?.is_admin && (
          <span className="admin-badge">Admin View</span>
        )}
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
      </div>
    </nav>
  );
};

export default AnalyticsNavigation;
