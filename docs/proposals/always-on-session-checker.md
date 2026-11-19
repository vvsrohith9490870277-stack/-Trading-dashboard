# Always-on session checker on Vercel — proposal (PROPOSE ONLY, not deployed)

Status: **PROPOSAL ONLY — nothing deployed, no Vercel project created, no credentials touched.**
The senior takes this to the user. It needs a user decision and Vercel access that Muse does not
have.

## Problem, with evidence

The box VM pauses when no agent task runs, so the guardian, worker and `sessiond` can stop
mid-session. Monitor coverage in the audit: 40.7%, 65.4%, 94.8%, 52.2%, 0%, 0%, 2.2% on
9, 11, 12, 13, 16, 17, 18 Nov. On the 0% days nothing watched the session at all, and nothing
told anyone.

What already exists on the box (read-only sources the checker would use):

- `paper_monitor_health` (`schema.sql`): one row per namespace — `pid`, `heartbeat_at`, `status`,
  `detail_json`. Written by `automation.py:418` `heartbeat()` on every worker poll
  (`poll_seconds: 30` in `config.json`). The detail carries `mono_offset` and `boot_id`, so
  `frozen_since()` can tell a VM pause apart from a dead worker.
- `paper_guardian_health` (`schema.sql`, `guardian.py:23`): one row per namespace — `pid`,
  `process_start`, `session_day`, `heartbeat_at`, `status`. Written on every guardian check
  (`guardian_seconds: 60` in `config.json`, up to 480 checks per session).
- `paper_runs` (`schema.sql`): append-only — `kind`, `at`, `status`, `details_json`. Every scan /
  monitor / session run leaves a row.
- Stale budgets already in `config.json`: `worker_stale_seconds: 90`,
  `worker_polling_stale_seconds: 90`, `backoff_heartbeat_stale_seconds: 90` — a heartbeat older
  than 90 seconds is already "stale" by the worker's own definition.

## Proposal: a small Vercel cron/function that only reads heartbeats

- **What it is**: a Vercel Cron Job (every 5 minutes, only 09:15–15:30 IST) hitting one serverless
  function. The function runs three read-only Turso queries for namespace `paper_v1` and raises an
  alert when any heartbeat is stale:
  1. `SELECT heartbeat_at, status FROM paper_monitor_health WHERE namespace='paper_v1'` — alert
     if `heartbeat_at` is more than 5 minutes old (10 missed polls) during session hours.
  2. `SELECT heartbeat_at, session_day, status FROM paper_guardian_health WHERE namespace='paper_v1'`
     — alert if `heartbeat_at` is more than 5 minutes old or `session_day` is not today.
  3. `SELECT kind, at, status FROM paper_runs WHERE namespace='paper_v1' ORDER BY id DESC LIMIT 5`
     — alert if no run of any kind in the last 15 minutes during session hours (catches the case
     where the heartbeats exist but nothing is actually running).
  5 minutes is deliberately looser than the 90-second stale budget, so a single slow poll does not
  page anyone; a dead worker is still caught within ~10 minutes of 09:15.
- **Alert channel — needs a decision**: the function needs somewhere to send the alert. Options:
  (i) a WhatsApp message (the user already gets WhatsApp notifications from Muse, but a Vercel
  function cannot use Muse's WhatsApp — it would need a WhatsApp Business / Twilio sender, which
  needs an account and a decision); (ii) email to the user; (iii) a webhook the user already
  monitors. Pick one before building.
- **Constraints (hard)**: read-only — `SELECT` only, no writes to Turso, no engine control, no
  broker calls. No secrets in code: the Turso URL and a **read-only** auth token live in Vercel
  environment variables, never in the repo. The function must not be able to start, stop or
  change anything on the box; it only reads and alerts.
- **What would be installed where**: a new Vercel project (one cron function, ~100 lines, no
  dependencies beyond the Turso HTTP client); a Turso read-only auth token in Vercel env vars; the
  alert destination's credentials in Vercel env vars; nothing installed on the box and nothing
  changed in the repo.
- **What it must NOT do**: it must not restart the worker/guardian, must not place or cancel
  anything, must not write to any table, and must not hold credentials for the broker or for a
  write-capable Turso token.

## Risks

- A read-only token is still a credential: if it leaks, anyone can read the paper-trading tables
  (paper data only, no real money, no broker access — but it must be rotated if exposed).
- False alarms on days the market is closed: the cron must skip non-session days (check the
  calendar first — a stale heartbeat on a holiday is not an alert). The session window 09:15–15:30
  IST must come from the same calendar the engine uses, not a hard-coded list.
- Vercel cron granularity is 1 minute at best on paid plans and the free tier runs daily at most
  — a 5-minute cadence may need a paid plan; that cost needs the user's approval.

## Decision needed

Approve (i) the alert channel, (ii) a Vercel project + a Turso read-only token, and (iii) the
5-minute / 15-minute thresholds. Only then should anyone build it. Do not deploy anything on
this proposal alone.
