# Seven-strategy PAPER session engine — upgrade v2

**No broker orders. No funded account. Long-only, independent virtual position per strategy/stock. Each new entry buys floor(₹50,000 / fill price) shares. A fill price above ₹50,000 is skipped as UNAFFORDABLE_ZERO_SHARES and the signal stays pending for that session. A strategy with 9 open positions skips the next entry as SLEEVE_SLOT_FULL. A fourth strategy on the same symbol is skipped as ISIN_CAP and recorded on the shadow ledger, which does not take a live slot. A buy that would spend more than the sleeve's remaining cash is skipped as INSUFFICIENT_CASH. A buy whose lagged 20-session median traded value is below the ₹10 crore placeholder is skipped as ADV_FILTER_SKIP. That floor stays pending funnel review. Entries whose combined notional on one symbol that day would exceed 0.005 times that ADV are skipped as ADV_PARTICIPATION_CAP and are not scaled. At the ₹10 crore placeholder the cap is ₹5,00,000. Exits are not ADV-filtered. Fill prices add tiered slippage: 8 bps through ₹20 crore ADV, 5 bps above, plus 10 times notional divided by the participation cap. Share count stays on the reference price. Stress is 1.5 times and is off. Next-open fills use the Fyers open_price when it is present and tag FEED_OPEN. Share count and slippage use that open. An auction equilibrium is used only when the open is missing (AUCTION_CAPTURE). A first trade with no official open is FALLBACK_OPEN. Anything else stays LTP_LEGACY. Polled stops still use the last trade. The bhavcopy script compares those opens in paise and does not change fills. An upper circuit blocks that buy for the rest of the session. A lower circuit blocks the sell and keeps the exit pending until a later clear session. BE, BZ, ASM, and GSM block new entries. A stop that gaps through fills at the open. A locked exit is marked down 1 percent for each locked session after the first. The signal stays pending, and an exit in the same session can free the slot. A split, bonus, or extraordinary dividend scales quantity and prices by new shares per old share. A fractional share is credited at the post-adjustment open. A cash dividend credits virtual cash and does not scale prices. Rights, demergers, and mergers stay CORP_ACTION_MANUAL_REVIEW. A strategy pauses new entries when shadow-book drawdown exceeds 10 percent, or when the one-sided 95 percent upper bound of its last 30 net shadow trades is below zero. Strategies 03 and 05 use only the drawdown rule. Open positions still exit. Shadow entries continue and do not consume the ISIN cap. A strategy stays paused until a manual override because the freeze does not give the hysteresis trade count N. The capped book pauses all new entries when master drawdown exceeds 8 percent and reinstates within 3 percent of the pre-pause peak, or on a manual override. The peak resets on reinstatement.** Only Fyers `data/history` and `data/quotes` endpoints are allowlisted. Manual dashboard demonstrations live in a separate table/namespace; this engine never queries them.

## Installation and deployment boundary

Production installation is `/workspace/paper-trading` on the shared bot box. Versioned source is `automation/paper-trading` in the dashboard GitHub repo. **Vercel hosts only the read-only dashboard/API, NOT this persistent Python worker. A GitHub push does not install/start the box engine or save routines.** Do not copy `.env`, logs, output files, auth circuits, SQLite test databases or caches into Git.

Tested Python3.13.5 (`requirements.txt`) and @libsql/client0.18.0 (matching the existing installed runtime) (`package-lock.json`). `run.sh` uses the existing external Python venv; `PAPER_PYTHON` can select a different compatible interpreter. Node resolves installed dependencies normally; use `npm ci` in this package on a new installation. Market-data auth remains the existing local `fyers_auth` integration. The shared protected credentials remain `/workspace/sahil-fyers-turso/.env`; never use browser-prefixed env variables. This source package contains no credentials. Tests use disposable, path-restricted local libSQL files and do not read the production env.

Install updates under the existing exclusive `engine.lock`; migrate additive schema first, then atomically replace versioned source files before releasing the lock. Preserve the lock inode, local credentials, logs and out files. Schema setup is `./run.sh init` (idempotent), which registers an immutable policy snapshot and `automation_schema_version=2`. Never initialize test fills in production.

## Frozen rules, completed data and eligibility

`strategy_indicators.py` and `strategy_rules.json` preserve the original seven rules. Source-function comparisons are in the original28 regression tests. Donchian uses prior20 highs/prior10 lows; actual Supertrend/EMA/MACD crosses remain events, not relaxed bullish states. RSI/Bollinger/momentum retain their existing inequalities. Momentum alone has its prescribed10% completed-close high-water trail, plus completed-close SMA50 exit. No extra target, stop, stop-and-reverse, rebalancing or end-of-day liquidation is introduced.

