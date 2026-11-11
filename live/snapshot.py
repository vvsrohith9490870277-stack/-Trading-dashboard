"""Read-only Turso -> local SQLite snapshot (mode=ro readers downstream). Never writes to Turso."""
import json, sqlite3
from pathlib import Path
from ro_client import TursoRO

def table_ddl(ro, names):
    rows = ro.query("SELECT name, sql FROM sqlite_master WHERE type='table' AND name IN (%s)" % ','.join('?' * len(names)), names)
    return {r['name']: r['sql'] for r in rows}

def build(path, specs, ro=None):
    """specs: list of (table, where_sql, args). Writes a fresh local SQLite file and returns row counts."""
    ro = ro or TursoRO()
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    if tmp.exists(): tmp.unlink()
    ddl = table_ddl(ro, sorted({t for t, _, _ in specs}))
    c = sqlite3.connect(tmp)
    counts = {}
    results = ro.many([(f'SELECT * FROM "{t}"' + (f' WHERE {w}' if w else ''), a) for t, w, a in specs])
    for (t, _, _), rows in zip(specs, results):
        if t not in ddl:
            counts[t] = None
            continue
        c.execute(ddl[t])
        if rows:
            cols = list(rows[0].keys())
            c.executemany(f'INSERT OR IGNORE INTO "{t}"({",".join(cols)}) VALUES({",".join("?" * len(cols))})',
                          [tuple(r[k] for k in cols) for r in rows])
        counts[t] = len(rows)
    c.commit(); c.close()
    tmp.replace(path)
    return counts
