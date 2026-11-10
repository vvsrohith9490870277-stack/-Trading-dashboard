# Paper ledger tab parity — old-trading reference vs Atlas Desk

Reference: `old-trading` @ `65e712a9` — `frontend/src/components/PaperTradingPage.tsx` (tabs, Positions/Orders/History tables, TradeDetailsModal, AlertsPanel, AccountPanel, AccountSummaryStrip, MarketEngineHealthWidget, PaperAccountWidgets), `AnalyticsPanel.tsx`, `DailyAnalyticsPanel.tsx`, `WatchlistTab.tsx`.
Atlas Desk: branch `feature/atlas-desk-ledger-parity` (off `main` 9ffd304). Second pass (stop / target / R:R / break-even / charges, user t100u–t104u) on top of 3ee1bed. Section: Paper desk → "Browse the paper ledger".

Legend: ✅ shown from real ledger data · 🟡 shown, with a stated limitation · ⛔ unavailable — labelled in the UI, never estimated · — not present.

"Before" = Atlas Desk on main 9ffd304. "After" = this branch.

## Tabs

| Reference tab | Before | After |
|---|---|---|
| Positions | ✅ (9 columns) | ✅ Positions (17 columns + row detail) |
| Orders | — (pending signals only inside Signals) | ✅ Orders = pending signals (status=pending) |
| History (closed trades) | — | ✅ History (round trips, new `trades` read view) |
| Analytics | — | ✅ Analytics (computed from the complete trade + fill record) |
| Daily | — | ✅ Daily (EOD snapshot + session activity) |
| Alerts | — | 🟡 Alerts = engine alerts from `paper_events`; user price alerts ⛔ |
| Capital | — | ✅ Capital (summary, policy settings, sleeves, transaction log, cash credits) |
| Watchlist | — | ⛔ labelled unavailable (no watchlist store; points to Strategy tester qualifiers) |
| (Atlas) Fills | ✅ (9 columns) | ✅ 12 columns + fee breakdown detail |
| (Atlas) Signals | ✅ (8 columns) | ✅ 12 columns + status filter + executing fill |
| (Atlas) Engine runs | ✅ (3 columns) | ✅ kind filter + whitelisted run details + health strip |
| (Atlas) Quote audit | ✅ (6 columns) | ✅ accepted/rejected filter + lag + source |
| Data audit (excluded bars) | — | ✅ new `dataaudit` read view |

## Per-tab fields

### Positions
Column order follows the user's old-trading list (t101u). Extra Atlas columns follow it.

