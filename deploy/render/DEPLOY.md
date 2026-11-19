# Deploying the Track A backend to Render (free tier)

This deploys the paper-trading backend only (the supervisor + engine).
The dashboard stays where it is; the adaptive sidecar stays on the box.
Nothing here deploys itself — every step below is done by a person in
the Render dashboard, and the service starts idle until the handoff
flag is set.

## 1. What runs where

- **Render** (this service): the Track A backend — supervisor, 30-second
  worker, IST daily jobs, Turso lease. One web service, no background
  worker, no cron jobs, no disk.
- **Vercel**: the dashboard, unchanged. It reads Turso directly, so it
  needs no change and no new env var.
- **The box**: the adaptive sidecar and its agents stay on the box (see
  "Adaptive stays on the box" below). At cutover the box routines are
  disabled by the senior; the box keeps no Track A runner.
- **Turso**: all state — leases, job runs, counters, tokens, budgets.
  The filesystem on Render is ephemeral; nothing may live only in a
  file there.

## 2. Free-tier limits that matter

- **750 instance-hours/month** — the service can stay awake 24/7 only if
  it is the only free web service on the account. The keep-awake pinger
  (§7) is set to Mon–Fri 07:45–19:00 IST, about 250 instance-hours a
  month, leaving headroom.
- **Spins down after 15 minutes without inbound HTTP traffic**; a cold
  start takes about 1 minute. The pinger (§7) keeps it awake during
  market hours. Overnight it sleeps; a restart at any time is normal.
- **512 MB RAM, about 0.1 vCPU, no persistent disk, no SSH, no one-off
  jobs.** Measured footprint: max 123 MB, worker 0.05–0.07 CPU-seconds
  per cycle (see "Footprint" below). A new deploy briefly overlaps the
  old instance; the Turso lease makes that safe.
- **The filesystem is wiped on every restart/deploy/spin-down.** All
  state lives in Turso (leases, counters, job runs, tokens). `/tmp` is
  the only writable directory.

## 3. Turso

1. Create (or reuse) the Turso database that already holds the paper
   trading tables.
2. Create a database token with read/write on that database.
3. Run the render schema once (creates only `paper_runner_lease`,
   `paper_job_runs`, `paper_runtime_kv`; it never alters an existing
   table):
   `python automation/paper-trading/render_ctl.py migrate`
   (with `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN` in env).

## 4. Secrets checklist

Set these in the Render dashboard (service → Environment), never in
git and never in a log. Values are never printed by any command.

| Secret | Set when | Where it comes from |
|---|---|---|
| `TURSO_DATABASE_URL` | Trial and live | The Turso database (§3) |
| `TURSO_AUTH_TOKEN` | Trial and live | The Turso token (§3) |
| `TOKEN_ENC_KEY` | Trial and live | The render control CLI: `python automation/paper-trading/render_ctl.py gen-key` |
| `FYERS_APP_ID` | Trial and live | The Fyers app on the box `.env` (name only here) |
| `FYERS_CLIENT_ID` | Live only | The box `.env` |
| `FYERS_SECRET_ID` | Live only | The box `.env` |
| `FYERS_PIN` | Live only | See the warning box below |
| `FYERS_TOTP_SECRET` | Live only | See the warning box below |

> **PIN / TOTP warning.** `FYERS_PIN` and `FYERS_TOTP_SECRET` are full
> account-login credentials: anyone who has them (plus the client id
> and secret id) can log in to the Fyers account. Mitigations:
> - Set them in Render **only at the Monday handoff** (§10), never
>   during the trial. The trial runs with `FYERS_AUTO_LOGIN=0` and no
>   PIN/TOTP set, so the login flow is unreachable.
> - Render env vars are write-only after save (they cannot be read back
>   from the dashboard); keep the box `.env` as the only other copy.
> - If they are ever suspected to be leaked, rotate the PIN and the
>   TOTP secret in Fyers first, then update Render.
> - Whether a Render-side login invalidates the box's token is
>   **unknown** — it has never been tested, and it must never be
>   tested against the live account. That is why the trial never logs
>   in, and why the handoff happens only after the box runner is
>   stopped.

## 5. Keys

Generate the token-encryption key and keep it somewhere safe:

```
python automation/paper-trading/render_ctl.py gen-key
```

Publish the current Fyers token from the box `.env` (never print it):

```
python automation/paper-trading/render_ctl.py publish-token --env-file <box-env-file>
```

(`publish-token` reads the box `.env` and stores the token encrypted
in Turso; the render control CLI never prints it. `seed-token` does the
same from a token passed on stdin — see the render control CLI help.)

## 6. Create the service

1. In Render: New → Blueprint, point it at this repo. Render reads
   `render.yaml` (service `signal-atlas-paper`, region `singapore`,
   plan `free`, `autoDeployTrigger: off`, health check `/health`).
