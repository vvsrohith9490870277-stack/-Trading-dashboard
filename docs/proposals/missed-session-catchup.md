# Missed-session catch-up of entries — proposal (PROPOSE ONLY, not implemented)

Status: **PROPOSAL ONLY — no code changed.** The senior takes this to the user.

## Problem, with evidence

When a scan covers several missed sessions, entries are evaluated only on the latest bar, while
exits look at all new bars — so signals on missed bars are lost.

- `engine.py` scan loop: `x=f.iloc[-1]; bar=str(x.trade_date)` — the entry condition
  (`x.entry_condition`) is read from the last bar only. A symbol whose latest bar is not the
  target session is skipped outright (`if str(g.iloc[-1].trade_date)!=str(target): skipped...`).
  After the scan the watermark advances to the latest bar
  (`UPDATE paper_watermarks SET bar_date=?` with the latest bar), so a missed bar is never
  re-evaluated for an entry. A pending BUY signal whose `eligible_date` has passed is expired
  (`UPDATE paper_signals SET status='expired'`).
- Exits are different: `newbars=f[(f.trade_date>previous)&...]` and
  `missed_exit=bool(newbars.exit_condition.any())` — an exit condition on ANY new bar since the
  watermark creates a SELL signal (bar = latest bar). Exits are not lost; entries are.
- Strategy 7's high-water `H`: the brief said H did not ratchet on missed closes. The code does
  ratchet it in the scan (`engine.py`, in the scan loop): `since=g[(g.trade_date>=entry)&
  (g.trade_date<=bar)]; H=max(p['high_water'], float(since.close.max()))`, written back with
  `high_water=MAX(high_water,?)`. So H ratchets to the highest close over ALL bars since entry
  whenever a scan runs. The remaining H gap is only when no scan runs at all (host down): H stays
  stale until the next scan, and the missed-trail-touch detector (`missed_trail_touch`) is
  alert-only — it emits a `missed_stop_touch` audit event (severity ERROR) and never a fill, never
  backdated.

Our current choice is: no backfill of stale signals; outages are recorded in `paper_outages`.

## Options

### (a) Keep as is and record it — CURRENT CHOICE

- What it is: entries are evaluated only on the latest bar; a missed session's entry signals are
  lost by design; exits still fire from any new bar; H ratchets in the scan.
- Effect on frozen rules: none. Entry timing stays exactly as the frozen strategy rules define
  (an entry is a signal on the latest completed bar, eligible the next session).
- Risks: a multi-day outage loses entries that would have qualified; the strategy's live
  behaviour silently diverges from its backtest on those days. The loss is visible only in the
  scan report (`unavailable_current_symbols`, `signals_queued`) and the `paper_outages` rows.

### (b) Replay missed bars with expiry rules

- What it is: when a scan covers N missed sessions, evaluate the entry condition on EACH missed
  bar (not just the latest) and create a signal for each qualifying bar — but only if the signal
  is still fresh under an expiry rule, e.g. "a missed-bar signal is eligible only if the current
  price is within X% of the missed bar's close and no more than N sessions have passed".
- Effect on frozen rules: entering late on a stale signal changes strategy behaviour (entry
  timing is frozen). This needs the senior/user to define the expiry rule — X and N are new
  policy numbers, not code tweaks — and to accept that a replayed entry is a deliberate deviation
  from the backtest, recorded as such in the decision payload.
- What would be installed where: `engine.py` scan loop (evaluate per missed bar, not
  `f.iloc[-1]` only); the expiry numbers in `config.json` (non-frozen numbers only); a test that
  a 3-missed-session scan creates the replayed signals and that an over-age signal expires
  instead. The watermark must advance per bar, not jump to the latest.
- Risks: the most complex option; a wrong expiry rule creates entries the strategy never intended
  (stale-price entries are exactly the kind of trade the frozen rules were written to avoid).

### (c) Ratchet H only — ALREADY DONE, verify with a test

- What it is: no entry change; keep the H ratchet as it is and add a test proving H ratchets over
  missed closes (a Strategy 7 position, a scan covering 3 sessions with a higher close on a
  missed bar, assert `high_water` moved).
- Effect on frozen rules: none — this is the current behaviour, pinned by a test.
- Risks: none, but it does not recover any lost entry.

## Recommendation

Keep (a) as the default, and do (c) as a test-only follow-up so the H ratchet is pinned. Take (b)
to the user only if missed entries are judged to cost more than the risk of stale entries: it
needs an expiry rule the user must set, and it changes frozen entry timing, so it cannot be
decided by Muse alone.
