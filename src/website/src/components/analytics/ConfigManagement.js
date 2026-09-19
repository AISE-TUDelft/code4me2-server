import React, { useEffect, useState } from 'react';
import {
  listConfigs,
  getConfigById,
  createConfigApi,
  updateConfigApi,
  deleteConfigApi,
  getLanguagesMapping,
  getAvailableModels,
} from '../../utils/api';
import HOCON from 'hocon-parser';
import './ConfigManagement.css';

const emptyModulesTemplate = () => ({
  modules: {
    available: [],
    categories: {
      behavioralTelemetry: { path: 'me.code4me.services.modules.telemetry.behavioral', description: 'Modules for behavioral telemetry collection' },
      contextualTelemetry: { path: 'me.code4me.services.modules.telemetry.contextual', description: 'Modules for contextual telemetry collection' },
      context: { path: 'me.code4me.services.modules.context', description: 'Modules for context retrieval' },
      aggregators: { path: 'me.code4me.services.modules.aggregators', description: 'Modules for data aggregation' },
      models: { path: 'me.code4me.services.modules.model', description: 'Modules for model selection and settings' },
      afterInsertion: { path: 'me.code4me.services.modules.afterInsertion', description: 'Modules for actions after code insertion' },
    },
  },
});

const defaultServer = { host: 'http://127.0.0.1', port: 8008, contextPath: '', timeout: 5000 };
const defaultAuth = { google: { clientId: '' } };
const defaultModels = { available: [], systemPrompt: '' };

// Simple HOCON serializer (object -> HOCON string)
const _escapeString = (s) => String(s).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
const _indent = (n) => '  '.repeat(n);
const _isPlainObject = (v) => v && typeof v === 'object' && !Array.isArray(v);

// Quote keys that contain spaces or special characters (for valid HOCON)
const _isSafeKey = (k) => /^[A-Za-z0-9_.-]+$/.test(String(k));
const _formatKey = (k) => (_isSafeKey(k) ? String(k) : `"${_escapeString(k)}"`);

const hoconStringify = (value, indent = 0) => {
  if (value === null || value === undefined) return 'null';
  if (typeof value === 'string') return `"${_escapeString(value)}"`;
  if (typeof value === 'number' || typeof value === 'bigint') return String(value);
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (Array.isArray(value)) {
    if (value.length === 0) return '[]';
    const items = value.map((v) => `${_indent(indent + 1)}${hoconStringify(v, indent + 1)}`).join(',\n');
    return `[\n${items}\n${_indent(indent)}]`;
  }
  if (_isPlainObject(value)) {
    const keys = Object.keys(value);
    if (keys.length === 0) return '{}';
    const lines = keys.map((k) => {
      const v = value[k];
      if (_isPlainObject(v)) {
        // key { ... }
        return `${_indent(indent)}${_formatKey(k)} ${hoconStringify(v, indent + 1)}`;
      } else {
        // key = value
        return `${_indent(indent)}${_formatKey(k)} = ${hoconStringify(v, indent + 1)}`;
      }
    });
    return `{\n${lines.join('\n')}\n${_indent(indent)}}`;
  }
  // Fallback
  return JSON.stringify(value);
};

// Always wrap the generated HOCON under a top-level "config { ... }" block
const objectToHocon = (obj) => {
  const root = _isPlainObject(obj) ? obj : { value: obj };
  return hoconStringify({ config: root }, 0);
};

// HOCON starter template and schema guide
const STARTER_HOCON = `config {
  modules {
    available = []
    categories = {
      behavioralTelemetry = { path = "me.code4me.services.modules.telemetry.behavioral" description = "Modules for behavioral telemetry collection" }
      contextualTelemetry = { path = "me.code4me.services.modules.telemetry.contextual" description = "Modules for contextual telemetry collection" }
      context = { path = "me.code4me.services.modules.context" description = "Modules for context retrieval" }
      aggregators = { path = "me.code4me.services.modules.aggregators" description = "Modules for data aggregation" }
      models = { path = "me.code4me.services.modules.model" description = "Modules for model selection and settings" }
      afterInsertion = { path = "me.code4me.services.modules.afterInsertion" description = "Modules for actions after code insertion" }
    }
  }
  server { host = "http://127.0.0.1" port = 8008 contextPath = "" timeout = 5000 }
  auth { google { clientId = "" } }
  models { available = [] systemPrompt = "" }
  // languages: static, provided by the system (you can omit this block)
}`;

