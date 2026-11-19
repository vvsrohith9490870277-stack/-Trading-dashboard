# Signal Atlas

A public, responsive **React JSX paper-trading observatory** with a server-side, read-only Turso API. Built with Vite, React, Recharts and Lucide. No login, real-order interface, Fyers credentials, broker client or browser database token.

## Run locally

Requires Node **24.x** and npm.

```bash
npm ci
# Choose ONE: provide server variables in an ignored .env.local, or read an existing external env:
LOCAL_TURSO_ENV_FILE=/absolute/path/to/existing.env npm run dev
# Default: http://127.0.0.1:5173
```

Only `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` and optional `PAPER_NAMESPACE` are read from the external file. The external file is not copied into this project. Alternatively copy `.env.example` to `.env.local` and fill server-only values. **Use a read-only Turso token**, especially for public deployment. Existing local credentials may have broader rights; application code itself executes SELECT only.

```bash
npm test
npm run lint
npm run build
LOCAL_TURSO_ENV_FILE=/absolute/path/to/existing.env npm run check:secrets
node scripts/smoke-live.mjs   # against running local server; performs only GET reads + rejected POST
# Built preview, still with its real server-side API:
NODE_ENV=production LOCAL_TURSO_ENV_FILE=/absolute/path/to/existing.env PORT=5174 npm run preview
```

Do not use `vite preview` alone for live data: it has no API. Both provided dev/preview commands host the same API. The local server binds loopback only; its localhost URL is on the box, not the viewer's own computer. Port is configurable with `PORT`.

## Render deployment (Track A backend) — see deploy/render/DEPLOY.md

The paper-trading backend (supervisor + engine) can run on Render's free
tier with all state in Turso; the dashboard and the adaptive sidecar stay
where they are. The service starts idle and runs nothing until the
`active_runner` handoff flag names `render`.

## Vercel deployment — next steps, not automatically deployed

1. Import this GitHub repository in Vercel. Select Vite; build `npm run build`, output `dist`, Node 24.x. The root `api/dashboard.js` is a Node serverless function; `vercel.json` supplies runtime/security configuration. No SPA deep-link routes are required: sections switch in React at `/`.
2. Set **server-side only** environment variables in Vercel:
   - `TURSO_DATABASE_URL`: existing Turso database URL.
   - `TURSO_AUTH_TOKEN`: a **read-only**, least-privilege token for that database.
   - Optional `PAPER_NAMESPACE` (paper_v2 or paper_v1 if set; it does not select the book).
3. Deploy and verify `/api/dashboard?view=overview` responds with namespace `paper_v2`, then inspect UI. Never set `VITE_…` / `NEXT_PUBLIC_…` versions of credentials. No Fyers variable is needed.
4. The public site intentionally exposes selected paper fills, positions, P&L, signals and non-sensitive operational summaries. Anyone with its URL can read these. It does not publish raw run JSON, database identifiers/URLs, account credentials, or broker errors.

Deployment itself, GitHub commit/push, and visual browser QA are separate review steps. No deployment was performed by the implementation script.

## Live data and public API

`GET /api/dashboard` defaults to an overview of the active book, production `paper_v2`. Add `book=paper_v1` to read the archived paper_v1 book instead. Allowlisted views: `overview`, `fills`, `positions`, `signals`, `runs`, `quotes`. List views accept `page=1`, `limit=25` (max100), `scope=all|today`. Only fills/positions/signals accept `strategy=1..7`. Unknown keys, client SQL, namespaces and invalid pagination are rejected. All DB inputs are bound; table/projection/order strings come from a fixed server map. No migration or write API is provided. All non-GET methods are rejected.

Data is read on page load, manual refresh and every five minutes when enabled. There is a 20-second in-memory server cache, bounded200 entries, and a best-effort90 requests/minute/client limiter, bounded2000 buckets. Serverless instances do not share these counters: use Vercel's edge/platform protections for stronger public abuse controls. No CORS is opened. Responses disable downstream HTTP caching. Rate-limited clients receive429. Error responses/logs are sanitized; public event reason/status fields are allowlisted. Public fields are intentionally selected, not `SELECT *` from paper tables.

