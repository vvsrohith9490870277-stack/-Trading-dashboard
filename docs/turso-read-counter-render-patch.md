# Render /health read-counter patch (turso-move Task 4)

**Status: NOT APPLIED HERE. Grok applies this patch on `feat/render-deploy`.**

`render_state.py`, `render_health.py` and `render_app.py` live ONLY on
`feat/render-deploy` — they do not exist on `main` (this branch,
`turso-a`, is main-based), so the patch cannot be applied here. It is
recorded verbatim below so Grok can apply it exactly as written.

- Base: `origin/feat/render-deploy` at `d9a7ae6`
  ("render: gate fix: JobGuardTests independent of calendar day").
- Apply from the repo root, on `feat/render-deploy`: save the fenced
  diff block below (from ` ```diff ` to the closing ` ``` `) as a
  `.patch` file and run `git apply <file>.patch`, then run the render
  tests (§Verification).
- This patch only COUNTS. No limits, throttling, caps or refusals anywhere.

## What it does

1. `render_state.py`: a `reads` counter on both store implementations
   (`TursoHttpStore` and `SqliteStore`), exposed as `read_count()`.
   It counts every read statement dispatched — one per execution,
   including `_pipeline` retries — and never counts writes.
2. `render_health.py`: the background refresher samples the counter (no
   store read — the counter lives on the store object in this process)
   and reports it as the `store_reads_total` field. The refresher still
   runs at most once every `RENDER_HEALTH_REFRESH_S` (default 60 s), as
   today; `/health` itself still never touches the store.
3. `render_app.py`: `snapshot()` passes the cached field through to
   `build_snapshot()`, so `/health` serves `store_reads_total`.
4. `/health` payload version bumps `1.0.0` → `1.1.0` (the file's own rule:
   bump when the `/health` shape changes).
5. Tests (`tests/test_render_app.py`, `tests/test_render_state.py`):
   the shape test's `LIVE_KEYS`, a positive-int assertion on the
   refreshed snapshot, and a counter test for `TursoHttpStore`.

## Counter semantics (same rules as the engine counter on main)

- A read is one read statement dispatched for execution. `SELECT`/`WITH`
  is the classification, reusing the exact test `execute()` already uses
  for its retry policy.
- Counted at dispatch: a `_pipeline` retry that re-sends a read counts
  again, and a dispatch that then fails (non-200, transport error) still
  counts — a failed read consumed a Turso read if it reached the server.
  For a usage guard, counting dispatches is the conservative choice.
- Writes (`execute_many`, `affected_rows`, non-`SELECT` `execute()`)
  never count.
- The counter lives on the store object in process memory: it survives
  nothing except the process — a supervisor restart resets it to 0.
  There is no persisted baseline, so `store_reads_total` is "reads
  dispatched by this supervisor process since it started".
- The refresher samples the counter AFTER its read groups, so the
  reported value includes the refresh's own reads (it is a cumulative
  "reads so far", sampled at the end of the refresh).
- `SqliteStore` gets the same counter, so the existing render tests
  (which use a `SqliteStore` fake) keep passing: `refresh_store_fields`
  finds `read_count()` and adds no reason. A store without a counter (a
  duck-typed test double such as `BrokenStore`, or a wrapper such as
  `_CountingStore` that does not delegate `read_count`) leaves the field
  null with reason `store_has_no_read_counter`, never a made-up zero.
  `test_health_store_fields_filled_from_fake_store` asserts
  `snap['reasons'] == {}` — that still holds, because the fake store IS
  a `SqliteStore` and now has a counter.
- This counts SUPERVISOR-side reads only (the supervisor's own store
  object). The engine worker's counter (`db_reads_total` in the
  `paper_monitor_health` heartbeat `detail_json`, added on main by the
  same task) is a separate number; the refresher already parses that
  `detail_json`, so surfacing it too is a small follow-up — deliberately
  NOT in this patch.

## Placement note (one deliberate deviation from the brief)

