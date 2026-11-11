"""Row counts for every Turso table (total + per protected namespace). Read-only."""
import json, re, sys
from datetime import datetime
from zoneinfo import ZoneInfo
from ro_client import TursoRO
PROTECTED = ('paper_v1', 'paper_demo_v1')
db = TursoRO()
tables = db.query("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
qs, keys = [], []
for t in tables:
    name = t['name']
    qs.append((f'SELECT COUNT(*) AS n FROM "{name}"', ())); keys.append((name, '*'))
    if re.search(r'\bnamespace\b', t['sql'] or ''):
        for ns in PROTECTED:
            qs.append((f'SELECT COUNT(*) AS n FROM "{name}" WHERE namespace=?', (ns,))); keys.append((name, ns))
res = db.many(qs)
out = {'at_ist': datetime.now(ZoneInfo('Asia/Kolkata')).isoformat(), 'counts': {}}
for (name, ns), rows in zip(keys, res):
    out['counts'].setdefault(name, {})[ns] = rows[0]['n']
json.dump(out, open(sys.argv[1], 'w'), indent=1, sort_keys=True)
print(len(out['counts']), 'tables counted')
