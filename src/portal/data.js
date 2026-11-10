// Read-only data hooks. Every number on the portal comes from /api/dashboard (Turso paper_v1)
// or from the saved research exports in /public/data. Nothing here invents values.
import {useEffect, useState} from 'react';

const staticCache = new Map();

export function loadStatic(path) {
  if (!staticCache.has(path)) {
    const p = fetch(path).then(async r => {
      if (!r.ok) throw new Error(`Saved research file unavailable (${path})`);
      return r.json();
    });
    p.catch(() => staticCache.delete(path));
    staticCache.set(path, p);
  }
  return staticCache.get(path);
}

export function clearStaticCache() { staticCache.clear(); }

export function useStatic(path) {
  const [state, setState] = useState({data: null, error: '', loading: Boolean(path)});
  useEffect(() => {
    if (!path) { setState({data: null, error: '', loading: false}); return undefined; }
    let active = true;
    setState(s => ({...s, loading: true, error: ''}));
    loadStatic(path)
      .then(data => active && setState({data, error: '', loading: false}))
      .catch(e => active && setState({data: null, error: e.message, loading: false}));
    return () => { active = false; };
  }, [path]);
  return state;
}

// Generic /api/dashboard reader. `params` must only contain keys the API accepts.
export function useDashboard(params, refreshKey = 0, enabled = true) {
  const query = new URLSearchParams({...params, refresh: '1'}).toString();
  const [state, setState] = useState({data: null, error: '', loading: enabled});
  useEffect(() => {
    if (!enabled) return undefined;
    const c = new AbortController();
    setState(s => ({...s, loading: true, error: ''}));
    fetch('/api/dashboard?' + query, {signal: c.signal, cache: 'no-store'})
      .then(async r => {
        const d = await r.json();
        if (!r.ok || d.available === false) throw new Error(d.error?.message ?? d.message ?? 'This read is unavailable.');
        return d;
      })
      .then(data => setState({data, error: '', loading: false}))
      .catch(e => { if (e.name !== 'AbortError') setState(s => ({data: s.data, error: e.message, loading: false})); });
    return () => c.abort();
  }, [query, refreshKey, enabled]);
  return state;
}

export const paiseToInr = v => (v === null || v === undefined || !Number.isFinite(Number(v)) ? null : Number(v) / 100);

// Candidate saved-research file names for a symbol typed in a URL (CUPID -> CUPID-EQ).
export function symbolCandidates(symbol) {
  if (!symbol) return [];
  return /-[A-Z]{2}$/.test(symbol) ? [symbol] : [symbol + '-EQ', symbol + '-BE', symbol];
}
