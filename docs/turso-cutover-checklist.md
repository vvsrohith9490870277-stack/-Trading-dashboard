# Turso cutover checklist — move off `tradinghistory-rohithvvs` to a new free Turso DB

For Grok (runs all live parts). Muse built and tested everything on this
branch; nothing here is pushed, merged or deployed by Muse, and creating the
DB / editing env files is out of scope for Muse.

Context: the old DB `tradinghistory-rohithvvs` blocks ALL reads (plan limit,
"SQL read operations are forbidden"); `/dump` is blocked too (HTTP 403), so
no data can be copied out of it right now. Target: Monday 23 Nov 2026,
08:45 IST scan, on the new DB. Report due Sun 22 Nov 14:00 IST; cut over
Sunday evening.

Golden rule, before anything else: **copy the current (old) values of every
env var below somewhere safe first** (Grok's own notes, not the repo, not
chat). Rollback needs them, and the old DB cannot be read to recover them.

## 0. Prerequisites (before the steps below)

- [ ] New free Turso DB created (VVS / Grok — out of scope for Muse).
- [ ] Two tokens for the new DB: one **read-write** token (box + Render both
      write) and one **read-only** token (Vercel dashboard only, least
      privilege — see `README.md:16`: "Use a read-only Turso token").
- [ ] New DB URL + tokens to hand — never paste them in chat or in the repo.
- [ ] This branch's scripts on the box: `automation/paper-trading/tools/`
      (`bootstrap_new_db.py`, `refill_history.py`, `import_history_sqlite.py`,
      `check_paper_v2_empty.py`).

## 1. Create the new DB and bootstrap it

