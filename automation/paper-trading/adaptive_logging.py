"""Skill-enhanced Comprehensive Logging for the Self-Improvement (Adaptive 3-Tier) System.

Implements:
- Append-only SHA-256 hash-chained decision context and full order lifecycle logs (`adaptive_decision_logs`)
- Tamper-evident `data_hash` on inputs and `prev_hash` -> `entry_hash` chain verification
- Persistent `correlation_id`, `spawn_id`, and `order_id` across Signal -> Order Decision -> Submission -> Fill/Skip/Reject -> Gate Evaluation -> Promotion/Probation/Calibration
- Querying by track (A/B/C), strategy, symbol, date, correlation_id, spawn_id, gate, and named_cause
- Full reconstruction of any past decision, order lifecycle, gate evaluation, or calibration run from the logs alone
"""
import hashlib, json, uuid
from datetime import datetime, timezone

GENESIS = '0' * 64

VALID_TRACKS = frozenset({'A', 'B', 'C', 'SYSTEM'})

VALID_CATEGORIES = frozenset({
    'DECISION_CONTEXT',
    'ORDER_LIFECYCLE',
    'SPAWN',
    'WARM_START',
    'GATE_EVALUATION',
    'TRIAL_COUNT',
    'CALIBRATION',
    'PROMOTION',
    'PROBATION',
    'MICROSTRUCTURE',
    'CORP_ACTION',
    'KILL_SWITCH',
    'SYSTEM',
})

VALID_DECISION_TYPES = frozenset({
    'SIGNAL',
    'ENTRY',
    'EXIT',
    'HOLD',
    'SKIP',
    'SUBMIT',
    'FILL',
    'REJECT',
    'CANCEL',
    'CORRECTION',
    'SPAWN',
    'WARM_START',
    'WINDOW_EXTEND',
    'INCONCLUSIVE',
    'GATE_PASS',
    'GATE_FAIL',
    'PROMOTE',
    'PROMOTE_BLOCKED_OBSERVATION',
    'CALIBRATION_GO',
    'CALIBRATION_NO_GO',
    'PROBATION_START',
    'PROBATION_PASS',
    'PROBATION_FAIL',
    'META_KILL',
    'SYSTEM_OP',
})