The existing paper schema is assumed and is **not modified**. Eight tables are queried: `paper_fills`, `paper_positions`, `paper_signals`, `paper_pnl`, `paper_runs`, `paper_quote_audit`, `paper_watermarks`, `paper_data_audit`. The ninth engine table, `paper_meta`, is not exposed. No existing historical market table is queried by the web app.

- Closed positions (`qty=0`) are excluded from open positions/P&L.
- Filled orders include both buys and sells; closed trades count sells, not orders.
- Realized P&L is the sum of stored fill realized P&L. Unrealized = sum `(last mark − entry) × qty` across open positions. Missing/invalid marks produce N/A, not bogus zero. Last-known marks older than60s or over5s future are stale. A page refresh is **not** a Fyers quote refresh.
- Daily P&L comes from that day's actual engine snapshot: `ending marked value − starting quantity × prior-session close + sell proceeds − buy payments`. Do not confuse it with entry-basis realized profit on today's exits. Daily snapshots are never summed as cumulative P&L.
- Charts show actual stored daily MTM snapshots, up to366 recorded days, without invented intermediate days or a fabricated equity curve. Missing days are absent, not zero.
- Engine signal candles are completed bars. Eligible next-session paper fills use the first fresh **observed Fyers LTP**, not claimed historical opening prices. Polling may miss stops. Live strategy scanning/execution remains the separate paper engine; this dashboard cannot create orders or supervise it.
- All displayed timestamps use Asia/Kolkata (IST). Raw downloadable CSV source timestamps retain their explicit offsets. The reviewed calendar is Nov–Dec2026; future calendar uncertainty is shown closed/review-required. It mirrors the existing engine configuration; update it when the engine calendar is reviewed.
- Live server pages are bounded25 rows: their search, sort and CSV apply to the current page (clearly labeled). Historical tables search/sort/download their entire selected dataset and paginate locally.

## Saved historical research — separate from the paper ledger

`public/data/historical.json` is a deterministic export of the original seven result folders and overlap analysis. Snapshot session **6 Nov2026 IST**, generated7 Nov2026. It contains seven-strategy comparison, full exact rules (seeds/recurrences/warmups), all qualifiers, all751 per-strategy filter audits, ordered funnels, top/bottom five closed trades, closed-trade exit-year/month statistics, per-stock analytics and full overlap/matched-stock performance.

```bash
npm run export:history  # requires original /workspace/strategies/ files; no strategy rerun
npm run build          # redeploy to publish a new saved research snapshot
```

The exporter asserts counts/mean/median against original reports, filled=closed+open, all per-symbol entry flags, qualifier counts and final ordered-funnel counts. It does not refresh market data, alter rules or rerun trades. `historical-verification.json` includes source reconciliation and the export's SHA256. Only allowlisted aggregate data is exported; no original raw OHLCV dumps, credential files, or full historical ledgers are bundled.

### Interpretation / known data limitations

- Gross zero costs; no equal-capital allocation, leverage assumptions, funded capital, CAGR, portfolio return/DD, compounding or risk-adjusted ranking.
- Historical mean/median are arithmetic **closed-trade returns**, not monthly/yearly portfolio returns. Open trades excluded. Exit-period groups with no closed observations are omitted, not zero.
- Fixed751 current universe (692 current,52 missing,7 stale) causes survivorship bias; adjustment consistency and missing sessions remain unverified.
- Two zero-OHLC18 Jun2026 bars (BAJAJ-AUTO, NAM-INDIA) were excluded only in Strategy7. Earlier six reports remain unchanged. RSI includes an invalid BAJAJ-AUTO exit at zero (−100%). Comparison is provisional; outliers are data-sensitive, not recommendations. The live paper engine uniformly rejects bad OHLC.
- Qualifiers include already-held conditions, not just fresh pending orders. EMA/MACD/Supertrend crossover strategies use actual events, never all bullish states.
- Overlap:156 distinct qualifying stocks,196 memberships,38 multi-strategy stocks; highest overlap3. Exactly7/6/5/4 all empty. Mean-of-stock-strategy-means and median-of-stock-strategy-medians are descriptive only, with unavailable histories omitted and denominators disclosed. Pooled observations can double-count exposure and are not independent or a combined strategy.