2. Fill the prompted env vars from `deploy/render/.env.render.example`.
   For the **first deploy (the trial)**, use the trial values:
   `RENDER_MODE=trial`, `PAPER_NAMESPACE_OVERRIDE=paper_render_trial_v1`,
   `FYERS_AUTO_LOGIN=0`, `PAPER_RUNTIME=render`,
   `PAPER_SESSIOND_DISABLE=1`, `TZ=Asia/Kolkata`,
   `PAPER_STATE_DIR=/tmp/paper-state`. Do **not** set `FYERS_CLIENT_ID`,
   `FYERS_SECRET_ID`, `FYERS_PIN` or `FYERS_TOTP_SECRET` yet.
3. Deploy. The service starts idle: `/health` answers 200 with
   `idle_not_active_runner` until the handoff flag is set. Check
   `GET https://<service>.onrender.com/health` returns a JSON object
   containing the literal `"ok": true`.

## 7. Keep-awake pinger

Render spins the service down after 15 minutes without inbound HTTP
traffic. An external free pinger calls `GET /health` every 5 minutes
during market hours so the worker keeps running.

### cron-job.org (free)

1. Create a free account at https://cron-job.org.
2. Dashboard → Create cronjob. Title: `signal-atlas-paper health`.
3. URL: `https://<service>.onrender.com/health` (replace
   `<service>` with the Render service name).
4. Method: `GET`. Timeout: 30 seconds.
5. Schedule: every 5 minutes. Timezone: `Asia/Kolkata`.
6. Restrict to **Mon–Fri, 07:45–19:00 IST** (the pinger's time
   restriction / custom schedule — this is the minimum window the
   worker needs; it keeps the service awake through the 08:15
   pre-flight, the 09:15–15:30 session and the 16:10 EOD, and lets it
   sleep overnight).
7. Notifications: on failure (email), after 2 consecutive failures.
8. Save, then run it once by hand and confirm the run log shows
   HTTP 200.

### UptimeRobot (alternative or second monitor)

1. Create a free account, add a monitor of type **HTTP(s)**.
2. URL: `https://<service>.onrender.com/health`.
3. Monitoring interval: 5 minutes.
4. Enable **keyword monitoring** with the keyword `"ok": true`
   (exactly that, with the space — it is the literal substring in the
   `/health` JSON; see Task 2 of the rework brief). A 200 without the
   keyword, or a 503, both count as down.
5. Alert contacts: email (and SMS if the free plan allows it).

Run both if possible: cron-job.org keeps the service awake, and
UptimeRobot's keyword check proves the supervisor is healthy, not
just answering.

## 8. Stall / missed-ping alerts

- A **503 from `/health`** means the supervisor is degraded
  (lease lost, store unreachable, worker crash loop) — see
  §15 Troubleshooting.
- A **timeout or connection failure** usually means the service spun
  down (the pinger missed its window) or Render is restarting it.
  The next ping wakes it; the supervisor then catches up the jobs it
  missed (restart catch-up, 30-minute window).
- The box evening routine runs `python automation/paper-trading/render_ctl.py status`
  and flags a stale heartbeat: if the lease heartbeat is older than
  120 seconds during market hours, the runner is stalled.
- Later (not in this brief): the support agent can raise a finding
  when the lease heartbeat is stale during a session.

## 9. Trial runbook — Fri 20 Nov 2026

The box stays the real `paper_v2` runner all day. Render runs the same
engine in trial mode on the namespace `paper_render_trial_v1`, with its
own lease, its own job-run rows, its own capped Fyers budget and **no
automatic Fyers login**.

1. **Thu 19 Nov, before the market closes** (so the last completed
   session day is 2026-11-18): run the schema and the trial baseline
   from a machine with Turso access:
   `python automation/paper-trading/render_ctl.py migrate`
   `python automation/paper-trading/render_ctl.py trial-init --namespace paper_render_trial_v1 --bar 2026-11-18`
   This runs the real bootstrap for the trial namespace and compares
   its watermarks with `paper_v2`'s. It prints JSON with
   `watermarks_match_paper_v2: true`; a mismatch exits 2 and must be
   fixed before the trial. `--bar` is required; without it the command
   exits 2 and writes nothing. It makes no Fyers or network call.
2. Publish the current Fyers token from the box `.env` (after the
   morning refresh):
   `python automation/paper-trading/render_ctl.py publish-token --env-file <box-env-file>`
   Then confirm the token is usable:
   `python automation/paper-trading/render_ctl.py token-check`
   (add `--validate` for a live quote check — the senior runs this,
   not Muse).
3. Deploy the service with the trial env vars (§6) and start the
   pinger (§7). Confirm `/health` shows `mode: trial`,
   `namespace: paper_render_trial_v1`, `login_allowed: false` and the
   literal `"ok": true`.