SCHEMA = """
CREATE TABLE IF NOT EXISTS adaptive_tracks(namespace TEXT NOT NULL,track TEXT NOT NULL CHECK(track IN('A','B','C')),strategy INTEGER NOT NULL,slot_index INTEGER NOT NULL DEFAULT 1,spawn_id TEXT NOT NULL,status TEXT NOT NULL,params_json TEXT NOT NULL,allocated_capital_paise INTEGER NOT NULL,cash_paise INTEGER NOT NULL,starting_equity_net_haircut_paise INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(namespace,track,strategy,slot_index));
CREATE TABLE IF NOT EXISTS adaptive_spawns(namespace TEXT NOT NULL,spawn_id TEXT NOT NULL,correlation_id TEXT NOT NULL,strategy INTEGER NOT NULL,track TEXT NOT NULL CHECK(track IN('A','B','C')),tier TEXT NOT NULL CHECK(tier IN('FAST_SWING','TREND')),parent_version TEXT NOT NULL,params_json TEXT NOT NULL,candidate_kind TEXT NOT NULL CHECK(candidate_kind IN('BASELINE','CANDIDATE','PLACEBO','PLATEAU_NEIGHBOR','SHADOW','TRACK_C_COMPARISON')),eligible_for_selection INTEGER NOT NULL CHECK(eligible_for_selection IN(0,1)),k_cand INTEGER NOT NULL DEFAULT 1,rho_bar REAL NOT NULL DEFAULT 0.0,k_eff REAL NOT NULL DEFAULT 1.0,effective_critical_value REAL NOT NULL DEFAULT 1.40,warm_start_days INTEGER NOT NULL,warm_start_start_date TEXT,warm_start_end_date TEXT,eval_window_target_days INTEGER NOT NULL,eval_window_start_date TEXT,eval_window_end_date TEXT,extension_days INTEGER NOT NULL DEFAULT 0,extension_reason TEXT,starting_equity_gross_paise INTEGER NOT NULL,exit_haircut_paise INTEGER NOT NULL,starting_equity_net_haircut_paise INTEGER NOT NULL,inherited_positions_count INTEGER NOT NULL DEFAULT 0,inherited_discordant_trades INTEGER NOT NULL DEFAULT 0,new_discordant_trades INTEGER NOT NULL DEFAULT 0,distinct_entry_weeks INTEGER NOT NULL DEFAULT 0,harness_hash TEXT NOT NULL,data_snapshot_hash TEXT NOT NULL,rng_seeds_json TEXT NOT NULL,decision_inputs_json TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN('SPAWNED','WARM_START','EVALUATING','EXTENDED','INCONCLUSIVE','PASSED','FAILED','PROMOTED','OBSERVATION_ONLY','PROBATION','RETIRED')),spawned_at TEXT NOT NULL,decided_at TEXT,PRIMARY KEY(namespace,spawn_id));
CREATE INDEX IF NOT EXISTS adaptive_spawns_strategy_idx ON adaptive_spawns(namespace,strategy,track,status);
CREATE TABLE IF NOT EXISTS adaptive_daily_returns(namespace TEXT NOT NULL,spawn_id TEXT NOT NULL,strategy INTEGER NOT NULL,trade_date TEXT NOT NULL,r_var REAL NOT NULL,r_base REAL NOT NULL,d_t REAL NOT NULL,r_index REAL NOT NULL DEFAULT 0.0,equity_var_paise INTEGER NOT NULL,equity_base_paise INTEGER NOT NULL,cash_var_paise INTEGER NOT NULL,cash_base_paise INTEGER NOT NULL,open_positions_var INTEGER NOT NULL DEFAULT 0,open_positions_base INTEGER NOT NULL DEFAULT 0,recorded_at TEXT NOT NULL,PRIMARY KEY(namespace,spawn_id,trade_date));
CREATE TABLE IF NOT EXISTS adaptive_gate_evaluations(namespace TEXT NOT NULL,eval_id TEXT NOT NULL,spawn_id TEXT NOT NULL,correlation_id TEXT NOT NULL,strategy INTEGER NOT NULL,decision_day TEXT NOT NULL,k_cand INTEGER NOT NULL,rho_bar REAL NOT NULL,k_eff REAL NOT NULL,effective_critical_value REAL NOT NULL,t_hac REAL NOT NULL,d_t_mean REAL NOT NULL,residual_alpha REAL NOT NULL,residual_beta REAL NOT NULL,beta_stable INTEGER NOT NULL DEFAULT 0,gate4_binding INTEGER NOT NULL DEFAULT 0,gates_json TEXT NOT NULL,overall_passed INTEGER NOT NULL CHECK(overall_passed IN(0,1)),status TEXT NOT NULL,observation_only INTEGER NOT NULL DEFAULT 1,data_hash TEXT NOT NULL,recorded_at TEXT NOT NULL,PRIMARY KEY(namespace,eval_id));
CREATE TABLE IF NOT EXISTS adaptive_calibration_runs(namespace TEXT NOT NULL,calibration_id TEXT NOT NULL,correlation_id TEXT NOT NULL,strategy INTEGER NOT NULL,partition_name TEXT NOT NULL,sigma_d_t REAL NOT NULL,mde REAL NOT NULL,injected_edge REAL NOT NULL,measured_power REAL NOT NULL,power_floor REAL NOT NULL,go_no_go TEXT NOT NULL CHECK(go_no_go IN('GO','NO_GO_MONITOR_ONLY','NO_GO_LENGTHEN_WINDOW')),pipeline_mode TEXT NOT NULL,holdout_touched INTEGER NOT NULL DEFAULT 0,vault_spent INTEGER NOT NULL DEFAULT 0,details_json TEXT NOT NULL,data_hash TEXT NOT NULL,recorded_at TEXT NOT NULL,PRIMARY KEY(namespace,calibration_id));
CREATE TABLE IF NOT EXISTS adaptive_holdout_vault(namespace TEXT NOT NULL,vault_id TEXT NOT NULL,touched_count INTEGER NOT NULL DEFAULT 0,vault_spent INTEGER NOT NULL DEFAULT 0,first_touched_at TEXT,calibration_id TEXT,status TEXT NOT NULL,details_json TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(namespace,vault_id));
CREATE TABLE IF NOT EXISTS adaptive_probation(namespace TEXT NOT NULL,probation_id TEXT NOT NULL,spawn_id TEXT NOT NULL,strategy INTEGER NOT NULL,tolerance_band_sigma REAL NOT NULL,historical_tracking_error REAL NOT NULL,tracking_error_limit REAL NOT NULL,days_monitored INTEGER NOT NULL DEFAULT 0,required_days INTEGER NOT NULL,cumulative_active_return REAL NOT NULL DEFAULT 0.0,status TEXT NOT NULL,meta_kill_triggered INTEGER NOT NULL DEFAULT 0,details_json TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(namespace,probation_id));
CREATE TABLE IF NOT EXISTS adaptive_eod_markers(namespace TEXT NOT NULL,trade_date TEXT NOT NULL,reconciled INTEGER NOT NULL CHECK(reconciled IN(0,1)),source_artifact TEXT NOT NULL,source_hash TEXT NOT NULL,recorded_at TEXT NOT NULL,PRIMARY KEY(namespace,trade_date));
CREATE TABLE IF NOT EXISTS adaptive_parity(namespace TEXT NOT NULL,strategy INTEGER NOT NULL,trade_date TEXT NOT NULL,fills_match INTEGER NOT NULL,skips_match INTEGER NOT NULL,qty_match INTEGER NOT NULL,equity_diff_paise INTEGER NOT NULL,parity_ok INTEGER NOT NULL CHECK(parity_ok IN(0,1)),details_json TEXT NOT NULL,recorded_at TEXT NOT NULL,PRIMARY KEY(namespace,strategy,trade_date));
CREATE TABLE IF NOT EXISTS adaptive_decision_logs(namespace TEXT NOT NULL,seq INTEGER NOT NULL,log_id TEXT NOT NULL,correlation_id TEXT NOT NULL,spawn_id TEXT,order_id TEXT,track TEXT NOT NULL CHECK(track IN('A','B','C','SYSTEM')),strategy INTEGER,symbol TEXT,category TEXT NOT NULL,decision_type TEXT NOT NULL,named_cause TEXT,rule_fired TEXT,execution_path TEXT,strategy_version TEXT,conditions_json TEXT NOT NULL,filters_json TEXT NOT NULL,indicator_snapshot_json TEXT NOT NULL,risk_state_json TEXT NOT NULL,execution_details_json TEXT NOT NULL,data_hash TEXT NOT NULL,source_at TEXT NOT NULL,recorded_at TEXT NOT NULL,prev_hash TEXT NOT NULL,entry_hash TEXT NOT NULL,PRIMARY KEY(namespace,seq));
CREATE INDEX IF NOT EXISTS adaptive_logs_corr_idx ON adaptive_decision_logs(namespace,correlation_id,seq);
CREATE INDEX IF NOT EXISTS adaptive_logs_spawn_idx ON adaptive_decision_logs(namespace,spawn_id,seq);
CREATE INDEX IF NOT EXISTS adaptive_logs_query_idx ON adaptive_decision_logs(namespace,track,strategy,symbol,category,named_cause);
"""


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def stable_json(val):
    if isinstance(val, list):
        return '[' + ','.join(stable_json(x) for x in val) + ']'
    if isinstance(val, dict):
        return '{' + ','.join(json.dumps(str(k)) + ':' + stable_json(val[k]) for k in sorted(val.keys())) + '}'
    return json.dumps(None if val is None else val, default=str)