## Verification and review

Unit/DOM fixtures are local test-only and never inserted into production. Tests cover read-only methods/namespace/parameter binding, null/zero/stale P&L, calendar/IST boundaries, closed-position exclusion, error/token redaction, rate limits, honest empty states, React rendering escaping, full table search/pagination and the real page-load/manual/five-minute/auto-off refresh flow. `scripts/smoke-live.mjs` performs a real HTTP read-only Turso roundtrip and validates every public view plus historical qualifier counts. `scripts/check-secrets.mjs` silently compares all project/build files against values in an optional authorized external env, and rejects server credential identifiers in client assets. It never prints a secret.

No browser visual QA or live-market execution should be inferred from build/unit tests. Inspect desktop/mobile overview, ledger, research selector, filter audit, overlap and error/empty states before committing. Public deployment and live-session fill validation require their own checks.

## Beginner redesign (default view)

The default journal has five sections only: today’s status, visual open-position cards, cumulative paper P&L, recent signals, and a simple performance scorecard. Advanced tools are **off by default**, and the large historical JSON is fetched only after opting in. Calm closed-session/empty states are neutral; negative P&L is red and connection problems are amber. No missing database read is displayed as a successful zero ledger.

There is **no funded starting capital**, so the requested equity curve is honestly labeled **Cumulative paper P&L**. It plots each actual snapshot’s total realized + last-known unrealized P&L (`total`), not a running sum of daily snapshots. A single real snapshot is a single point; there is no seeded cash, invented value, or example trend. Position sparklines use only accepted recorded quote audit observations. Less than two observations shows a waiting message. Position cards show strategy-rule explanations, not invented historical indicator values.

Win rate uses closed paper SELL fills, with strictly positive realized P&L as wins. Average hold is the mean calendar-day difference between each SELL and its preceding BUY for the same namespace/strategy/symbol. Unmatched exits are omitted from the average, with the matched denominator shown. These metrics never use historical backtest trades.

### Safe connection diagnostics

Canonical server variables remain `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN`; optional `PAPER_NAMESPACE`, when set, must be paper_v2 or paper_v1; it does not select the book. Surrounding whitespace/newlines and matching accidental surrounding quotes are normalized server-side. No unsafe browser-variable alias or client token fallback is accepted. The existing local external-env loader still reads only the Turso keys.

The public GET API returns safe actionable error codes, never raw exceptions or credentials:
- `CONFIG_MISSING_URL` / `CONFIG_MISSING_TOKEN`: named server variable absent.
- `CONFIG_URL_INVALID` / `CONFIG_NAMESPACE`: configuration shape invalid.
- `DB_AUTH_REJECTED`: server credential rejected (replace the server token and redeploy).
- `DB_SCHEMA_MISSING`: configured database lacks the paper schema.
- `DATA_UNAVAILABLE`: other connectivity/service failure.

Configuration is read at server runtime in the root Vercel Node function, not injected into the Vite bundle. Set variables for the correct **Production** project/environment and redeploy after changing them. A successful local connection does not prove the production environment is configured correctly. Production diagnostics/environment updates require authorized Vercel access; this code change does not itself set Vercel secrets.

New bounded endpoint: `GET /api/dashboard?view=history&symbol=RELIANCE-EQ` reads up to60 accepted positive quote observations in the fixed namespace, deduplicates exchange timestamps, and returns chronological points. Symbols are validated and bound parameters; arbitrary SQL/namespaces remain forbidden. It does not contact Fyers, write data, or imply stored quotes are current.

### Initial production diagnostics history — 7 Nov 2026 IST

