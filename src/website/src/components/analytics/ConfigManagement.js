import React, { useEffect, useMemo, useState } from 'react';
import {
  listConfigs,
  getConfigById,
  createConfigApi,
  updateConfigApi,
  deleteConfigApi,
  getLanguagesMapping,
  getAvailableModels,
  validateHuggingFaceModel,
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

const getConfigRoot = (parsed) => {
  if (parsed && typeof parsed === 'object') {
    if (parsed.config && typeof parsed.config === 'object') return parsed.config;
  }
  return parsed || {};
};

const normalizeParsedToForm = (parsedRoot, languagesMapping) => {
  const safe = (v, d) => (v === undefined || v === null ? d : v);
  const server = safe(parsedRoot.server, defaultServer);
  const auth = safe(parsedRoot.auth, defaultAuth);
  const models = safe(parsedRoot.models, defaultModels);
  const modules = safe(parsedRoot.modules, emptyModulesTemplate().modules);
  return {
    modules,
    server,
    auth,
    models,
    // Always enforce static languages from backend
    languages: languagesMapping || {},
  };
};

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

function ModuleCard({ mod, onChange, onRemove }) {
  const [expanded, setExpanded] = useState(true);

  const updateField = (field, value) => onChange({ ...mod, [field]: value });
  const updateSubmodule = (idx, value) => {
    const submodules = [...(mod.submodules || [])];
    submodules[idx] = value; onChange({ ...mod, submodules });
  };
  const addSubmodule = () => onChange({ ...mod, submodules: [...(mod.submodules || []), { id: '', class: '', name: '', type: '', description: '', enabled: true }] });
  const removeSubmodule = (idx) => {
    const submodules = (mod.submodules || []).filter((_, i) => i !== idx);
    onChange({ ...mod, submodules });
  };

  const updateDependency = (idx, value) => {
    const dependencies = [...(mod.dependencies || [])];
    dependencies[idx] = value; onChange({ ...mod, dependencies });
  };
  const addDependency = () => onChange({ ...mod, dependencies: [...(mod.dependencies || []), { moduleId: '', isHard: false }] });
  const removeDependency = (idx) => onChange({ ...mod, dependencies: (mod.dependencies || []).filter((_, i) => i !== idx) });

  return (
    <div className="module-card">
      <div className="module-header" onClick={() => setExpanded(!expanded)}>
        <div className="module-title">{mod.name || mod.id || 'New Module'}</div>
        <div className="module-actions">
          <button className="danger" onClick={(e) => { e.stopPropagation(); onRemove(); }}>Delete</button>
          <button onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); }}>{expanded ? 'Collapse' : 'Expand'}</button>
        </div>
      </div>
      {expanded && (
        <div className="module-body">
          <div className="grid-2">
            <label>Id<input value={mod.id || ''} onChange={(e) => updateField('id', e.target.value)} placeholder="BehavioralTelemetryAggregator"/></label>
            <label>Class<input value={mod.class || ''} onChange={(e) => updateField('class', e.target.value)} placeholder="me.code4me.services.modules.aggregators.BaseBehavioralTelemetryAggregator"/></label>
            <label>Name<input value={mod.name || ''} onChange={(e) => updateField('name', e.target.value)} placeholder="Behavioral Telemetry Aggregator"/></label>
            <label>Type<input value={mod.type || ''} onChange={(e) => updateField('type', e.target.value)} placeholder="aggregator / telemetry / model / context / afterInsertion"/></label>
          </div>
          <label>Description<textarea value={mod.description || ''} onChange={(e) => updateField('description', e.target.value)} placeholder="Module description"/></label>
          <label className="inline">Enabled<input type="checkbox" checked={!!mod.enabled} onChange={(e) => updateField('enabled', e.target.checked)} /></label>

          <div className="section">
            <div className="section-header">
              <h4>Submodules</h4>
              <button onClick={addSubmodule}>+ Add submodule</button>
            </div>
            {(mod.submodules || []).map((sm, idx) => (
              <div className="submodule-card" key={idx}>
                <div className="grid-2">
                  <label>Id<input value={sm.id || ''} onChange={(e) => updateSubmodule(idx, { ...sm, id: e.target.value })}/></label>
                  <label>Class<input value={sm.class || ''} onChange={(e) => updateSubmodule(idx, { ...sm, class: e.target.value })}/></label>
                  <label>Name<input value={sm.name || ''} onChange={(e) => updateSubmodule(idx, { ...sm, name: e.target.value })}/></label>
                  <label>Type<input value={sm.type || ''} onChange={(e) => updateSubmodule(idx, { ...sm, type: e.target.value })}/></label>
                </div>
                <label>Description<textarea value={sm.description || ''} onChange={(e) => updateSubmodule(idx, { ...sm, description: e.target.value })}/></label>
                <div className="submodule-actions">
                  <label className="inline">Enabled<input type="checkbox" checked={!!sm.enabled} onChange={(e) => updateSubmodule(idx, { ...sm, enabled: e.target.checked })} /></label>
                  <button className="danger" onClick={() => removeSubmodule(idx)}>Remove</button>
                </div>
              </div>
            ))}
          </div>

          <div className="section">
            <div className="section-header">
              <h4>Dependencies</h4>
              <button onClick={addDependency}>+ Add dependency</button>
            </div>
            {(mod.dependencies || []).map((dep, idx) => (
              <div className="dependency-row" key={idx}>
                <label>Module Id<input value={dep.moduleId || ''} onChange={(e) => updateDependency(idx, { ...dep, moduleId: e.target.value })} /></label>
                <label className="inline">Hard?
                  <input type="checkbox" checked={!!dep.isHard} onChange={(e) => updateDependency(idx, { ...dep, isHard: e.target.checked })} />
                </label>
                <button className="danger" onClick={() => removeDependency(idx)}>Remove</button>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default function ConfigManagement({ user }) {
  const [configs, setConfigs] = useState([]);
  const [selectedId, setSelectedId] = useState(null);

  // Form builder state (structured object)
  const [configObj, setConfigObj] = useState({
    ...emptyModulesTemplate(),
    server: defaultServer,
    auth: defaultAuth,
    models: defaultModels,
    languages: {},
  });

  // HOCON/raw editing state (Form Builder currently disabled; HOCON is the only active mode)
  const [editMode, setEditMode] = useState('hocon'); // 'hocon' only (Form Builder coming soon)
  const [hoconText, setHoconText] = useState('');
  // const [lockedToHocon, setLockedToHocon] = useState(false); // Form builder disabled for now

  const [languages, setLanguages] = useState({ list: [], mapping: {} });
  const [availableModels, setAvailableModels] = useState([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [parseWarning, setParseWarning] = useState('');

  // Build the canonical payload from the form builder (used for preview and saving)
  const formPayload = useMemo(() => {
    // Clean models: prevent manual ID entry; only include id when chosen from DB
    const cleanedModels = (configObj.models.available || []).map((m) => {
      const base = { name: m.name || '', isChatModel: !!m.isChatModel, isDefault: !!m.isDefault };
      if (m.id !== undefined && m.id !== null && String(m.id).trim() !== '') {
        base.id = Number(m.id);
      }
      return base;
    });
    return {
      modules: configObj.modules,
      server: configObj.server,
      auth: configObj.auth,
      models: { available: cleanedModels, systemPrompt: configObj.models.systemPrompt || '' },
      // Enforce static languages mapping from backend
      languages: languages.mapping || {},
    };
  }, [configObj, languages]);

  const hoconPreview = useMemo(() => objectToHocon(formPayload), [formPayload]);

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
          setAvailableModels(models.data || []);
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

  const addModule = () => {
    setConfigObj((prev) => ({
      ...prev,
      modules: {
        ...prev.modules,
        available: [
          ...(prev.modules.available || []),
          { id: '', class: '', name: '', type: '', description: '', enabled: true, submodules: [], dependencies: [] },
        ],
      },
    }));
  };

  const updateModule = (idx, value) => {
    const updated = [...(configObj.modules.available || [])];
    updated[idx] = value;
    setConfigObj((prev) => ({ ...prev, modules: { ...prev.modules, available: updated } }));
  };

  const removeModule = (idx) => {
    setConfigObj((prev) => ({
      ...prev,
      modules: { ...prev.modules, available: (prev.modules.available || []).filter((_, i) => i !== idx) },
    }));
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
