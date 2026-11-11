"""P4: compare adaptive Track A replica arms (local adaptive.sqlite) with the live paper_v1 ledger (Turso read-only).

Never writes to Turso or /workspace/paper-trading. Usage:
  python track_a_parity.py [--session YYYY-MM-DD]
"""
import argparse, json, sqlite3, sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from ro_client import TursoRO

IST = ZoneInfo('Asia/Kolkata')
CFG = json.loads((HERE / 'adaptive_live_config.json').read_text())
NS = CFG['namespace']
TRACK_A_NS = 'paper_v1'


def live_ledger(ro, session=None):
    eod_days = ro.query('SELECT day, as_of, report_json FROM paper_eod WHERE namespace=? ORDER BY day', (TRACK_A_NS,))
    latest = eod_days[-1] if eod_days else None
    day = session or (latest['day'] if latest else None)
    sleeves = {}
    if latest:
        rep = json.loads(latest['report_json'])
        sleeves = ((rep.get('capped_master_ledger') or {}).get('sleeves')) or {}
    positions = ro.query(
        'SELECT strategy, symbol, qty, entry_price, entry_at FROM paper_positions WHERE namespace=? AND qty>0 ORDER BY strategy, symbol',
        (TRACK_A_NS,),
    )
    fills = ro.query(
        'SELECT strategy, symbol, side, qty, price, filled_at, execution_path, reason FROM paper_fills WHERE namespace=? ORDER BY filled_at, id',
        (TRACK_A_NS,),
    )
    fills_by_day = defaultdict(list)
    for f in fills:
        fills_by_day[str(f['filled_at'])[:10]].append(f)
    pos_by_s = defaultdict(int)
    for p in positions:
        pos_by_s[int(p['strategy'])] += 1
    return {
        'latest_eod_day': latest['day'] if latest else None,
        'latest_eod_as_of': latest['as_of'] if latest else None,
        'ledger_reconciled': bool(json.loads(latest['report_json']).get('ledger_reconciled')) if latest else None,
        'sleeves': {int(k): {'equity_paise': v.get('equity_paise'), 'cash_paise': v.get('cash_paise'),
                             'market_value_paise': v.get('market_value_paise'), 'open_positions': v.get('open_positions')}
                    for k, v in sleeves.items()},
        'open_positions_now': {'total': len(positions), 'by_strategy': dict(sorted(pos_by_s.items())),
                               'rows': positions},
        'fills_by_day': {d: len(v) for d, v in sorted(fills_by_day.items())},
        'fills': fills,
    }


def adaptive_state(session=None):
    dbp = HERE.parent / CFG['adaptive_db']
    if not dbp.exists():
        return {'adaptive_db': 'absent'}
    c = sqlite3.connect(dbp)
    c.row_factory = sqlite3.Row
    tracks = [dict(r) for r in c.execute(
        "SELECT track, strategy, slot_index, spawn_id, status, cash_paise, allocated_capital_paise, starting_equity_net_haircut_paise FROM adaptive_tracks WHERE namespace=? ORDER BY track, strategy, slot_index",
        (NS,),
    )]
    equity = [dict(r) for r in c.execute(
        'SELECT * FROM adaptive_live_track_a_equity WHERE namespace=? ORDER BY trade_date, strategy',
        (NS,),
    )]
    markers = [dict(r) for r in c.execute(
        'SELECT trade_date, reconciled FROM adaptive_eod_markers WHERE namespace=? ORDER BY trade_date',
        (NS,),
    )]
    returns_n = c.execute('SELECT COUNT(*) n FROM adaptive_daily_returns WHERE namespace=?', (NS,)).fetchone()['n']
    archive_days = [r['trade_date'] for r in c.execute('SELECT DISTINCT trade_date FROM bhavcopy_series_archive ORDER BY trade_date')]
    c.close()
    report_path = Path('/workspace/reports/adaptive') / (session or (equity[-1]['trade_date'] if equity else '')) / 'adaptive-daily.json'
    daily = json.loads(report_path.read_text()) if report_path.exists() else None
    return {
        'adaptive_db': str(dbp),
        'tracks': tracks,
        'track_a_replica_count': sum(1 for t in tracks if t['track'] == 'A'),
        'track_b_count': sum(1 for t in tracks if t['track'] == 'B'),
        'copied_equity_rows': equity,
        'eod_markers': markers,
        'daily_returns_rows': returns_n,
        'bhavcopy_archive_days': archive_days,
        'daily_report': {
            'session_day': (daily or {}).get('session_day'),
            'simulation_status': (daily or {}).get('simulation_status'),
            'pit_series_coverage': (daily or {}).get('pit_series_coverage'),
            'track_a_forward_arm': (daily or {}).get('track_a_forward_arm'),
        } if daily else None,
    }