At the initial diagnostic stage, existing paper-system Turso credentials were verified through the local server and all seven API views passed read-only database roundtrips; production configuration was not yet verified. The Vercel connector could list the repository-linked project, but project details, environment metadata and runtime logs returned **403 team-scope access denied** for its owning team. No production environment variables were changed and no credential values were exposed. Private team-scope operations were not retried or bypassed. Subsequently the public9a93752 release was verified serving live analytics and real Turso reads at13:30 IST; further releases still require their own public checks. A GitHub push/redeployment alone is not proof that the production database connection is fixed.

## Isolated manual visibility test

A user-requested **Dummy Test** is stored in the separate additive `paper_demo_trades` table, fixed namespace `paper_demo_v1`. It is **not** a row in `paper_positions`, `paper_fills`, `paper_signals`, watermarks or quote audits. The engine only reads its original tables, so demos cannot trigger quotes/orders/stops/marking and cannot affect real paper P&L, win rate, holding time, signals or the curve. No existing engine schema, rule or automation behavior is changed.

The default dashboard displays a separately labeled manual-demo card and recent-test record. The entry price is explicitly user-supplied, **not a Fyers quote**. There is no fabricated current market price, quote timestamp, sparkline or realized/unrealized return. The card is excluded from the real open-position count and every scorecard statistic. Public read-only endpoint `GET /api/dashboard?view=demo` reads at most25 test records; it cannot create/update/delete any.

Local administration (server-side credentials only; no secret command arguments):

```bash
# Read existing manual test records
LOCAL_TURSO_ENV_FILE=/path/to/existing.env node scripts/manage-demo.mjs list
# Only when explicitly requested: additive migration + atomic, idempotent one-row insertion
LOCAL_TURSO_ENV_FILE=/path/to/existing.env node scripts/manage-demo.mjs insert-reliance --confirm-manual-test
# Only AFTER a separate explicit cleanup request; not run automatically:
LOCAL_TURSO_ENV_FILE=/path/to/existing.env node scripts/manage-demo.mjs delete-reliance --confirm-delete
```

Deterministic ID `dummy_reliance_manual_v1` prevents duplicate logical trades and preserves its original timestamp on retries. The administrative script is not imported by the public API. The migration creates only the new table; it does not alter/drop/reset existing data. SQLite tests use a private temporary database and verify mixed real/demo metric invariance, untouched engine tables, invalid-price/quantity rejection, idempotence and clear UI provenance. Removal, if requested later, is restricted to this exact demo ID/namespace/is_test flag.

### Refresh correctness

The former in-app Refresh could return the same actual `readAt` within the API’s20-second cache window. Manual refresh, initial load and five-minute automatic refresh now request validated `refresh=1`, bypassing that cache and executing fresh Turso SELECTs. Position/history and advanced-ledger refreshes do the same. Browser fetches use `cache: no-store`; normal API requests without the flag can still use the bounded20-second cache. The existing rate limiter applies before either path. Timestamps originate in the actual reader execution, never a frontend cosmetic update. A fresh database read still does **not** obtain new broker quotes or run a strategy scan.

## Strategy workspace: three screens

The new default landing screen has seven strategy cards. Select one to see **only** its complete all-filters-satisfied stock list, with actual company names/industry from the exact symbol+series universe, saved reference close, historical fresh/held status, exact numeric qualification reason and an honest rule-based trade plan. Select a stock for its dedicated backtest workspace: performance, checklist and trade-plan tabs. Hash links preserve strategy/stock/window and support browser back/forward; the Paper journal remains separately accessible with live Turso reads and the isolated manual demo.

- `public/data/strategy-catalog.json`: compact133KB verified catalog, snapshot **6 Nov2026 IST**; qualifier counts49/19/19/7/6/7/89. Coverage751 exact entries,692 current,52 missing,7 stale.
- `public/data/stock-backtests/<strategy>/<symbol>.json`:196 on-demand stock-strategy files, full saved closed histories for the qualified stocks. No arbitrary trade truncation; selected trades are searchable/paginated/exportable. Large full-universe data is not fetched for this flow.
- Rebuild deterministically with `python3 scripts/export_stock_backtests.py`. This reads existing CSVs/configs only; no strategy/data refresh, Fyers call, database write, new indicator or altered backtest rule. `stock-backtest-verification.json` reconciles every file’s trade count to prior per-stock reports and every qualifier’s complete checklist.