The frozen paper universe is692 exact symbol+series/ISIN identities, with59 explicitly excluded identities from the original751 audit. No alias substitution or new-market expansion. Missing/stale/current-bar-invalid data cannot qualify. Only previous **completed exchange sessions** are scanned; the daily bar is conservatively considered completed from16:00 IST. History sync is incremental and rejects future, closed/unverified-session and invalid OHLC candles. Existing invalid data exclusion remains separate from the unchanged research backtests. EMA/ATR/RSI history initialization is not truncated or redefined.

Each scan transaction saves all seven strategy/current-stock filter checklists, observed indicator values, qualification flags and honest plans to Turso, including explicit unavailable states. `paper_scan_batches.report_json` contains sequential and individual filter pass counts. Every signal retains its strategy/symbol/completed-bar identity. Watermarks, pending-state checks, unique fill IDs and a transactional same-bar re-entry guard prevent duplicate/replayed orders. **Existing Friday6Nov watermarks are not reseeded or backfilled.** Entry requires a NEW completed bar after its watermark, no position, no pending entry, and first fresh eligible-session Fyers LTP; never a claimed exact opening fill. Unchanged bar replay creates no new order. A daily condition need not become false and then true again: its original rule is preserved.

The calendar retains the previously verified8Nov2026 Sunday special session as historical/completed-bar input, but new automatic scan/worker execution is **Mon–Fri only**. Thus a genuinely new verified Sunday daily bar could be processed Monday; previously processed Friday data alone does not generate retrospective orders. Holidays fail closed. Calendar validity remains1Nov–31Dec2026;2027 sessions are **not guessed**. Review/extend from official NSE notices before expiry. Health warns within14days; unsupported dates stop automation. Existing calendar URLs remain in config.

## Commands and persistence

```sh
./run.sh scan --sync       # scheduled08:45 IST weekdays; sync prior completed session, persist screen, queue NEW eligible signals
./run.sh screen-only       # explicit screen refresh, NO sync/queue/watermark/position changes (does write screen/audit/run records)
./run.sh ensure-monitor    # supervised singleton startup/healthcheck; closed/unverified/weekend -> no worker or Fyers call
./run.sh worker            # normally launched only by ensure-monitor; bounded to verified weekday session
./run.sh monitor-once      # backwards-compatible bounded iteration; closed -> no quote/fill/P&L snapshot
./run.sh health            # read-only DB/worker health; no P&L write
./run.sh positions         # read-only real strategy positions
./run.sh trades-today      # read-only paper fills
./run.sh finance           # read-only gross/net/estimated costs, reconciliation, freshness, strategy/cohort groups
./run.sh charges           # read-only immutable fill-charge components
./run.sh overlap           # read-only saved current qualifiers -> sorted strategy sets and exclusive buckets
./run.sh eod               # persist/upsert weekday EOD at/after16:00 IST; never force-close a position
./run.sh weekly            # read-only Mon-through-current-session change summary
./run.sh pnl               # legacy GROSS snapshot command: WRITES paper_pnl, adds estimated_finance
./run.sh status            # legacy compatibility: also WRITES paper_pnl; use health+finance for read-only checks
```

New additive tables: `paper_charge_policies`, `paper_fill_costs`, `paper_dp_debits`, `paper_entry_cohorts`, `paper_scan_batches`, `paper_qualifier_states`, `paper_monitor_health`, `paper_eod`. All namespace-scoped rows use existing `paper_v1`; policy versions are shared immutable public configuration, not credentials. Legacy tables/column meanings are preserved. No historical fee backfill: existing unpriced fills make net metrics unavailable and pause new entries until an explicit migration decision.

## Worker safety and supervision — durable routines are saved separately

30-second target polling,50symbols/request batches, existing1.3-second minimum between requests. Actual cadence can be longer for large batches/network/DB latency. This is **polling, not every tick**; a gap can fill a stop at the observed price, never at an invented threshold. Quote source status, same verified session, positive finite price, exchange timestamp and60-second age are checked **again for each individual fill** and mark update. Stale/unavailable quotes never create fills. Entries expire after their eligible session; exits are processed first. Auth refresh remains single-attempt with a30-minute circuit; the circuit is checked BEFORE another HTTP call. Transport failures use bounded backoff up to300seconds; auth failure pauses/exits the worker.

