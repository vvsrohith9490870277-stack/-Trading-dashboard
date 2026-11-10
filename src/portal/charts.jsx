// Small dependency-free SVG charts that read their colours from CSS variables, so they follow the theme.
import {useState} from 'react';
import {fmt} from './format';
import {Blank} from './ui';

const MONTHS = ['J', 'F', 'M', 'A', 'M', 'J', 'J', 'A', 'S', 'O', 'N', 'D'];

export function Columns({rows, valueKey = 'value', labelKey = 'label', unit = '%', height = 200, empty, describe}) {
  const [active, setActive] = useState(null);
  const data = rows.filter(r => Number.isFinite(Number(r[valueKey])));
  if (!data.length) return <Blank {...(empty ?? {title: 'No observations', text: 'A period without observations is not a zero.'})}/>;
  const extent = Math.max(...data.map(r => Math.abs(Number(r[valueKey])))) || 1;
  const w = 960, mid = height / 2, band = Math.min(w / data.length, 80);
  const focus = active ?? data.at(-1);
  return <figure className="ad-chart">
    <figcaption className="ad-chart-read"><span>{focus[labelKey]}</span><strong>{fmt(focus[valueKey])}{unit}</strong>{describe && <small>{describe(focus)}</small>}</figcaption>
    <svg viewBox={`0 0 ${w} ${height}`} role="img" aria-label="Column chart">
      <line x1="0" x2={w} y1={mid} y2={mid} className="ad-axis"/>
      {data.map((r, i) => {
        const v = Number(r[valueKey]), h = Math.max(1.5, Math.abs(v) / extent * (mid - 8));
        return <rect key={r[labelKey] ?? i} x={i * band + band * 0.15} width={Math.max(2, band * 0.7)} y={v >= 0 ? mid - h : mid} height={h} rx="2"
          className={`${v >= 0 ? 'ad-pos' : 'ad-neg'} ${focus === r ? 'focus' : ''}`} tabIndex="0" aria-label={`${r[labelKey]}: ${fmt(v)}${unit}`}
          onMouseEnter={() => setActive(r)} onFocus={() => setActive(r)}/>;
      })}
    </svg>
    <div className="ad-chart-scale"><span>{data[0][labelKey]}</span><span>{data.at(-1)[labelKey]}</span></div>
  </figure>;
}

// Multi-series line chart; series: [{name, color, points:[{x:string, y:number}]}], x values are sortable strings.
export function Lines({series, height = 260, unit = '', empty, baseline}) {
  const all = series.flatMap(s => s.points.filter(p => Number.isFinite(p.y)));
  if (!all.length) return <Blank {...(empty ?? {title: 'No series to draw'})}/>;
  const xs = [...new Set(all.map(p => p.x))].sort();
  const ys = all.map(p => p.y).concat(baseline === undefined ? [] : [baseline]);
  const min = Math.min(...ys), max = Math.max(...ys), span = max - min || 1, w = 960, pad = 16;
  const X = x => (xs.length === 1 ? w / 2 : pad + xs.indexOf(x) / (xs.length - 1) * (w - pad * 2));
  const Y = y => height - pad - (y - min) / span * (height - pad * 2);
  return <figure className="ad-chart">
    <div className="ad-legend">{series.map(s => <span key={s.name}><i style={{background: s.color}}/>{s.name}</span>)}</div>
    <svg viewBox={`0 0 ${w} ${height}`} role="img" aria-label={`Line chart of ${series.map(s => s.name).join(', ')}`}>
      {[0, 0.5, 1].map(f => <g key={f}><line x1="0" x2={w} y1={pad + f * (height - pad * 2)} y2={pad + f * (height - pad * 2)} className="ad-grid"/>
        <text x="2" y={pad + f * (height - pad * 2) - 3} className="ad-tick">{fmt(max - f * span, 1)}{unit}</text></g>)}
      {baseline !== undefined && <line x1="0" x2={w} y1={Y(baseline)} y2={Y(baseline)} className="ad-axis"/>}
      {series.map(s => {
        const pts = s.points.filter(p => Number.isFinite(p.y)).sort((a, b) => a.x.localeCompare(b.x));
        if (!pts.length) return null;
        const d = pts.map((p, i) => `${i ? 'L' : 'M'}${X(p.x).toFixed(1)} ${Y(p.y).toFixed(1)}`).join(' ');
        return <g key={s.name}><path d={d} fill="none" style={{stroke: s.color}} strokeWidth="2" strokeLinejoin="round"/>
          {pts.length < 40 && pts.map(p => <circle key={p.x} cx={X(p.x)} cy={Y(p.y)} r={pts.length === 1 ? 5 : 2.6} style={{fill: s.color}}><title>{`${s.name} · ${p.x}: ${fmt(p.y)}${unit}`}</title></circle>)}</g>;
      })}
    </svg>
    <div className="ad-chart-scale"><span>{xs[0]}</span><span>{xs.at(-1)}</span></div>
  </figure>;
}

