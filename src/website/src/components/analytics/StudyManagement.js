import React, { useState, useEffect } from 'react';
import { getStudies, getStudyEvaluation, activateStudy, createStudy } from '../../utils/api';
import './StudyManagement.css';

const StudyManagement = ({ user }) => {
  const [studies, setStudies] = useState([]);
  const [selectedStudy, setSelectedStudy] = useState(null);
  const [evaluationData, setEvaluationData] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState(null);
  const [showCreateModal, setShowCreateModal] = useState(false);

  useEffect(() => {
    fetchStudies();
  }, []);

  const fetchStudies = async () => {
    setIsLoading(true);
    setError(null);

    try {
      const response = await getStudies(true); // Include inactive studies
      if (response.ok) {
        setStudies(response.data.studies || []);
      } else {
        setError(response.error);
      }
    } catch (err) {
      setError("Failed to load studies");
      console.error("Studies error:", err);
    } finally {
      setIsLoading(false);
    }
  };

  const fetchStudyEvaluation = async (studyId) => {
    try {
      const response = await getStudyEvaluation(studyId);
      if (response.ok) {
        setEvaluationData(response.data);
      } else {
        console.warn("Evaluation error:", response.error);
        setEvaluationData(null);
      }
    } catch (err) {
      console.error("Evaluation fetch error:", err);
      setEvaluationData(null);
    }
  };

  const handleStudySelect = (study) => {
    setSelectedStudy(study);
    fetchStudyEvaluation(study.study_id);
  };

  const handleActivateStudy = async (studyId) => {
    try {
      const response = await activateStudy(studyId);
      if (response.ok) {
        fetchStudies(); // Refresh the list
        alert("Study activated successfully!");
      } else {
        alert(`Failed to activate study: ${response.error}`);
      }
    } catch (err) {
      alert("Failed to activate study");
      console.error("Activation error:", err);
    }
  };

  if (!user?.is_admin) {
    return (
      <div className="study-management">
        <div className="access-denied">
          <h2>Access Denied</h2>
          <p>This section is only available to administrators.</p>
        </div>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="study-management">
        <div className="loading-message">
          <h3>Loading Studies...</h3>
          <div className="loading-spinner"></div>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="study-management">
        <div className="error-message">
          <h3>Failed to load studies</h3>
          <p>{error}</p>
          <button onClick={fetchStudies}>Retry</button>
        </div>
      </div>
    );
  }

  return (
    <div className="study-management">
      <div className="analytics-header">
        <h2>A/B Testing & Study Management</h2>
        <p>Create, manage, and evaluate user studies for configuration testing</p>
      </div>

      <div className="study-controls">
        <button 
          className="create-study-btn"
          onClick={() => setShowCreateModal(true)}
        >
          + Create New Study
        </button>
        <div className="study-stats">
          <span>
            {studies.filter(s => s.is_active).length} active, {studies.length} total studies
          </span>
        </div>
      </div>

      <div className="study-layout">
        {/* Studies List */}
        <div className="studies-list">
          <h3>Studies</h3>
          {studies.length === 0 ? (
            <div className="empty-state">
              <p>No studies found. Create your first study to get started.</p>
            </div>
          ) : (
            <div className="study-items">
              {studies.map(study => (
                <div 
                  key={study.study_id}
                  className={`study-item ${selectedStudy?.study_id === study.study_id ? 'selected' : ''}`}
                  onClick={() => handleStudySelect(study)}
                >
                  <div className="study-header">
                    <div className="study-title">
                      <span className="study-name">{study.name}</span>
                      <span className={`study-status ${study.is_active ? 'active' : 'inactive'}`}>
                        {study.is_active ? '🟢 Active' : '🔴 Inactive'}
                      </span>
                    </div>
                    <div className="study-meta">
                      <span>{study.assigned_users_count} users</span>
                      <span>•</span>
                      <span>Started {new Date(study.starts_at).toLocaleDateString()}</span>
                    </div>
                  </div>
                  {study.description && (
                    <div className="study-description">
                      {study.description}
                    </div>
                  )}
                  <div className="study-actions">
                    {!study.is_active && (
                      <button
                        className="activate-btn"
                        onClick={(e) => {
                          e.stopPropagation();
                          handleActivateStudy(study.study_id);
                        }}
                      >
                        Activate
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Study Details */}
        <div className="study-details">
          {selectedStudy ? (
            <div className="study-evaluation">
              <div className="study-info">
                <h3>{selectedStudy.name}</h3>
                <div className="study-metadata">
                  <div className="metadata-item">
                    <span className="label">Status:</span>
                    <span className={`value status-${selectedStudy.is_active ? 'active' : 'inactive'}`}>
                      {selectedStudy.is_active ? 'Active' : 'Inactive'}
                    </span>
                  </div>
                  <div className="metadata-item">
                    <span className="label">Created:</span>
                    <span className="value">{new Date(selectedStudy.created_at).toLocaleDateString()}</span>
                  </div>
                  <div className="metadata-item">
                    <span className="label">Participants:</span>
                    <span className="value">{selectedStudy.assigned_users_count} users</span>
                  </div>
                  <div className="metadata-item">
                    <span className="label">Started:</span>
                    <span className="value">{new Date(selectedStudy.starts_at).toLocaleDateString()}</span>
                  </div>
                  {selectedStudy.ends_at && (
                    <div className="metadata-item">
                      <span className="label">Ended:</span>
                      <span className="value">{new Date(selectedStudy.ends_at).toLocaleDateString()}</span>
                    </div>
                  )}
                </div>
              </div>

              {evaluationData && (
                <div className="evaluation-results">
                  <h4>Study Results</h4>
                  <div className="results-grid">
                    {evaluationData.results?.map((config, index) => (
                      <div key={config.config_id} className="config-result">
                        <div className="config-header">
                          <span className="config-name">
                            Config {config.config_id}
                            {config.is_baseline && <span className="baseline-tag">Baseline</span>}
                          </span>
                        </div>
                        
                        <div className="config-metrics">
                          <div className="metric-row">
                            <span className="metric-label">Users:</span>
                            <span className="metric-value">{config.metrics.total_users}</span>
                          </div>
                          <div className="metric-row">
                            <span className="metric-label">Active:</span>
                            <span className="metric-value">
                              {config.metrics.active_users} ({((config.metrics.activation_rate) * 100).toFixed(1)}%)
                            </span>
                          </div>
                          <div className="metric-row">
                            <span className="metric-label">Queries:</span>
                            <span className="metric-value">{config.metrics.total_queries}</span>
                          </div>
                          <div className="metric-row">
                            <span className="metric-label">Acceptance:</span>
                            <span className="metric-value">
                              {(config.metrics.acceptance_rate * 100).toFixed(1)}%
                            </span>
                          </div>
                          <div className="metric-row">
                            <span className="metric-label">Avg Latency:</span>
                            <span className="metric-value">
                              {Math.round(config.metrics.avg_generation_time)}ms
                            </span>
                          </div>
                        </div>

                        {config.vs_baseline && (
                          <div className="baseline-comparison">
                            <div className="comparison-item">
                              <span className="comparison-label">vs Baseline:</span>
                              <span className={`comparison-value ${config.vs_baseline.is_better_acceptance ? 'positive' : 'negative'}`}>
                                {config.vs_baseline.acceptance_rate_uplift_pct > 0 ? '+' : ''}
                                {config.vs_baseline.acceptance_rate_uplift_pct.toFixed(1)}%
                              </span>
                            </div>
                            <div className="comparison-item">
                              <span className="comparison-label">Latency:</span>
                              <span className={`comparison-value ${config.vs_baseline.is_faster ? 'positive' : 'negative'}`}>
                                {config.vs_baseline.generation_time_change_pct > 0 ? '+' : ''}
                                {config.vs_baseline.generation_time_change_pct.toFixed(1)}%
                              </span>
                            </div>
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          ) : (
            <div className="no-selection">
              <h3>Select a Study</h3>
              <p>Choose a study from the list to view its details and evaluation results.</p>
            </div>
          )}
        </div>
      </div>

      {/* Create Study Modal */}
      {showCreateModal && (
        <CreateStudyModal
          onClose={() => setShowCreateModal(false)}
          onStudyCreated={() => {
            setShowCreateModal(false);
            fetchStudies();
          }}
        />
      )}
    </div>
  );
};

// Simple Create Study Modal Component
const CreateStudyModal = ({ onClose, onStudyCreated }) => {
  const [formData, setFormData] = useState({
    name: '',
    description: '',
    starts_at: new Date().toISOString().slice(0, 16),
    config_ids: [1, 2], // Default configs
    default_config_id: 1
  });
  const [isSubmitting, setIsSubmitting] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setIsSubmitting(true);

    try {
      const response = await createStudy(formData);
      if (response.ok) {
        alert("Study created successfully!");
        onStudyCreated();
      } else {
        alert(`Failed to create study: ${response.error}`);
      }
    } catch (err) {
      alert("Failed to create study");
      console.error("Create study error:", err);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>Create New Study</h3>
          <button className="modal-close" onClick={onClose}>×</button>
        </div>
        
        <form onSubmit={handleSubmit} className="study-form">
          <div className="form-group">
            <label>Study Name *</label>
            <input
              type="text"
              value={formData.name}
              onChange={(e) => setFormData({ ...formData, name: e.target.value })}
              required
              placeholder="e.g., Model Performance Comparison"
            />
          </div>
          
          <div className="form-group">
            <label>Description</label>
            <textarea
              value={formData.description}
              onChange={(e) => setFormData({ ...formData, description: e.target.value })}
              placeholder="Brief description of the study goals"
              rows="3"
            />
          </div>
          
          <div className="form-group">
            <label>Start Date *</label>
            <input
              type="datetime-local"
              value={formData.starts_at}
              onChange={(e) => setFormData({ ...formData, starts_at: e.target.value })}
              required
            />
          </div>
          
          <div className="form-group">
            <label>Configuration IDs (comma-separated) *</label>
            <input
              type="text"
              value={formData.config_ids.join(', ')}
              onChange={(e) => {
                const ids = e.target.value.split(',').map(id => parseInt(id.trim())).filter(id => !isNaN(id));
                setFormData({ ...formData, config_ids: ids });
              }}
              placeholder="1, 2, 3"
              required
            />
          </div>
          
          <div className="form-group">
            <label>Default Config ID *</label>
            <input
              type="number"
              value={formData.default_config_id}
              onChange={(e) => setFormData({ ...formData, default_config_id: parseInt(e.target.value) })}
              required
            />
          </div>
          
          <div className="form-actions">
            <button type="button" onClick={onClose} className="cancel-btn">
              Cancel
            </button>
            <button type="submit" disabled={isSubmitting} className="submit-btn">
              {isSubmitting ? 'Creating...' : 'Create Study'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};

export default StudyManagement;