The brief says "a counter in `TursoHttpStore.execute()`
(`render_state.py:191)". The counter attribute (`self.reads`) and the
`read_count()` accessor DO live on `TursoHttpStore`, but the increment
lives in `_pipeline()`, not in `execute()`: `_pipeline` is the single
place a statement is actually sent over HTTP, and the only place its
retries are visible. Counting in `execute()` would miss retries and
under-count — the wrong direction for a usage guard. `execute()` is the
only read entry point (it is the only caller that passes
`read_only=True`, computed from the SQL), so nothing is lost.

## The patch (verbatim, against d9a7ae6)

```diff
diff --git a/automation/paper-trading/render_app.py b/automation/paper-trading/render_app.py
index 17cc7ad..6b1fdca 100644
--- a/automation/paper-trading/render_app.py
+++ b/automation/paper-trading/render_app.py
@@ -726,6 +726,7 @@ class Supervisor:
             last_job_peak_rss_mb=fields.get('last_job_peak_rss_mb'),
             job_rss_cap_mb=job_rss_cap_mb(),
             quote_probe=fields.get('quote_probe'),
+            store_reads_total=fields.get('store_reads_total'),
             fyers_budget={
                 'day_used': fields.get('fyers_budget_day_used'),
                 'per_day_limit': int(
diff --git a/automation/paper-trading/render_health.py b/automation/paper-trading/render_health.py
index 96d6f2c..a8d2759 100644
--- a/automation/paper-trading/render_health.py
+++ b/automation/paper-trading/render_health.py
@@ -37,7 +37,7 @@ from zoneinfo import ZoneInfo
 IST = ZoneInfo('Asia/Kolkata')
 MEMORY_LIMIT_MB = 450
 # /health payload version; bump when the /health shape changes.
-VERSION = '1.0.0'
+VERSION = '1.1.0'
 
 
 def memory_rss_mb():
@@ -217,7 +217,7 @@ _HEALTH_FIELDS = (
     'last_job', 'fyers_budget_day_used', 'trial_budget_day_used',
     'box_conflict_check', 'peak_job_rss_mb', 'last_job_peak_rss_mb',
     'breakers', 'worker_status', 'worker_consecutive_failures',
-    'worker_last_quote_ok_at', 'quote_probe')
+    'worker_last_quote_ok_at', 'quote_probe', 'store_reads_total')
 
 
 def new_health_cache():
@@ -236,6 +236,7 @@ def new_health_cache():
             'breakers': {}, 'worker_status': None,
             'worker_consecutive_failures': None,
             'worker_last_quote_ok_at': None, 'quote_probe': None,
+            'store_reads_total': None,
         },
         'reasons': {k: 'not_refreshed_yet' for k in _HEALTH_FIELDS},
     }
@@ -255,7 +256,9 @@ def refresh_store_fields(store, namespace, mode, now=None):
       last_job, fyers_budget_day_used, trial_budget_day_used (trial
       only), box_conflict_check (trial only), peak_job_rss_mb,
       last_job_peak_rss_mb, breakers, worker_status,
-      worker_consecutive_failures, worker_last_quote_ok_at.
+      worker_consecutive_failures, worker_last_quote_ok_at,
+      store_reads_total (no store read: the counter lives on the
+      store object in this process).
     - reasons: {field: reason} for every field that could not be
       computed (never a made-up zero).
     - ok: True when at least one read group succeeded; False only when
@@ -538,6 +541,22 @@ def refresh_store_fields(store, namespace, mode, now=None):
         except Exception as ex:
             reasons['quote_probe'] = _read_reason(ex)
 
+    # --- supervisor store reads (no store read: the counter lives on
+    # the store object in this process, so sampling it costs nothing
+    # and is not a read group — it does not change `ok`). The value is
+    # sampled after the groups above, so it includes this refresh's own
+    # reads. A store without a counter (a duck-typed test double, or a
+    # wrapper that does not delegate read_count) leaves the field null
+    # with a reason, never a made-up zero.
+    read_count = getattr(store, 'read_count', None)
+    if callable(read_count):
+        try:
+            fields['store_reads_total'] = read_count()
+        except Exception:
+            reasons['store_reads_total'] = 'counter_unreadable'
+    else:
+        reasons['store_reads_total'] = 'store_has_no_read_counter'
+
     return {'ok': ok_groups > 0, 'fields': fields, 'reasons': reasons}
 
 
