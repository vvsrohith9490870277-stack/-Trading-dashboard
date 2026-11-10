# Atlas Desk portal (new UI)

A new, original UI for the dashboard, at path routes. The classic hash-routed dashboard is unchanged at `/`.
The old-trading repo and the trading-labs reference site were used only to decide which screens and features
were needed. Layout, components and visual design here are new.

| Route | Screen | Data source |
|---|---|---|
| `/paper` | Paper desk: capped-ledger headline, account tiles, seven sleeve cards (slot meter, equity, net), recent fills and engine runs, server-paginated ledger browser (positions, fills, signals, runs, quote audit), demo record kept separate | `/api/dashboard?view=overview` and `view=positions|fills|signals|runs|quotes` |
| `/strategy-tester` | Test bench: strategy and run-source picker. **Saved backtest** (KPIs, sequential funnel, yearly columns, exit-month heatmap, matched stocks, per-stock ranking, best and worst trades) or **Live scan** (latest completed-bar counts and qualifiers) | `public/data/historical.json`, `strategy-catalog.json`, `view=scan`, `view=qualifiers` |
| `/strategy-comparison?ids=1,4,7` | Compare 2–4 strategies: metric matrix, yearly mean overlay, signal agreement (union, intersection, Jaccard, pairwise matrix), shared and exclusive names, live paper per strategy | historical export and overview |
| `/performance` | Gross total, daily snapshot, capped capital return, closed stats, daily MTM columns, cumulative total, per-strategy table, net-of-cost book (per strategy, cohort, EOD, charges) | overview and `view=analytics` |
| `/stock/:symbol?runId=…` | Stock detail: last accepted quote and sparkline, the run (saved per-trade backtest with 1w/1m/1y window, filters, plan; or live-scan checklist and plan), paper positions and fills, footprint across all seven saved runs | `view=history`, `positions`, `fills`, `qualifiers`, `public/data/stock-backtests/<id>/<SYMBOL>.json` |

Run IDs: `hist-<asOf>-s<1..7>` is a saved backtest export. `scan-<barDate>-s<1..7>` is a live completed-bar scan.

Theme: dark and light, toggled from the top bar. The choice is saved in `localStorage` (`atlas-portal-theme`) and defaults to the OS preference. All colours are CSS variables under `.ad-root[data-theme]`, and the SVG charts read them too.

## Deliberately not implemented (no data source)

- **Custom rule builder / Pine import / launching new backtests.** There is no compute endpoint, so the tester only shows saved runs and live scans. The UI shows this as a locked panel.
- **Fundamentals, corporate events and news** on stock detail. There is no source, so the panel says that and shows nothing else.
- **CAGR, Sharpe, Sortino, drawdown of paper equity.** The paper record is too short, so these are not computed.
- **Order ticket, alerts, account reset.** The dashboard is read-only and paper-only, and orders come only from the scheduled engine.
- Stock-detail fills come from the latest 100 fills (the fills API has no symbol filter). The screen says so.

The isolated `paper_demo_v1` RELIANCE demo record is shown only in its own labelled panel and is excluded from every total.