def compute_hash(val):
    raw = val if isinstance(val, str) else stable_json(val)
    return hashlib.sha256(raw.encode('utf8')).hexdigest()


def init(db):
    db.batch([(s.strip(), ()) for s in SCHEMA.split(';') if s.strip()])


def _log_body(row):
    return {
        'namespace': row['namespace'],
        'seq': int(row['seq']),
        'log_id': row['log_id'],
        'correlation_id': row['correlation_id'],
        'spawn_id': row.get('spawn_id'),
        'order_id': row.get('order_id'),
        'track': row['track'],
        'strategy': row.get('strategy'),
        'symbol': row.get('symbol'),
        'category': row['category'],
        'decision_type': row['decision_type'],
        'named_cause': row.get('named_cause'),
        'rule_fired': row.get('rule_fired'),
        'execution_path': row.get('execution_path'),
        'strategy_version': row.get('strategy_version'),
        'conditions': json.loads(row['conditions_json']) if isinstance(row.get('conditions_json'), str) else (row.get('conditions') or {}),
        'filters': json.loads(row['filters_json']) if isinstance(row.get('filters_json'), str) else (row.get('filters') or {}),
        'indicator_snapshot': json.loads(row['indicator_snapshot_json']) if isinstance(row.get('indicator_snapshot_json'), str) else (row.get('indicator_snapshot') or {}),
        'risk_state': json.loads(row['risk_state_json']) if isinstance(row.get('risk_state_json'), str) else (row.get('risk_state') or {}),
        'execution_details': json.loads(row['execution_details_json']) if isinstance(row.get('execution_details_json'), str) else (row.get('execution_details') or {}),
        'data_hash': row['data_hash'],
        'source_at': row['source_at'],
        'recorded_at': row['recorded_at'],
    }


