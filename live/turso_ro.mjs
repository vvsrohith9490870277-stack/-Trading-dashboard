// Read-only Turso reader for the adaptive / support-agent sidecar.
// - Accepts ONLY single SELECT / WITH statements (no ';', no write/DDL/PRAGMA/ATTACH keywords).
// - Executes inside a libsql 'read' transaction, so the server rejects any write even if validation missed one.
// - Prints rows as JSON on stdout. Never prints credentials; errors are reduced to a short code.
import fs from 'fs';
import {createRequire} from 'module';
const require = createRequire(import.meta.url);
const {createClient} = require('@libsql/client');

const FORBIDDEN = /\b(INSERT|UPDATE|DELETE|REPLACE|UPSERT|CREATE|DROP|ALTER|ATTACH|DETACH|PRAGMA|VACUUM|REINDEX|ANALYZE|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE)\b/i;
export function assertReadOnly(sql) {
  const s = String(sql || '').trim();
  if (!/^(SELECT|WITH)\b/i.test(s)) throw Error('ro_not_select');
  if (s.replace(/'[^']*'/g, "''").includes(';')) throw Error('ro_multi_statement');
  if (FORBIDDEN.test(s.replace(/'[^']*'/g, "''"))) throw Error('ro_forbidden_keyword');
  return s;
}
async function main() {
  const req = JSON.parse(fs.readFileSync(0, 'utf8'));
  const queries = (req.queries || []).map(q => ({sql: assertReadOnly(q.sql), args: q.args || []}));
  const envFile = process.env.PAPER_ENV_FILE || '/workspace/sahil-fyers-turso/.env';
  const env = {};
  for (const l of fs.readFileSync(envFile, 'utf8').split('\n')) {
    const p = l.indexOf('=');
    if (p > 0 && !l.startsWith('#')) env[l.slice(0, p).trim()] = l.slice(p + 1).trim().replace(/^(['"])(.*)\1$/, '$2');
  }
  const db = createClient({url: env.TURSO_DATABASE_URL, authToken: env.TURSO_AUTH_TOKEN});
  const tx = await db.transaction('read');
  const out = [];
  try {
    for (const q of queries) {
      const r = await tx.execute(q);
      out.push(r.rows.map(row => Object.fromEntries(r.columns.map((c, i) => [c, typeof row[i] === 'bigint' ? Number(row[i]) : row[i]]))));
    }
  } finally { tx.close(); }
  process.stdout.write(JSON.stringify(out));
}
if (process.argv[1] && process.argv[1].endsWith('turso_ro.mjs')) {
  main().catch(e => { const m = String(e && e.message || e); process.stderr.write(JSON.stringify({code: /^ro_/.test(m) ? m : 'turso_read_failed'}) + '\n'); process.exit(2); });
}