const SCHEMA_GUIDE = `config {
  // module configuration
  modules {
    // List of available modules
    available = [
      {
        id = "BehavioralTelemetryAggregator"
        class = "me.code4me.services.modules.aggregators.BaseBehavioralTelemetryAggregator"
        name = "Behavioral Telemetry Aggregator"
        type = "aggregator"
        description = "Module for aggregating behavioral telemetry data"
        enabled = true
        // Example of submodules
        submodules = [
          {
            id = "TimeSinceLastAcceptedCompletion"
            class = "me.code4me.services.modules.telemetry.behavioral.TimeSinceLastAcceptedCompletion"
            name = "Time Since Last Accepted Completion"
            type = "telemetry"
            description = "Calculates the time since the last accepted completion"
            enabled = true
          }
        ]
        // Example of dependencies
        dependencies = [
          { moduleId = "TimeSinceLastAcceptedCompletion" isHard = false }
        ]
      }
    ]

    // Module categories
    categories = {
      behavioralTelemetry = {
        path = "me.code4me.services.modules.telemetry.behavioral"
        description = "Modules for behavioral telemetry collection"
      }
      contextualTelemetry = {
        path = "me.code4me.services.modules.telemetry.contextual"
        description = "Modules for contextual telemetry collection"
      }
      context = {
        path = "me.code4me.services.modules.context"
        description = "Modules for context retrieval"
      }
      aggregators = {
        path = "me.code4me.services.modules.aggregators"
        description = "Modules for data aggregation"
      }
      models = {
        path = "me.code4me.services.modules.model"
        description = "Modules for model selection and settings"
      }
      afterInsertion = {
        path = "me.code4me.services.modules.afterInsertion"
        description = "Modules for actions after code insertion"
      }
    }
  }

  // Server Settings
  server { host = "http://127.0.0.1" port = 8008 contextPath = "" timeout = 5000 }

  // Authentication Settings
  auth { google { clientId = "" } }

  // Model configuration
  models {
    available = [
      { name = "Ministral-8B-Instruct" isChatModel = true isDefault = true }
      { name = "Mellum-4b-base" isChatModel = false isDefault = false }
    ]
    systemPrompt = "You are a helpful assistant..."
  }

  // Languages are static and provided by the system; this block is optional
  // languages { ... }
}`;



const parseHoconSafe = (text) => {
  try {
    if (!text || !text.trim()) return {};
    // hocon-parser exposes parse()
    if (HOCON && typeof HOCON.parse === 'function') {
      return HOCON.parse(text);
    }
  } catch (_) {
    // swallow
  }
  throw new Error('Failed to parse HOCON');
};