def append_log(
    db,
    namespace,
    correlation_id,
    track,
    category,
    decision_type,
    strategy=None,
    symbol=None,
    spawn_id=None,
    order_id=None,
    named_cause=None,
    rule_fired=None,
    execution_path=None,
    strategy_version=None,
    conditions=None,
    filters=None,
    indicator_snapshot=None,
    risk_state=None,
    execution_details=None,
    input_data=None,
    source_at=None,
    recorded_at=None,
    log_id=None,
):
    """Append a structured, hash-chained entry to adaptive_decision_logs."""
    if track not in VALID_TRACKS:
        raise ValueError(f'invalid_track:{track}')
    if category not in VALID_CATEGORIES:
        raise ValueError(f'invalid_category:{category}')
    if decision_type not in VALID_DECISION_TYPES:
        raise ValueError(f'invalid_decision_type:{decision_type}')
    if not correlation_id:
        raise ValueError('correlation_id_required')

    prev_rows = db.query(
        'SELECT seq,entry_hash FROM adaptive_decision_logs WHERE namespace=? ORDER BY seq DESC LIMIT 1',
        (namespace,),
    )
    prev_hash = str(prev_rows[0]['entry_hash']) if prev_rows else GENESIS
    seq = (int(prev_rows[0]['seq']) + 1) if prev_rows else 1
    lid = str(log_id or uuid.uuid4().hex)
    src_at = str(source_at or _utc_now())
    rec_at = str(recorded_at or _utc_now())

    cond_obj = conditions or {}
    filt_obj = filters or {}
    ind_obj = indicator_snapshot or {}
    risk_obj = risk_state or {}
    exec_obj = execution_details or {}
    dhash = compute_hash(
        input_data
        if input_data is not None
        else {
            'conditions': cond_obj,
            'filters': filt_obj,
            'indicator_snapshot': ind_obj,
            'risk_state': risk_obj,
            'execution_details': exec_obj,
        }
    )

    body = {
        'namespace': namespace,
        'seq': seq,
        'log_id': lid,
        'correlation_id': str(correlation_id),
        'spawn_id': str(spawn_id) if spawn_id is not None else None,
        'order_id': str(order_id) if order_id is not None else None,
        'track': track,
        'strategy': int(strategy) if strategy is not None else None,
        'symbol': str(symbol) if symbol is not None else None,
        'category': category,
        'decision_type': decision_type,
        'named_cause': str(named_cause) if named_cause is not None else None,
        'rule_fired': str(rule_fired) if rule_fired is not None else None,
        'execution_path': str(execution_path) if execution_path is not None else None,
        'strategy_version': str(strategy_version) if strategy_version is not None else None,
        'conditions': cond_obj,
        'filters': filt_obj,
        'indicator_snapshot': ind_obj,
        'risk_state': risk_obj,
        'execution_details': exec_obj,
        'data_hash': dhash,
        'source_at': src_at,
        'recorded_at': rec_at,
    }
    entry_hash = compute_hash(prev_hash + '\n' + stable_json(body))

    db.batch([(
        'INSERT INTO adaptive_decision_logs('
        'namespace,seq,log_id,correlation_id,spawn_id,order_id,track,strategy,symbol,category,decision_type,'
        'named_cause,rule_fired,execution_path,strategy_version,conditions_json,filters_json,indicator_snapshot_json,'
        'risk_state_json,execution_details_json,data_hash,source_at,recorded_at,prev_hash,entry_hash'
        ') VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (
            namespace,
            seq,
            lid,
            body['correlation_id'],
            body['spawn_id'],
            body['order_id'],
            track,
            body['strategy'],
            body['symbol'],
            category,
            decision_type,
            body['named_cause'],
            body['rule_fired'],
            body['execution_path'],
            body['strategy_version'],
            stable_json(cond_obj),
            stable_json(filt_obj),
            stable_json(ind_obj),
            stable_json(risk_obj),
            stable_json(exec_obj),
            dhash,
            src_at,
            rec_at,
            prev_hash,
            entry_hash,
        ),
    )])
    return {
        'seq': seq,
        'log_id': lid,
        'correlation_id': body['correlation_id'],
        'spawn_id': body['spawn_id'],
        'order_id': body['order_id'],
        'data_hash': dhash,
        'prev_hash': prev_hash,
        'entry_hash': entry_hash,
    }


