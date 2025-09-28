import React, { useState, useEffect } from 'react';
import Chart from '../visualization/Chart';
import { getQueriesOverTime, getAcceptanceRates, getUserEngagement } from '../../utils/api';
import './UsageAnalytics.css';

const UsageAnalytics = () => {
  const [queriesData, setQueriesData] = useState(null);
  const [acceptanceData, setAcceptanceData] = useState(null);
  const [engagementData, setEngagementData] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState(null);

  // Filter states
  const [timeRange, setTimeRange] = useState('7d');
  const [granularity, setGranularity] = useState('1h');
  const [groupBy, setGroupBy] = useState('model');

  useEffect(() => {
    const fetchData = async () => {
      setIsLoading(true);
      setError(null);

      try {
        const [queriesResponse, acceptanceResponse, engagementResponse] = await Promise.all([
          getQueriesOverTime({ granularity }),
          getAcceptanceRates({ group_by: groupBy }),
          getUserEngagement('30d')
        ]);

        if (queriesResponse.ok) {
          setQueriesData(queriesResponse.data);
        } else {
          console.warn("Queries error:", queriesResponse.error);
        }

        if (acceptanceResponse.ok) {
          setAcceptanceData(acceptanceResponse.data);
        } else {
          console.warn("Acceptance error:", acceptanceResponse.error);
        }

        if (engagementResponse.ok) {
          setEngagementData(engagementResponse.data);
        } else {
          console.warn("Engagement error:", engagementResponse.error);
        }

      } catch (err) {
        setError("Failed to load usage analytics");
        console.error("Usage analytics error:", err);
      } finally {
        setIsLoading(false);
      }
    };

    fetchData();
  }, [granularity, groupBy]);

  const formatQueriesOverTime = (data) => {
    if (!data?.data) return [];
    
    // Group by time bucket and sum counts
    const timeGroups = {};
    data.data.forEach(point => {
      const time = new Date(point.time_bucket).getTime();
      if (!timeGroups[time]) {
        timeGroups[time] = 0;
      }
      timeGroups[time] += point.count;
    });

    return Object.entries(timeGroups).map(([timestamp, value]) => ({
      timestamp: parseInt(timestamp),
      value
    })).sort((a, b) => a.timestamp - b.timestamp);
  };

  const formatAcceptanceRatesChart = (data) => {
    if (!data?.data) return [];
    return data.data.slice(0, 8).map((item) => ({
      label: item.group_name,
      value: Math.round(item.acceptance_rate * 100),
    }));
  };

  if (isLoading) {
    return (
      <div className="usage-analytics">
        <div className="loading-message">
          <h3>Loading Usage Analytics...</h3>
          <div className="loading-spinner"></div>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="usage-analytics">
        <div className="error-message">
          <h3>Failed to load usage analytics</h3>
          <p>{error}</p>
          <button onClick={() => window.location.reload()}>Retry</button>
        </div>
      </div>
    );
  }

  return (
    <div className="usage-analytics">
      <div className="analytics-header">
        <h2>Usage Analytics</h2>
        <p>Detailed analysis of query patterns, user behavior, and acceptance rates</p>
      </div>

      <div className="analytics-controls">
        <div className="control-group">
          <label>Time Granularity:</label>
          <select value={granularity} onChange={(e) => setGranularity(e.target.value)}>
            <option value="15m">15 Minutes</option>
            <option value="1h">1 Hour</option>
            <option value="6h">6 Hours</option>
            <option value="1d">1 Day</option>
          </select>
        </div>
        
        <div className="control-group">
          <label>Group Acceptance By:</label>
          <select value={groupBy} onChange={(e) => setGroupBy(e.target.value)}>
            <option value="model">Model</option>
            <option value="config">Configuration</option>
            <option value="language">Programming Language</option>
            <option value="trigger">Trigger Type</option>
          </select>
        </div>
      </div>

      <div className="analytics-grid">
        {/* Query Volume Over Time */}
        <div className="analytics-card full-width">
          <h3>Query Volume Over Time</h3>
          <div className="chart-container">
            <Chart
              data={formatQueriesOverTime(queriesData)}
              title="Queries"
              color="#4285f4"
            />
          </div>
          <div className="chart-info">
            <p>Total queries: {queriesData?.data?.reduce((sum, point) => sum + point.count, 0) || 0}</p>
            <p>Granularity: {granularity}</p>
          </div>
        </div>

        {/* Acceptance Rates */}
        <div className="analytics-card">
          <h3>Acceptance Rates by {groupBy.charAt(0).toUpperCase() + groupBy.slice(1)}</h3>
          <div className="chart-container">
            <Chart
              data={formatAcceptanceRatesChart(acceptanceData)}
              title="Acceptance Rate (%)"
              color="#34a853"
              xKey="label"
              xType="category"
            />
          </div>
          <div className="acceptance-table">
            {acceptanceData?.data?.slice(0, 5).map((item, index) => (
              <div key={index} className="acceptance-row">
                <span className="group-name">{item.group_name}</span>
                <span className="sample-size">({item.sample_size} samples)</span>
                <div className="acceptance-bar">
                  <div 
                    className="acceptance-fill"
                    style={{ width: `${item.acceptance_rate * 100}%` }}
                  />
                </div>
                <span className="acceptance-value">
                  {(item.acceptance_rate * 100).toFixed(1)}%
                </span>
              </div>
            ))}
          </div>
        </div>

        {/* User Engagement */}
        <div className="analytics-card">
          <h3>User Engagement Overview</h3>
          {engagementData?.engagement && (
            <div className="engagement-metrics">
              {engagementData.engagement.summary_type === 'personal' ? (
                // Personal metrics for regular users
                <div className="personal-metrics">
                  <div className="metric-item">
                    <span className="metric-label">Your Queries</span>
                    <span className="metric-value">{engagementData.engagement.total_queries}</span>
                  </div>
                  <div className="metric-item">
                    <span className="metric-label">Acceptance Rate</span>
                    <span className="metric-value">
                      {(engagementData.engagement.acceptance_rate * 100).toFixed(1)}%
                    </span>
                  </div>
                  <div className="metric-item">
                    <span className="metric-label">Active Days</span>
                    <span className="metric-value">{engagementData.engagement.active_days}</span>
                  </div>
                  <div className="metric-item">
                    <span className="metric-label">Total Sessions</span>
                    <span className="metric-value">{engagementData.engagement.total_sessions}</span>
                  </div>
                </div>
              ) : (
                // Aggregate metrics for admins
                <div className="aggregate-metrics">
                  <div className="metric-item">
                    <span className="metric-label">Total Active Users</span>
                    <span className="metric-value">{engagementData.engagement.total_active_users}</span>
                  </div>
                  <div className="metric-item">
                    <span className="metric-label">Avg Queries/User</span>
                    <span className="metric-value">
                      {engagementData.engagement.avg_queries_per_user?.toFixed(1)}
                    </span>
                  </div>
                  <div className="metric-item">
                    <span className="metric-label">Highly Active Users</span>
                    <span className="metric-value">{engagementData.engagement.highly_active_users}</span>
                  </div>
                  <div className="metric-item">
                    <span className="metric-label">Regular Users</span>
                    <span className="metric-value">{engagementData.engagement.regular_users}</span>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Query Type Distribution */}
        {queriesData?.data && (
          <div className="analytics-card">
            <h3>Query Type Distribution</h3>
            <div className="distribution-chart">
              {(() => {
                const completionCount = queriesData.data
                  .filter(point => point.query_type === 'completion')
                  .reduce((sum, point) => sum + point.count, 0);
                const chatCount = queriesData.data
                  .filter(point => point.query_type === 'chat')
                  .reduce((sum, point) => sum + point.count, 0);
                const total = completionCount + chatCount;

                return (
                  <div className="distribution-bars">
                    <div className="distribution-item">
                      <span className="type-label">Completions</span>
                      <div className="type-bar">
                        <div 
                          className="type-fill completion"
                          style={{ width: `${total ? (completionCount / total) * 100 : 0}%` }}
                        />
                      </div>
                      <span className="type-count">
                        {completionCount} ({total ? ((completionCount / total) * 100).toFixed(1) : 0}%)
                      </span>
                    </div>
                    <div className="distribution-item">
                      <span className="type-label">Chat</span>
                      <div className="type-bar">
                        <div 
                          className="type-fill chat"
                          style={{ width: `${total ? (chatCount / total) * 100 : 0}%` }}
                        />
                      </div>
                      <span className="type-count">
                        {chatCount} ({total ? ((chatCount / total) * 100).toFixed(1) : 0}%)
                      </span>
                    </div>
                  </div>
                );
              })()}
            </div>
          </div>
        )}

        {/* Language Insights */}
        {queriesData?.data && (
          <div className="analytics-card full-width">
            <h3>Programming Language Insights</h3>
            <div className="language-insights">
              {(() => {
                // Group by language
                const languageStats = {};
                queriesData.data.forEach(point => {
                  if (point.language) {
                    if (!languageStats[point.language]) {
                      languageStats[point.language] = { count: 0, total: 0 };
                    }
                    languageStats[point.language].count += point.count;
                    languageStats[point.language].total += point.count;
                  }
                });

                const sortedLanguages = Object.entries(languageStats)
                  .sort(([,a], [,b]) => b.count - a.count)
                  .slice(0, 8);

                const maxCount = Math.max(...sortedLanguages.map(([,stats]) => stats.count));

                return (
                  <div className="language-grid">
                    {sortedLanguages.map(([language, stats]) => (
                      <div key={language} className="language-insight">
                        <div className="language-header">
                          <span className="language-name">{language}</span>
                          <span className="language-count">{stats.count}</span>
                        </div>
                        <div className="language-bar">
                          <div 
                            className="language-fill"
                            style={{ 
                              width: `${(stats.count / maxCount) * 100}%`,
                              backgroundColor: `hsl(${Math.abs(language.split('').reduce((acc, char) => acc + char.charCodeAt(0), 0)) % 360}, 70%, 50%)`
                            }}
                          />
                        </div>
                      </div>
                    ))}
                  </div>
                );
              })()}
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

export default UsageAnalytics;