export default function ConfigManagement({ user }) {
  const [configs, setConfigs] = useState([]);
  const [selectedId, setSelectedId] = useState(null);

  // HOCON/raw editing state (Form Builder currently disabled; HOCON is the only active mode)
  const [editMode, setEditMode] = useState('hocon'); // 'hocon' only (Form Builder coming soon)
  const [hoconText, setHoconText] = useState('');
  // const [lockedToHocon, setLockedToHocon] = useState(false); // Form builder disabled for now

  const [languages, setLanguages] = useState({ list: [], mapping: {} });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [parseWarning, setParseWarning] = useState('');



  // Form builder disabled: previously synced HOCON from form here
  // useEffect(() => {
  //   if (editMode === 'form') {
  //     setHoconText(hoconPreview);
  //   }
  // }, [hoconPreview, editMode]);

  // Validate HOCON syntax (debounced)
  useEffect(() => {
    if (editMode !== 'hocon') return;
    const t = setTimeout(() => {
      try {
        parseHoconSafe(hoconText);
        setParseWarning('');
      } catch (e) {
        setParseWarning('Cannot parse HOCON. Please fix syntax.');
      }
    }, 250);
    return () => clearTimeout(t);
  }, [hoconText, editMode]);

  useEffect(() => {
    if (!user?.is_admin) return;
    const load = async () => {
      setLoading(true);
      try {
        const [cfgs, langs, models] = await Promise.all([
          listConfigs(),
          getLanguagesMapping(),
          getAvailableModels(),
        ]);
        if (cfgs.ok) {
          // Include all configs; HOCON is stored as string
          setConfigs(cfgs.data || []);
        }
        if (langs.ok) setLanguages({ list: langs.data.languages, mapping: langs.data.mapping });
        if (models.ok !== false) {
        }
      } catch (e) {
        console.error(e);
        setError('Failed to load data');
      } finally {
        setLoading(false);
      }
    };
    load();
  }, [user]);

  const selectConfig = async (id) => {
    setSelectedId(id);
    const res = await getConfigById(id);
    if (res.ok) {
      const data = res.data.config_data;
      setEditMode('hocon');
      if (typeof data === 'string') {
        setHoconText(data);
      } else {
        const obj = {
          modules: data?.modules || emptyModulesTemplate().modules,
          server: data?.server || defaultServer,
          auth: data?.auth || defaultAuth,
          models: data?.models || defaultModels,
          languages: languages.mapping || {},
        };
        setHoconText(objectToHocon(obj));
      }
    }
  };

  const newConfig = () => {
    setSelectedId(null);
    setEditMode('hocon');
    setHoconText(STARTER_HOCON);
    // setConfigObj({ ...emptyModulesTemplate(), server: defaultServer, auth: defaultAuth, models: defaultModels, languages: languages.mapping || {} }); // Form builder disabled
  };




  const saveConfig = async () => {
    setSaving(true);
    setError(null);
    try {
      // Build HOCON to send (Form Builder disabled)
      const bodyToSend = (hoconText && hoconText.trim()) ? hoconText : STARTER_HOCON;

      const apiCall = selectedId ? updateConfigApi : createConfigApi;
      const args = selectedId ? [selectedId, bodyToSend] : [bodyToSend];
      const res = await apiCall(...args);
      if (res.ok) {
        alert('Configuration saved successfully');
        // refresh list (include all since configs are HOCON strings)
        const cfgs = await listConfigs();
        if (cfgs.ok) {
          setConfigs(cfgs.data || []);
        }
        if (!selectedId && res.data?.config_id) setSelectedId(res.data.config_id);
      } else {
        setError(res.error || 'Failed to save config');
      }
    } catch (e) {
      console.error(e);
      setError('Unexpected error saving config');
    } finally {
      setSaving(false);
    }
  };

  const deleteConfig = async (id) => {
    if (!window.confirm('Delete this configuration?')) return;
    const res = await deleteConfigApi(id);
    if (res.ok) {
      setConfigs((prev) => prev.filter((c) => c.config_id !== id));
      if (selectedId === id) newConfig();
    } else {
      alert(res.error || 'Failed to delete');
    }
  };

  if (!user?.is_admin) {
    return (
      <div className="config-mgmt">
        <div className="error">Admin privileges required.</div>
      </div>
    );
  }

  return (
    <div className="config-mgmt">
      <div className="sidebar">
        <div className="sidebar-header">
          <h3>Configurations</h3>
          <button onClick={newConfig}>+ New</button>
        </div>
        {loading ? (
          <div className="muted">Loading...</div>
        ) : (
          <ul className="config-list">
            {configs.map((c) => (
              <li key={c.config_id} className={selectedId === c.config_id ? 'active' : ''}>
                <button onClick={() => selectConfig(c.config_id)}>
                  <span className="cfg-id">#{c.config_id}</span>
                  <span className="cfg-summary">{typeof c.config_data === 'string' ? 'HOCON' : (c.config_data?.name || 'Configuration')}</span>
                </button>
                <button className="danger small" onClick={() => deleteConfig(c.config_id)}>Delete</button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="editor">
        <div className="editor-header">
          <h2>{selectedId ? `Edit Config #${selectedId}` : 'Create New Config'}</h2>
          <div className="actions">
            <div className="inline" style={{ gap: 6 }}>
              <span className="muted">Mode:</span>
              <button
                className="disabled-toggle"
                title="Form Builder is coming soon"
                disabled
              >Form Builder</button>
              <button
                className="primary"
                disabled
                title="HOCON Editor (active)"
              >HOCON Editor</button>
            </div>
            {error && <span className="error">{error}</span>}
            <button className="primary" disabled={saving} onClick={saveConfig}>{saving ? 'Saving...' : 'Save Config'}</button>
          </div>
        </div>

        <div className="panel">
          <h3>HOCON Editor</h3>
          <p className="muted">Edit the raw HOCON configuration. See the schema guide below for structure.</p>
          {parseWarning && <p className="warning-text">{parseWarning}</p>}
          <textarea value={hoconText} onChange={(e)=>setHoconText(e.target.value)} style={{ width: '100%', minHeight: '420px' }} />
        </div>
        <div className="panel schema-panel">
          <h3>Configuration Schema</h3>
          <p className="muted">Reference structure for composing a configuration. Comments starting with // are allowed.</p>
          <pre className="code-block"><code>{SCHEMA_GUIDE}</code></pre>
        </div>
        {/*
        Form Builder is currently disabled. Previous UI preserved for future reactivation.

        (Former Form Builder UI was rendered here.)
        */}

      </div>
    </div>
  );
}
