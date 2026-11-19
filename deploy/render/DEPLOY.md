# Deploying the Track A backend to Render (free tier)

This deploys the paper-trading backend only (the supervisor + engine). The
dashboard stays where it is; the adaptive sidecar stays on the box. Nothing
here deploys itself — every step below is done by a person in the Render
dashboard, and the service starts idle until the handoff flag is set.

## 1. Turso

1. Create (or reuse) the Turso database that already holds the paper
   trading tables.
2. Create a database token with read/write on that database.
3. Run the render schema once (creates only `paper_runner_lease`,
   `paper_job_runs`, `paper_runtime_kv`; it never alters an existing table):
   `python automation/paper-trading/render_ctl.py migrate`
   (with `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN` in env).

## 2. Keys

Generate the token-encryption key and keep it somewhere safe:

```
python automation/paper-trading/render_ctl.py gen-key
```

Publish the current Fyers token from the box `.env` (never print it):

```
python automation/paper-trading/render_ctl.py publish-token
```

## 3. Create the service

1. In Render: New → Blueprint, point it at this repo. Render reads
   `render.yaml` (service `signal-atlas-paper`, region `singapore`,
   plan `free`, `autoDeployTrigger: off`, health check `/health`).
2. Fill the prompted env vars from `deploy/render/.env.render.example`:
   `PAPER_RUNTIME=render`, `RENDER_MODE=live`,
   `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `TOKEN_ENC_KEY`,
   `FYERS_AUTO_LOGIN=0`, `PAPER_SESSIOND_DISABLE=1`.
   Leave `PAPER_NAMESPACE_OVERRIDE` empty for live.
3. Deploy. The service starts with `active_runner` unset, so it serves
   `/health` as `idle_not_active_runner` (HTTP 200) and runs nothing.

## 4. Trial (Fri 20 Nov)

1. `RENDER_MODE=trial`, `PAPER_NAMESPACE_OVERRIDE=paper_render_trial_v1`.
2. `python automation/paper-trading/render_ctl.py trial-init --namespace paper_render_trial_v1`
3. `python automation/paper-trading/render_ctl.py handoff --to render`
4. Watch `/health` (status `ok`, worker `running`) and compare at end of
   day: `trial-compare --namespace paper_render_trial_v1 --day 2026-11-20`.
5. Clean up: `trial-purge --namespace paper_render_trial_v1`.

## 5. Handoff to live (Mon 23 Nov)

1. Set `RENDER_MODE=live`, clear `PAPER_NAMESPACE_OVERRIDE`, redeploy.
2. `python automation/paper-trading/render_ctl.py handoff --to render`
3. Confirm `/health` is `ok` and the box worker is stopped. The box guard
   (`PAPER_RUNNER_ENFORCE=1`) refuses any box mutating command while the
   flag names render.

## 6. Rollback

`python automation/paper-trading/render_ctl.py handoff --to box --yes` — the
supervisor idles and the box resumes. No data is lost: all state is in
Turso, and the Render filesystem is ephemeral by design.

## Adaptive stays on the box

The adaptive sidecar is NOT moved to Render. Measured 2026-11-19 on this
machine (`render_ctl.py` process-tree sampling, /proc every 100 ms):

- Adaptive runner, light phases (`status`, `preflight`): peak RSS 69 MB.
- Adaptive engine on its fixtures (test_adaptive_engine, phase2, phase3,
  sim_trades — the engine's real work): peak RSS 88 MB, 21.98 CPU-s.

Both are under the 420 MB limit, so memory is not the blocker. The blocker
is local state: the sidecar keeps its state in a local SQLite database
(`adaptive.sqlite`) plus a local market-snapshot archive, which must
survive restarts. Render free has no persistent disk (the filesystem is
lost on every redeploy, restart and spin-down), and moving `adaptive.sqlite`
to Turso is explicitly out of scope for this brief. So the adaptive sidecar
stays on the box, and `render_schedule.json` keeps the `adaptive` job
disabled on Render.

## Footprint (measured 2026-11-19, `render_ctl.py footprint --seconds 5`)

Fixture: 7 strategies x 9 positions, a 50-symbol universe, 60 bars of
daily_ohlcv each. No Fyers credentials in the measurement env, so jobs
that need market data fail fast after startup; their rows measure startup
+ attempt, and the exit code is shown. Peak RSS of the whole process tree,
CPU seconds (user+sys), wall time at 0.1 CPU = CPU / 0.1.

| job | peak RSS MB | CPU s | est. wall s at 0.1 CPU | exit |
| --- | --- | --- | --- | --- |
| preflight | 82 | 0.75 | 7.5 | 1 (no creds) |
| scan | 123 | 1.77 | 17.7 | 0 |
| eod | 85 | 0.83 | 8.3 | 0 |
| sync_history | 92 | 0.71 | 7.1 | 1 (no creds) |
| support | 87 | 0.88 | 8.8 | 0 |
| revalue | 83 | 0.79 | 7.9 | 1 (no creds) |
| worker_cycle | 91 | 0.00 | 0.0 | 0 |

- `worker_cpu_seconds_per_cycle`: 0.07 s (limit 2.0). Measured as one
  monitor poll's own CPU (a `monitor-once` run minus a bare import) plus
  the idle rate; a lower bound for a live poll, which fails fast at the
  refresh without credentials.
- `max_peak_rss_mb`: 123 (limit 420).
- `baseline_idle_rss_mb`: 77 (idle supervisor + fake worker).

## Free-tier limits that matter

- 750 free instance hours per workspace per month; the service spins down
  after 15 min without inbound traffic and takes ~1 min to spin up.
- 512 MB RAM, 0.1 CPU, single instance, no persistent disk. The supervisor
  restarts the worker over 450 MB; `render_ctl.py footprint` measures the
  real footprint.
- No SSH, no one-off jobs, no outbound SMTP. Render may restart a free
  service at any time — the lease (90 s TTL, 20 s renewal) and the 30 min
  schedule catch-up cover that.