An inherited OS file-lock lease prevents launch races/duplicate workers. Each iteration takes existing `engine.lock`, so scan/old routine commands cannot overlap a worker fill. Health and supervision deliberately bypass that command mutex, so a hung iteration cannot block its own recovery. The worker releases that lock between iterations. Heartbeats record actual PID/process-start identity and status. A stale lease-free PID is harmless: OS locks release on crash. A heartbeat over600seconds old permits supervisor to TERM **only a verified same-start, exact-path owned worker**, never arbitrary/reused PIDs; next supervision starts a replacement. Otherwise report attention needed. Worker exits when session ends. No system cron, systemd or routines are installed by this package. Host restart needs the durable supervisor to run again.

Confirmed by the coordinator7Nov2026 at12:52 IST: the old scan and monitor routines were updated, and weekday EOD accounting was saved. No duplicate scheduler was installed by the engine. First future market-session execution is still unverified. All trigger times **Asia/Calcutta (IST)**:
- Scan: `45 8 * * 1-5` → `run.sh scan --sync`.
- Worker bootstrap/supervisor trigger group: `15,20,25,30,35,40,45,50,55 9 * * 1-5`; `*/5 10-14 * * 1-5`; `0,5,10,15,20,25,30 15 * * 1-5` → `run.sh ensure-monitor`, then `run.sh health`.
- EOD: `10 16 * * 1-5` → `run.sh eod`, then `run.sh weekly`.
- Report failures, paused_auth, stale heartbeat, missing cost/basis/marks or calendar/policy warning; remain quiet on normal verified closures. The verified full692-symbol screen from existing completed history took about6minutes; fresh history sync adds API-dependent time. The08:45 trigger is a start time, not a guarantee that all scanning finishes before09:15. A routine may start late (scheduler jitter), so09:15 exact execution is not guaranteed. Between-supervisor downtime can be5minutes plus jitter. The15:30 tick correctly refuses new worker startup.

## Estimated delivery charges: sources, assumptions and rounding

Rates retrieved/opened7Nov2026 IST, recorded in `charge_policy.json`. **These are modeled paper costs, never actual debits or a contract-note guarantee.** Standard plan assumed; Prime/promotion not verified.

| Component | Policy |
|---|---|
|Brokerage|min(₹20,0.3% turnover), per executed virtual order, both sides; effective1Oct2024|
|STT|0.1% delivery buy/sell; namespace/day cumulative nearest-rupee liability, incremental allocation|
|NSE transaction|0.0030699% both sides; revised split effective1Mar2026|
|NSE IPFT|₹0.01/crore (0.0000001%) both sides, NOT obsolete₹10/crore|
|SEBI|₹10/crore (0.0001%) both sides|
|Stamp|0.015% buy; cumulative day rounded to paise|
|GST|18% brokerage+exchange+SEBI+IPFT; DP GST separate|
|DP|₹12.50+₹2.25 GST once/ISIN/day on overnight sells, split pro-rata by those sells' turnover; remainder paise go to the latest sell|
|Cash clearing|₹0 assumed; cash row not separately specified. Do not apply derivatives clearing rates to cash.|