4. During the day, check the trial from time to time:
   `python automation/paper-trading/render_ctl.py status`
   and the combined Fyers usage check below.
5. After the 16:10 EOD, compare the trial with the box:
   `python automation/paper-trading/render_ctl.py trial-compare --namespace paper_render_trial_v1 --session 2026-11-20`
   This prints the full report: signals and symbol sets per strategy,
   fills with price differences, open positions, scan batches, EOD
   presence, error counts and trial heartbeat gaps.

### Combined Fyers usage check

The trial and the box share one Fyers account, so their usage must be
checked together:

```
python automation/paper-trading/render_ctl.py budget-check --box-file <box-budget-file>
```

It reads the box budget file (`<box-budget-file>` is the box's
`fyers_request_budget.json`) and the trial budget from Turso, and
prints `within_limits`. The limits are: box ≤ 4000 requests/day,
trial ≤ 1000/day, and per-minute 40 (box) + 8 (trial) = 48 < 50.
Run it at midday and after EOD on the trial day. If `within_limits`
is false, do **not** start the Monday handoff — find the extra usage
first (`status` and `budget-check`), and stop the trial service if the
trial side is the cause.

> Whether a Render-side login invalidates the box's token is unknown.
> The trial never logs in (§4), so the trial cannot be the cause of a
> box token failure.

## 10. Monday handoff

Do this the evening before Mon 23 Nov (Sun) or before 08:15 Mon,
in this order:

1. **Stop the box runner first.** Senior stops the box worker,
   guardian and sessiond, and disables the box routines (08:45 scan,
   the three 09:15–15:30 worker crons, 16:10 EOD, evening adaptive,
   evening support). Verify nothing of the box is running
   (`status` must show no box lease or heartbeat).
2. **Switch Render to live.** In the Render dashboard set
   `RENDER_MODE=live`, delete `PAPER_NAMESPACE_OVERRIDE`, set
   `FYERS_AUTO_LOGIN=1`, and add `FYERS_CLIENT_ID`,
   `FYERS_SECRET_ID`, `FYERS_PIN` and `FYERS_TOTP_SECRET` (§4).
   Redeploy.
3. Hand the flag to Render:
   `python automation/paper-trading/render_ctl.py handoff --to render --yes`
   (without `--yes` it only prints what it would do).
4. Confirm `/health` shows `mode: live`, `namespace: paper_v2`,
   `login_allowed: true` and the literal `"ok": true`, and that
   `status` shows the Render lease as active.
5. Delete the trial data (dry run first — it prints what would be
   deleted and deletes nothing):
   `python automation/paper-trading/render_ctl.py trial-purge --namespace paper_render_trial_v1` (dry run)
   then, once the dry run looks right:
   `python automation/paper-trading/render_ctl.py trial-purge --namespace paper_render_trial_v1 --yes`
   (without `--yes` it only prints what it would do; it refuses
   `paper_v2` and every non-trial namespace).
6. Confirm the box worker is stopped and stays stopped; the box guard
   (`PAPER_RUNNER_ENFORCE=1`) refuses box mutating commands while the
   flag names Render.

## 11. Cutover sequence

The exact order, with the command for each step:

1. Precondition: the deployed commit's `config.json` namespace is
   `paper_v2`. The supervisor refuses to start on `paper_v1` or
   `paper_demo_v1`, and `/health` shows the namespace.
2. `python automation/paper-trading/render_ctl.py migrate`
3. Seed the Fyers budget from the box counters (so a restart never
   resets them):
   `python automation/paper-trading/render_ctl.py seed-budget --from-file <box-budget-file>`
4. Deploy with the flag still naming the box. Render starts idle
   (`/health` 200, `idle_not_active_runner`).
5. `python automation/paper-trading/render_ctl.py net-check` (Turso
   reachable, Fyers reachable; NSE may answer 403 to cloud IPs — see
   §15) and `python automation/paper-trading/render_ctl.py dry-run --fake-clock`
   (the full day in simulation; no live calls).
6. Stop the box runner and disable the box routines (§10 step 1).
7. `python automation/paper-trading/render_ctl.py handoff --to render --yes`
8. Watch `/health` and `status` for two worker cycles (about a
   minute), then through the first live jobs. Cut over in the evening
   where possible, so the 08:15 pre-flight and 08:45 scan are the
   first live runs.

## 12. Rollback to the box

1. Hand the flag back:
   `python automation/paper-trading/render_ctl.py handoff --to box --yes`
2. Confirm Render `/health` reads `idle_not_active_runner` and the
   Render lease expires (about 60 seconds).
3. Restart the box worker (the box `ensure-session` routine) and
   re-enable the box routines (§10 step 1, in reverse).