def log_correction(db, namespace, original_log_id, correlation_id, track, reason, corrected_details, source_at=None):
    """Append an immutable correction entry referencing original_log_id without editing prior logs."""
    orig = db.query('SELECT * FROM adaptive_decision_logs WHERE namespace=? AND log_id=?', (namespace, str(original_log_id)))
    if not orig:
        raise KeyError(f'original_log_not_found:{original_log_id}')
    row = orig[0]
    return append_log(
        db,
        namespace=namespace,
        correlation_id=correlation_id or row['correlation_id'],
        track=track or row['track'],
        category=row['category'],
        decision_type='CORRECTION',
        strategy=row['strategy'],
        symbol=row['symbol'],
        spawn_id=row['spawn_id'],
        order_id=row['order_id'],
        named_cause=reason,
        rule_fired='immutable_log_correction',
        strategy_version=row['strategy_version'],
        execution_details={
            'original_log_id': str(original_log_id),
            'original_seq': int(row['seq']),
            'original_entry_hash': str(row['entry_hash']),
            'correction_reason': str(reason),
            'corrected_details': corrected_details,
        },
        source_at=source_at,
    )


def verify_hash_chain(db, namespace):
    """Verify the SHA-256 hash chain across all adaptive_decision_logs in namespace."""
    rows = db.query('SELECT * FROM adaptive_decision_logs WHERE namespace=? ORDER BY seq', (namespace,))
    prev = GENESIS
    breaks = []
    for row in rows:
        seq = int(row['seq'])
        try:
            body = _log_body(row)
            expected = compute_hash(prev + '\n' + stable_json(body))
        except Exception:
            breaks.append(seq)
            prev = str(row.get('entry_hash') or '')
            continue
        if str(row.get('prev_hash')) != prev or str(row.get('entry_hash')) != expected:
            breaks.append(seq)
        prev = str(row['entry_hash'])
    return {
        'ok': len(breaks) == 0,
        'count': len(rows),
        'breaks': breaks,
        'head_hash': prev if rows else GENESIS,
    }


