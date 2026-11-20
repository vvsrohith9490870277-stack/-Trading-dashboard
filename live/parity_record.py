"""Record real Track A replica parity (Task 4) under the APPROVED P2+E1 basis.

Offline-testable, pure logic: no DB client, no network, no broker. Inputs are
plain lists/dicts; the caller (adaptive_daily.py, fed live fills by
adaptive_runner.py) supplies them.

Comparison basis — APPROVED by the user 2026-11-18 21:37 IST (Review 2,
P2+E1 with amendments a-d and conditions 1-6). The R4 hold is LIFTED on this
branch (feat/adaptive-tuning) only, because the approved basis now exists:
- (1) Match per strategy. Key = (strategy, symbol, side, session day). Live
  fills filtered with int(f['strategy']) == int(strategy) (R3). Never compare
  one strategy against the pool.
- (2) Entry-day vs fill-day. Track A decides on day D's close and fills on
  D+1 shortly after the open. The comparison day is the FILL day: the live
  filled_at date (IST) against the replica's entry_date/exit_date, which the
  simulator already stamps with the fill day (signals at close t fill at open
  t+1). Warm-start-inherited replica positions are excluded from the
  comparison (evaluation_window_fills).
- (3) Price = informational, not pass/fail. Live fills are the quote 1-3 min
  after 09:15, the replica uses the open/simulated price, so exact equality
  never holds. Both prices and the bps difference are recorded; there is no
  tolerance number (none was approved) and no pass/fail on price. An
  OHLC-range plausibility check is recorded when day bars are supplied.
- (4) Partial overlap is expected. Live has cash/slot/ASM-GSM limits; the
  replica may take a name live skipped (or the reverse). Report matched /
  live-only / replica-only counts per strategy-day instead of one boolean.
- (5) Equity basis must not compare levels. Replica equity includes
  warm-start positions; the live sleeve started fresh. Only a change-based
  figure (day-over-day change) is recorded, in paise, rupees and percent.
  If the previous day's equities are unavailable the equity part is
  not_evaluable and NO level difference is recorded as the equity figure.
  Note: the replica change is on the liquidation-net basis while the live
  change is on gross ledger marks (see adaptive_live_track_a_equity); the
  recorded diff is informational and carries that basis note.
- (6) No false evidence. Rows only for reconciled + SIMULATED days with live
  fills available; idempotent re-run (INSERT OR REPLACE); never write for
  paper_v1 / paper_demo_v1 / paper_v2, only adaptive_parity in the sidecar
  DB; no policy numbers added.
- (a) qty is REPORTED (live qty, replica qty, difference), not a hard match
  condition, because live sizing depends on cash and slots the replica
  cannot see.
- (d) E1 tolerance is NOT a number Muse chooses: the change difference is
  recorded in rupees and percent and pass/fail is left off until the user
  sets a tolerance. The adaptive_parity.parity_ok column is NOT NULL
  CHECK IN (0,1), so the row stores parity_ok=0 with
  details.verdict='informational_no_pass_fail' — 0 here means "no verdict
  claimed", not "failed".

The old D3 basis (exact-2dp price match + |equity level diff| <= 100 paise
in adaptive_observation.record_parity) is NOT used by record_for_session
anymore. record_parity itself is untouched (it lives in
adaptive_observation.py, outside this branch's file scope); the new recorder
writes the adaptive_parity row directly with INSERT OR REPLACE.
"""
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE.parent / 'automation' / 'paper-trading'
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

BASIS = 'P2+E1_informational'
VERDICT = 'informational_no_pass_fail'
PROTECTED_NAMESPACES = frozenset({'paper_v1', 'paper_demo_v1', 'paper_v2'})


def _price(v):
    return round(float(v), 2)


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def evaluation_window_fills(replica_closed_trades, replica_open_positions, warm_start_end, session_day):
    """Replica fills for one session day, evaluation-window entries only."""
    ws_end = str(warm_start_end)
    day = str(session_day)[:10]
    fills = []
    for t in replica_closed_trades or []:
        if str(t.get('entry_date') or '') <= ws_end:
            continue  # inherited warm-start trade: not discordance by itself
        if str(t.get('entry_date') or '')[:10] == day:
            fills.append({'symbol': str(t['symbol']), 'side': 'BUY', 'qty': int(t['qty']),
                          'price': _price(t['entry_price'])})
        if str(t.get('exit_date') or '')[:10] == day:
            fills.append({'symbol': str(t['symbol']), 'side': 'SELL', 'qty': int(t['qty']),
                          'price': _price(t['exit_price'])})
    for p in replica_open_positions or []:
        if str(p.get('entry_date') or '') <= ws_end:
            continue
        if str(p.get('entry_date') or '')[:10] == day:
            fills.append({'symbol': str(p['symbol']), 'side': 'BUY', 'qty': int(p['qty']),
                          'price': _price(p['entry_price'])})
    return fills


