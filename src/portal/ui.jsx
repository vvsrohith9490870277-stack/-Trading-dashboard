import {useMemo, useState} from 'react';
import {ArrowDown, ArrowUp, ChevronLeft, ChevronRight, CircleAlert, Inbox} from 'lucide-react';

export function Section({eyebrow, title, actions, children, className = '', id}) {
  return <section className={`ad-section ${className}`} id={id}>
    {(title || actions) && <header className="ad-section-head">
      <div>{eyebrow && <span className="ad-eyebrow">{eyebrow}</span>}{title && <h2>{title}</h2>}</div>
      {actions && <div className="ad-section-actions">{actions}</div>}
    </header>}
    {children}
  </section>;
}

export function Tile({label, value, hint, tone = 'flat', accent}) {
  return <div className={`ad-tile ad-${tone}`} style={accent ? {'--ad-accent': accent} : undefined}>
    <span className="ad-tile-label">{label}</span>
    <strong className="ad-tile-value">{value}</strong>
    {hint && <span className="ad-tile-hint">{hint}</span>}
  </div>;
}

export function Tag({children, tone = 'neutral', title}) { return <span className={`ad-tag ad-tag-${tone}`} title={title}>{children}</span>; }

export function Callout({tone = 'info', title, children}) {
  return <div className={`ad-callout ad-callout-${tone}`} role={tone === 'error' ? 'alert' : 'note'}>
    <CircleAlert size={16} aria-hidden="true"/>
    <div>{title && <strong>{title} </strong>}{children}</div>
  </div>;
}

export function Blank({title = 'Nothing recorded yet', text = 'No matching records. Nothing has been simulated or filled in.'}) {
  return <div className="ad-blank"><Inbox size={22} aria-hidden="true"/><strong>{title}</strong><p>{text}</p></div>;
}

export function Loading({label = 'Reading…'}) { return <div className="ad-loading" role="status"><span className="ad-pulse"/>{label}</div>; }

export function Tabs({items, value, onChange, label}) {
  return <div className="ad-tabs" role="tablist" aria-label={label}>
    {items.map(([id, text, count]) => <button key={id} role="tab" aria-selected={value === id} className={value === id ? 'on' : ''} onClick={() => onChange(id)}>
      {text}{count !== undefined && count !== null && <span className="ad-count">{count}</span>}
    </button>)}
  </div>;
}

function compare(a, b) {
  if (a === b) return 0;
  if (a === null || a === undefined) return 1;
  if (b === null || b === undefined) return -1;
  return typeof a === 'number' && typeof b === 'number' ? a - b : String(a).localeCompare(String(b));
}

// Sortable, paginated table. `columns`: [{key, title, render?, align?, sortValue?}].
export function SortTable({rows = [], columns, pageSize = 12, empty, caption, rowKey = (r, i) => r.id ?? `${r.symbol ?? ''}-${r.strategy ?? ''}-${i}`, filter}) {
  const [sort, setSort] = useState({key: null, dir: 1});
  const [page, setPage] = useState(1);
  const [q, setQ] = useState('');
  const sorted = useMemo(() => {
    const term = q.trim().toLowerCase();
    const list = term ? rows.filter(r => JSON.stringify(r).toLowerCase().includes(term)) : rows;
    if (!sort.key) return list;
    const col = columns.find(c => c.key === sort.key);
    const val = r => (col?.sortValue ? col.sortValue(r) : r[sort.key]);
    return [...list].sort((a, b) => compare(val(a), val(b)) * sort.dir);
  }, [rows, sort, columns, q]);
  const pages = Math.max(1, Math.ceil(sorted.length / pageSize));
  const current = Math.min(page, pages);
  const visible = sorted.slice((current - 1) * pageSize, current * pageSize);
  return <div className="ad-table-wrap">
    {filter && <div className="ad-table-tools"><input className="ad-input" aria-label={`Filter ${caption ?? 'rows'}`} placeholder="Filter…" value={q} onChange={e => { setQ(e.target.value); setPage(1); }}/><span>{sorted.length} rows</span></div>}
    {visible.length ? <div className="ad-table-scroll"><table className="ad-table">
      {caption && <caption className="ad-sr">{caption}</caption>}
      <thead><tr>{columns.map(c => <th key={c.key} scope="col" className={c.align === 'right' ? 'r' : ''} aria-sort={sort.key === c.key ? (sort.dir > 0 ? 'ascending' : 'descending') : 'none'}>
        <button onClick={() => setSort(s => ({key: c.key, dir: s.key === c.key ? -s.dir : 1}))}>{c.title}{sort.key === c.key && (sort.dir > 0 ? <ArrowUp size={11}/> : <ArrowDown size={11}/>)}</button>
      </th>)}</tr></thead>
      <tbody>{visible.map((r, i) => <tr key={rowKey(r, i)}>{columns.map(c => <td key={c.key} className={c.align === 'right' ? 'r num' : ''}>{c.render ? c.render(r[c.key], r) : (r[c.key] ?? '—')}</td>)}</tr>)}</tbody>
    </table></div> : <Blank {...(empty ?? {})}/>}
    {pages > 1 && <div className="ad-pager">
      <button aria-label="Previous page" disabled={current <= 1} onClick={() => setPage(current - 1)}><ChevronLeft size={14}/></button>
      <span>{current} / {pages}</span>
      <button aria-label="Next page" disabled={current >= pages} onClick={() => setPage(current + 1)}><ChevronRight size={14}/></button>
    </div>}
  </div>;
}

export function Meter({value, max, label}) {
  const ratio = max > 0 ? Math.min(1, value / max) : 0;
  const over = max > 0 && value > max;
  return <div className={`ad-meter ${over ? 'over' : ''}`} role="meter" aria-valuemin={0} aria-valuemax={max} aria-valuenow={value} aria-label={label}>
    <span style={{width: `${ratio * 100}%`}}/>
  </div>;
}
