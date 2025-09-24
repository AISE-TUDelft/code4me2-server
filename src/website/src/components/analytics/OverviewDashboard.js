import React, { useState, useEffect } from 'react';
import Chart from '../visualization/Chart';
import { getDashboardOverview, getActivityTimeline } from '../../utils/api';
import './OverviewDashboard.css';

const OverviewDashboard = ({ timeWindow, onTimeWindowChange }) => {
  const [overviewData, setOverviewData] = useState(null);
  const [timelineData, setTimelineData] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    const fetchData = async () => {
      setIsLoading(true);
      setError(null);

      try {
        // Fetch overview data
        const overviewResponse = await getDashboardOverview(timeWindow);
        if (overviewResponse.ok) {
          setOverviewData(overviewResponse.data);
        } else {
          setError(`Overview: ${overviewResponse.error}`);
        }

        // Fetch timeline data
        const timelineResponse = await getActivityTimeline("24h", "1h");
        if (timelineResponse.ok) {
          setTimelineData(timelineResponse.data);
        } else {
          console.warn("Timeline error:", timelineResponse.error);
        }

      } catch (err) {
        setError("Failed to load dashboard data");
        console.error("Dashboard error:", err);
      } finally {
        setIsLoading(false);
      }
    };

    fetchData();
  }, [timeWindow]);

  const formatTimelineData = (timeline) => {
    if (!timeline) return [];
    return timeline.map(point => ({
      timestamp: new Date(point.time_bucket).getTime(),
      value: point.query_count
    }));
  };

  const StatCard = ({ title, value, trend, description, icon }) => (
    <div className="stat-card">
      <div className="stat-header">
        <div className="stat-icon">{icon}</div>
        <h3>{title}</h3>
      </div>
      <div className="stat-value">{value}</div>
      {trend && (
        <div className="stat-trend">
          <span className={`trend ${trend.direction}`}>
            {trend.direction === 'up' ? '↗' : trend.direction === 'down' ? '↘' : '→'} 
            {Math.abs(trend.change).toFixed(1)}%
          </span>
          <span className="trend-label">vs previous period</span>
        </div>
      )}
      {description && (
        <div className="stat-description">{description}</div>
      )}
    </div>
  );

  if (isLoading) {
    return (
      <div className="overview-dashboard">
        <div className="dashboard-controls">
          <div className="time-window-selector">
            <label>Time Window:</label>
            <select 
              value={timeWindow} 
              onChange={(e) => onTimeWindowChange(e.target.value)}
              disabled
            >
              <option value="1d">1 Day</option>
              <option value="7d">7 Days</option>
              <option value="30d">30 Days</option>
            </select>
          </div>
        </div>
        
        <div className="loading-content">
          {Array.from({ length: 4 }).map((_, index) => (
            <div key={index} className="stat-card loading">
              <div className="loading-chart"></div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="overview-dashboard">
        <div className="error-message">
          <h3>Failed to load dashboard data</h3>
          <p>{error}</p>
          <button onClick={() => window.location.reload()}>Retry</button>
        </div>
      </div>
    );
  }

  const overview = overviewData?.overview || {};
  const trends = overviewData?.trends || {};

  return (
    <div className="overview-dashboard">
      <div className="dashboard-controls">
        <div className="time-window-selector">
          <label>Time Window:</label>
          <select 
            value={timeWindow} 
            onChange={(e) => onTimeWindowChange(e.target.value)}
          >
            <option value="1d">1 Day</option>
            <option value="7d">7 Days</option>
            <option value="30d">30 Days</option>
          </select>
        </div>
      </div>

      <div className="overview-stats">
        <StatCard
          title="Total Queries"
          value={overview.total_queries?.toLocaleString() || '0'}
          trend={{
            direction: trends.direction?.queries || 'stable',
            change: trends.queries_change_pct || 0
          }}
          description={`${overview.completion_queries || 0} completions, ${overview.chat_queries || 0} chats`}
          icon="📊"
        />
        
        <StatCard
          title="Active Users"
          value={overview.active_users?.toLocaleString() || '0'}
          trend={{
            direction: trends.direction?.users || 'stable',
            change: trends.users_change_pct || 0
          }}
          description={`${overview.total_sessions || 0} total sessions`}
          icon="👥"
        />
        
        <StatCard
          title="Acceptance Rate"
          value={overview.overall_acceptance_rate ? 
            `${(overview.overall_acceptance_rate * 100).toFixed(1)}%` : 
            '0%'
          }
          trend={{
            direction: trends.direction?.acceptance || 'stable',
            change: trends.acceptance_change_pct || 0
          }}
          description={`${overview.total_accepted_generations || 0} accepted generations`}
          icon="✅"
        />
        
        <StatCard
          title="Avg Generation Time"
          value={overview.avg_generation_time_ms ? 
            `${Math.round(overview.avg_generation_time_ms)}ms` : 
            '0ms'
          }
          description={`${overview.models_used || 0} models in use`}
          icon="⚡"
        />
      </div>

      <div className="dashboard-charts">
        <div className="chart-section">
          <h3>Query Activity Timeline (Last 24h)</h3>
          <div className="chart-container">
            <Chart
              data={formatTimelineData(timelineData?.timeline)}
              title="Queries per Hour"
              color="#4285f4"
            />
          </div>
        </div>

        <div className="insights-section">
          <div className="insight-group">
            <h3>Top Programming Languages</h3>
            <div className="language-list">
              {overviewData?.top_languages?.slice(0, 5).map((lang, index) => (
                <div key={lang.language} className="language-item">
                  <div className="language-info">
                    <span className="language-name">{lang.language}</span>
                    <span className="language-count">{lang.query_count} queries</span>
                  </div>
                  <div className="language-bar">
                    <div 
                      className="language-bar-fill"
                      style={{ 
                        width: `${(lang.query_count / Math.max(...overviewData.top_languages.map(l => l.query_count))) * 100}%`,
                        backgroundColor: `hsl(${210 + index * 30}, 70%, 50%)`
                      }}
                    />
                  </div>
                  <span className="acceptance-rate">
                    {(lang.acceptance_rate * 100).toFixed(1)}% accepted
                  </span>
                </div>
              ))}
            </div>
          </div>

          <div className="insight-group">
            <h3>Top Performing Models</h3>
            <div className="model-list">
              {overviewData?.top_models?.slice(0, 3).map((model, index) => (
                <div key={model.model_id} className="model-item">
                  <div className="model-header">
                    <span className="model-name">
                      {model.model_name.split('/').pop() || model.model_name}
                    </span>
                    <span className="model-usage">{model.usage_count} uses</span>
                  </div>
                  <div className="model-stats">
                    <div className="model-acceptance">
                      <span className="label">Acceptance:</span>
                      <span className={`value ${model.acceptance_rate > 0.7 ? 'good' : model.acceptance_rate > 0.5 ? 'medium' : 'low'}`}>
                        {(model.acceptance_rate * 100).toFixed(1)}%
                      </span>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default OverviewDashboard;