@@ -554,7 +573,7 @@ def build_snapshot(mode, namespace, active_runner, lease_held, lease, worker,
                    breakers=None, store_ok=None, store_age_s=None,
                    reasons=None, peak_job_rss_mb=None,
                    last_job_peak_rss_mb=None, job_rss_cap_mb=None,
-                   quote_probe=None):
+                   quote_probe=None, store_reads_total=None):
     """Build the /health payload (brief 01 Task 6.3).
 
     The first nine arguments are the original snapshot inputs and keep
@@ -574,6 +593,10 @@ def build_snapshot(mode, namespace, active_runner, lease_held, lease, worker,
     values), `peak_job_rss_mb` / `last_job_peak_rss_mb` (largest
     job-tree peak today / the last job's peak, from the guard's sampled
     RSS in the job rows), `job_rss_cap_mb` (the cap in force).
+    `store_reads_total` (Turso reads dispatched by the supervisor
+    process since it started, from the store object's counter —
+    sampled by the refresher, never a store read, at most every
+    RENDER_HEALTH_REFRESH_S like the other Task 3 fields).
     """
     if active_runner != 'render':
         status = 'idle_not_active_runner'
@@ -658,6 +681,7 @@ def build_snapshot(mode, namespace, active_runner, lease_held, lease, worker,
         'breakers': breakers if breakers is not None else {},
         'store_ok': store_ok,
         'store_age_s': store_age_s,
+        'store_reads_total': store_reads_total,
         'reasons': reasons if reasons is not None else {},
         'peak_job_rss_mb': peak_job_rss_mb,
         'last_job_peak_rss_mb': last_job_peak_rss_mb,
diff --git a/automation/paper-trading/render_state.py b/automation/paper-trading/render_state.py
index b544173..ad1e8b0 100644
--- a/automation/paper-trading/render_state.py
+++ b/automation/paper-trading/render_state.py
@@ -71,13 +71,24 @@ class SqliteStore:
         self.conn.execute('PRAGMA busy_timeout=30000')
         self._lock = threading.RLock()
 
+        # Read counter (turso-move Task 4): SELECT/WITH statements
+        # dispatched through this store since the process started.
+        # Same rule as TursoHttpStore; no limits are enforced.
+        self.reads = 0
+
     def execute(self, sql, args=()):
         with self._lock:
+            if sql.strip().upper().startswith(('SELECT', 'WITH')):
+                self.reads += 1
             cur = self.conn.execute(sql, args)
             rows = [dict(r) for r in cur.fetchall()] if cur.description is not None else []
             self.conn.commit()
             return rows
 
+    def read_count(self):
+        """Reads dispatched through this store since the process started."""
+        return self.reads
+
     def execute_many(self, statements):
         counts = []
         with self._lock:
@@ -147,6 +158,16 @@ class TursoHttpStore:
         self.session = session or requests.Session()
         self.timeout = timeout
 
+        # Read counter (turso-move Task 4): read statements dispatched
+        # over HTTP since the supervisor process started. Incremented
+        # in _pipeline (below), the single place a statement is actually
+        # sent; no limits are enforced, this only counts.
+        self.reads = 0
+
+    def read_count(self):
+        """Reads dispatched through this store since the process started."""
+        return self.reads
+
     def _pipeline(self, reqs, read_only=False):
         body = {'requests': reqs}
         headers = {'Authorization': 'Bearer ' + self.token,
@@ -154,6 +175,14 @@ class TursoHttpStore:
         attempts = 0
         while True:
             try:
+                # Count every read statement dispatched, including
+                # _pipeline retries (read-only pipelines are re-sent up
+                # to twice on transport failure). Writes
+                # (read_only=False) never count. execute() is the only
+                # read entry point: it sets read_only from the SQL
+                # (SELECT/WITH), the same test its retry policy uses.
+                if read_only:
+                    self.reads += 1
                 r = self.session.post(self.base + '/v2/pipeline', headers=headers,
                                       json=body, timeout=self.timeout)
             except Exception:
diff --git a/automation/paper-trading/tests/test_render_app.py b/automation/paper-trading/tests/test_render_app.py
index d6fd6c6..615e5e0 100644
--- a/automation/paper-trading/tests/test_render_app.py
+++ b/automation/paper-trading/tests/test_render_app.py
@@ -84,7 +84,8 @@ class HealthFieldsTests(unittest.TestCase):
         'last_tick_ist', 'last_lease_renew_age_s', 'jobs_today',
         'last_job', 'rss_mb', 'children_rss_mb', 'fyers_budget', 'auth',
         'git_sha', 'version', 'breakers', 'store_ok', 'store_age_s',
-        'reasons', 'peak_job_rss_mb', 'last_job_peak_rss_mb',
+        'store_reads_total', 'reasons', 'peak_job_rss_mb',
+        'last_job_peak_rss_mb',
         'job_rss_cap_mb',
         # kept from the original snapshot:
         'status', 'active_runner', 'lease', 'jobs', 'uptime_seconds',
@@ -416,6 +417,11 @@ class HealthStoreTests(unittest.TestCase):
             self.assertIsInstance(snap['store_age_s'], int)
             self.assertLessEqual(snap['store_age_s'], 30)
             self.assertEqual(snap['reasons'], {})
+            # Read counter (Task 4): the refresher sampled the store's
+            # counter — an int, and positive: it includes this refresh's
+            # own reads.
+            self.assertIsInstance(snap['store_reads_total'], int)
+            self.assertGreater(snap['store_reads_total'], 0)
             # The lease + jobs come from the cache too.
             self.assertEqual(snap['lease']['owner_id'], 'render:h:1:1')
             self.assertIn('scan', snap['jobs'])
diff --git a/automation/paper-trading/tests/test_render_state.py b/automation/paper-trading/tests/test_render_state.py
index 0cd3630..9d1ca62 100644
--- a/automation/paper-trading/tests/test_render_state.py
+++ b/automation/paper-trading/tests/test_render_state.py
@@ -190,6 +190,21 @@ class TursoHttpTests(unittest.TestCase):
             store.execute("SELECT * FROM no_such_table_xyz")
         self.assertNotIn('SECRETTOKEN123', str(cm.exception))
 
+    def test_read_counter_counts_reads_only(self):
+        sess = FakeTursoSession()
+        store = rs.TursoHttpStore('libsql://mydb-myorg.turso.io', 'T', session=sess)
+        rs.migrate(store)
+        base = store.read_count()
+        store.execute('SELECT 1 AS n')
+        self.assertEqual(store.read_count(), base + 1)
+        store.execute("INSERT INTO paper_runtime_kv(scope,key,value,updated_at) VALUES(?,?,?,?)",
+                      ('_global', 'k', 'v', '2026-11-19T00:00:00+05:30'))
+        store.execute_many([("INSERT INTO paper_runtime_kv(scope,key,value,updated_at) VALUES(?,?,?,?)",
+                             ('_global', 'k2', 'v', '2026-11-19T00:00:00+05:30'))])
+        self.assertEqual(store.read_count(), base + 1, 'writes never count')
+        store.server_time()
+        self.assertEqual(store.read_count(), base + 2, 'server_time is a SELECT')
+
     def test_server_time_used_not_local(self):
         # The store must ask the server for time (strftime('%s','now') in SQL),
         # never compute it from the container clock.
```

## Verification (for Grok, after applying on feat/render-deploy)

- `git apply --check` must pass on the base above; the patch was verified
  to apply cleanly against `d9a7ae6` (checked 2026-11-21 in a scratch
  worktree, reset + re-applied from the generated patch file).
- The patch INCLUDES its tests (they are part of the diff, not a
  follow-up): `LIVE_KEYS` gains `store_reads_total` (the shape test
  pins the exact `/health` keys, so it must be updated),
  `test_health_store_fields_filled_from_fake_store` asserts
  `snap['store_reads_total']` is an `int` greater than 0, and
  `TursoHttpTests.test_read_counter_counts_reads_only` proves the
  `TursoHttpStore` counter counts `SELECT`s (including `server_time`)
  and never counts writes.
- Run the directly affected render tests (no full suite needed for this):
  `python3 -m unittest tests.test_render_state tests.test_render_app`
  (from `automation/paper-trading`). Verified 2026-11-21 in the scratch
  worktree with the patch applied: 40 tests, OK. (First run showed 1
  failure — `test_health_has_all_brief_fields` — because `LIVE_KEYS`
  did not yet include the new key; that is why the `LIVE_KEYS` hunk is
  in the patch.)
- Live check after deploy: `GET /health` shows `store_reads_total` as
  an integer that grows across refreshes (at most one refresh per
  `RENDER_HEALTH_REFRESH_S`, default 60 s); it resets to 0 only when the
  supervisor process restarts.
