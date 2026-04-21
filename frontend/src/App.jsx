import { useState, useEffect, useRef, useCallback } from 'react';
import { useWebSocket } from './hooks/useWebSocket';
import './index.css';

const STAT_KEYS = [
  { key: 'downloaded', label: 'Downloaded', color: 'bg-green-600' },
  { key: 'skipped', label: 'Skipped', color: 'bg-slate-600' },
  { key: 'nopdf', label: 'No PDF', color: 'bg-yellow-600' },
  { key: 'notfound', label: 'Not Found', color: 'bg-slate-600' },
  { key: 'captcha', label: 'Captcha Fail', color: 'bg-orange-600' },
  { key: 'captcha_retries', label: 'Captcha Retries', color: 'bg-slate-700', noDrilldown: true },
  { key: 'errors', label: 'Errors', color: 'bg-red-600' },
  { key: 'total', label: 'Total', color: 'bg-blue-600', noDrilldown: true },
];

const LOG_COLORS = {
  success: 'text-green-400',
  warning: 'text-yellow-400',
  error: 'text-red-400',
  info: 'text-slate-300',
  dim: 'text-slate-500',
};

const TAG_COLORS = {
  downloaded: 'text-green-400',
  skipped: 'text-cyan-400',
  nopdf: 'text-yellow-400',
  notfound: 'text-slate-500',
  captcha: 'text-purple-400',
  errors: 'text-red-400',
};

const TAG_LABELS = {
  downloaded: 'OK',
  skipped: 'SKIP',
  nopdf: 'NO-PDF',
  notfound: 'MISS',
  captcha: 'CAPTCHA',
  errors: 'ERR',
};