// Year × month heatmap of mean closed-trade returns (rows: [{period:'YYYY-MM', mean, closed}]).
export function MonthGrid({rows, years = 8}) {
  const [active, setActive] = useState(null);
  if (!rows?.length) return <Blank title="No monthly observations"/>;
  const map = new Map(rows.map(r => [r.period, r]));
  const allYears = [...new Set(rows.map(r => r.period.slice(0, 4)))].sort().slice(-years);
  const extent = Math.max(1, ...rows.filter(r => allYears.includes(r.period.slice(0, 4))).map(r => Math.abs(r.mean)));
  return <div className="ad-heat">
    <div className="ad-heat-read">{active ? <><span>{active.period}</span><strong>{fmt(active.mean)}%</strong><small>{active.closed} closed · mean gross / trade</small></> : <span>Hover a cell · mean gross return per closed trade, by exit month</span>}</div>
    <div className="ad-heat-grid" style={{gridTemplateColumns: `44px repeat(12, 1fr)`}}>
      <span/>{MONTHS.map((m, i) => <span key={i} className="ad-heat-h">{m}</span>)}
      {allYears.map(y => [<span key={y} className="ad-heat-y">{y}</span>, ...MONTHS.map((_, i) => {
        const r = map.get(`${y}-${String(i + 1).padStart(2, '0')}`);
        if (!r) return <span key={y + i} className="ad-heat-cell none" aria-label={`${y}-${i + 1}: no trades`}/>;
        const a = Math.min(1, Math.abs(r.mean) / extent);
        return <button key={y + i} className={`ad-heat-cell ${r.mean >= 0 ? 'pos' : 'neg'}`} style={{'--a': (0.15 + a * 0.85).toFixed(2)}}
          aria-label={`${r.period}: ${fmt(r.mean)} percent mean, ${r.closed} trades`} onMouseEnter={() => setActive(r)} onFocus={() => setActive(r)}/>;
      })])}
    </div>
  </div>;
}

export function Funnel({stages}) {
  if (!stages?.length) return <Blank title="No funnel recorded"/>;
  const top = Math.max(1, ...stages.map(s => s.entering ?? s.passed ?? 0));
  return <ol className="ad-funnel">{stages.map((s, i) => <li key={s.stage ?? i}>
    <span className="ad-funnel-name">{s.stage}</span>
    <span className="ad-funnel-bar"><i style={{width: `${(s.passed / top) * 100}%`}}/></span>
    <span className="num">{fmt(s.passed, 0)}</span>
    <span className="ad-funnel-drop num">{s.failed ? `−${fmt(s.failed, 0)}` : ''}</span>
  </li>)}</ol>;
}

export function Spark({points, height = 56}) {
  const ys = points.map(p => p.y).filter(Number.isFinite);
  if (ys.length < 2) return <span className="ad-muted">Not enough recorded quotes</span>;
  const min = Math.min(...ys), max = Math.max(...ys), span = max - min || 1, w = 240;
  const d = ys.map((y, i) => `${i ? 'L' : 'M'}${(i / (ys.length - 1) * w).toFixed(1)} ${(height - 4 - (y - min) / span * (height - 8)).toFixed(1)}`).join(' ');
  return <svg className="ad-spark" viewBox={`0 0 ${w} ${height}`} role="img" aria-label="Recorded quote trend"><path d={d} fill="none" strokeWidth="1.8"/></svg>;
}