| Reference field | Before (3ee1bed) | After |
|---|---|---|
| Symbol | ✅ | ✅ (link to stock page; sticky first column on phones) |
| Signal | — | ✅ our strategy identity + the real entry signal (signal id, signal bar, detection time) of the BUY fill with the same `entry_at`. No MATCH / score / confidence exists in this engine — none shown |
| Qty, Avg entry | ✅ | ✅ |
| Current | ✅ | ✅ last verified mark; "no mark" when missing (never 0) |
| Stop | ⛔ "n/a" | ✅ **Strategy 07:** real rupee trail stop = `trail_level(high_water)` = Python `round(TRAIL × high_water, 2)` with `TRAIL = config.json strategy_07_trail_fraction` (same value engine.py loads; checked against `strategy_rules.json 7.trailing_update`). Shows the fraction, the high-water used and % distance below the mark. Missing high-water → "trail stop unavailable" with reason. **Strategies 01–06:** "No fixed stop · <exit rule>"; plus a *rule level* only when it follows exactly from stored completed bars (see "Rule levels" below) |
| Target | ⛔ "n/a" | ✅ "No fixed target · exit: <rule>" (rule wording from `src/exitRules.js`, engine wording from `strategy_rules.json` in the detail). Never a price |
| R:R | ⛔ | 🟡 "— (no target)" with the reason: R:R needs a stop **and** a defined target; no strategy defines a target. Not computed anywhere |
| Break-even | — | ✅ lowest exit price where gross − recorded entry charges − estimated exit charges ≥ 0 (integer-paise search with the engine's `fees.mjs estimate()`). Unavailable without an entry fee row |
| Gross P&L | ✅ (Unrealized) | ✅ (mark − avg entry) × qty; unavailable without a mark |
| Taxes & charges | — | ✅ recorded entry fee row (`paper_fill_costs`, matched on strategy+symbol+entry_at) + engine-estimated exit charges if sold alone at the mark (`fees.mjs estimate()`, incl. DP when held overnight — the same estimator behind "net after estimated exit"). Unavailable, not zero, when the entry fee row (legacy fills) or the mark is missing |
| Net unrealized | — | ✅ gross − taxes & charges |
| % Net return | — | ✅ net unrealized ÷ cost basis (avg entry × qty) |
| Status | 🟡 mark tag | ✅ open / exit pending (lower-circuit carry, `pending_exit` + since, when the column exists) / entries paused (`paper_pause_state` cause + time) |
| Open time | ✅ Opened | ✅ `entry_at` with seconds |
| Action (Exit / Square off / Square off all) | ⛔ | ⛔ "Exit / square off · read-only" in every row; no buttons |
| Trade details: Entry, Current, Unrealized, Position size, Stop-loss, Target | — | ✅ row detail: entry/current with times, qty, position size (cost basis), market value, gross, entry/exit charges, net, % net, break-even, status, and the full stop/target block (trail fraction, high-water, distance to stop, risk distance entry→stop per share and total — labelled *risk distance, not R:R* —, open risk mark→stop, how the stop fills, S07 SMA50 leg level, target, R:R reason, engine rule text, last evaluated bar, last-scan indicator values) |
| (extra) Risk to stop | — | ✅ S07 only: (avg entry − stop) per share and × qty; "above entry" when the stop has locked a gain |
| (extra) Sector, Market value, % gross, High-water, Monitoring/mark, Quote time, Updated | ✅ | ✅ |
| Account strip: Total capital, Equity/balance, Available funds, Invested value, Realized P/L, Unrealized P/L, Total P/L (net), Daily P/L, Open positions, Open orders | partial (Capital tab) | ✅ strip on top of Positions (capped book; daily P/L from the EOD snapshot, "—" when none) |

#### Rule levels (Strategies 01–06 and the S07 SMA50 leg)
Inputs already in the database only: completed bars in `daily_ohlcv` up to the position's `paper_watermarks.bar_date`, filtered exactly like engine.py (positive finite OHLC, high ≥ max(o,c,l), low ≤ min(o,c,h); `config.json indicator_excluded_sessions` removed), and the indicator values the latest scan stored in `paper_qualifier_states.checklist_json.observed_values` (whitelisted numbers only).

| Strategy | Level shown | How |
|---|---|---|
| 01 Donchian | ✅ exact next-close trigger | lowest low of the last 10 completed bars (`close < min(low[t-10:t])`) |
| 02 Supertrend | 🟡 current line (lower bound of the next trigger) | stored `supertrend` when direction is green; only if the scan stored values (prefilter PASS) |
| 03 RSI pullback | ✅ exact SMA5 leg | next close > mean(last 4 closes) ⇔ close > SMA5; RSI14 leg shown as the stored value (RSI trigger price needs full Wilder state → not computed) |
| 04 Dual MA, 06 MACD | ⛔ no price level | crossover rule; stored EMA20/EMA50 or MACD/signal values shown instead |
| 05 Bollinger | ✅ exact | next close ≥ mean(last 19 closes) ⇔ close ≥ SMA20 middle band |
| 07 SMA50 leg | ✅ exact | next close < mean(last 49 closes) ⇔ close < SMA50 |
Always labelled "rule level from completed bars" — never as a stored ticket stop. Old-trading ticket stops (e.g. FEDERALBNK 318.38) belong to a different manual book and are not copied.

### Orders
| Reference field | Before | After |
|---|---|---|
| Order ID | — | ✅ engine signal id (the paper order identifier) |
| Symbol, Strategy | ✅ | ✅ (our strategy names; old "Top 2: 12-1…" labels are not in our rules file) |
| Signal | — | ✅ Entry / Daily exit / Stop exit / Carried exit + signal bar |
| Order type | ✅ | ✅ side + type + "market at open" |
| Quantity | ⛔ | 🟡 exits: the held quantity (an exit sells the whole position); entries: "at fill · target ₹<policy target notional>" as context only — never a quantity |
| Order price | ⛔ | 🟡 "none (market)" + the stored signal-bar close (`plan_json.reference_close`) as context — never a limit price |
| Current price | — | ✅ latest accepted recorded quote for the stock (time in tooltip); "—" if none |
| Stop / Target | — | ✅ exits: the held position's current stop (S07 trail level) or rule; entries: what the stop will be after the fill ("trail at TRAIL × actual entry fill" / rule). Target: "No fixed target" |
| Order status | ✅ | ✅ + "entries paused" when the sleeve/system is paused |
| Created time | ✅ | ✅ with seconds |
| Execution time | — | ✅ fill time when filled, else "— · expected <eligible session>" |
| Remarks | — | ✅ the engine's real reason code |
| Product | — | ✅ Delivery (charges modelled by the dated delivery charge policy) |
| Action (Edit / Cancel) | ⛔ | ⛔ "Edit / cancel · read-only" |

### History (closed trades)
| Reference field | Before | After |
|---|---|---|
| Symbol, Qty, Entry, Exit | ✅ | ✅ (unmatched exits say "unmatched") |
| Break-even | — | ✅ entry + (recorded charges of both fills ÷ qty), rounded up to a paisa; needs both fee rows and matched qty |
| Gross P&L, Charges, Net P&L | ✅ | ✅ (green/red) |
| Net % | — | ✅ net P&L ÷ entry notional |
| Signal | 🟡 | ✅ entry signal type + reason of the paired BUY fill (signal id in tooltip) |
| Strategy | ✅ | ✅ separate column |
| Score | ⛔ | ⛔ "not stored" — signals are pass/fail rule checks |
| Opened, Closed, Hold, Exit reason | ✅ | ✅ (times with seconds) + exit source |
| Stop used | — | ✅ for stop exits (`trail_gap`, `trail_touch`, legacy `observed_LTP_below_close_highwater_stop`): threshold, price and rule from the engine's `risk_hit` audit event on the exit session at/before the exit fill; "level not recorded" when no such event exists. Rule exits: "not hit · rule exit" / "no fixed stop" |
| Target / R:R | — | ✅ "No fixed target"; R:R "—" with reason |
| Trade exit details | ✅ | ✅ + break-even, net %, entry signal, stop/target, engine rule text; reconciliation flag ⛔ (no reconciliation exit source) |
| Bug fixed | | `trail_gap` / `trail_touch` / `pending_exit_carry` reasons were relabelled `recorded_event`; now shown and classified as stop / carried exits |

### Analytics (ranges: Today, This week, This month, Last month, Last 3/6 months, Last year, All time, Custom date range)
| Reference metric | After |
|---|---|
| Total / Winning / Losing trades, Win rate | ✅ |
| Total, Realized (gross + net), Today's, Unrealized P&L | ✅ |
| Portfolio value, Available cash, Capital utilized, Open positions, ROI % | ✅ (ROI all time / all strategies) |
| Average profit / loss, Profit factor, Largest profit / loss, Current streak, Expectancy | ✅ |
| Avg risk:reward | ⛔ no strategy defines a target, so no R:R exists |
| Max drawdown, Sharpe, Sortino | ⛔ too few daily snapshots — the count is shown |
| Equity curve / Capital growth | ✅ latest known total P&L per EOD snapshot (+ paper capital note) |
| Daily P&L | ✅ |
| Monthly P&L | ✅ realized gross by exit month (chart) |
| Win/loss distribution | ✅ trades per gross-return bucket |
| Sector-wise | ✅ table |
| Trade frequency | ✅ fills and closed trades per session day |
| Portfolio allocation | ✅ current open positions by sector and by strategy (market value, share; unmarked positions counted separately, not valued) |
| Stop risk (new) | ✅ S07 open risk to trail stops Σ(mark−stop)×qty, risk from entry Σ(entry−stop)×qty, nearest to stop; S01–06 "rule-based" |
| Trade stats, Orders counts (executed/pending/expired=cancelled/blocked, buy/sell, same-day/overnight), Per-symbol table | ✅ |

### Daily (session picker: any day with a snapshot or fills)
| Reference section | After |
|---|---|
| Overview: day P&L, return %, realized/unrealized, trades, wins/losses, open/closed, capital used, cash, largest/avg win/loss | ✅ snapshot MTM + day return % (÷ paper capital), fills/closed trades, buy turnover (capital used), avg hold; unrealized / cash / open positions shown "now" on today only |
| Activity | ✅ fills, buys/sells, closed trades, avg order value, turnover, charges |
| Edge metrics: net/gross, profit factor, win/loss rate, expectancy | ✅ |
| Edge metrics: R:R, max DD, Sharpe, Sortino, recovery | ⛔ no target; series too short (count shown) |
| Allocation / Exposure (largest/smallest position, concentration, risk) | ✅ today: allocation by sector and strategy, largest/smallest position share, S07 risk to stops. Past sessions: ⛔ no per-session position snapshot |
| Best / worst trade, Trades by session slot, Allocation & P&L, Per-name results | ✅ |
| Visual: hourly P&L | ⛔ no intraday P&L series |
| Process scores, AI coach, Daily journal, Market context (Nifty/VIX/breadth), Session backdrop | ⛔ not recorded by the engine / read-only desk |
| CSV export | ✅ "Export session CSV" builds a file in the browser from the fills and trades already loaded. PDF export ⛔ |

### Alerts
| Reference field | After |
|---|---|
| Create alert (symbol, condition, price), delete alert, active alert list | ⛔ labelled: no user alert store; desk is read-only |
| (Atlas) Engine alerts: time, type, severity, outcome, stock, strategy, bar, price, threshold, rule/reason | ✅ risk hits, blocked paper orders, WARN/ERROR/CRITICAL events (whitelisted payload fields only) |

### Capital
| Reference field | After |
|---|---|
| Starting capital, Current total capital, Available funds, Margin used (invested), Total realized/unrealized P&L | ✅ |
| Balance/Equity/Invested/Available cash/Open positions/Open orders strip | ✅ (open orders = pending signals) |
| Total P&L, Daily P&L (+%) widgets | ✅ Net P&L with return %; daily P&L on Analytics/Daily |
| Account settings: set starting capital / save / reset account | ⛔ shown as read-only policy values (policy version, capital, sleeve capital, target notional, max positions per sleeve, max strategies per ISIN, ADV floor, uncovered legacy fills) |
| Transaction log: DateTime, Symbol, Action, Amount, Balance after, paging | ✅ + turnover, charges, sleeve, policy; balance = per-sleeve cash after each fill (window sum over the whole ledger) |
| (extra) Cash by sleeve; dividend/cash credits | ✅ |
| Token management panel | — out of scope (credentials never shown on the public desk) |

### Market engine health widget (reference) → Engine runs tab
| Reference field | After |
|---|---|
| Status, last updated | ✅ last run of each kind with status and count |
| Last tick | ✅ last accepted quote time |
| Last reconciliation | 🟡 latest run of each recorded kind; no separate reconciliation run kind exists |
| Open positions, tracked symbols | ✅ open positions on tab badge; symbols ever quoted |
| Start / stop feed buttons | ⛔ read-only |

### Watchlist
Symbol, scanner score, confidence, add/remove → ⛔ labelled unavailable; Strategy tester shows qualifiers per strategy.

## Bugs fixed
- Tab switch showed the previous tab's rows under the new headings: `useDashboard` now only returns data for the current query; a new tab/filter shows "Loading …" until its own rows arrive.
- Recent fills was silently capped at 12: panel now says "Latest 12 of N fills" and opens the full Fills tab.
- Recent runs showed mostly monitor checks: Engine runs tab has a kind filter and a latest-per-kind strip, so the 08:45 scan is visible.
- `partial` scan runs and `ignored_same_bar` / `shadow_closed` signals were relabelled "recorded"; now shown with their real status.
- Hero return showed −0.00%: values under 0.01% now show 4 decimals.
- Stale vs missing marks are now distinct states (fresh / stale / missing / future).

## API changes (thin, read-only, fixed SELECTs)
- New views: `trades`, `costs`, `credits`, `dataaudit`, `events`.
- New enum filters: `status` (signals), `kind` (runs), `accepted` (quotes).
- Enrichment (second pass): positions + `exit_plan` (stop/rule level/target/R:R reason/indicator snapshot), entry/exit charges, break-even, net unrealized, entry signal, pause state, pending exit; pending signals + exit plan / entry plan, current quote; trades + stop threshold, break-even, net %, entry signal; fills + execution path. New fixed SELECTs on `paper_watermarks`, `daily_ohlcv`, `paper_qualifier_states`, `paper_pause_state`, `paper_fill_costs`, `paper_events (risk_hit)`, `paper_quote_audit`; missing tables/columns fall back to unavailable.
- Enrichment: fills + fee rows; signals + executing fill; runs + whitelisted summary + health; quotes + lag; positions + mark_state, %, cost basis, market value, sector.
- Raw `details_json`, `data_json`, `components_json` never leave the server. Max page size is still 100; tabs that summarise (Analytics, Daily) read every page (up to 50 pages = 5,000 rows) and show a "Partial record" warning if that cap is ever hit.

## Colour and phone layout (second pass)
- Ledger section is scoped with `.ad-ledger-palette`, using the old-trading colour tokens from `frontend/src/styles.css` (slate borders `#e2e8f0`/`#2a3444`, soft cards `#f7f9fc`, blue primary `#2563eb`/`#3b82f6`, teal stop accent, amber warning `#d97706`/`#f59e0b`, green/red P&L `#15803d`/`#b91c1c` light and `#22c55e`/`#ef4444` dark, pill status tags). Layout and type remain Atlas Desk. (The parent-supplied screenshot files were not present on the box; colours come from the old-trading source.)
- Phones (≤640px, checked at 375px in headless Chrome): tables scroll horizontally inside their card with a sticky first column, long rule/stop cells wrap within a bounded width, tabs stay one scrollable row, filters are full width with 36px touch targets, tiles/account strip are two columns, row details and key-values stack. Covered by `tests/ledger-mobile.test.jsx`.

## Engine / rules untouched
Dashboard and API display only. `automation/paper-trading` (strategy rules, TRAIL, engine, fee model) unchanged; the dashboard imports `config.json`, `strategy_rules.json`, `capital_policy.json`, `charge_policy.json` and `fees.mjs estimate()` read-only. No new Fyers calls.