### Performance definitions (important)

1-week/1-month/1-year windows anchor to the last completed saved session,6 Nov2026, **not today’s wall clock**. Start boundaries are respectively30 Oct2026,6 Oct2026 and6 Nov2025. We include `exit_date > start` and `exit_date <= asOf`; monthly/yearly shifts clamp calendar-end dates. A trade may have entered before the window; its **whole** closed-trade return is grouped by exit date. Open trades are excluded. No observations means N/A, not zero win rate.

No funded capital exists. The requested equity visualization is therefore explicitly **Realized-trade return index**, initialized to100 independently for each selected window. Sequential, non-overlapping closed trades multiply `index *= (1 + gross_return_pct/100)`; the index is stepwise only at actual exit dates. It is NOT daily mark-to-market, a paper account balance, a portfolio equity curve or a sum of overlapping stocks. Closed-index maximum drawdown is `max((runningPeak - index)/runningPeak ×100)`, including the100 baseline. It excludes intratrade drawdown and should not be confused with portfolio risk. Invalid/zero-price, nonfinite, overflow or overlapping observations make the index/DD unavailable; original closed-trade statistics remain preserved and flagged, not silently cleaned. Known corporate-action-sensitive extremes remain provisional.

Charts (line, exit-month arithmetic-mean bars, win/loss/flat donut) expose actual values on hover/focus/click/tap, with accessible focus targets and persistent readouts/legend controls. Metric info controls explain definitions and examples. Exit-month bars are average **trade** returns, never monthly portfolio returns. There is no compounding across stocks or strategies. The UI is custom; no TradingView/Chartink product affiliation or external tracking embed is implied.

### Trade-plan limits

No invented fixed price targets or extra hard stops. Six strategies use their prescribed indicator/channel exits. Momentum alone has the prescribed10% highest-completed-close trailing stop; a90%-of-saved-close illustration is explicitly not an active order stop. Actual fills/high-water are unknown before an entry. Reference closes are frozen historical data, not live quotes. Historical next-open fills and live-paper first-fresh-observed-LTP execution are distinguished. Browsing an old fresh/held qualifier never creates a live order.

## Box-hosted paper automation source

Versioned engine source, additive schema, isolated tests, fee policy and operational commands are in [`automation/paper-trading`](automation/paper-trading/README.md). Vercel serves only this dashboard/read-only API; the session worker runs separately on the bot box and needs durable supervisor/scan/EOD routines. No credentials or runtime logs are part of that package. Gross legacy fields remain gross; modeled fees/net P&L and immutable entry-overlap cohorts have explicit contracts in its README.

## Automated Turso results and current scans

`Live results` (`#results`) is separate from frozen historical backtesting and the paper journal. Its Overview/Strategies/Overlap/Charges/Daily & weekly tabs read actual engine ledgers; manual demo rows never enter those queries. There is no funded equity curve or fabricated EOD history. Gross/net charts require real near-close EOD observations; incomplete baselines/marks make daily and weekly changes unavailable. Historical backtests remain gross, zero-cost research dated6Nov2026.

The seven-card selector now prefers the latest saved Turso completed-bar screen. Use its source switch for `Latest Turso scan` or `Saved historical research`. `#strategy=2&screen=live` shows current qualified stocks; add `&stock=ALKEM-EQ` for the real current checklist/plan. `#strategy=2&stock=ALKEM-EQ&window=1y&screen=research` preserves the frozen stock backtest. A newly qualified stock with no frozen stock-strategy file gets an honest historical-unavailable explanation, not a404 or invented trades.

