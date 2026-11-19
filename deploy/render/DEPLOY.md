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

`python automation/paper-trading/render_ctl.py handoff --to box` — the
supervisor idles and the box resumes. No data is lost: all state is in
Turso, and the Render filesystem is ephemeral by design.

## Free-tier limits that matter

- 750 free instance hours per workspace per month; the service spins down
  after 15 min without inbound traffic and takes ~1 min to spin up.
- 512 MB RAM, 0.1 CPU, single instance, no persistent disk. The supervisor
  restarts the worker over 450 MB; `render_ctl.py footprint` measures the
  real footprint.
- No SSH, no one-off jobs, no outbound SMTP. Render may restart a free
  service at any time — the lease (90 s TTL, 20 s renewal) and the 30 min
  schedule catch-up cover that.
