-- Render state schema (additive only). Applied by render_state.migrate() with
-- CREATE TABLE IF NOT EXISTS - safe to run on every start. Do NOT edit
-- schema.sql - do not alter any existing table.
CREATE TABLE IF NOT EXISTS paper_runner_lease(
  namespace TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  owner_kind TEXT NOT NULL,
  epoch INTEGER NOT NULL,
  acquired_unix INTEGER NOT NULL,
  expires_unix INTEGER NOT NULL,
  heartbeat_at TEXT NOT NULL,
  detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_job_runs(
  namespace TEXT NOT NULL,
  job TEXT NOT NULL,
  session_day TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  status TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  claimed_unix INTEGER NOT NULL,
  lease_until_unix INTEGER NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  exit_code INTEGER,
  result_json TEXT,
  PRIMARY KEY(namespace, job, session_day)
);
CREATE TABLE IF NOT EXISTS paper_runtime_kv(
  scope TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(scope, key)
);
