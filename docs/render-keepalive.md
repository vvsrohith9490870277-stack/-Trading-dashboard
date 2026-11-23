# Render keepalive: keeping signal-atlas-paper awake on the free tier

## Why this is needed

The Render free tier sleeps a web service after about 15 minutes
without HTTP traffic. `signal-atlas-paper` serves `/health` over HTTP,
so it is a web service and it sleeps. When it sleeps, the supervisor
(and with it the in-process scheduler, the worker loop and the support
monitor) stops. It wakes on the next HTTP request, but any job whose
slot passed while it slept is missed — the scheduler's catch-up only
covers a missed scan (before 15:00) and a missed EOD (after 16:10),
so a long sleep can still lose the pre-flight, the worker hours and
the history sync.

The fix on the free tier is an external pinger: something outside
Render that hits `/health` every 10 minutes during the trading day,
so the service never goes 15 minutes without traffic.

## The pinger to set up (free, cron-job.org)

1. Open https://cron-job.org and create a free account (email only,
   no card).
2. Create a cron job:
   - **Title:** `signal-atlas-paper keepalive`
   - **URL:** `https://signal-atlas-paper.onrender.com/health`
   - **Schedule:** every 10 minutes, weekdays (Monday–Friday),
     08:00–18:00, timezone **Asia/Kolkata**.
     As a cron expression that is `*/10 8-18 * * 1-5` in the
     Asia/Kolkata timezone (08:00, 08:10, …, 17:50, 18:00).
   - Leave the defaults for everything else (GET, no auth, no body).
3. Save and enable it. cron-job.org shows the last run and the HTTP
   status; `/health` answers 200 while the service is up and 503
   while the lease is lost, so a 200 in the log means the pinger is
   working.
4. Check it once on the first trading day: open the cron-job.org
   execution log and confirm a 200 every 10 minutes between 08:00
   and 18:00 IST. If a run shows an error, the service may have
   slept — the next ping wakes it, and the scheduler catches up the
   scan (before 15:00) and the EOD (after 16:10) on its own.

The pinger only needs to run on weekdays, 08:00–18:00 IST. Outside
those hours the service may sleep; the scheduler starts the day
fresh at 08:15 pre-flight when the first ping wakes it.

## /health is cheap

A ping costs the service nothing measurable: `/health` is served
from an in-memory snapshot (the supervisor refreshes the store
fields at most once every 300 s in the background). A ping does no
Turso read, no Fyers call and no job run. The pinger's 61 pings a
day (08:00–18:00 every 10 minutes) add no Turso reads and no Fyers
requests.

## Would the paid Starter plan remove the need for the pinger?

Yes. On Render, paid instances (Starter and above) do not sleep:
they stay running around the clock, so the scheduler would never
need an external ping to stay awake. The free tier is the only one
that sleeps after ~15 minutes without traffic.

This project stays on the free plan, so the pinger above is the
way the service stays awake. If the plan ever changes to Starter,
the cron-job.org job can simply be deleted — nothing in the service
depends on it.
