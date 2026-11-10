import {lazy, Suspense, useEffect, useState} from 'react';
import {Moon, RefreshCw, Sun, ShieldCheck, LayoutGrid} from 'lucide-react';
import {Link, PORTAL_ROUTES, parseRoute, useLocation} from './router';
import {useTheme} from './theme';
import {useDashboard} from './data';
import {istTime} from './format';
import {Callout, Loading} from './ui';
import './portal.css';

const PaperDesk = lazy(() => import('./PaperDesk'));
const StrategyTester = lazy(() => import('./StrategyTester'));
const StrategyComparison = lazy(() => import('./StrategyComparison'));
const Performance = lazy(() => import('./Performance'));
const StockDetail = lazy(() => import('./StockDetail'));

const TITLES = {paper: 'Paper desk', tester: 'Strategy tester', compare: 'Strategy comparison', performance: 'Performance', stock: 'Stock detail'};

export default function Portal() {
  const loc = useLocation();
  const route = parseRoute(loc.pathname, loc.search);
  const {theme, toggle} = useTheme();
  const [refreshKey, setRefreshKey] = useState(0);
  const overview = useDashboard({view: 'overview'}, refreshKey);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), 30000); return () => clearInterval(id); }, []);
  useEffect(() => { const id = setInterval(() => setRefreshKey(k => k + 1), 300000); return () => clearInterval(id); }, []);
  useEffect(() => { document.title = `${TITLES[route.key]} · Atlas Desk`; }, [route.key]);
  const market = overview.data?.market;

  const shared = {overview, refreshKey, route};
  return <div className="ad-root" data-theme={theme}>
    <header className="ad-top">
      <Link href="/paper" className="ad-brand" aria-label="Atlas Desk home"><span className="ad-logo" aria-hidden="true"><i/><i/><i/></span>Atlas&nbsp;Desk</Link>
      <nav className="ad-nav" aria-label="Portal">
        {PORTAL_ROUTES.map(r => <Link key={r.key} href={r.path} aria-current={route.key === r.key ? 'page' : undefined} className={route.key === r.key ? 'on' : ''}>{r.title}</Link>)}
      </nav>
      <div className="ad-top-actions">
        <a className="ad-ghost" href="/#journal" title="Open the classic dashboard"><LayoutGrid size={15}/><span>Classic</span></a>
        <button className="ad-ghost" onClick={() => setRefreshKey(k => k + 1)} disabled={overview.loading} aria-label="Refresh live paper data"><RefreshCw size={15} className={overview.loading ? 'ad-spin' : ''}/></button>
        <button className="ad-ghost" onClick={toggle} aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}>{theme === 'dark' ? <Sun size={15}/> : <Moon size={15}/>}</button>
      </div>
    </header>
    <div className="ad-rail" aria-live="polite">
      <span className={`ad-dot ${market?.open ? 'live' : ''}`}/>
      <span>{market?.label ?? 'Market state unknown'}</span>
      <span className="ad-sep"/>
      <span>{overview.data ? `Ledger read ${istTime(overview.data.readAt)}` : overview.loading ? 'Reading paper ledger…' : 'Ledger not read'}</span>
      <span className="ad-sep"/>
      <span>{new Intl.DateTimeFormat('en-IN', {timeZone: 'Asia/Kolkata', weekday: 'short', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false}).format(now)} IST</span>
      <span className="ad-rail-guard"><ShieldCheck size={13}/>Paper only · read-only · no broker access</span>
    </div>
    <main className="ad-main">
      {overview.error && <Callout tone="error" title="Live ledger read failed.">{overview.error}{overview.data ? ' Showing the last successful read; do not treat it as current.' : ''}</Callout>}
      <Suspense fallback={<Loading label="Opening screen…"/>}>
        {route.key === 'paper' && <PaperDesk {...shared}/>}
        {route.key === 'tester' && <StrategyTester {...shared}/>}
        {route.key === 'compare' && <StrategyComparison {...shared}/>}
        {route.key === 'performance' && <Performance {...shared}/>}
        {route.key === 'stock' && <StockDetail {...shared}/>}
      </Suspense>
    </main>
    <footer className="ad-foot">Atlas Desk · live figures from the paper_v1 ledger, research from saved backtest exports · not investment advice</footer>
  </div>;
}