def _pair_fills(replica_fills, live_fills, day_bars=None):
    """Match fills on the P2 key (symbol, side) as multisets. Pairs are formed
    deterministically (sorted by price); leftovers are live-only/replica-only.
    Price is informational: both prices and the bps difference are reported."""
    rep = defaultdict(list)
    liv = defaultdict(list)
    for r in replica_fills:
        rep[(r['symbol'], r['side'])].append(r)
    for f in live_fills:
        liv[(f['symbol'], f['side'])].append(f)
    matched, live_only, replica_only = [], [], []
    for key in sorted(set(rep) | set(liv)):
        rs = sorted(rep.get(key, []), key=lambda x: x['price'])
        ls = sorted(liv.get(key, []), key=lambda x: x['price'])
        n = min(len(rs), len(ls))
        for r, l in zip(rs[:n], ls[:n]):
            bps = round((float(l['price']) - float(r['price'])) / float(r['price']) * 10000, 2) if float(r['price']) else None
            plausible = None
            if day_bars is not None:
                bar = day_bars.get(r['symbol'])
                if bar is not None:
                    lo, hi = float(bar['low']), float(bar['high'])
                    plausible = bool(lo <= float(l['price']) <= hi and lo <= float(r['price']) <= hi)
            matched.append({'symbol': key[0], 'side': key[1],
                            'live_qty': int(l['qty']), 'replica_qty': int(r['qty']),
                            'qty_diff': int(l['qty']) - int(r['qty']),
                            'live_price': _price(l['price']), 'replica_price': _price(r['price']),
                            'price_bps_diff': bps, 'price_plausible': plausible})
        for l in ls[n:]:
            live_only.append({'symbol': key[0], 'side': key[1], 'qty': int(l['qty']), 'price': _price(l['price'])})
        for r in rs[n:]:
            replica_only.append({'symbol': key[0], 'side': key[1], 'qty': int(r['qty']), 'price': _price(r['price'])})
    return matched, live_only, replica_only


def compare_session_parity(replica_closed_trades, replica_open_positions, replica_entry_skips,
                           live_fills, warm_start_end, session_day,
                           replica_equity_paise, live_equity_paise, strategy=None,
                           replica_equity_prev_paise=None, live_equity_prev_paise=None,
                           day_bars=None):
    """Pure P2+E1 comparison. live_fills: Turso paper_fills rows for the
    strategy/day (any extra days/strategies are filtered out here). No
    pass/fail is computed anywhere in this function."""
    day = str(session_day)[:10]
    replica_fills = evaluation_window_fills(replica_closed_trades, replica_open_positions,
                                            warm_start_end, day)
    live_day = []
    for f in live_fills or []:
        if str(f.get('filled_at') or '')[:10] != day:
            continue
        # R3 / condition 1: a strategy is compared only against its own fills.
        if strategy is not None and f.get('strategy') is not None and int(f['strategy']) != int(strategy):
            continue
        live_day.append({'symbol': str(f['symbol']), 'side': str(f['side']).upper(),
                         'qty': int(f['qty']), 'price': _price(f['price'])})
    matched, live_only, replica_only = _pair_fills(replica_fills, live_day, day_bars)
    counts = {'matched': len(matched), 'live_only': len(live_only), 'replica_only': len(replica_only)}
    live_bought = {r['symbol'] for r in live_day if r['side'] == 'BUY'}
    skipped = [s for s in (replica_entry_skips or []) if str(s.get('day') or '')[:10] == day]
    skips_match = not any(str(s.get('symbol')) in live_bought for s in skipped)
    equity_change = None
    reasons = []
    if (replica_equity_paise is not None and live_equity_paise is not None
            and replica_equity_prev_paise is not None and live_equity_prev_paise is not None):
        rc = int(replica_equity_paise) - int(replica_equity_prev_paise)
        lc = int(live_equity_paise) - int(live_equity_prev_paise)
        diff = rc - lc
        pct = round(diff / abs(lc) * 100, 4) if lc != 0 else None
        equity_change = {'replica_change_paise': rc, 'live_change_paise': lc,
                         'diff_paise': diff, 'diff_inr': round(diff / 100, 2), 'diff_pct': pct}
    else:
        reasons.append('equity_change_not_evaluable: prev-day equity missing (levels never compared)')
    for m in matched:
        if m['price_bps_diff'] not in (0, 0.0, None):
            reasons.append(f"{m['symbol']} {m['side']}: live price differs from replica by "
                           f"{m['price_bps_diff']} bps (informational)")
    if live_only or replica_only:
        reasons.append('partial overlap: live-only/replica-only fills present (expected; counts reported)')
    return {'replica_fills': replica_fills, 'live_fills': live_day,
            'matched': matched, 'live_only': live_only, 'replica_only': replica_only,
            'counts': counts, 'skips_match': skips_match,
            'equity_change': equity_change, 'reasons': reasons}


