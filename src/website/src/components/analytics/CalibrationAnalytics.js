import React, { useEffect, useMemo, useState } from 'react';
import Chart from '../visualization/Chart';
import { 
  getReliabilityDiagram, 
  getConfidenceDistribution, 
  getBrierScore, 
  getCalibrationSummary 
} from '../../utils/api';
import './CalibrationAnalytics.css';

const CalibrationAnalytics = () => {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [reliability, setReliability] = useState(null);
  const [confidenceDist, setConfidenceDist] = useState(null);
  const [brier, setBrier] = useState(null);
  const [summary, setSummary] = useState(null);

  // Controls
  const [bins, setBins] = useState(10);
  const [histBins, setHistBins] = useState(20);
  const [groupBy, setGroupBy] = useState('model');

  useEffect(() => {
    const fetchAll = async () => {
      setLoading(true);
      setError(null);
      try {
        const [relRes, confRes, brierRes, summaryRes] = await Promise.all([
          getReliabilityDiagram({ bins }),
          getConfidenceDistribution({ bins: histBins }),
          getBrierScore({ group_by: groupBy }),
          getCalibrationSummary(),
        ]);

        if (relRes.ok) setReliability(relRes.data);
        else console.warn('Reliability error:', relRes.error);

        if (confRes.ok) setConfidenceDist(confRes.data);
        else console.warn('Confidence distribution error:', confRes.error);

        if (brierRes.ok) setBrier(brierRes.data);
        else console.warn('Brier score error:', brierRes.error);

        if (summaryRes.ok) setSummary(summaryRes.data);
        else console.warn('Summary error:', summaryRes.error);
      } catch (e) {
        setError('Failed to load calibration analytics');
        console.error(e);
      } finally {
        setLoading(false);
      }
    };

    fetchAll();
  }, [bins, histBins, groupBy]);

  const reliabilitySeries = useMemo(() => {
    const models = reliability?.data || [];
    if (!models.length) return [];
    // For simplicity, show first model's empirical accuracy per bin as percentages
    const first = models[0];
    const sortedBins = [...(first?.bins || [])].sort((a,b) => a.bin_number - b.bin_number);
    return sortedBins.map(bin => ({
      label: `Bin ${bin.bin_number}`,
      value: Math.round((bin.empirical_accuracy || 0) * 100),
    }));
  }, [reliability]);

  const confidenceHistogram = useMemo(() => {
    const models = confidenceDist?.data || [];
    if (!models.length) return [];
    const first = models[0];
    const sorted = [...(first?.histogram || [])].sort((a,b) => a.bin_number - b.bin_number);
    return sorted.map(bin => ({
      label: `Bin ${bin.bin_number}`,
      value: bin.frequency || 0,
    }));
  }, [confidenceDist]);

  const eceRows = useMemo(() => {
    const models = reliability?.data || [];
    return models.map(m => ({
      model_id: m.model_id,
      model_name: m.model_name,
      ece: m.expected_calibration_error ?? 0,
      total: m.total_samples || 0,
    })).sort((a,b) => a.ece - b.ece);
  }, [reliability]);

  const eceByModel = useMemo(() => {
    const map = {};
    (reliability?.data || []).forEach(m => { map[m.model_id] = m.expected_calibration_error ?? 0; });
    return map;
  }, [reliability]);

  const brierRows = useMemo(() => {
    const rows = brier?.data || [];
    return rows.map(r => ({
      group_name: r.group_name,
      model_id: r.model_id,
      brier_score: r.brier_score,
      skill_score: r.skill_score,
      base_rate: r.base_rate,
    })).sort((a,b) => a.brier_score - b.brier_score).slice(0, 8);
  }, [brier]);

  if (loading) {
    return (
      <div className="calibration-analytics">
        <div className="loading-message">
          <h3>Loading Model Calibration...</h3>
          <div className="loading-spinner"></div>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="calibration-analytics">
        <div className="error-message">
          <h3>Failed to load calibration analytics</h3>
          <p>{error}</p>
          <button onClick={() => window.location.reload()}>Retry</button>
        </div>
      </div>
    );
  }

  return (
    <div className="calibration-analytics">
      <div className="analytics-header">
        <h2>Model Calibration Analytics</h2>
        <p>Reliability, confidence distribution, and calibration quality metrics</p>
      </div>

      <div className="calibration-controls">
        <div className="control-group">
          <label>Reliability bins</label>
          <input type="number" min={5} max={20} value={bins} onChange={(e) => setBins(parseInt(e.target.value || '10', 10))} />
        </div>
        <div className="control-group">
          <label>Histogram bins</label>
          <input type="number" min={10} max={50} value={histBins} onChange={(e) => setHistBins(parseInt(e.target.value || '20', 10))} />
        </div>
        <div className="control-group">
          <label>Group by (Brier)</label>
          <select value={groupBy} onChange={(e) => setGroupBy(e.target.value)}>
            <option value="model">Model</option>
            <option value="config">Config</option>
            <option value="language">Language</option>
          </select>
        </div>
      </div>

      <div className="cards-grid">
        <div className="card">
          <div className="card-header">
            <h3>Reliability Diagram (Empirical Accuracy per Bin)</h3>
            <span className="subtle">First model shown • Higher is better</span>
          </div>
          <Chart
            data={reliabilitySeries}
            title="Empirical Accuracy by Confidence Bin (%)"
            xKey="label"
            valueKey="value"
            color="#6c5ce7"
            xType="category"
          />
          {eceRows?.length > 0 && (
            <div className="table-wrapper">
              <table className="simple-table">
                <thead>
                  <tr>
                    <th>Model</th>
                    <th>ECE</th>
                    <th>Samples</th>
                  </tr>
                </thead>
                <tbody>
                  {eceRows.slice(0, 6).map(row => (
                    <tr key={`${row.model_id}-${row.model_name}`}>
                      <td title={row.model_name}>{(row.model_name.split('/').pop() || row.model_name)}</td>
                      <td>{row.ece.toFixed(3)}</td>
                      <td>{row.total}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div className="card">
          <div className="card-header">
            <h3>Confidence Distribution (Histogram)</h3>
            <span className="subtle">First model shown</span>
          </div>
          <Chart
            data={confidenceHistogram}
            title="Confidence Histogram (count)"
            xKey="label"
            valueKey="value"
            color="#00b894"
            xType="category"
          />
          {confidenceDist?.data?.[0]?.statistics && (
            <div className="stats-row">
              <div className="stat">
                <div className="stat-label">Mean confidence</div>
                <div className="stat-value">{confidenceDist.data[0].statistics.mean_confidence.toFixed(3)}</div>
              </div>
              <div className="stat">
                <div className="stat-label">Mode confidence</div>
                <div className="stat-value">{confidenceDist.data[0].statistics.mode_confidence.toFixed(3)}</div>
              </div>
            </div>
          )}
        </div>

        <div className="card">
          <div className="card-header">
            <h3>Brier Score (Top Groups)</h3>
            <span className="subtle">Lower is better</span>
          </div>
          <div className="table-wrapper">
            <table className="simple-table">
              <thead>
                <tr>
                  <th>Group</th>
                  <th>Brier</th>
                  <th>Skill</th>
                  <th>Base rate</th>
                </tr>
              </thead>
              <tbody>
                {brierRows.map((r, idx) => (
                  <tr key={`${r.group_name}-${idx}`}>
                    <td title={r.group_name}>{(r.group_name?.split?.('/')?.pop?.() || r.group_name)}</td>
                    <td>{r.brier_score.toFixed(3)}</td>
                    <td>{(r.skill_score ?? 0).toFixed(3)}</td>
                    <td>{(r.base_rate ?? 0).toFixed(3)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {summary?.data && (
        <div className="summary-section">
          <h3>Calibration Summary</h3>
          <div className="table-wrapper">
            <table className="simple-table">
              <thead>
                <tr>
                  <th>Model</th>
                  <th>ECE</th>
                  <th>Brier</th>
                  <th>Confidence gap</th>
                  <th>Acceptance rate</th>
                </tr>
              </thead>
              <tbody>
                {summary.data.slice(0, 10).map((m) => (
                  <tr key={m.model_id}>
                    <td title={m.model_name}>{(m.model_name.split('/').pop() || m.model_name)}</td>
                    <td>{(eceByModel[m.model_id] ?? 0).toFixed(3)}</td>
                    <td>{(m.brier_score ?? 0).toFixed(3)}</td>
                    <td>{Math.round((m.confidence_gap ?? 0) * 100)}%</td>
                    <td>{Math.round((m.acceptance_rate ?? 0) * 100)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
};

export default CalibrationAnalytics;
