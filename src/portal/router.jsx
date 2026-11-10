// Path-based routing for the portal screens. The classic dashboard keeps its hash routes at "/".
import {useEffect, useState} from 'react';

export const PORTAL_ROUTES = [
  {path: '/paper', key: 'paper', title: 'Paper desk'},
  {path: '/strategy-tester', key: 'tester', title: 'Strategy tester'},
  {path: '/strategy-comparison', key: 'compare', title: 'Compare'},
  {path: '/performance', key: 'performance', title: 'Performance'},
];

const SYMBOL = /^[A-Z0-9&_.-]{1,80}$/;

export function isPortalPath(pathname) {
  return PORTAL_ROUTES.some(r => pathname === r.path || pathname === r.path + '/') || /^\/stock\/[^/]+\/?$/.test(pathname);
}

export function parseRoute(pathname, search = '') {
  const params = new URLSearchParams(search);
  const stock = /^\/stock\/([^/]+)\/?$/.exec(pathname);
  if (stock) {
    let symbol = '';
    try { symbol = decodeURIComponent(stock[1]).toUpperCase(); } catch { symbol = ''; }
    return {key: 'stock', symbol: SYMBOL.test(symbol) ? symbol : null, runId: parseRunId(params.get('runId')), params};
  }
  const match = PORTAL_ROUTES.find(r => pathname === r.path || pathname === r.path + '/');
  return {key: match ? match.key : 'paper', params};
}

// Runs are addressed as "<kind>-<date>-s<strategy>":
//   hist-2026-11-06-s3  saved backtest export (public/data), as-of date of the export
//   scan-2026-11-09-s3  live completed-bar qualifier scan stored in Turso, bar date of the scan
export function runIdFor(asOf, strategyId, kind = 'hist') { return `${kind}-${asOf}-s${strategyId}`; }
export function parseRunId(value) {
  const m = /^(hist|scan)-(\d{4}-\d{2}-\d{2})-s([1-7])$/.exec(value ?? '');
  return m ? {raw: value, kind: m[1], asOf: m[2], strategyId: Number(m[3])} : null;
}

export function stockHref(symbol, runId) {
  const base = '/stock/' + encodeURIComponent(String(symbol).replace(/-EQ$/, ''));
  return runId ? `${base}?runId=${encodeURIComponent(runId)}` : base;
}

export function navigate(href) {
  window.history.pushState(null, '', href);
  window.dispatchEvent(new PopStateEvent('popstate'));
  window.scrollTo?.({top: 0});
}

export function useLocation() {
  const read = () => ({pathname: window.location.pathname, search: window.location.search});
  const [loc, setLoc] = useState(read);
  useEffect(() => {
    const sync = () => setLoc(read());
    window.addEventListener('popstate', sync);
    return () => window.removeEventListener('popstate', sync);
  }, []);
  return loc;
}

// Plain anchor that keeps navigation inside the SPA without a full reload.
export function Link({href, children, className, ...rest}) {
  return <a href={href} className={className} onClick={e => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    navigate(href);
  }} {...rest}>{children}</a>;
}
