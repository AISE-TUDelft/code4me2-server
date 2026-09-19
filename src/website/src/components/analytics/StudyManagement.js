import React, { useState, useEffect } from 'react';
import { getStudies, getStudyEvaluation, getStudyDetails, activateStudy, deactivateStudy, createStudy, getStudyAgentEvaluation, getAgentProfiles, listConfigs } from '../../utils/api';
import AgentResults from './AgentResults';
import './StudyManagement.css';

const StudyManagement = ({ user }) => {
  const [studies, setStudies] = useState([]);
  const [selectedStudy, setSelectedStudy] = useState(null);
  const [evaluationData, setEvaluationData] = useState(null);
  // Agent A/B arms attached to the selected study, and their per-arm results.
  // Both stay empty for completion-only studies, which is the common case.
  const [agentEvaluationData, setAgentEvaluationData] = useState(null);
  const [agentProfiles, setAgentProfiles] = useState([]);
  const [studyDetails, setStudyDetails] = useState(null);
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
    // Agent arms are evaluated by a separate endpoint, because the metrics are
    // entirely different (steps, tool calls, edit acceptance) and a study can
    // have completion arms, agent arms, or both.
    try {
      const agentResponse = await getStudyAgentEvaluation(studyId);
      if (agentResponse.ok) {
        setAgentEvaluationData(agentResponse.data);
      } else {
        console.warn("Agent evaluation error:", agentResponse.error);
        setAgentEvaluationData(null);
      }
    } catch (err) {
      console.error("Agent evaluation fetch error:", err);
      setAgentEvaluationData(null);
    }
  };

  const fetchStudyDetails = async (studyId) => {
    try {
      const response = await getStudyDetails(studyId);
      if (response.ok) {
        setStudyDetails(response.data);
        // agent_profiles is empty for completion-only studies.
        setAgentProfiles(response.data?.agent_profiles || []);
      } else {
        console.warn("Details error:", response.error);
        setStudyDetails(null);
        setAgentProfiles([]);
      }
    } catch (err) {
      console.error("Details fetch error:", err);
      setStudyDetails(null);
      setAgentProfiles([]);
    }
  };

  const handleStudySelect = (study) => {
    setSelectedStudy(study);
    setEvaluationData(null);
    setStudyDetails(null);
    setAgentEvaluationData(null);
    setAgentProfiles([]);
    fetchStudyEvaluation(study.study_id);
    fetchStudyDetails(study.study_id);
  };

  const handleActivateStudy = async (studyId) => {
    try {
      const response = await activateStudy(studyId);
      if (response.ok) {
        fetchStudies(); // Refresh the list
        alert("Study activated successfully!");
        if (selectedStudy?.study_id === studyId) {
          setSelectedStudy({ ...selectedStudy, is_active: true });
        }
      } else {
        alert(`Failed to activate study: ${response.error}`);
      }
    } catch (err) {
      alert("Failed to activate study");
      console.error("Activation error:", err);
    }
  };

  const handleDeactivateStudy = async (studyId) => {
    try {
      const response = await deactivateStudy(studyId);
      if (response.ok) {
        fetchStudies();
        alert("Study deactivated successfully!");
        if (selectedStudy?.study_id === studyId) {
          setSelectedStudy({ ...selectedStudy, is_active: false, ends_at: new Date().toISOString() });
        }
      } else {
        alert(`Failed to deactivate study: ${response.error}`);
      }
    } catch (err) {
      alert("Failed to deactivate study");
      console.error("Deactivation error:", err);
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
        <h2>Legacy completion study management</h2>
        <p>
          Legacy completion-based A/B studies for configuration testing. Agent
          research studies are managed separately in the Research Control Plane.
        </p>
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
                    {!study.is_active ? (
                      <button
                        className="activate-btn"
                        onClick={(e) => {
                          e.stopPropagation();
                          handleActivateStudy(study.study_id);
                        }}
                      >
                        Activate
                      </button>
                    ) : (
                      <button
                        className="deactivate-btn"
                        onClick={(e) => {
                          e.stopPropagation();
                          handleDeactivateStudy(study.study_id);
                        }}
                      >
                        Deactivate
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

              {studyDetails && studyDetails.assignments?.length > 0 && (
                <div className="study-assignments">
                  <h4>Configuration Assignments</h4>
                  <div className="assignments-grid">
                    {studyDetails.assignments.map((a) => (
                      <div key={a.config_id} className="assignment-item">
                        <div className="assignment-header">
                          <span className="config-name">Config {a.config_id}</span>
                        </div>
                        <div className="assignment-metrics">
                          <div className="metric-row">
                            <span className="metric-label">Users:</span>
                            <span className="metric-value">{a.user_count}</span>
                          </div>
                          <div className="metric-row">
                            <span className="metric-label">Engagement:</span>
                            <span className="metric-value">{(a.engagement_rate * 100).toFixed(1)}%</span>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

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
                            <span className="metric-label">Completion acceptance:</span>
                            <span className="metric-value">
                              {config.metrics.acceptance_rate == null
                                ? "unavailable"
                                : `${(config.metrics.acceptance_rate * 100).toFixed(1)}%`}
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
                              <span className={`comparison-value ${config.vs_baseline.is_better_acceptance === true ? 'positive' : config.vs_baseline.is_better_acceptance === false ? 'negative' : ''}`}>
                                {config.vs_baseline.acceptance_rate_uplift_pct == null
                                  ? "unavailable"
                                  : `${config.vs_baseline.acceptance_rate_uplift_pct > 0 ? '+' : ''}${config.vs_baseline.acceptance_rate_uplift_pct.toFixed(1)}%`}
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

              {agentEvaluationData?.results?.length > 0 && (
                <div className="evaluation-results">
                  <AgentResults
                    data={agentEvaluationData}
                    profiles={agentProfiles}
                  />
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
  const [availableConfigs, setAvailableConfigs] = useState([]);
  const [availableProfiles, setAvailableProfiles] = useState([]);
  const [isLoadingOptions, setIsLoadingOptions] = useState(true);
  const [formData, setFormData] = useState({
    name: '',
    description: '',
    starts_at: new Date().toISOString().slice(0, 16),
    config_ids: [],
    default_config_id: '',
    agent_profile_ids: [],
    baseline_agent_profile_id: null,
  });
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    let isCancelled = false;

    const loadStudyOptions = async () => {
      const [configResponse, profileResponse] = await Promise.all([
        listConfigs(),
        getAgentProfiles(),
      ]);

      if (isCancelled) {
        return;
      }

      const configs = configResponse.ok ? configResponse.data : [];
      const profiles = profileResponse.ok
        ? profileResponse.data.filter((profile) => profile.is_active)
        : [];
      const configIds = configs.map((config) => config.config_id);
      const initialConfigIds = configIds.slice(0, 2);

      setAvailableConfigs(configs);
      setAvailableProfiles(profiles);
      setFormData((current) => ({
        ...current,
        config_ids: initialConfigIds,
        default_config_id: initialConfigIds[0] || '',
      }));
      setIsLoadingOptions(false);
    };

    loadStudyOptions().catch((error) => {
      console.error("Study options error:", error);
      if (!isCancelled) {
        setIsLoadingOptions(false);
      }
    });

    return () => {
      isCancelled = true;
    };
  }, []);

  const toggleConfig = (configId) => {
    setFormData((current) => {
      const configIds = current.config_ids.includes(configId)
        ? current.config_ids.filter((id) => id !== configId)
        : [...current.config_ids, configId];
      const defaultConfigId = configIds.includes(current.default_config_id)
        ? current.default_config_id
        : configIds[0] || '';
      return { ...current, config_ids: configIds, default_config_id: defaultConfigId };
    });
  };

  const toggleAgentProfile = (profileId) => {
    setFormData((current) => {
      const agentProfileIds = current.agent_profile_ids.includes(profileId)
        ? current.agent_profile_ids.filter((id) => id !== profileId)
        : [...current.agent_profile_ids, profileId];
      const baselineAgentProfileId = agentProfileIds.includes(current.baseline_agent_profile_id)
        ? current.baseline_agent_profile_id
        : agentProfileIds[0] || null;
      return { ...current, agent_profile_ids: agentProfileIds, baseline_agent_profile_id: baselineAgentProfileId };
    });
  };

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
            <fieldset className="study-option-group">
              <legend>Completion config arms *</legend>
              {isLoadingOptions ? (
                <p className="form-help">Loading available configs...</p>
              ) : availableConfigs.length === 0 ? (
                <p className="form-help">Create a completion config before starting a study.</p>
              ) : (
                <div className="study-options-grid">
                  {availableConfigs.map((config) => (
                    <label className="study-option" key={config.config_id}>
                      <input
                        type="checkbox"
                        checked={formData.config_ids.includes(config.config_id)}
                        onChange={() => toggleConfig(config.config_id)}
                      />
                      <span>Config {config.config_id}</span>
                    </label>
                  ))}
                </div>
              )}
            </fieldset>
          </div>
          
          <div className="form-group">
            <label>Default Config ID *</label>
            <select
              value={formData.default_config_id}
              onChange={(e) => setFormData({ ...formData, default_config_id: parseInt(e.target.value) })}
              required
              disabled={!formData.config_ids.length}
            >
              {formData.config_ids.map((configId) => (
                <option key={configId} value={configId}>Config {configId}</option>
              ))}
            </select>
          </div>

          <div className="form-group">
            <fieldset className="study-option-group">
              <legend>Agent profile arms (optional)</legend>
              <p className="form-help">Select active profiles to randomize agent users between arms.</p>
              {isLoadingOptions ? (
                <p className="form-help">Loading active agent profiles...</p>
              ) : availableProfiles.length === 0 ? (
                <p className="form-help">Create and activate an agent profile to add an agent arm.</p>
              ) : (
                <div className="study-options-grid">
                  {availableProfiles.map((profile) => (
                    <label className="study-option" key={profile.profile_id}>
                      <input
                        type="checkbox"
                        checked={formData.agent_profile_ids.includes(profile.profile_id)}
                        onChange={() => toggleAgentProfile(profile.profile_id)}
                      />
                      <span>{profile.name}</span>
                    </label>
                  ))}
                </div>
              )}
              {formData.agent_profile_ids.length > 0 && (
                <label className="baseline-select">
                  Baseline agent arm
                  <select
                    value={formData.baseline_agent_profile_id || ''}
                    onChange={(e) => setFormData({ ...formData, baseline_agent_profile_id: e.target.value || null })}
                    required
                  >
                    {formData.agent_profile_ids.map((profileId) => {
                      const profile = availableProfiles.find((item) => item.profile_id === profileId);
                      return <option key={profileId} value={profileId}>{profile?.name || profileId}</option>;
                    })}
                  </select>
                </label>
              )}
            </fieldset>
          </div>
          
          <div className="form-actions">
            <button type="button" onClick={onClose} className="cancel-btn">
              Cancel
            </button>
            <button
              type="submit"
              disabled={isSubmitting || isLoadingOptions || !formData.config_ids.length}
              className="submit-btn"
            >
              {isSubmitting ? 'Creating...' : 'Create Study'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};

export default StudyManagement;