- [ ] Grok creates the new free Turso DB (out of scope for Muse).
- [ ] Grok runs the bootstrap script against the **new** DB:
      `automation/paper-trading/tools/bootstrap_new_db.py` (see that
      script's `--help` for how it takes the new DB). It creates every
      table/index the app needs, idempotently — safe to re-run.
- [ ] **Check the URL before running it.** Running the bootstrap (or the
      refill) against the OLD database URL would write to the wrong DB and
      burn Fyers calls. The old URL contains `tradinghistory-rohithvvs`;
      the new URL must not.
- [ ] Expected: bootstrap completes without error and the new DB holds every
      table the app needs (73 tables per the schema inventory — see the
      Turso-move report for the list).

## 2. Refill history

- [ ] Grok refills history with `tools/refill_history.py`: **694 Fyers
      history calls** (692 universe symbols + 2 benchmark), fits in one day
      (budget ≤ 3000/day; code cap 2500 after the 1500-call monitor reserve).
      Wall time floor ~15–18 min.
- [ ] And/or Grok imports from the box's local copy with
      `tools/import_history_sqlite.py` (source:
      `/workspace/adaptive-live/state/market.sqlite`, if it already holds
      history). Either path fills `daily_ohlcv` + `index_ohlcv`.
- [ ] Expected: every universe symbol has ≥ 253 sessions of bars (prefilter
      beta needs 252 + 1; adaptive candidates need 253). Verify with the
      refill script's own report — do not proceed on a partial refill.

## 3. Prove paper_v2 is empty (box, before any writer touches the new DB)

- [ ] On the box, in `/workspace/paper-trading`, run:
      `python3 tools/check_paper_v2_empty.py --days 2026-11-19,2026-11-20`
- [ ] Expected: `VERDICT: EMPTY`, `blocked signals (not fills): 17`
      (the 17 BUYs of 20 Nov, all blocked with reason
      `missing_or_failed_current_prefilter_proof`), 0 fills, 0 positions,
      0 realised P&L. Exit code 0.
- [ ] If it prints `NOT EMPTY` (exit 2), STOP: list what it found, tell VVS,
      do not cut over fresh. If it prints `ERROR` (exit 1), it names the
      missing/unreadable file — usually a missing `out/audit-<day>.jsonl`
      (run the audit export first; sessiond exports at 20:00 IST) or a
      missing `logs/engine.jsonl`.
- [ ] Also run the Turso-side check against the new DB (2 reads):
      `python3 tools/check_paper_v2_empty.py --turso`
      Expected: `db (turso (db.mjs)): 0 open positions, 0 fills`.
      This is the same two counts the script runs:
      `SELECT COUNT(*) FROM paper_positions WHERE namespace='paper_v2'`
      and `SELECT COUNT(*) FROM paper_fills WHERE namespace='paper_v2'`.

## 4. Change the env vars — order: box → Render → Vercel

Only these two variable names change anywhere. The Fyers keys in the box
`.env` (`FYERS_APP_ID`, `FYERS_ACCESS_TOKEN`, …) do **not** change.

| # | Where | Vars to change | Read by (file:line) |
|---|-------|----------------|---------------------|
| 1 | Box `/workspace/sahil-fyers-turso/.env` | `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` → new DB URL + **read-write** token | `automation/paper-trading/db.mjs:14`, `automation/paper-trading/db_helper.mjs:13`, `live/turso_ro.mjs:43-49` (all read that `.env` file; `PAPER_ENV_FILE` overrides the path) |
| 2 | Render dashboard, service `signal-atlas-paper` | `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` → new DB URL + **read-write** token | `automation/paper-trading/render_state.py:227-236` `open_store_from_env` (on branch `feat/render-deploy`; Render reads process env, no `.env` file) |
| 3 | Vercel dashboard (dashboard project) | `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` → new DB URL + **read-only** token; `PAPER_NAMESPACE` stays `paper_v2` | `server/data.mjs:50,58`; `.env.example` (same three names) |

Why this order:

1. **Stop both writers first.** Stop the box worker and the Render
   supervisor before changing anything, so nothing writes to the old DB
   mid-move and nothing writes to the new DB before it is bootstrapped
   and refilled.
2. **Box first.** The box is the only writer of paper_v2 state, and Monday's
   scan runs on the box. Restart the box worker after its env change and
   confirm it ticks (see §5) before touching anything else.
3. **Render second.** The Render trial writes to its own namespace
   (`paper_render_trial_v1`) but shares the DB (leases, `paper_runtime_kv`).
   Save the new values in the Render dashboard; Render restarts/redeploys
   the service — confirm it is back up via `/health` (see §5) before
   proceeding.
4. **Vercel last.** The dashboard is read-only. Flipping it last means it
   never shows a half-moved state: flipped first, it would show the empty
   new DB while the box was still writing the old one. After changing the
   Vercel env vars, **redeploy** from the Vercel dashboard — env changes
   only apply to new deployments.

## 5. Verify after cutover

Do these in order; every one must pass before Monday 08:45 IST.

- [ ] **Box worker:** worker ticking, no errors in `logs/engine.jsonl`;
      re-run the paper_v2 check with `--turso` — still `EMPTY`
      (nothing has been written yet).
- [ ] **Render `/health`:** `GET https://signal-atlas-paper.onrender.com/health`
      → 200, `ok: true`, worker running, `last_tick` current. (`/health`
      itself costs 0 Turso reads.)
- [ ] **New DB, direct counts** (read-only query, e.g. via the dashboard
      SQL shell or the check script's `--turso`):
      `SELECT COUNT(*) FROM paper_positions WHERE namespace='paper_v2'` → 0;
      `SELECT COUNT(*) FROM paper_fills WHERE namespace='paper_v2'` → 0;
      `SELECT COUNT(DISTINCT symbol) FROM daily_ohlcv` → 692 (plus the
      benchmark in `index_ohlcv`).
- [ ] **Dashboard:** open it — overview / fills / positions / signals views
      show namespace `paper_v2`, empty fills and positions, and the history
      view shows the refilled bars. If the dashboard shows a setup error
      naming `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN`, the Vercel env or
      the redeploy is the problem (`server/data.mjs:58` lists the messages).
- [ ] **Monday 08:45 IST scan:** the scan runs on the box against the new DB
      and the worker, EOD and dashboard all follow from it.

## 6. Rollback — point everything back at the old DB

Rollback is the exact reverse of §4, using the old values saved before the
cutover:

1. Stop the box worker and the Render supervisor.
2. Box `/workspace/sahil-fyers-turso/.env`: restore the old
   `TURSO_DATABASE_URL` + `TURSO_AUTH_TOKEN`.
3. Render dashboard (`signal-atlas-paper`): restore the old values; confirm
   via `/health`.
4. Vercel dashboard: restore the old values; redeploy.
5. Restart the box worker.

What happens to data written to the new DB during the trial — stated plainly:

- **It stays in the new DB. There is no merge procedure.** Nothing in this
  repo copies fills, positions or EOD rows from one Turso DB to another, and
  no such procedure was invented for this move.
- If rollback happens **before Monday's scan**, nothing is lost: the new DB
  holds only history + an empty paper_v2.
- If rollback happens **after fills exist in the new DB**, those fills stay
  in the new DB and are orphaned: the live book resumes from the old DB's
  paper_v2 state (empty, as proven in §3), and the new-DB fills are not part
  of it. They remain queryable in the new DB, but they do not come back.
- Rollback also restores the **broken** state: the old DB still blocks reads,
  so after rollback the dashboard reads fail again until the monthly reset
  or a paid upgrade. Rollback is an undo of the move, not a fix for reads.
- Do not run a partial rollback (e.g. box only). If in doubt, stop and ask
  VVS before changing any env var.

## 7. What NOT to do

- **Never delete, write to, or "clean up" the old DB.** The paper_v1 archive
  stays there; its reads come back at the monthly reset or after an upgrade.
  Nothing in this move touches it.
- Never run the bootstrap or refill against the OLD database URL (§1).
- Never change the frozen files (§8) or invent a policy number.
- Never carry positions or fills into the new DB — the §3 check proves there
  are none to carry, and a manual carry would break the fresh start.
- Never paste a DB URL or token in chat, in the repo, or in a report. The
  check script never prints secrets; keep it that way.
- Never edit env files on the box / Render / Vercel from this machine —
  Grok does those, on the box / in the dashboards.

## 8. Fresh-start plan for paper_v2 (what "fresh" means)

paper_v2 starts **fresh** in the new DB under the **same frozen rules** —
nothing about the strategy changes, only the database does.

- **Capital (unchanged):** ₹35,00,000 total, ₹5,00,000 per strategy
  (7 strategies × ₹5L = ₹35L), ₹50,000 per trade — from
  `automation/paper-trading/capital_policy.json`, version
  `freeze_capital_v2` (`master_capital_inr` 3500000, `sleeve_capital_inr`
  500000, `target_notional_inr` 50000).
- **Frozen files that must NOT change** (all under `automation/paper-trading/`):
  `capital_policy.json`, `charge_policy.json`, `charge_rates.json`,
  `strategy_rules.json`, `freeze_lock.json`. (The freeze version
  `freeze_capital_v2` is the `version` field inside `capital_policy.json`,
  not a separate file.)
- **Nothing carried over:** no positions, no fills, no realised P&L — the §3
  check proves there are none to carry. History (`daily_ohlcv` /
  `index_ohlcv`) is refilled from Fyers (§2); that is market data, not
  positions.
- **The 17 blocked 20 Nov signals are NOT re-entered.** Entry signals expire
  after their eligible session (`expired_entry_signal`); Monday's scan
  generates fresh signals under the same rules. Do not re-create them by hand.
- **First live session on the new DB:** Monday 23 Nov 2026, 08:45 IST scan,
  then the worker, EOD and dashboard as normal.