Official opened sources: [FYERS detailed charges](https://fyers.in/charges-list), [FYERS plan pricing](https://fyers.in/pricing), [brokerage revision/effective date](https://support.fyers.in/portal/en/kb/articles/what-are-the-brokerage-charges-for-equity-intraday-and-delivery-trades-at-fyers), [DP per-ISIN/day](https://support.fyers.in/portal/en/kb/articles/how-are-dp-charges-levied-in-fyers), [NSE statutory rates](https://www.nseindia.com/static/invest/first-time-investor-sebi-turnover-fees-stt-other-levies), [NSE Clearing EOD STT](https://www.nseclearing.in/equities). The NSE27Feb2026 [FA73061 circular](https://nsearchives.nseindia.com/content/circulars/FA73061.pdf) was indexed with the March1 split but direct PDF retrieval returned403; the live FYERS charges list independently shows the same current split. Older FYERS support statutory text conflicts on IPFT/GST: use the current detailed charges list/NSE dated revision, not obsolete figures. Sources are not a claim that the user's actual account plan was checked.

BigInt integer-paise cashflows and charge components; half-up paise rounding per order, except cumulative STT whole rupees and cumulative stamp paise. GST is computed from the rounded taxable components. STT/stamp incremental allocation reconciles day totals; attribution between strategies is fill-order-dependent. Example on separate days: buy1@₹2500 estimated₹12.32, overnight sell1@₹2600 estimated₹27.05, gross₹100/net₹60.63. DP is capped once, not repeated for every strategy. Same-day exits have NO DP; delivery STT/stamp remain conservative assumptions, not an emulation of actual client security-wise netted intraday settlement. Subscription, interest, and income tax are not modeled. Corporate-action cash is a separate virtual credit, not a fee. Policy `FYERS_STANDARD_NSE_DELIVERY_ESTIMATE_2026_11_09_v2` is effective from7Nov2026 through31Dec2026; expiry fails closed and requires a new version, never mutation of an already-recorded policy. The v2 change is DP attribution only: one ₹14.75 charge per ISIN per sell day, turnover pro-rata across the strategies that sell it.

Fill, position, gross cashflow, fee components/net cashflow, DP debit, entry cohort and signal status commit in ONE transaction. A rollback cannot leave an uncharged fill, double DP or orphan cohort. Replays cannot charge again. Existing `paper_fills.realized_pnl/cashflow` retain legacy GROSS meanings; exact new cashflows are integer paise in `paper_fill_costs`.

## Analytics definitions / next-dashboard contract

`finance` reports integer INR paise:
- gross total = closed realized + latest accepted marked unrealized;
- accrued model costs = executed BUY/SELL fees only;
- net realized = closed gross less its associated entry+exit costs;
- net unrealized accrued = open gross less its entry costs;
- net total accrued = gross total minus ALL accrued model fees;
- net after estimated exit = net total accrued minus a separate projected close-all sell-cost scenario, DP capped once. Projection is NOT booked, and stale mark provenance is explicit.
- net cashflow + current marked holdings reconciles to net total; missing cost/mark coverage is explicit. No funded capital/account equity/percentage return is invented.

Entry overlap freezes **the signal bar's** complete sorted strategy set. Exclusive buckets: single, exactly_2, exactly_3,4_plus, unavailable. Current-screen overlap also exposes at-least2/3 counts, which must NOT be summed as disjoint buckets. Strategy and entry-cohort gross/net totals reconcile without counting a trade in multiple exclusive buckets. The same stock can be separate virtual holdings in several strategies; these are not a deployable combined funded portfolio.

EOD preserves last accepted in-session mark timestamps, never claims official close/live quotes or closes swing positions. Daily changes require current marks within the last5minutes of the session (still NOT official close) and a preceding verified-session near-close EOD cumulative baseline; older marks preserve only clearly labeled latest-known cumulative values, not a falsely precise daily return; missing prior baseline yields unavailable daily P&L (except proven zero pre-inception ledger). No fabricated historical snapshots. Weekly sums verified DAILY CHANGES, not cumulative totals; missing session coverage yields unavailable totals. These are gross/net P&L curves unless funded capital is explicitly introduced in a later authorized change.

Logs: `logs/engine.jsonl`, `logs/monitor-worker.log`; tests use `logs/tests.jsonl`. Logs redact remote error contents and expose vetted codes only. Persisted `paper_runs`, worker health, scan batches and EOD reports provide auditability. Do not publish logs or credentials.

## Verification

```sh
PAPER_PYTHON=/workspace/sahil-fyers-turso/.venv/bin/python
"$PAPER_PYTHON" -m unittest discover -s tests -v
```

All execution examples in tests use local libSQL, fixtures and test namespaces, NOT production fills. Includes frozen indicator identity, fresh/session boundaries, bootstrap/no backlog/replay/oversell guards, atomic rollback/concurrency, independent Decimal fee arithmetic, DP cap, integer cashflows, immutable cohorts/policies, supervisor singleton/inherited lease/auth pause/backoff, completed-only sync, per-fill time revalidation, missing marks/baselines, EOD idempotence and weekly change arithmetic. Saturday verification cannot prove future in-session market availability or stop execution; report that limitation instead of claiming uninterrupted tick monitoring.

## Two-layer pre-entry gate — `beta_momentum_v1`

These are explicit configurable defaults chosen for this project, **not** a reproduction of proprietary Swing Trade Analyzer rules. They have not been backtested together with the seven strategies, and do not promise returns. Higher beta can magnify losses.

The weekday08:45 IST scan first synchronizes completed daily stock bars and the exact Fyers benchmark `NSE:NIFTY50-INDEX` into `index_ohlcv` under `NIFTY50-INDEX`. Existing `NIFTY500` data is never relabeled or substituted. The original audited universe is751 identities:692 eligible exact symbols plus59 separately retained original exclusions—not every NSE listing.

Stage1 requires ALL:
- Beta≥1.2 versus Nifty50. Simple daily close returns, up to252 latest benchmark intervals (253 closes), at least200 finite aligned pairs. Beta is sample covariance / sample variance. Each paired stock and index return has exactly the same previous/current dates; no forward-fill, no accidental pairing of a two-day stock move with a one-day index move.
- Latest close>SMA50>SMA200, using a complete latest200 benchmark-session stock-close sequence.
- 63-session stock return≥10%; stock minus benchmark return over those same endpoints≥5 percentage points;20-session stock return>0.
- Both latest bars equal the target completed session. Missing data, nonpositive/nonfinite/inconsistent raw OHLC, nonpositive benchmark variance or insufficient coverage is UNAVAILABLE, never a pass. A raw daily move exceeding50% in the window requires corporate-action review, without silently clipping or adjusting prices.

Prices remain unadjusted. Beta return sessions are defined by the actual official-provider benchmark daily sequence; benchmark dates are checked against independently stored NIFTY500 session dates plus the engine’s verified NSE calendar window. Missing verified sessions fail closed; no holiday or missing close is guessed. NIFTY500 prices are never used for beta. Volume expansion is diagnostic ONLY: average last20 completed volumes / prior20≥1.2, if all40 volumes are finite and positive. Missing volume never fails the gate. The audit ranks passing stocks by63-session relative momentum, then symbol; this is descriptive ordering, not an additional buy signal. No invented support levels, fixed targets, RR, HHHL or learning weights are added.

Only Stage1 PASS stocks reach unchanged seven-strategy ENTRY indicators, with full original warmup history. Saved checklists distinguish Stage1 from Stage2; a blocked stock has `NOT_EVALUATED_PREFILTER`, not a fabricated failed strategy test. Held stocks still run their original daily exits and completed-close high-water updates regardless of Stage1 failure. Intraday marking and stop SELL fills do not require a prefilter pass.

New additive tables: `paper_prefilter_policies` (immutable versioned JSON), `paper_prefilter_states` (all751 audit identities, metrics/reasons/coverage), `paper_entry_gate_proofs` (exact signal/symbol/bar/policy linkage). Scans save these states, seven-strategy qualification, signals/proofs and watermarks atomically. The BUY fill transaction independently requires a matching active-policy PASS and qualified strategy row; legacy unfiltered pending entries become `blocked_prefilter` with a readable reason. SELL risk exits are not blocked by the gate. Failed/stale scans advance normal watermarks, avoiding later replay; `screen-only` updates saved screen metadata only, **never** orders or watermarks.

`run.sh screen-only` is the explicit order-free screen verification command. The existing `scan --sync`, `ensure-monitor`, `eod`, `finance` schedules remain unchanged. Historical research remains frozen6 Nov2026 and ungated; its gross historical curves are not results of this combined approach. Calendar and fee policy still require review before31 Dec2026. A Saturday screen is not an in-session execution test.

### Initial real-data screen (6 Nov2026 completed bar)
Order-free verification found1 Stage1 PASS (JYOTICNC-EQ),137 FAIL and554 UNAVAILABLE among692 eligible identities, plus59 retained original audit exclusions. Only138 stocks had complete Stage1 coverage;37 of those met beta. All seven final strategy counts were0, without relaxing any rule.500 eligible stocks have missing closes in their latest200 benchmark sessions; a common gap is17 Sep2026 (474 symbols). Fifty have fewer than200 aligned returns; two need extreme raw-return review and two have invalid OHLC. Existing latest-date incremental synchronization does not silently invent or automatically repair these internal historical holes. At that baseline those stocks were unavailable; the recoverable official-data repair and final screening are documented below.638 calculable betas independently reconciled using scalar covariance sums with zero mismatches. Only one passing sample exists; extra passing examples were not fabricated. The original ungated historical counts remain separate.

### Recoverable historical gap repair
`scan --sync` now also checks the latest253 verified benchmark-session dates for internal stock-history holes, not merely the latest stock date. It requests only missing ranges from the audited exact Fyers EQ symbols, inserts only valid missing official OHLC, and leaves existing valid bars untouched. Historical validation uses the verified benchmark-session sequence, not the execution calendar’s Nov–Dec window; execution/quote safety calendars remain unchanged.

Normal scans cap internal repair at20 history requests /240 seconds, with actual one-time elapsed provider calls potentially finishing their existing bounded retries. A resumable, order-free maintenance command is available under the same engine mutex:
`run.sh repair-history --max-history-requests 1100 --max-history-seconds 1080`
This command does not scan strategies, queue signals, fill trades, update watermarks, or create P&L snapshots. It checkpoints each small chunk in `paper_history_gap_checks`. Missing provider bars retry after7 days for older dates,1 day for recent dates; transport failures after1 hour; provider-rejected ranges after7 days. Provider absence is not automatically labeled an IPO. Authentication/circuit failure stops immediately; three consecutive other failures stop the run. Progress: `out/history-repair-progress.json`. The initial pre-repair coverage paragraph above is an audit baseline, not a claim that the database gaps remain unrepaired forever.

When an existing OHLC row is itself provably invalid (zero/nonpositive or internally inconsistent), repair may conditionally replace **only that invalid row** with the actual valid Fyers candle. Its complete original OHLC/source/timestamp is retained in the repair checkpoint’s `invalid_original_before_image`. No valid historical row is revised, no split adjustment is invented, and no row is deleted. Original frozen research files/results are not regenerated.

Historical calendar evidence checked7 Nov2026: the sole Sunday in the253-close benchmark window is1 Feb2026, a genuine special NSE session—not a synthetic date. Official NSE capital-market circular11/2026 (NSE/CMTR/72349,16 Jan2026) confirms normal09:15–15:30 IST trading for the Union Budget: https://nsearchives.nseindia.com/content/circulars/CMTR72349.pdf . Direct Fyers history also returned actual valid17 Sep2026 daily bars for `NSE:NIFTY50-INDEX`, `NSE:CESC-EQ` and `NSE:CGCL-EQ`, confirming that widespread17 Sep stock omissions were recoverable database holes. Neither finding extends the engine’s execution-calendar authorization window.

### Completed gap repair, before live session-input correction (7 Nov2026)
The complete one-time backfill used553 logical Fyers history calls:20,722 returned bars,13,146 actual missing rows inserted, zero rejected bars/provider absences/errors. All50 apparently short-history names had recoverable older official data, so none was incorrectly classified as an IPO. Every one of692 eligible stocks now contains all253 required benchmark-session closes. Two further calls corrected invalid all-zero18 Jun2026 bars for BAJAJ-AUTO-EQ and NAM-INDIA-EQ, retaining before-images; no valid row was overwritten or deleted. Fresh order-free screening:11 PASS,678 FAIL,3 raw-extreme-move review cases, plus the original59 exclusions. Final strategy counts:4/1/0/0/0/0/9.689 calculable betas independently reconcile with zero mismatches. Ledger/signals/positions stay0; watermarks4844 remain6 Nov2026; manual demo1; no EOD fabricated.

### Evidenced, non-destructive live session-input correction (7 Nov2026)
The former publication hold is resolved in `verified_live_inputs_v1`, with supplemental immutable evidence `session_evidence_20261107_v1`. All14 suspect dates were classified against official evidence—not benchmark absence alone:
-5 Nov2025 and25 Dec2025: NSE/CMTR/65587, https://nsearchives.nseindia.com/content/circulars/CMTR65587.pdf .
-15 Jan2026 election closure: NSE/CMTR/72260, https://nsearchives.nseindia.com/content/circulars/CMTR72260.pdf .
-26 Jan;3/26/31 Mar;3/14 Apr;1/28 May2026: NSE/CMTR/71775, https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf .
-4 Jul,11 Jul and1 Aug2026: **mock-only**, not live cash sessions: https://nsearchives.nseindia.com/content/circulars/CMTR74982.pdf , https://nsearchives.nseindia.com/content/circulars/CMTR75097.pdf , https://nsearchives.nseindia.com/content/circulars/CMTR75479.pdf . Two fresh official Fyers daily ranges, Nifty50 and RELIANCE,1 Jul–3 Aug2026, independently return the same24 neighboring live sessions and none of these mock dates.
All sources retrieved7 Nov2026 IST; full date-linked evidence and provider date lists are in `session_input_evidence.json`. The1 Feb2026 Sunday Budget session is **retained**, supported by NSE/CMTR/72349; Muhurat is never removed by a generic weekday rule.

Exactly7,789 raw rows on those14 dates are ignored across every live seven-strategy entry, held daily-exit, and momentum completed-close high-water calculation. A single shared upstream filter applies before Stage1 and Stage2, leaving all seven formulas identical. All2,383,100 raw candles remain stored; no raw row was deleted or changed by this correction. Frozen historical research JSON/CSV is untouched and ungated, and retains its original legacy inputs; its results are not claimed to match the corrected live inputs or to backtest the combined gate.

This is **explicit closure evidence**, not an exhaustive2003-present historical calendar. Coverage is30 Oct2025–31 Dec2026 for listed/evidenced dates, and older full warmup remains present, explicitly legacy/unverified. It is not truncated to253 bars. Unknown stock dates inside the latest benchmark window fail new entry eligibility as `unverified_stock_session_dates`; they are not guessed closed or dropped. A genuinely missing benchmark trading session must be repaired, not erased from stocks. The14 reviewed dates required no missing real-session index repair. Execution authorization/calendar remains Nov–Dec2026 and is not widened.

Additive tables: `paper_input_policies`, `paper_input_evidence`, `paper_entry_input_proofs`. A newly queued BUY records an input-policy/bar proof in the same transaction as signal and prefilter proof. Atomic fills independently require both active policies and the current qualified row's input-policy marker; old unfiltered pending BUYs fail closed with a trace. Held SELL exits/stop monitoring remain independent of Stage1. Saved per-stock metrics record ignored dates/counts, full-warmup provenance and unknown-session reasons; scan batches record per-date row counts. Dashboard advanced details link directly to official date-specific evidence.

Release verification:104 isolated engine tests (including shared filtering across all seven live paths, special Sunday retained, missing real benchmark day not removed, full warmup preserved and legacy-input BUY blocked),111 dashboard tests, lint/build, secret scan and production-dependency audit. Actual market-session execution remains unproven on Saturday. Local visual QA is partial (funnel, counts, optional volume and both entry stages confirmed); full remaining visual QA must run on the public deployment. Parent authorized publication of the tested changes after the local browser/loopback environment discrepancy persisted. No live-session execution is claimed.

Final corrected6 Nov2026 screen:692 eligible identities →689 evaluable (176 meet beta),11 Stage1 PASS,678 FAIL,3 UNAVAILABLE extreme-return reviews (ALLCARGO, INDIAGLYCO, TDPOWERSYS), plus59 original exclusions. Seven strategy counts: **4 /1 /0 /0 /0 /0 /10**;10 unique stocks,15 memberships,5 single and5 exact-two current overlaps,0 exact-three. The final input read contains2,051,016 raw rows for692 eligible symbols;2,043,227 are passed to live calculations after7,789 evidenced exclusions. The1,868,151 pre-coverage warmup rows are retained and not claimed calendar-clean. All2,383,100 database candles remain unchanged by the session filter.689 independent scalar beta checks have0 mismatches. Real signals/fills/positions/EOD remain0,4844 watermarks remain6 Nov2026, and the isolated manual demo remains1.

Timing note: Friday6 Nov is already watermarked and cannot seed historical orders. The verified Sunday8 Nov special session is retained as completed-bar input: a genuinely new available daily bar may be processed by Monday's weekday scan.10 Nov is a verified holiday. No first-entry date or fill is promised; fresh data, exact active-policy proofs, strategy qualification and session/price guards still apply. Parent confirmed the saved morning routine intent now includes the prefilter/input-policy safeguards with its existing08:45 weekday timing unchanged.

Snapshot identity is also enforced: the BUY proof observation, active prefilter observation and strategy scan timestamps must agree, not only their policy/bar. A deliberate maintenance `screen-only` re-screen invalidates any older pending proof rather than silently carrying it onto refreshed inputs; ordinary read-only dashboard refresh does not alter these timestamps.

## Faster local monitoring and complete-flow audit (pt34u)

The healthy local worker retains its **30-second target polling** (already faster than the requested1–2minutes). New `ensure-session` arms a detached singleton **60-second guardian** from08:30 on a verified weekday; the08:45 routine should arm it **before** starting the scan. Before09:15 it makes no quote/order calls. During the verified session, capped at15:30 IST, it checks/restarts the existing worker. It stops at close, on a changed date, or after8hours/480checks. No weekend/holiday busy loop. Existing leases, PID/start/cwd identity checks and atomic paper guards remain authoritative. A stale worker is noticed after120seconds (600seconds during intentional outage backoff), signalled only when exactly owned, and restarted at a subsequent check. Auth-paused workers have a300-second retry spacing; provider auth circuits/backoff remain intact.

These are healthy-host **targets, not an SLA**: API/DB latency, lock contention, backoff and recovery add delay. A dead host or lost guardian still needs the existing native **five-minute supervisor plus scheduler jitter**, potentially longer during outage/restart; native routines cannot honestly promise1/2-minute scheduling. This runs on the box, not Vercel. No operating-system service was installed. Execution remains paper-only, weekdays and verified sessions, with no forced buys or fixed targets. Strategy7 retains its actual10% completed-close high-water stop; reported hits are observed quote crossings, not guaranteed threshold-price fills.

### Coordinator routine commands (save externally; not mutated by this package)

```bash
cd /workspace/paper-trading
./run.sh ensure-session            #08:45 weekday routine: BEFORE scan
./run.sh scan --sync
# Existing five-minute9:15–15:30 weekday supervisor:
./run.sh ensure-session
./run.sh ensure-monitor
./run.sh health
# Existing16:10 weekday accounting routine:
./run.sh eod
./run.sh weekly
./run.sh finance
./run.sh replay-audit --max-files 1000
#16:25 IST review on an actual verified trading day (replace date):
./run.sh replay-audit --max-files 1000
./run.sh export-logs --session 2026-11-09 --format jsonl
./run.sh export-logs --session 2026-11-09 --format csv
```

The coordinator must confirm the saved routine wording and bounded next-trading-day observation separately. Existing timings are preserved; this release does not itself save routines or claim Monday execution.9Nov is an eligible weekday in the current verified calendar; roll observation forward if the actual session/data are unavailable. Review after16:10 EOD plus its allowed delay; bounded observation deadline20Nov2026. No first-fill or winning-performance promise.

### Durable, sanitized logs and exports

Additive Turso `paper_events` stores run/parent/batch correlation, signal/strategy/symbol/bar, both active policy IDs, event/outcome/reasons and allowlisted metrics. Scan decisions cover all751 audited identities, including59 original exclusions and7 explicit not-evaluated decisions for gated-out stocks. Qualification/watermarks/queued signals and their decision audit commit together. Actual fill/block audit commits inside the existing atomic fill transaction: failure rolls back the trade. Quotes include logical and actual provider-batch request/response timestamps, freshness/acceptance and position/stop evaluations. Idle checks, missed-slot/heartbeat telemetry, failures and EOD/reconciliation are recorded; no invented target events.

Frequent telemetry is synchronously fsynced into0600 local write-ahead files under `logs/audit-spool/`, then bulk replayed into Turso at monitor/scan/EOD boundaries and on subsequent startup/guardian checks. Stable event IDs make replay idempotent. A DB outage retains the spool; DB plus spool failure stops the operation. Unflushed telemetry is explicitly flagged in exports. Trade transactions still require their atomic DB audit even when telemetry is spooled. There is no log pruning in this release; monitor storage and archive reviewed exports rather than silently deleting history.

`out/audit-YYYY-MM-DD.jsonl`, `.csv` and `-report.json` are private local artifacts. Exports stream keyset pages, default bounded200000events; truncation is explicit. Each event carries source time, actual local recording time and actual first DB persistence time, with UTC and **IST** renderings. Export reports distinguish a complete751×7 decision segment, successful reconciled EOD, missing/gapped monitor coverage (>120seconds), pending spool and truncation. They separately state whether an execution or stop crossing was actually observed: no trades/hits is legitimate and never fabricated. A Saturday startup export is not proof of Monday flow. Logs exclude credentials, headers/provider dumps, remote URLs, arbitrary exception messages and process secrets. No expanded public unauthenticated logging API was added; use the CLI exports and attach the sanitized files privately for review. JSONL `data_json` is the allowlisted structured payload encoded as a JSON string; CSV fields are formula-safe.

Monitoring release verification:135 isolated engine tests passed in both source and installed runtime;111 dashboard tests passed. Lint, production build, exact9-secret scan and production dependency audit passed. Saturday installation added only audit/guardian tables and3 truthful configuration/start/end events: real signals/fills/positions/costs/EOD remain0, demo1, watermarks4844 all6Nov2026. Closed-day ensure-session started no guardian/worker and made no quote/order calls. Saved guardian routine commands and real-session observation still need separate coordinator confirmation.

## Render deployment (Track A backend)

The paper-trading backend (supervisor + engine) can run on Render free tier with all state in Turso; see [deploy/render/DEPLOY.md](../../deploy/render/DEPLOY.md).