def query_logs(
    db,
    namespace,
    track=None,
    strategy=None,
    symbol=None,
    date_prefix=None,
    correlation_id=None,
    spawn_id=None,
    order_id=None,
    category=None,
    gate=None,
    named_cause=None,
):
    """Query structured logs by track, strategy, symbol, date, correlation_id, spawn_id, gate, or named_cause."""
    clauses = ['namespace=?']
    args = [namespace]
    if track is not None:
        clauses.append('track=?')
        args.append(str(track))
    if strategy is not None:
        clauses.append('strategy=?')
        args.append(int(strategy))
    if symbol is not None:
        clauses.append('symbol=?')
        args.append(str(symbol))
    if date_prefix is not None:
        clauses.append('substr(source_at,1,10)=?')
        args.append(str(date_prefix)[:10])
    if correlation_id is not None:
        clauses.append('correlation_id=?')
        args.append(str(correlation_id))
    if spawn_id is not None:
        clauses.append('spawn_id=?')
        args.append(str(spawn_id))
    if order_id is not None:
        clauses.append('order_id=?')
        args.append(str(order_id))
    if category is not None:
        clauses.append('category=?')
        args.append(str(category))
    if named_cause is not None:
        clauses.append('named_cause=?')
        args.append(str(named_cause))

    sql = f"SELECT * FROM adaptive_decision_logs WHERE {' AND '.join(clauses)} ORDER BY seq"
    rows = db.query(sql, tuple(args))
    parsed = []
    for r in rows:
        item = dict(r)
        item['conditions'] = json.loads(r['conditions_json'])
        item['filters'] = json.loads(r['filters_json'])
        item['indicator_snapshot'] = json.loads(r['indicator_snapshot_json'])
        item['risk_state'] = json.loads(r['risk_state_json'])
        item['execution_details'] = json.loads(r['execution_details_json'])
        if gate is not None:
            gate_key = str(gate)
            gates_map = item['conditions'].get('gates') or item['execution_details'].get('gates') or {}
            if gate_key not in gates_map and item.get('rule_fired') != gate_key:
                continue
        parsed.append(item)
    return parsed


def export_reconstructed_record(db, namespace, correlation_id=None, spawn_id=None):
    """Reconstruct the complete lifecycle and decision context for a correlation_id or spawn_id from logs alone."""
    if not correlation_id and not spawn_id:
        raise ValueError('correlation_id_or_spawn_id_required')
    chain_status = verify_hash_chain(db, namespace)
    logs = query_logs(db, namespace, correlation_id=correlation_id, spawn_id=spawn_id)
    lifecycle_stages = [entry['decision_type'] for entry in logs]
    categories = [entry['category'] for entry in logs]

    spawn_record = None
    gate_evaluations = []
    order_transitions = []
    promotions = []
    calibrations = []
    corrections = []

    for entry in logs:
        cat = entry['category']
        dtype = entry['decision_type']
        if dtype == 'CORRECTION':
            corrections.append(entry)
        elif cat == 'SPAWN':
            spawn_record = {
                'spawn_id': entry['spawn_id'],
                'strategy': entry['strategy'],
                'track': entry['track'],
                'strategy_version': entry['strategy_version'],
                'conditions': entry['conditions'],
                'execution_details': entry['execution_details'],
                'data_hash': entry['data_hash'],
                'source_at': entry['source_at'],
            }
        elif cat in ('DECISION_CONTEXT', 'ORDER_LIFECYCLE', 'MICROSTRUCTURE', 'CORP_ACTION'):
            order_transitions.append({
                'seq': entry['seq'],
                'log_id': entry['log_id'],
                'order_id': entry['order_id'],
                'decision_type': dtype,
                'named_cause': entry['named_cause'],
                'rule_fired': entry['rule_fired'],
                'execution_path': entry['execution_path'],
                'conditions': entry['conditions'],
                'filters': entry['filters'],
                'indicator_snapshot': entry['indicator_snapshot'],
                'risk_state': entry['risk_state'],
                'execution_details': entry['execution_details'],
                'data_hash': entry['data_hash'],
                'source_at': entry['source_at'],
            })
        elif cat == 'GATE_EVALUATION':
            gate_evaluations.append({
                'seq': entry['seq'],
                'decision_type': dtype,
                'named_cause': entry['named_cause'],
                'conditions': entry['conditions'],
                'filters': entry['filters'],
                'execution_details': entry['execution_details'],
                'data_hash': entry['data_hash'],
                'source_at': entry['source_at'],
            })
        elif cat == 'PROMOTION':
            promotions.append(entry)
        elif cat == 'CALIBRATION':
            calibrations.append(entry)

    reconstructable = len(logs) > 0 and chain_status['ok']
    return {
        'namespace': namespace,
        'correlation_id': correlation_id,
        'spawn_id': spawn_id,
        'chain_verified': chain_status['ok'],
        'reconstructable': reconstructable,
        'event_count': len(logs),
        'lifecycle_stages': lifecycle_stages,
        'categories': categories,
        'spawn_record': spawn_record,
        'order_transitions': order_transitions,
        'gate_evaluations': gate_evaluations,
        'promotions': promotions,
        'calibrations': calibrations,
        'corrections': corrections,
        'entries': logs,
    }