def record_for_session(db, namespace, strategy, session_day, *, reconciled, simulated,
                       live_fills, replica_closed_trades, replica_open_positions,
                       replica_entry_skips, warm_start_end,
                       replica_equity_paise, live_equity_paise,
                       replica_equity_prev_paise=None, live_equity_prev_paise=None,
                       day_bars=None):
    """Record one informational parity row, or nothing (not_evaluable) when the
    comparison cannot honestly be made. Idempotent: re-running the same day
    replaces the row (INSERT OR REPLACE on the (namespace, strategy,
    trade_date) primary key). Writes ONLY the adaptive_parity table.

    R4 hold LIFTED on this branch only (feat/adaptive-tuning): the approved
    P2+E1 basis above now exists. Do not use this recorder on any branch
    where the basis is not approved.
    """
    day = str(session_day)[:10]
    if str(namespace) in PROTECTED_NAMESPACES:
        return {'status': 'not_evaluable', 'reason': 'protected_namespace'}
    if not reconciled:
        return {'status': 'not_evaluable', 'reason': 'track_a_eod_not_reconciled'}
    if not simulated:
        return {'status': 'not_evaluable', 'reason': 'simulation_not_simulated'}
    if live_fills is None:
        return {'status': 'not_evaluable', 'reason': 'live_fills_unavailable'}
    out = compare_session_parity(replica_closed_trades, replica_open_positions, replica_entry_skips,
                                 live_fills, warm_start_end, day,
                                 replica_equity_paise, live_equity_paise, strategy,
                                 replica_equity_prev_paise, live_equity_prev_paise, day_bars)
    counts = out['counts']
    # fills_match / qty_match are informational key/qty agreement flags, NOT a
    # verdict: parity_ok stays 0 (no pass/fail) with the verdict in details.
    fills_match = 1 if not out['live_only'] and not out['replica_only'] else 0
    qty_match = 1 if (fills_match == 1 and all(m['qty_diff'] == 0 for m in out['matched'])) else 0
    equity_diff_paise = out['equity_change']['diff_paise'] if out['equity_change'] else 0
    details = {'basis': BASIS, 'verdict': VERDICT,
               'counts': counts, 'matched': out['matched'],
               'live_only': out['live_only'], 'replica_only': out['replica_only'],
               'skips_match': out['skips_match'], 'equity_change': out['equity_change'],
               'equity_basis_note': ('replica change is liquidation-net; live change is gross '
                                     'ledger marks (adaptive_live_track_a_equity)'),
               'note': ('P2+E1: no pass/fail until the user sets a tolerance; '
                        'qty reported, not matched; price informational')}
    db.batch([(
        'INSERT OR REPLACE INTO adaptive_parity(namespace,strategy,trade_date,fills_match,skips_match,qty_match,equity_diff_paise,parity_ok,details_json,recorded_at) '
        'VALUES(?,?,?,?,?,?,?,?,?,?)',
        (str(namespace), int(strategy), day, fills_match, int(bool(out['skips_match'])), qty_match,
         equity_diff_paise, 0, json.dumps(details, sort_keys=True), _utc_now()),
    )])
    return {'status': 'recorded', 'namespace': str(namespace), 'strategy': int(strategy),
            'trade_date': day, 'counts': counts, 'skips_match': bool(out['skips_match']),
            'equity_change': out['equity_change'], 'parity_ok': 0, 'verdict': VERDICT}
