"""Adaptive (Track B / Track C) daily post-EOD step. Runs in a scrubbed environment (no credentials, TZ=Asia/Kolkata)
and touches ONLY local SQLite files: reads state/market.sqlite + state/track_a_eod-<day>.json, writes state/adaptive.sqlite
(namespace from adaptive_live_config.json) and /workspace/reports/adaptive/<day>/. Paper-only; no broker, no Turso.

Usage: adaptive_daily.py --session YYYY-MM-DD --eod-json PATH [--report-dir DIR]
"""
import argparse, hashlib, json, sqlite3, sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
LIB = HERE.parent / 'automation' / 'paper-trading'
sys.path.insert(0, str(LIB))
import pandas as pd                    # noqa: E402
import adaptive_engine as ae           # noqa: E402
import adaptive_guard as ag            # noqa: E402
import adaptive_logging as alog        # noqa: E402
import adaptive_observation as aobs    # noqa: E402
import adaptive_policy as apol         # noqa: E402
import pit_archive as pit              # noqa: E402
import prefilter as pf                 # noqa: E402

IST = ZoneInfo('Asia/Kolkata')
CFG = json.loads((HERE / 'adaptive_live_config.json').read_text())
AP = apol.POLICY
NS = CFG['namespace']
ENGINE_CFG = apol.read_json('config.json')
EXCLUDED = set(ENGINE_CFG.get('indicator_excluded_sessions') or {})


class LocalDB:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.c = sqlite3.connect(path); self.c.row_factory = sqlite3.Row
    def batch(self, stm):
        with self.c:
            for sql, args in stm:
                self.c.execute(sql, args)
    def query(self, sql, args=()):
        return [dict(r) for r in self.c.execute(sql, args).fetchall()]


def open_adaptive_db():
    if NS in CFG['protected_namespaces'] or not NS:
        raise RuntimeError('adaptive_namespace_must_not_be_track_a')
    # ADAPTIVE_LIVE_DB lets the offline smoke test point at a throwaway copy; production uses the config path.
    db = LocalDB(__import__('os').environ.get('ADAPTIVE_LIVE_DB') or str(HERE.parent / CFG['adaptive_db']))
    for sql in (LIB / 'schema.sql').read_text().split(';'):
        s = sql.strip()
        # Only the adaptive / PIT-archive tables; never create Track A (paper_*) or support tables here.
        if s.upper().startswith('CREATE') and 'paper_' not in s and 'support_' not in s:
            db.batch([(s.replace('CREATE TABLE ', 'CREATE TABLE IF NOT EXISTS ', 1) if 'IF NOT EXISTS' not in s else s, ())])
    pit.init(db); alog.init(db); aobs.init(db)
    db.batch([('CREATE TABLE IF NOT EXISTS adaptive_live_track_a_equity(namespace TEXT NOT NULL,trade_date TEXT NOT NULL,strategy INTEGER NOT NULL,'
               'equity_paise INTEGER NOT NULL,cash_paise INTEGER NOT NULL,market_value_paise INTEGER NOT NULL,open_positions INTEGER NOT NULL,'
               'basis TEXT NOT NULL,source_hash TEXT NOT NULL,recorded_at TEXT NOT NULL,PRIMARY KEY(namespace,trade_date,strategy))', ())])
    return db


def candidate_params(strategy):
    """Track B candidates (pending review D6): baseline with ONE parameter moved +/- one plateau step, deterministic order."""
    s = int(strategy)
    base = ae.BASELINE_PARAMS[s]
    steps = AP['plateau_step_sizes'][str(s)]
    out = []
    for k in base:
        for sign in (+1, -1):
            v = base[k] + sign * steps[k]
            if isinstance(base[k], int):
                v = int(v)
            else:
                v = round(float(v), 10)
            try:
                p = ae.validate_parameter_only_mutation(s, {k: v})
            except ValueError:
                continue
            if p != base and p not in out:
                out.append(p)
            if len(out) >= AP['max_track_b_challengers']:
                return out
    return out


