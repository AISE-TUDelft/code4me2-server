import React, { useState, useEffect } from 'react';
import Chart from '../visualization/Chart';
import { getModelComparison } from '../../utils/api';
import './ModelAnalytics.css';

const ModelAnalytics = () => {
  const [modelData, setModelData] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selectedModels, setSelectedModels] = useState([]);
  const [timeRange, setTimeRange] = useState('30d');

  useEffect(() => {
    const fetchData = async () => {
      setIsLoading(true);
      setError(null);

      try {
        const params = {};
        if (selectedModels.length > 0) {
          params.model_ids = selectedModels.join(',');
        }

        const response = await getModelComparison(params);
        if (response.ok) {
          setModelData(response.data);
        } else {
          setError(response.error);
        }
      } catch (err) {
        setError("Failed to load model analytics");
        console.error("Model analytics error:", err);
      } finally {
        setIsLoading(false);
      }
    };

    fetchData();
  }, [selectedModels, timeRange]);

  const formatModelPerformanceChart = (models) => {
    if (!models) return [];
    return models.map((model) => ({
      label: (model.model_name.split('/').pop() || model.model_name),
      value: Math.round(model.metrics.acceptance_rate * 100),
    }));
  };

  const formatLatencyChart = (models) => {
    if (!models) return [];
    return models.map((model) => ({
      label: (model.model_name.split('/').pop() || model.model_name),
      value: Math.round(model.metrics.avg_generation_time),
    }));
  };

  const getPerformanceRating = (acceptance_rate) => {
    if (acceptance_rate >= 0.8) return 'excellent';
    if (acceptance_rate >= 0.7) return 'good';
    if (acceptance_rate >= 0.6) return 'average';
    return 'needs-improvement';
  };

  if (isLoading) {
    return (
      <div className="model-analytics">
        <div className="loading-message">
          <h3>Loading Model Analytics...</h3>
          <div className="loading-spinner"></div>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="model-analytics">
        <div className="error-message">
          <h3>Failed to load model analytics</h3>
          <p>{error}</p>
          <button onClick={() => window.location.reload()}>Retry</button>
        </div>
      </div>
    );
  }

  const models = modelData?.data || [];

  return (
    <div className="model-analytics">
      <div className="analytics-header">
        <h2>Model Performance Analytics</h2>
        <p>Compare AI model performance, quality metrics, and usage patterns</p>
      </div>

      <div className="model-controls">
        <div className="control-group">
          <label>Model Selection:</label>
          <div className="model-checkboxes">
            {models.slice(0, 6).map(model => (
              <label key={model.model_id} className="checkbox-label">
                <input
                  type="checkbox"
                  checked={selectedModels.includes(model.model_id.toString())}
                  onChange={(e) => {
                    if (e.target.checked) {
                      setSelectedModels([...selectedModels, model.model_id.toString()]);
                    } else {
                      setSelectedModels(selectedModels.filter(id => id !== model.model_id.toString()));
                    }
                  }}
                />
                <span className="model-name-short">
                  {model.model_name.split('/').pop() || model.model_name}
                </span>
              </label>
            ))}
          </div>
        </div>
        
        <div className="control-group">
          <button 
            onClick={() => setSelectedModels([])}
            className="clear-selection"
          >
            Show All Models
          </button>
        </div>
      </div>

      <div className="analytics-grid">
        {/* Model Performance Chart */}
        <div className="analytics-card full-width">
          <h3>Model Acceptance Rates</h3>
          <div className="chart-container">
            <Chart
              data={formatModelPerformanceChart(models)}
              title="Acceptance Rate (%)"
              color="#4285f4"
              xKey="label"
              xType="category"
            />
          </div>
        </div>

        {/* Model Comparison Table */}
        <div className="analytics-card full-width">
          <h3>Model Performance Comparison</h3>
          <div className="model-table-container">
            <table className="model-table">
              <thead>
                <tr>
                  <th>Model</th>
                  <th>Type</th>
                  <th>Generations</th>
                  <th>Acceptance Rate</th>
                  <th>Avg Latency</th>
                  <th>Confidence</th>
                  <th>Unique Users</th>
                  <th>Rating</th>
                </tr>
              </thead>
              <tbody>
                {models.map(model => (
                  <tr key={model.model_id} className="model-row">
                    <td className="model-name-cell">
                      <div className="model-name-full">
                        {model.model_name}
                      </div>
                      <div className="model-short-name">
                        {model.model_name.split('/').pop() || model.model_name}
                      </div>
                    </td>
                    <td>
                      <span className={`model-type ${model.is_instruction_tuned ? 'instruct' : 'base'}`}>
                        {model.is_instruction_tuned ? 'Instruct' : 'Base'}
                      </span>
                    </td>
                    <td>{model.metrics.total_generations.toLocaleString()}</td>
                    <td>
                      <div className="acceptance-cell">
                        <span className="acceptance-value">
                          {(model.metrics.acceptance_rate * 100).toFixed(1)}%
                        </span>
                        <div className="acceptance-bar">
                          <div 
                            className="acceptance-fill"
                            style={{ width: `${model.metrics.acceptance_rate * 100}%` }}
                          />
                        </div>
                      </div>
                    </td>
                    <td>
                      <span className={`latency ${model.metrics.avg_generation_time > 300 ? 'slow' : model.metrics.avg_generation_time > 200 ? 'medium' : 'fast'}`}>
                        {Math.round(model.metrics.avg_generation_time)}ms
                      </span>
                    </td>
                    <td>{(model.metrics.avg_confidence * 100).toFixed(1)}%</td>
                    <td>{model.metrics.unique_users}</td>
                    <td>
                      <span className={`rating ${getPerformanceRating(model.metrics.acceptance_rate)}`}>
                        {getPerformanceRating(model.metrics.acceptance_rate).replace('-', ' ')}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {/* Latency Comparison */}
        <div className="analytics-card">
          <h3>Generation Time Comparison</h3>
          <div className="chart-container">
            <Chart
              data={formatLatencyChart(models)}
              title="Avg Generation Time (ms)"
              color="#ff9800"
              xKey="label"
              xType="category"
            />
          </div>
          <div className="latency-insights">
            <div className="insight-item">
              <span className="label">Fastest:</span>
              <span className="value">
                {models.reduce((fastest, model) => 
                  model.metrics.avg_generation_time < fastest.metrics.avg_generation_time ? model : fastest
                )?.model_name.split('/').pop()} 
                ({Math.round(models.reduce((fastest, model) => 
                  model.metrics.avg_generation_time < fastest.metrics.avg_generation_time ? model : fastest
                )?.metrics.avg_generation_time)}ms)
              </span>
            </div>
            <div className="insight-item">
              <span className="label">Slowest:</span>
              <span className="value">
                {models.reduce((slowest, model) => 
                  model.metrics.avg_generation_time > slowest.metrics.avg_generation_time ? model : slowest
                )?.model_name.split('/').pop()} 
                ({Math.round(models.reduce((slowest, model) => 
                  model.metrics.avg_generation_time > slowest.metrics.avg_generation_time ? model : slowest
                )?.metrics.avg_generation_time)}ms)
              </span>
            </div>
          </div>
        </div>

        {/* Model Quality Insights */}
        <div className="analytics-card">
          <h3>Quality Insights</h3>
          <div className="quality-metrics">
            {models.slice(0, 3).map(model => (
              <div key={model.model_id} className="quality-item">
                <div className="quality-header">
                  <span className="model-name">
                    {model.model_name.split('/').pop() || model.model_name}
                  </span>
                  <span className={`quality-score ${getPerformanceRating(model.metrics.acceptance_rate)}`}>
                    {(model.metrics.acceptance_rate * 100).toFixed(0)}%
                  </span>
                </div>
                <div className="quality-details">
                  <div className="quality-stat">
                    <span>Confidence:</span>
                    <span>{(model.metrics.avg_confidence * 100).toFixed(1)}%</span>
                  </div>
                  <div className="quality-stat">
                    <span>Usage:</span>
                    <span>{model.metrics.total_generations.toLocaleString()}</span>
                  </div>
                  <div className="quality-stat">
                    <span>Users:</span>
                    <span>{model.metrics.unique_users}</span>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Model Usage Distribution */}
        <div className="analytics-card full-width">
          <h3>Model Usage Distribution</h3>
          <div className="usage-distribution">
            {models.map(model => {
              const totalGenerations = models.reduce((sum, m) => sum + m.metrics.total_generations, 0);
              const usagePercentage = totalGenerations > 0 ? (model.metrics.total_generations / totalGenerations) * 100 : 0;
              
              return (
                <div key={model.model_id} className="usage-item">
                  <div className="usage-header">
                    <span className="model-name">
                      {model.model_name.split('/').pop() || model.model_name}
                    </span>
                    <span className="usage-percentage">
                      {usagePercentage.toFixed(1)}%
                    </span>
                  </div>
                  <div className="usage-bar">
                    <div 
                      className="usage-fill"
                      style={{ 
                        width: `${usagePercentage}%`,
                        backgroundColor: `hsl(${(model.model_id * 50) % 360}, 70%, 50%)`
                      }}
                    />
                  </div>
                  <div className="usage-stats">
                    <span>{model.metrics.total_generations.toLocaleString()} generations</span>
                    <span>•</span>
                    <span>{model.metrics.unique_users} users</span>
                    <span>•</span>
                    <span>{model.metrics.active_days} active days</span>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
};

export default ModelAnalytics;