def compare(live, adaptive):
    reasons = []
    sleeve_rows = []
    live_s = live.get('sleeves') or {}
    copied = {int(r['strategy']): r for r in (adaptive.get('copied_equity_rows') or [])}
    for st in sorted(set(live_s) | set(copied)):
        L, A = live_s.get(st), copied.get(st)
        row = {'strategy': st, 'live_eod': L, 'adaptive_copy': ({
            'equity_paise': A.get('equity_paise') if A else None,
            'cash_paise': A.get('cash_paise') if A else None,
            'market_value_paise': A.get('market_value_paise') if A else None,
            'open_positions': A.get('open_positions') if A else None,
            'session_day': A.get('trade_date') if A else None,
        } if A else None)}
        if L and A:
            row['equity_match'] = int(L['equity_paise']) == int(A['equity_paise'])
            row['open_positions_match'] = int(L['open_positions']) == int(A['open_positions'])
            if not row['equity_match'] or not row['open_positions_match']:
                reasons.append(f'S{st} copied EOD equity/positions differ from live paper_eod')
        elif L and not A:
            row['equity_match'] = False
            reasons.append(f'S{st} present on live EOD but no adaptive_live_track_a_equity row')
        sleeve_rows.append(row)

    sim = ((adaptive.get('daily_report') or {}).get('simulation_status'))
    trade_parity = 'not_evaluable'
    if sim == 'BLOCKED_PIT_SERIES_ARCHIVE_P1' or not (adaptive.get('bhavcopy_archive_days')):
        trade_parity = 'not_evaluable'
        reasons.append('Track A replica trades cannot be compared yet: PIT bhavcopy series archive is empty / simulations blocked (P1)')
    elif (adaptive.get('daily_returns_rows') or 0) == 0:
        trade_parity = 'not_evaluable'
        reasons.append('No adaptive_daily_returns rows, so no forward Track A replica marks to compare')
    else:
        trade_parity = 'compared'

    # Live open book after the last EOD may include later sessions (e.g. 11 Nov fills).
    live_now = live.get('open_positions_now') or {}
    eod_open = sum(int((live_s.get(st) or {}).get('open_positions') or 0) for st in live_s)
    if live_now.get('total') != eod_open:
        reasons.append(
            f"Live open positions now ({live_now.get('total')}) differ from last EOD open count ({eod_open}) "
            f"because later sessions have traded; compare trade-level parity only up to the last reconciled EOD"
        )

    return {
        'sleeve_equity_copy': sleeve_rows,
        'sleeve_equity_all_match': all(r.get('equity_match') for r in sleeve_rows if r.get('live_eod') and r.get('adaptive_copy')),
        'trade_parity': trade_parity,
        'reasons': reasons,
        'live_open_now_by_strategy': live_now.get('by_strategy'),
        'last_eod_open_by_strategy': {st: (live_s[st] or {}).get('open_positions') for st in live_s},
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--session', default=None)
    a = ap.parse_args(argv)
    ro = TursoRO()
    live = live_ledger(ro, a.session)
    adaptive = adaptive_state(a.session or live.get('latest_eod_day'))
    cmp = compare(live, adaptive)
    report = {
        'at_ist': datetime.now(IST).isoformat(),
        'namespace_live': TRACK_A_NS,
        'namespace_adaptive': NS,
        'live': {k: live[k] for k in live if k != 'fills'},
        'live_fill_count': len(live.get('fills') or []),
        'adaptive': {k: adaptive[k] for k in adaptive if k != 'tracks'},
        'adaptive_tracks_summary': [
            {'track': t['track'], 'strategy': t['strategy'], 'status': t['status'],
             'cash_paise': t['cash_paise'], 'allocated_capital_paise': t['allocated_capital_paise']}
            for t in (adaptive.get('tracks') or [])
        ],
        'comparison': cmp,
    }
    day = a.session or live.get('latest_eod_day') or datetime.now(IST).date().isoformat()
    out_dir = Path('/workspace/reports/adaptive') / day
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / 'track-a-parity.json'
    path.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps({
        'report': str(path),
        'latest_eod_day': live.get('latest_eod_day'),
        'ledger_reconciled': live.get('ledger_reconciled'),
        'sleeve_equity_all_match': cmp['sleeve_equity_all_match'],
        'trade_parity': cmp['trade_parity'],
        'reasons': cmp['reasons'],
        'track_a_replicas': adaptive.get('track_a_replica_count'),
        'daily_returns_rows': adaptive.get('daily_returns_rows'),
        'archive_days': adaptive.get('bhavcopy_archive_days'),
    }, indent=2, default=str))
    return report


if __name__ == '__main__':
    main()
