"""Record real Track A replica parity (Task 4). Offline-testable, pure logic: no
DB client, no network, no broker. Inputs are plain lists/dicts; the caller
(adaptive_daily.py, fed live fills by adaptive_runner.py) supplies them.

Comparison basis — the engine's own definition
(adaptive_engine.classify_discordant_trades): a trade is *inherited* when its
entry_date is on or before warm_start_end, and *new* (evaluation window) when
its entry_date is after warm_start_end. Only evaluation-window entries/exits
and skips are compared against the live ledger for the session day:
- replica entries on the session day = evaluation-window trades (closed or
  still open) whose entry_date == session_day;
- replica exits on the session day = evaluation-window trades whose
  exit_date == session_day;
- replica skips on the session day: a skipped symbol must not appear as a
  live BUY fill that day (live skips are not observable from fills alone, so
  consistency with fills is the checkable form).
Fills/qty compare as multisets of (symbol, side, qty, price) with prices
rounded to 2dp (Rs, as in paper_fills). Equity is the straight D3 difference
replica daily equity minus live sleeve equity for the strategy/day, judged
against parity_equity_tolerance_paise (100 paise) inside record_parity.

If live fills cannot be obtained, or the EOD is not reconciled, or the
simulation did not run, record_for_session records NOTHING — a missing parity
row correctly breaks the parity streak — and returns not_evaluable + reason.
Never write parity_ok=1 unless every compared field truly matched.

R4 HOLD (pending Grok's approval of a comparison basis): the exact-2dp price
basis and the replica-vs-live-sleeve equity basis are both unevaluable as
specified (see review of adaptive-engine-01), so record_for_session currently
writes NO row and returns {'status': 'not_evaluable',
'reason': 'parity_basis_pending_review'} for every call. The pure comparison
functions below (compare_session_parity, evaluation_window_fills) are
unchanged and still unit-tested; only the recording step is held.
"""
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE.parent / 'automation' / 'paper-trading'
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))
# NOTE: adaptive_observation is intentionally NOT imported here while the R4
# hold is in force: record_for_session writes no parity row, so there is no
# record_parity call to make.


def _price(v):
    return round(float(v), 2)


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


def compare_session_parity(replica_closed_trades, replica_open_positions, replica_entry_skips,
                           live_fills, warm_start_end, session_day,
                           replica_equity_paise, live_equity_paise, strategy=None):
    """Pure comparison. live_fills: Turso paper_fills rows for the strategy/day
    (any extra days/strategies are filtered out here)."""
    day = str(session_day)[:10]
    replica_fills = evaluation_window_fills(replica_closed_trades, replica_open_positions,
                                            warm_start_end, day)
    live_day = []
    for f in live_fills or []:
        if str(f.get('filled_at') or '')[:10] != day:
            continue
        # R3: a strategy is compared only against its own fills, never against
        # another strategy's fills from the same day. (Rows without a strategy
        # key predate the filter and are kept, as before.)
        if strategy is not None and f.get('strategy') is not None and int(f['strategy']) != int(strategy):
            continue
        live_day.append({'symbol': str(f['symbol']), 'side': str(f['side']).upper(),
                         'qty': int(f['qty']), 'price': _price(f['price'])})
    key = lambda r: (r['symbol'], r['side'], r['qty'], r['price'])
    qty_key = lambda r: (r['symbol'], r['side'], r['qty'])
    fills_match = Counter(map(key, replica_fills)) == Counter(map(key, live_day))
    qty_match = Counter(map(qty_key, replica_fills)) == Counter(map(qty_key, live_day))
    live_bought = {r['symbol'] for r in live_day if r['side'] == 'BUY'}
    skipped = [s for s in (replica_entry_skips or []) if str(s.get('day') or '')[:10] == day]
    skips_match = not any(str(s.get('symbol')) in live_bought for s in skipped)
    equity_diff = int(replica_equity_paise) - int(live_equity_paise)
    reasons = []
    if not fills_match:
        reasons.append('replica fills for the session differ from live fills')
    if not qty_match:
        reasons.append('replica fill quantities differ from live fills')
    if not skips_match:
        reasons.append('live bought a symbol the replica skipped')
    return {'replica_fills': replica_fills, 'live_fills': live_day,
            'fills_match': fills_match, 'qty_match': qty_match, 'skips_match': skips_match,
            'equity_diff_paise': equity_diff, 'reasons': reasons}


def record_for_session(db, namespace, strategy, session_day, *, reconciled, simulated,
                       live_fills, replica_closed_trades, replica_open_positions,
                       replica_entry_skips, warm_start_end,
                       replica_equity_paise, live_equity_paise):
    """Record one parity row, or nothing (not_evaluable) when the comparison
    cannot honestly be made. Idempotent: re-running the same day replaces the row.

    R4 HOLD: no row is written and this returns
    {'status': 'not_evaluable', 'reason': 'parity_basis_pending_review'} until
    Grok approves a comparison basis. Do not wire any parity row writes until then."""
    day = str(session_day)[:10]
    if not reconciled:
        return {'status': 'not_evaluable', 'reason': 'track_a_eod_not_reconciled'}
    if not simulated:
        return {'status': 'not_evaluable', 'reason': 'simulation_not_simulated'}
    if live_fills is None:
        return {'status': 'not_evaluable', 'reason': 'live_fills_unavailable'}
    if replica_equity_paise is None or live_equity_paise is None:
        return {'status': 'not_evaluable', 'reason': 'equity_unavailable'}
    # R4 HOLD (pending Grok's approval of a comparison basis): the pure
    # comparison (compare_session_parity) is kept and unit-tested, but no
    # parity row is written here. The pre-hold write path (compare, then
    # aobs.record_parity) is deliberately removed, not left unreachable,
    # so no row can be written until the basis is approved and re-wired.
    return {'status': 'not_evaluable', 'reason': 'parity_basis_pending_review'}
