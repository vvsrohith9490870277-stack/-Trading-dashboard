# Paper ledger tab parity — old-trading reference vs Atlas Desk

Reference: `old-trading` @ `65e712a9` — `frontend/src/components/PaperTradingPage.tsx` (tabs, Positions/Orders/History tables, TradeDetailsModal, AlertsPanel, AccountPanel, AccountSummaryStrip, MarketEngineHealthWidget, PaperAccountWidgets), `AnalyticsPanel.tsx`, `DailyAnalyticsPanel.tsx`, `WatchlistTab.tsx`.
Atlas Desk: branch `feature/atlas-desk-ledger-parity` (off `main` 9ffd304). Section: Paper desk → "Browse the paper ledger".

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
| Reference field | Before | After |
|---|---|---|
| Symbol | ✅ | ✅ (link to stock page) |
| Qty | ✅ | ✅ |
| Avg entry | ✅ (Entry) | ✅ |
| Current | ✅ (Last mark) | ✅ |
| Unrealized | ✅ | ✅ (null when mark missing, never 0) |
| % P&L | — | ✅ |
| Stop | — | ⛔ "n/a" — engine evaluates exits per check; no stop level stored |
| Target | — | ⛔ "n/a" — no fixed target (`target_defined=false`) |
| R:R | — | ⛔ (no stop/target stored) — listed in Analytics unavailable |
| Monitoring (lifecycle) | 🟡 single "stale / missing" tag | ✅ fresh / stale (with age, "market closed") / missing / future-dated |
| Action (Exit / Square off / Square off all) | — | ⛔ read-only desk, stated in tab note |
| (extra) Strategy, Sector, Cost basis, Market value, High-water, Quote time, Opened, Updated | partial | ✅ |
| Filters | strategy, date | strategy, date |

### Orders
| Reference field | Before | After |
|---|---|---|
| Symbol, Side | — | ✅ |
| Type | — | ✅ Entry / Daily exit / Stop exit (from signal reason) |
| Qty | — | ⛔ sized at fill time |
| Requested price | — | ⛔ signals carry no limit price |
| Order time | — | ✅ |
| Status | — | ✅ |
| Expected execution | — | ✅ eligible session date |
| Action (Edit / Cancel) | — | ⛔ read-only |
| (extra) Strategy, Signal bar, Reason | — | ✅ |

### History (closed trades)
| Reference field | Before | After |
|---|---|---|
| Symbol, Qty, Entry, Exit | — | ✅ (exit paired with latest earlier BUY fill; unmatched exits show no entry) |
| P&L, P&L % | — | ✅ gross; % only when entry/exit quantity match |
| Signal | — | ✅ strategy that generated the trade |
| Score | — | ⛔ not stored in the paper ledger |
| Opened, Closed, Hold | — | ✅ |
| Exit reason | — | ✅ |
| Trade exit details: Reason, Source, Exit price, Exit time, reconciliation flag | — | ✅ Reason, Source (rule / intraday stop), Exit price, Exit time, exit quote time, entry/exit fill ids; reconciliation flag ⛔ (engine has no reconciliation exit source) |
| (extra) Charges (both fills), Net P&L, per-fill fee breakdown, Sector | — | ✅ (null if either fee row missing) |

### Analytics (range: Today, This week, This month, Last month, Last 3/6 months, Last year, All time)
| Reference metric | After |
|---|---|
| Total / Winning / Losing trades, Win rate | ✅ |
| Total P&L, Realized P&L | ✅ gross and net-of-charges |
| Today's P&L | ✅ latest EOD snapshot (null if none) |
| Unrealized P&L, Portfolio value, Available cash, Capital utilized, Open positions | ✅ current values (labelled "now") |
| ROI % | ✅ all time / all strategies only (capped net ÷ paper capital) |
| Average profit / loss, Profit factor, Largest profit / loss, Current streak | ✅ |
| Avg risk:reward | ⛔ no stop/target levels |
| Max drawdown, Sharpe | ⛔ too few daily snapshots — count shown |
| Charts: Equity curve / Cumulative returns / Capital growth | ✅ cumulative P&L per EOD snapshot |
| Charts: Daily P&L | ✅ |
| Charts: Monthly P&L | 🟡 already on Performance screen; daily columns here |
| Win/loss distribution, Sector-wise, Trade frequency, Portfolio allocation charts | 🟡 shown as tables (per stock, per sector, per strategy) |
| Trade stats: Avg holding time, Avg entry/exit price, Best/Worst trade, Most profitable/losing symbol, Longest win/loss streak, Avg return %, Avg holding period | ✅ |
| Orders: Total/Executed/Cancelled/Pending, Buy/Sell orders, Intraday/Delivery trades | ✅ fills, buy/sell fills, pending/expired/blocked signals, same-day vs overnight round trips; "cancelled" = expired signals |
| Per-symbol stats table (avg hold, trades, win rate, P&L) | ✅ |
| (extra) Expectancy, turnover, charges, unmatched exits, per-strategy table | ✅ |

### Daily (session picker: any day with a snapshot or fills)
| Reference section | After |
|---|---|
| Overview: today's profit/loss, return %, realized/unrealized, trades, wins/losses, open/closed, capital used, cash, largest win/loss, avg win/loss | ✅ session fills/trades + EOD snapshot (daily MTM, total, realized on closures, live/last-session valuation, missing basis symbols, as-of) |
| Activity: total trades, executed, pending, cancelled, rejected, avg hold, avg size | ✅ fills, buys/sells, closed trades, avg order value (pending/expired counts on Analytics) |
| Edge metrics: net/gross profit/loss, profit factor, win/loss rate, expectancy | ✅ |
| Edge metrics: R:R, max DD, Sharpe, Sortino, recovery | ⛔ (no stop/target; series too short) |
| Allocation | ✅ Capital tab (current); per-session sector allocation of buys |
| Best / worst trade (entry, exit, profit, hold, reason) | ✅ |
| Exposure (largest/smallest position, risk %, concentration) | 🟡 positions table shows cost basis/market value per position; risk % ⛔ |
| Trades by session slot | ✅ fills by IST hour with realized P&L |
| Sector analysis, Per-name results | ✅ (sector = NSE industry from instruments list) |
| Visual: hourly P&L | ⛔ no intraday P&L series |
| Process scores, AI coach notes | ⛔ not produced by the engine |
| Daily journal (notes) | ⛔ read-only desk, no journal store |
| Market context (Nifty, Bank Nifty, VIX, breadth) | ⛔ not recorded in the ledger |
| CSV export | — not added (audit export already exists in EOD tooling) |

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
- Enrichment: fills + fee rows; signals + executing fill; runs + whitelisted summary + health; quotes + lag; positions + mark_state, %, cost basis, market value, sector.
- Raw `details_json`, `data_json`, `components_json` never leave the server. Max page size is still 100; tabs that summarise (Analytics, Daily) read every page (up to 50 pages = 5,000 rows) and show a "Partial record" warning if that cap is ever hit.