export default function App() {
  const [config, setConfig] = useState(null);
  const [runState, setRunState] = useState('idle');
  const [stats, setStats] = useState({});
  const [logs, setLogs] = useState([]);
  const [drilldown, setDrilldown] = useState(null);
  const [drilldownData, setDrilldownData] = useState([]);
  const [configOpen, setConfigOpen] = useState(true);
  const logRef = useRef(null);

  // Form state
  const [form, setForm] = useState({
    benches: [],
    app_type: 'ITA',
    years: '2011-2026',
    start_number: 1,
    max_number: 10000,
    rate_per_hour: '',
    max_workers: 50,
    max_consecutive_missing: 20,
    captcha_retries: 5,
    pipeline_retries: 3,
    model_size: 'large-v3-turbo',
    device: 'auto',
    captcha_refetch: true,
    out_dir: './downloads',
  });

  // Fetch config on mount
  useEffect(() => {
    fetch('/api/config').then(r => r.json()).then(setConfig).catch(() => {});
    fetch('/api/status').then(r => r.json()).then(data => {
      setRunState(data.state || 'idle');
      if (data.stats) setStats(data.stats);
    }).catch(() => {});
    fetch('/api/logs?limit=500').then(r => r.json()).then(data => {
      if (Array.isArray(data)) setLogs(data);
    }).catch(() => {});
  }, []);

  // WebSocket handler
  const handleWS = useCallback((msg) => {
    if (msg.stats) setStats(msg.stats);
    if (msg.state) setRunState(msg.state);
    if (msg.log) {
      setLogs(prev => {
        const next = [...prev, msg.log];
        return next.length > 1500 ? next.slice(-1000) : next;
      });
    }
  }, []);

  const { connected } = useWebSocket(handleWS);

  // Auto-scroll log
  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [logs]);

  // Parse years string
  function parseYears(spec) {
    spec = spec.trim();
    if (spec.includes(',')) return spec.split(',').map(s => parseInt(s.trim())).filter(n => !isNaN(n));
    if (spec.includes('-')) {
      const [a, b] = spec.split('-').map(s => parseInt(s.trim()));
      const lo = Math.min(a, b), hi = Math.max(a, b);
      return Array.from({ length: hi - lo + 1 }, (_, i) => lo + i);
    }
    const n = parseInt(spec);
    return isNaN(n) ? [] : [n];
  }

  async function handleStart() {
    const body = {
      ...form,
      years: parseYears(form.years),
      rate_per_hour: form.rate_per_hour ? parseInt(form.rate_per_hour) : null,
      start_number: parseInt(form.start_number),
      max_number: parseInt(form.max_number),
      max_workers: parseInt(form.max_workers),
      max_consecutive_missing: parseInt(form.max_consecutive_missing),
      captcha_retries: parseInt(form.captcha_retries),
      pipeline_retries: parseInt(form.pipeline_retries),
    };
    try {
      const res = await fetch('/api/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (data.error) alert(data.error);
      else setConfigOpen(false);
    } catch (e) {
      alert('Failed to start: ' + e.message);
    }
  }

  async function handleStop() {
    await fetch('/api/stop', { method: 'POST' });
  }

  async function handlePause() {
    await fetch('/api/pause', { method: 'POST' });
  }

  async function handleDrilldown(category) {
    setDrilldown(category);
    try {
      const res = await fetch(`/api/results/${category}`);
      const data = await res.json();
      setDrilldownData(data);
    } catch {
      setDrilldownData([]);
    }
  }

  function toggleBench(bench) {
    setForm(prev => ({
      ...prev,
      benches: prev.benches.includes(bench)
        ? prev.benches.filter(b => b !== bench)
        : [...prev.benches, bench],
    }));
  }

  function selectAllBenches() {
    if (!config) return;
    setForm(prev => ({
      ...prev,
      benches: prev.benches.length === config.benches.length ? [] : [...config.benches],
    }));
  }

  const isRunning = runState === 'running';
  const isPaused = runState === 'paused';
  const isIdle = runState === 'idle';

  return (
    <div className="h-screen flex flex-col overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2 bg-slate-800 border-b border-slate-700">
        <div className="flex items-center gap-3">
          <h1 className="text-lg font-bold text-sky-400">ITAT Scraper</h1>
          <span className={`text-xs px-2 py-0.5 rounded ${connected ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300'}`}>
            {connected ? 'LIVE' : 'DISCONNECTED'}
          </span>
          <span className={`text-xs px-2 py-0.5 rounded ${
            isRunning ? 'bg-green-900 text-green-300' : isPaused ? 'bg-yellow-900 text-yellow-300' : 'bg-slate-700 text-slate-400'
          }`}>
            {runState.toUpperCase()}
          </span>
        </div>
        <div className="flex gap-2">
          <button onClick={handleStart} disabled={!isIdle}
            className="px-3 py-1 text-sm rounded bg-green-600 hover:bg-green-500 disabled:opacity-30 disabled:cursor-not-allowed">
            Start
          </button>
          <button onClick={handlePause} disabled={isIdle}
            className="px-3 py-1 text-sm rounded bg-yellow-600 hover:bg-yellow-500 disabled:opacity-30 disabled:cursor-not-allowed">
            {isPaused ? 'Resume' : 'Pause'}
          </button>
          <button onClick={handleStop} disabled={isIdle}
            className="px-3 py-1 text-sm rounded bg-red-600 hover:bg-red-500 disabled:opacity-30 disabled:cursor-not-allowed">
            Stop
          </button>
          <button onClick={() => setConfigOpen(!configOpen)}
            className="px-3 py-1 text-sm rounded bg-slate-600 hover:bg-slate-500">
            {configOpen ? 'Hide Config' : 'Show Config'}
          </button>
        </div>
      </div>

      {/* Config panel */}
      {configOpen && config && (
        <div className="bg-slate-800/50 border-b border-slate-700 px-4 py-3 overflow-y-auto max-h-[45vh]">
          <div className="grid grid-cols-[250px_1fr] gap-4">
            {/* Benches */}
            <div>
              <div className="flex items-center justify-between mb-1">
                <label className="text-xs text-slate-400">Benches</label>
                <button onClick={selectAllBenches} className="text-xs text-sky-400 hover:text-sky-300">
                  {form.benches.length === config.benches.length ? 'Deselect All' : 'Select All'}
                </button>
              </div>
              <div className="h-48 overflow-y-auto border border-slate-600 rounded p-1 space-y-0.5">
                {config.benches.map(b => (
                  <label key={b} className="flex items-center gap-2 px-1 py-0.5 hover:bg-slate-700 rounded cursor-pointer text-xs">
                    <input type="checkbox" checked={form.benches.includes(b)} onChange={() => toggleBench(b)}
                      className="rounded border-slate-500" />
                    {b}
                  </label>
                ))}
              </div>
            </div>

            {/* Fields */}
            <div className="grid grid-cols-4 gap-3 text-xs">
              <Field label="Appeal Type">
                <select value={form.app_type} onChange={e => setForm({ ...form, app_type: e.target.value })}
                  className="w-full bg-slate-700 border border-slate-600 rounded px-2 py-1.5 text-xs">
                  {Object.entries(config.appeal_types).map(([k, v]) => (
                    <option key={k} value={k}>{k} - {v}</option>
                  ))}
                </select>
              </Field>
              <Field label="Years">
                <input value={form.years} onChange={e => setForm({ ...form, years: e.target.value })}
                  className="w-full bg-slate-700 border border-slate-600 rounded px-2 py-1.5 text-xs"
                  placeholder="2011-2026" />
              </Field>
              <Field label="Start #">
                <input type="number" value={form.start_number} onChange={e => setForm({ ...form, start_number: e.target.value })}
                  className="w-full bg-slate-700 border border-slate-600 rounded px-2 py-1.5 text-xs" />
              </Field>
              <Field label="Max # per year">
                <input type="number" value={form.max_number} onChange={e => setForm({ ...form, max_number: e.target.value })}
                  className="w-full bg-slate-700 border border-slate-600 rounded px-2 py-1.5 text-xs" />
              </Field>
              <Field label="Rate limit (appeals/hr)">
                <input value={form.rate_per_hour} onChange={e => setForm({ ...form, rate_per_hour: e.target.value })}
                  className="w-full bg-slate-700 border border-slate-600 rounded px-2 py-1.5 text-xs"
                  placeholder="unlimited" />
              </Field>
              <Field label="Parallel Workers">
                <input type="number" value={form.max_workers} onChange={e => setForm({ ...form, max_workers: e.target.value })}
                  className="w-full bg-slate-700 border border-slate-600 rounded px-2 py-1.5 text-xs" />
              </Field>
              <Field label="Whisper Model">
                <select value={form.model_size} onChange={e => setForm({ ...form, model_size: e.target.value })}
                  className="w-full bg-slate-700 border border-slate-600 rounded px-2 py-1.5 text-xs">
                  {config.models.map(m => (
                    <option key={m.value} value={m.value}>
                      {m.label} {m.cached ? '(cached)' : ''}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Device">
                <select value={form.device} onChange={e => setForm({ ...form, device: e.target.value })}
                  className="w-full bg-slate-700 border border-slate-600 rounded px-2 py-1.5 text-xs">
                  {config.devices.map(d => (
                    <option key={d} value={d}>{d}</option>
                  ))}
                </select>
              </Field>
              <Field label="Consecutive miss limit">
                <input type="number" value={form.max_consecutive_missing} onChange={e => setForm({ ...form, max_consecutive_missing: e.target.value })}
                  className="w-full bg-slate-700 border border-slate-600 rounded px-2 py-1.5 text-xs" />
              </Field>
              <Field label="Captcha retries">
                <input type="number" value={form.captcha_retries} onChange={e => setForm({ ...form, captcha_retries: e.target.value })}
                  className="w-full bg-slate-700 border border-slate-600 rounded px-2 py-1.5 text-xs" />
              </Field>
              <Field label="Pipeline retries">
                <input type="number" value={form.pipeline_retries} onChange={e => setForm({ ...form, pipeline_retries: e.target.value })}
                  className="w-full bg-slate-700 border border-slate-600 rounded px-2 py-1.5 text-xs" />
              </Field>
              <Field label="Download folder">
                <input value={form.out_dir} onChange={e => setForm({ ...form, out_dir: e.target.value })}
                  className="w-full bg-slate-700 border border-slate-600 rounded px-2 py-1.5 text-xs" />
              </Field>
              <div className="flex items-end">
                <label className="flex items-center gap-2 cursor-pointer text-xs">
                  <input type="checkbox" checked={form.captcha_refetch}
                    onChange={e => setForm({ ...form, captcha_refetch: e.target.checked })} />
                  Auto-refetch captcha
                </label>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Stats bar */}
      <div className="flex gap-1 px-4 py-2 bg-slate-800/30 border-b border-slate-700 flex-wrap">
        {STAT_KEYS.map(({ key, label, color, noDrilldown }) => (
          <button key={key}
            onClick={() => !noDrilldown && handleDrilldown(key)}
            className={`px-3 py-1 rounded text-xs font-mono ${color} ${
              drilldown === key ? 'ring-2 ring-white' : ''
            } ${noDrilldown ? 'cursor-default' : 'hover:brightness-125 cursor-pointer'}`}>
            {label}: {stats[key] || 0}
          </button>
        ))}
        <button onClick={() => { setDrilldown(null); setDrilldownData([]); }}
          className="px-3 py-1 rounded text-xs bg-slate-600 hover:bg-slate-500">
          Show All
        </button>
      </div>

      {/* Main content: table + log */}
      <div className="flex-1 flex min-h-0">
        {/* Results table */}
        <div className="flex-1 flex flex-col border-r border-slate-700 min-w-0">
          <div className="px-3 py-1 text-xs text-sky-400 font-bold border-b border-slate-700">
            Results {drilldown ? `(${TAG_LABELS[drilldown] || drilldown})` : '(Latest)'}
          </div>
          <div className="flex-1 overflow-auto">
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-slate-800">
                <tr className="text-left text-slate-400">
                  <th className="px-2 py-1">Bench</th>
                  <th className="px-2 py-1">Year</th>
                  <th className="px-2 py-1">Appeal</th>
                  <th className="px-2 py-1">Status</th>
                  <th className="px-2 py-1">Parties</th>
                  <th className="px-2 py-1">Tries</th>
                  <th className="px-2 py-1">Note</th>
                </tr>
              </thead>
              <tbody>
                {drilldownData.map((r, i) => {
                  const cat = r.category || drilldown || 'errors';
                  return (
                    <tr key={i} className={`border-b border-slate-700/50 ${i % 2 ? 'bg-slate-800/30' : ''}`}>
                      <td className="px-2 py-1">{r.bench}</td>
                      <td className="px-2 py-1">{r.year}</td>
                      <td className="px-2 py-1">{r.number}</td>
                      <td className={`px-2 py-1 font-bold ${TAG_COLORS[cat] || 'text-slate-400'}`}>
                        {TAG_LABELS[cat] || cat}
                      </td>
                      <td className="px-2 py-1 max-w-[200px] truncate">{r.parties}</td>
                      <td className="px-2 py-1">{r.attempts}</td>
                      <td className="px-2 py-1 max-w-[300px] truncate text-slate-400">{r.note}</td>
                    </tr>
                  );
                })}
                {drilldownData.length === 0 && (
                  <tr><td colSpan={7} className="px-2 py-8 text-center text-slate-500">
                    {drilldown ? 'No results in this category' : 'Click a stat button to drill down, or start a run'}
                  </td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        {/* Log panel */}
        <div className="w-[45%] flex flex-col min-w-0">
          <div className="px-3 py-1 text-xs text-yellow-400 font-bold border-b border-slate-700">
            Log
          </div>
          <div ref={logRef} className="flex-1 overflow-auto px-3 py-1 font-mono text-xs leading-5">
            {logs.map((log, i) => (
              <div key={i} className={LOG_COLORS[log.level] || 'text-slate-300'}>
                {log.message}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }) {
  return (
    <div>
      <label className="block text-slate-400 mb-0.5">{label}</label>
      {children}
    </div>
  );
}