4. Confirm the box heartbeat is fresh in `status`. Keep
   `PAPER_RUNNER_ENFORCE=1` on the box until the Render service is
   deleted, so a stray box command cannot run while Render is live.
5. The trial namespace is untouched by a rollback; delete it with
   `trial-purge` only when the trial is finished (§10 step 5).

## 13. Never-double-run rules

- **One active runner.** The Turso lease (`paper_runner_lease`) plus
  the handover flag (`active_runner` in `paper_runtime_kv`) plus the
  box guard (`PAPER_RUNNER_ENFORCE=1`) together guarantee at most one
  runner is active. Verify with `status`: it prints the namespace,
  the flag value, the lease owner and epoch, and the last job run
  times. If two runners ever both report a held lease, stop both and
  tell the senior — do not try to fix it by hand.
- **Trial and box side by side** is the one exception, and only
  because the trial has its own namespace, its own lease, its own
  job-run rows and its own capped Fyers budget — and it never logs
  in. The trial writes only rows of `paper_render_trial_v1` plus the
  trial `_global` keys; it never writes a `paper_v2` row.
- **Never start the Render worker while the box worker is running
  for the same namespace.** The flag is the switch; the lease is the
  lock. Change the flag only with `handoff`, never by editing Turso
  by hand.
- **Overlapping deploys** are safe: the new instance cannot take the
  lease until the old one releases it.

## 14. Calendar renewal

The holiday calendar in `config.json` is valid through **2026-12-31**;
after that the engine fails closed (no scan, no EOD). `/health` shows
`calendar_valid_through` and `calendar_days_left` — check the days
left each week. Renewal is a `config.json` change by the senior (a new
holiday list for the next year), deployed like any other change; it is
not something this service can do for itself.

## 15. Troubleshooting

- **`/health` 503, `lease_lost`.** The flag names Render but the lease
  is not held — another runner may hold it, or the store is
  unreachable. Check `status`, then the service logs.
- **`/health` 503, `degraded`.** The worker crashed more than the
  backoff allows, or the store is unreachable. Check the service
  logs for the crash reason; check `token-check` if the crash is an
  auth error.
- **`/health` 200, `idle_not_active_runner`.** The flag names the box
  (or is unset). This is correct before handoff and after a rollback;
  it is wrong after a handoff — re-run `handoff` (§10 step 3).
- **Auth circuit open.** The Fyers token is stale or the login failed
  repeatedly; the engine stops calling Fyers until the circuit closes.
  Re-publish the token (§5) and watch `/health` `auth.circuit_open`.
- **Budget exhausted.** `budget-check` shows the trial or the box at
  its daily cap. The trial cap is a hard maximum (1000/day); it cannot
  be raised. Wait for the next day.
- **Reference feeds fail / NSE 403.** NSE can answer 403 to cloud IPs;
  `net-check` shows this. The reference feeds fail closed (no scan).
  Do not work around it with a different User-Agent — tell the senior.
- **`/health` is slow or empty.** The supervisor is starting (cold
  start, about 1 minute). Wait and retry; the snapshot is in memory
  after the first tick.
- **`/health` fields.** `mode`, `namespace`, `state`, `role`,
  `login_allowed`, `trial_budget` (trial only), `box_conflict_check`
  (trial only), `fyers_budget`, `auth.circuit_open`, `git_sha`,
  `version`, `session_open`, `calendar_days_left` — see Task 6.3 of
  the brief for the full list. Fields that need a store read are
  present with a null value; they are filled in only if the senior
  decides the tick may read the store.

## Adaptive stays on the box

The adaptive sidecar keeps ~435 MB of local SQLite state
(`state/adaptive.sqlite` 308 MB, `state/market.sqlite` 63 MB) and
would not fit the free tier. It stays on the box; only the evening
adaptive run and the support runner remain there. Measured: the
adaptive runner's light phases are 68–69 MB, and the engine on
fixtures peaks at 88 MB — memory is not the blocker, local state is.

## Footprint

Measured with the render control CLI (`python automation/paper-trading/render_ctl.py footprint --seconds 5`,
fixture: 7 strategies x 9 positions, 50 symbols, 60 bars):

| Job | Peak RSS (MB) | CPU (s) |
|---|---|---|
| preflight | 82 | 0.75 |
| scan | 123 | 1.77 |
| eod | 85 | 0.83 |
| sync_history | 92 | 0.71 |
| support | 87 | 0.88 |
| revalue | 83 | 0.79 |
| worker_cycle | 91 | — |

`worker_cpu_seconds_per_cycle` 0.05–0.07 s (limit 2.0),
`max_peak_rss_mb` 123 (limit 420), `baseline_idle_rss_mb` 77.
**Verdict: limits OK.**