def market_frames(as_of):
    c = sqlite3.connect(HERE.parent / 'state' / 'market.sqlite')
    stock = pd.read_sql('SELECT symbol,trade_date,open,high,low,close,volume FROM daily_ohlcv WHERE trade_date<=? ORDER BY symbol,trade_date', c, params=(as_of,))
    idx = pd.read_sql('SELECT symbol,trade_date,open,high,low,close,volume FROM index_ohlcv WHERE symbol=? AND trade_date<=? ORDER BY trade_date',
                      c, params=(pf.POLICY['benchmark_db_symbol'], as_of))
    cas = pd.read_sql('SELECT symbol,ex_date,kind,ratio_num,ratio_den,cash_paise,source,recorded_at FROM paper_corporate_actions WHERE ex_date<=?', c, params=(as_of,))
    bands = pd.read_sql('SELECT * FROM paper_price_bands WHERE trade_date<=?', c, params=(as_of,))
    c.close()
    return stock, idx, cas.to_dict('records'), bands


def snapshot_hash(stock, start):
    h = hashlib.sha256()
    part = stock[stock['trade_date'] >= start]
    h.update(pd.util.hash_pandas_object(part, index=False).values.tobytes())
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--session', required=True)
    ap.add_argument('--eod-json', required=True)
    ap.add_argument('--report-dir', default='/workspace/reports/adaptive')
    a = ap.parse_args()
    day = str(a.session)[:10]
    ag.assert_runnable(heavy=True)
    leaked = ag.credential_like_keys(dict(__import__('os').environ))
    if leaked:
        raise RuntimeError('adaptive_env_not_scrubbed')
    db = open_adaptive_db()
    stamp = datetime.now(IST).isoformat()
    eod = json.loads(Path(a.eod_json).read_text())
    report = {'session_day': day, 'namespace': NS, 'started_at_ist': stamp, 'observation_only': bool(ENGINE_CFG.get('adaptive_observation_only', True)),
              'harness_hash': ae.compute_harness_hash()}

    # 1. Reconciled Track A EOD marker (session-truth rule input) and the forward Track A arm per strategy.
    rec = bool(eod.get('ledger_reconciled'))
    src_hash = alog.compute_hash(eod)
    report['eod_marker'] = aobs.record_eod_marker(db, NS, day, rec, f"turso:paper_eod/{eod.get('namespace')}/{day}", src_hash)
    sleeves = ((eod.get('capped_master_ledger') or {}).get('sleeves')) or {}
    db.batch([('INSERT OR REPLACE INTO adaptive_live_track_a_equity VALUES(?,?,?,?,?,?,?,?,?,?)',
               (NS, day, int(k), int(v['equity_paise']), int(v['cash_paise']), int(v['market_value_paise']), int(v['open_positions']),
                'track_a_ledger_marks_gross_not_liquidation_net', src_hash, stamp)) for k, v in sleeves.items()])
    report['track_a_forward_arm'] = {k: {'equity_paise': v['equity_paise'], 'open_positions': v['open_positions']} for k, v in sorted(sleeves.items())}

    # 2. Market inputs (local snapshot only) and the session list.
    stock, idx, cas, bands_df = market_frames(day)
    sessions = [d for d in sorted(idx['trade_date'].astype(str).unique()) if d not in EXCLUDED]
    latest_bar = sessions[-1] if sessions else None
    report['latest_completed_bar_in_snapshot'] = latest_bar
    report['excluded_sessions_skipped'] = sorted(EXCLUDED & set(stock['trade_date'].astype(str).unique()))

    # 3. Spawns: Track A replica arm + up to max_track_b_challengers Track B challengers per strategy (idempotent).
    spawns = {}
    for s in sorted(ae.BASELINE_PARAMS):
        live = db.query("SELECT track,slot_index,spawn_id,params_json,status FROM adaptive_tracks WHERE namespace=? AND strategy=? AND status IN ('active','challenger','champion','probation') ORDER BY track,slot_index", (NS, s))
        have_a = any(r['track'] == 'A' for r in live)
        have_b = [json.loads(r['params_json']) for r in live if r['track'] == 'B']
        ws = ae.default_warm_start_days(s)
        if len(sessions) < ws:
            spawns[s] = {'error': 'not_enough_sessions_for_warm_start'}; continue
        # Warm-start end is fixed at spawn time (forward-only evaluation); later runs reuse the recorded value.
        rec_end = [json.loads(r['decision_inputs_json']).get('warm_start_end') for r in db.query(
            "SELECT decision_inputs_json FROM adaptive_spawns WHERE namespace=? AND strategy=? AND track='A' AND candidate_kind='BASELINE' ORDER BY spawned_at LIMIT 1", (NS, s))]
        ws_end = rec_end[0] if rec_end and rec_end[0] else sessions[-1]
        ws_dates = [d for d in sessions if d <= ws_end][-ws:]
        d_hash = snapshot_hash(stock, ws_dates[0])
        common = dict(warm_start_days=ws, data_snapshot_hash=d_hash, rng_seeds=[AP['base_seed']], spawned_at=stamp)
        if not have_a:
            ae.spawn_candidate(db, NS, s, dict(ae.BASELINE_PARAMS[s]), track='A', candidate_kind='BASELINE',
                               decision_inputs={'role': 'track_a_replica_for_parity', 'warm_start_end': ws_dates[-1]}, **common)
        for p in candidate_params(s):
            if p in have_b or len(have_b) >= AP['max_track_b_challengers']:
                continue
            ae.spawn_candidate(db, NS, s, p, track='B', candidate_kind='CANDIDATE',
                               decision_inputs={'rule': CFG['track_b_candidates']['rule'], 'status': CFG['track_b_candidates']['status'], 'warm_start_end': ws_dates[-1]}, **common)
            have_b.append(p)
        rows = db.query("SELECT track,slot_index,spawn_id,params_json,cash_paise,allocated_capital_paise,starting_equity_net_haircut_paise FROM adaptive_tracks WHERE namespace=? AND strategy=? AND status IN ('active','challenger','champion','probation') ORDER BY track,slot_index", (NS, s))
        spawns[s] = {'warm_start_days': ws, 'warm_start': [ws_dates[0], ws_dates[-1]],
                     'track_a_replica': [r['spawn_id'] for r in rows if r['track'] == 'A'],
                     'track_b_challengers': [{'spawn_id': r['spawn_id'], 'params_changed': {k: v for k, v in json.loads(r['params_json']).items() if ae.BASELINE_PARAMS[s][k] != v}} for r in rows if r['track'] == 'B'],
                     'track_c_champions': [r['spawn_id'] for r in rows if r['track'] == 'C'],
                     'ledger': [{'track': r['track'], 'slot': r['slot_index'], 'cash_paise': r['cash_paise'], 'allocated_paise': r['allocated_capital_paise'],
                                 'reconciled': int(r['cash_paise']) == int(r['allocated_capital_paise']) == int(r['starting_equity_net_haircut_paise'])} for r in rows]}
    report['spawns'] = spawns
    report['track_c_note'] = 'No Track C champion can exist: promotion is locked (observation-only, calibration P7 and CI proof not done).'

    # 4. PIT Bhavcopy SERIES coverage gate (P1). Without it every new entry is refused (EQ-only, never assumed).
    need_from = min(v['warm_start'][0] for v in spawns.values() if 'warm_start' in v)
    need_dates = [d for d in sessions if d >= need_from]
    covered = {r['trade_date'] for r in db.query('SELECT DISTINCT trade_date FROM bhavcopy_series_archive WHERE trade_date>=? AND source LIKE ?', (need_from, aobs.OFFICIAL_BHAVCOPY_PREFIX + '%'))}
    cov = len(covered & set(need_dates)) / float(len(need_dates)) if need_dates else 0.0
    report['pit_series_coverage'] = {'needed_sessions': len(need_dates), 'covered_sessions': len(covered & set(need_dates)), 'coverage': cov,
                                     'required': AP['archive_min_coverage'], 'from': need_from, 'to': latest_bar}

    sims = {}
    if cov < AP['archive_min_coverage']:
        report['simulation_status'] = 'BLOCKED_PIT_SERIES_ARCHIVE_P1'
    else:
        # 5. Forward-only evaluation: warm-start ends at the spawn's warm_start_end; eval = sessions after it, up to latest_bar.
        closes = idx.set_index(idx['trade_date'].astype(str))['close'].astype(float)
        r_idx = (closes / closes.shift(1) - 1.0).to_dict()   # benchmark daily return for Gate 4 / residualized d_t
        bands = {(str(r['trade_date']), str(r['symbol'])): {'series': r.get('series'), 'circuit': r.get('circuit')} for r in bands_df.to_dict('records')}
        for s, info in spawns.items():
            if 'warm_start' not in info:
                continue
            ws_all = [d for d in sessions if d <= info['warm_start'][1]][-info['warm_start_days']:]
            ev = [d for d in sessions if d > info['warm_start'][1]]
            if not ev:
                sims[s] = {'status': 'WARM_START_ONLY_NO_FORWARD_SESSION_YET'}; continue
            base = ae.simulate_track_window(db, NS, s, ae.BASELINE_PARAMS[s], stock, idx, ws_all, ev, corp_actions=cas, bands_by_date_symbol=bands)
            out = []
            for ch in info['track_b_challengers']:
                params = {**ae.BASELINE_PARAMS[s], **ch['params_changed']}
                var = ae.simulate_track_window(db, NS, s, params, stock, idx, ws_all, ev, corp_actions=cas, bands_by_date_symbol=bands)
                rows = []
                for rv, rb in zip(var['daily_records'], base['daily_records']):
                    rows.append(('INSERT OR REPLACE INTO adaptive_daily_returns(namespace,spawn_id,strategy,trade_date,r_var,r_base,d_t,r_index,equity_var_paise,equity_base_paise,cash_var_paise,cash_base_paise,open_positions_var,open_positions_base,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                 (NS, ch['spawn_id'], s, rv['trade_date'], rv['return'], rb['return'], rv['return'] - rb['return'], float(r_idx.get(rv['trade_date']) or 0.0), rv['equity_paise'], rb['equity_paise'], rv['cash_paise'], rb['cash_paise'], rv['open_positions'], rb['open_positions'], stamp)))
                db.batch(rows)
                out.append({'spawn_id': ch['spawn_id'], 'eval_days': len(ev), 'closed_trades': len(var['closed_trades']), 'open_positions': len(var['open_positions']),
                            'skips': dict(Counter(x['cause'] for x in var['entry_skips']))})
            sims[s] = {'status': 'SIMULATED', 'baseline_replica_closed': len(base['closed_trades']), 'challengers': out}
            aobs.log_shadow_session_completed(db, NS, s, day, {'eval_days': len(ev)})
        report['simulation_status'] = 'SIMULATED'
    report['simulations'] = sims
    report['hash_chain'] = alog.verify_hash_chain(db, NS)
    report['decision_log_rows'] = db.query('SELECT COUNT(*) n FROM adaptive_decision_logs WHERE namespace=?', (NS,))[0]['n']
    report['observation'] = {s: {k: v for k, v in aobs.observation_status(db, NS, s).items() if k in ('sessions', 'parity_streak', 'completed', 'min_sessions')} for s in sorted(ae.BASELINE_PARAMS)}
    report['finished_at_ist'] = datetime.now(IST).isoformat()
    out_dir = Path(a.report_dir) / day; out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'adaptive-daily.json').write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, default=str))


if __name__ == '__main__':
    main()