Additional public GET views under `/api/dashboard`:
- `view=analytics`: bounded SQL aggregates over all real fills/costs, current marks, immutable ENTRY cohorts, policy, sanitized worker health, up to366 EOD records and verified weekly changes. Net accrued and projected exit-cost scenario are separate. Integer-paise bounds, cost/mark coverage and cashflow reconciliation are explicit.
- `view=scan`: latest saved completed bar, all7 qualification counts and current stock→strategy sets. Current overlap is NOT retrospective entry-performance attribution.
- `view=qualifiers&strategy=1&page=1&limit=12`: paginated qualified-only rows with fixed namespace; optional exact `symbol` or validated literal symbol search `q`. Maximum100rows/page, strategy1–7. Selected checklists/plans are read from saved engine JSON. Unknown historical membership does not block current qualification.

All views are SELECT-only, parameterized, namespace-pinned and share existing90/min rate limiting, bounded20-second cache and `refresh=1` bypass. New-schema failure degrades new views without breaking legacy journal reads. Manual refresh and5-minute auto-refresh reread the new views; UI read time comes from actual server queries, never a cosmetic clock change.

Server modules reuse the engine’s pure charge estimator and immutable policy via Node-supported JSON imports; no CLI, migrations, Fyers auth or broker methods are invoked by HTTP. Vercel is Node-backed read-only API/static UI only. Function tracing explicitly includes public instrument metadata, calendar and fee policy JSON. No env values or auth files are bundled. Read-only analytics may span a concurrent fill; reconciliation flags expose inconsistent coverage instead of claiming a reliable net figure.

Durable routines were confirmed saved by the coordinator7Nov2026 at12:52 IST: weekday08:45 scan;9:15–15:30 five-minute supervisor;16:10 EOD. These are future scheduled tasks, NOT proof of a successful live market session. Cadence is30-second polling, not every tick; restart/scheduler jitter and31Dec2026 calendar/policy review boundaries remain documented in the engine README.

### Two-layer daily entry screen
The live selector now distinguishes the versioned high-beta/strong-momentum Stage1 from the unchanged seven strategy Stage2. See `automation/paper-trading/prefilter_policy.json` and engine README for exact thresholds, alignment, unadjusted-price review rules and entry-proof guards. Historical backtests explicitly do **not** include this gate. The server reads bounded audit pages with `GET /api/dashboard?view=prefilter&page=1&limit=12`; no client SQL, broker endpoint or credentials are exposed. Existing paper accounting and the isolated manual demo are unchanged.

Live-input data quality: evidenced non-session rows are excluded non-destructively under `verified_live_inputs_v1`; raw candles, original seven formulas and frozen research are preserved. Explicit recent closure coverage is not claimed as a full historical calendar. See the engine README and `automation/paper-trading/session_input_evidence.json` for date-specific official evidence.

Release QA note (7 Nov2026): local visual QA confirmed the new funnel, current counts, ANGELONE both-stage checks and optional volume. Remaining full visual QA is deferred to the published public site because Chrome loopback access refused while the same healthy box server returned HTTP200. This is not represented as complete browser QA. Parent confirmed existing morning routine wording includes the new gate/input provenance; schedules and execution safety are unchanged.

### Local monitoring and review logs

The paper worker targets30-second checks during verified weekday09:15–15:30 IST sessions. A new owned60-second local guardian can be armed before the08:45 scan using `run.sh ensure-session`; the native five-minute supervisor remains host/guardian-outage recovery, not a1-minute SLA. The coordinator must save the updated command wording separately. Additive Turso audit plus durable sanitized local write-ahead logging covers scans/all751 stock decisions,7 strategy outcomes, paper guards/fills, actual quote/stop checks and EOD. `run.sh export-logs --session YYYY-MM-DD --format jsonl|csv` produces private review files and an honest coverage report; no real-session completion is claimed before observation. See the engine README for exact commands and limitations. Prior prefilter release5cce913 received coordinator-confirmed public desktop/mobile QA; this monitoring/logging release changes no dashboard UI or strategy formula.
