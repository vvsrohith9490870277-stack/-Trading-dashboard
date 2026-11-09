"""Observation-Only Mode and Promotion Gate Lock for the Self-Improvement (Adaptive) System.

Implements Section 5, Section 7 (items 8 & 10), A6, A8.8, A8.10, and B4:
- Enforces observation-only mode by default (`adaptive_observation_only: true` in config.json).
- Blocks automatic promotion of Track B challengers to Track C unless:
  1. Observation-only period is completed (derived from logs + Bhavcopy + reconciled EOD + parity streak) AND
     observation_only is False
  2. Calibration harness passed (`go_no_go == 'GO'`, measured_power >= floor, HAC/bootstrap agree) AND the latest
     single-touch holdout vault passed (a spent vault => monitor-only until a fresh holdout passes)
  3. CI proven for the exact SHA (public check-runs, no token) plus a local truncation re-run; fail closed
  4. Meta-kill switch is not active for the strategy
  5. Candidate passed all 9 Atomic Gates vs Track A and incremental superiority vs Track C
- When promoted, retires any prior Track C champion so Track C holds at most 1 champion per strategy.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import adaptive_ci
import adaptive_engine as ae
import adaptive_logging as alog
import adaptive_observation as obs
import adaptive_policy as apol
import calibration as cal

CFG_PATH = Path(__file__).resolve().parent / 'config.json'


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def default_observation_only():
    try:
        cfg = json.loads(CFG_PATH.read_text(encoding='utf8'))
        return bool(cfg.get('adaptive_observation_only', True))
    except Exception:
        return True


def record_observation_period_completed(db, namespace, strategy, recorded_at=None):
    """Log the observation status for a strategy. The session count is DERIVED from the decision logs, official
    Bhavcopy and reconciled-EOD markers (E8); callers cannot supply it. Promotion re-derives it and never reads
    this log entry as a flag."""
    s = int(strategy)
    st = obs.observation_status(db, namespace, s)
    stamp = recorded_at or _utc_now()
    alog.append_log(
        db, namespace, correlation_id=f'obs_period_s{s}', track='SYSTEM', strategy=s,
        category='PROMOTION', decision_type='SYSTEM_OP',
        named_cause='observation_period_completed' if st['completed'] else 'observation_period_incomplete',
        rule_fired='observation_sessions_from_logs_bhavcopy_and_reconciled_eod_plus_parity_streak',
        conditions={k: st[k] for k in ('sessions', 'min_sessions', 'parity_streak', 'min_parity_streak', 'completed')},
        execution_details={'session_dates': st['session_dates'], 'excluded': st['excluded']},
        source_at=stamp,
    )
    return st


def check_promotion_prerequisites(db, namespace, strategy, spawn_id, observation_only=None, ci_sha=None):
    """Verify all mandatory prerequisites before allowing a Track B challenger to promote to Track C.

    Every prerequisite is derived at call time from evidence; nothing is read from a settable flag:
    observation sessions from logs (E8), CI from public check-runs + local truncation re-run (E6), calibration GO
    and an unspent, passed single-touch holdout vault (E7/C-09).
    """
    s = int(strategy)
    obs_flag = default_observation_only() if observation_only is None else bool(observation_only)
    obs_state = obs.observation_status(db, namespace, s)
    obs_completed = bool(obs_state['completed'])

    mk_rows = db.query('SELECT value FROM paper_meta WHERE namespace=? AND key=?', (namespace, f'adaptive_meta_kill_strategy_{s}'))
    meta_kill_active = bool(mk_rows and mk_rows[0]['value'] == '1')

    cal_rows = db.query(
        'SELECT calibration_id,go_no_go,measured_power,power_floor,vault_spent FROM adaptive_calibration_runs WHERE namespace=? AND strategy=? ORDER BY recorded_at DESC LIMIT 1',
        (namespace, s),
    )
    calibration_passed = bool(
        cal_rows
        and cal_rows[0]['go_no_go'] == 'GO'
        and float(cal_rows[0]['measured_power']) >= float(cal_rows[0]['power_floor']) - 1e-12
    )
    holdout = cal.holdout_eligibility(db, namespace, s)

    gate_rows = db.query(
        'SELECT overall_passed,gates_json FROM adaptive_gate_evaluations WHERE namespace=? AND spawn_id=? ORDER BY recorded_at DESC LIMIT 1',
        (namespace, str(spawn_id)),
    )
    gates_passed = bool(gate_rows and int(gate_rows[0]['overall_passed']) == 1)

    sp_rows = db.query('SELECT track,candidate_kind,status FROM adaptive_spawns WHERE namespace=? AND spawn_id=?', (namespace, str(spawn_id)))
    is_track_b_candidate = bool(sp_rows and sp_rows[0]['track'] == 'B' and sp_rows[0]['candidate_kind'] == 'CANDIDATE'
                                and sp_rows[0]['status'] in ('OBSERVATION_ONLY', 'PASSED'))

    live_champion = db.query(
        "SELECT spawn_id FROM adaptive_tracks WHERE namespace=? AND track='C' AND strategy=? AND status IN ('champion','probation') AND spawn_id<>?",
        (namespace, s, str(spawn_id)),
    )
    track_c_superiority_ok = True
    if live_champion:
        details = json.loads(gate_rows[0]['gates_json']) if gate_rows else {}
        cmp_ = details.get('track_c_comparison') or {}
        track_c_superiority_ok = bool(cmp_.get('passed')) and bool(details.get('track_c_champion_present'))

    # CI is checked last and only when everything else already passes (it costs a network read and a local re-run).
    pre_blockers = []
    if obs_flag:
        pre_blockers.append('observation_only_mode_enabled')
    if not obs_completed:
        pre_blockers.append('observation_only_period_not_completed')
    if not calibration_passed:
        pre_blockers.append('calibration_go_power_floor_not_passed')
    if not holdout['eligible']:
        pre_blockers.append(holdout['reason'])
    if meta_kill_active:
        pre_blockers.append('meta_kill_switch_active')
    if not gates_passed:
        pre_blockers.append('atomic_gates_not_passed')
    if not is_track_b_candidate:
        pre_blockers.append('not_a_gate_passed_track_b_candidate')
    if not track_c_superiority_ok:
        pre_blockers.append('incremental_superiority_vs_current_track_c_not_tested')

    if pre_blockers:
        ci = {'passed': False, 'reason': 'not_checked_other_blockers_present'}
    else:
        ci = adaptive_ci.verify_ci_for_promotion(sha=ci_sha)
    blockers = list(pre_blockers)
    if not ci['passed']:
        blockers.append('ci_lookahead_truncation_test_not_passed')

    return {
        'can_promote': len(blockers) == 0,
        'observation_only': obs_flag,
        'observation_period_completed': obs_completed,
        'observation_sessions': obs_state['sessions'],
        'parity_streak': obs_state['parity_streak'],
        'calibration_passed': calibration_passed,
        'holdout': holdout,
        'ci_lookahead_passed': bool(ci['passed']),
        'ci': {k: ci.get(k) for k in ('passed', 'reason', 'sha')},
        'meta_kill_active': meta_kill_active,
        'gates_passed': gates_passed,
        'track_c_superiority_ok': track_c_superiority_ok,
        'blockers': blockers,
    }


def attempt_promotion(db, namespace, spawn_id, observation_only=None, promoted_at=None, ci_sha=None):
    """Attempt to promote a Track B challenger (`spawn_id`) to Track C champion."""
    sp_rows = db.query('SELECT * FROM adaptive_spawns WHERE namespace=? AND spawn_id=?', (namespace, str(spawn_id)))
    if not sp_rows:
        raise KeyError(f'spawn_not_found:{spawn_id}')
    sp = sp_rows[0]
    s = int(sp['strategy'])
    prereq = check_promotion_prerequisites(db, namespace, s, spawn_id, observation_only=observation_only, ci_sha=ci_sha)
    stamp = promoted_at or _utc_now()

    if not prereq['can_promote']:
        cause = prereq['blockers'][0]
        dtype = 'PROMOTE_BLOCKED_OBSERVATION' if 'observation_only' in cause else 'HOLD'
        new_status = 'OBSERVATION_ONLY' if prereq['gates_passed'] and ('observation_only' in cause) else sp['status']
        db.batch([(
            'UPDATE adaptive_spawns SET status=? WHERE namespace=? AND spawn_id=?',
            (new_status, namespace, str(spawn_id)),
        )])
        alog.append_log(
            db, namespace, correlation_id=sp['correlation_id'], spawn_id=str(spawn_id),
            track='B', strategy=s,
            category='PROMOTION',
            decision_type=dtype,
            named_cause=cause,
            rule_fired='promotion_lock_requires_calibration_lookahead_and_observation_completion',
            conditions=prereq,
            execution_details={'promoted': False, 'status': new_status},
            source_at=stamp,
        )
        return {
            'promoted': False,
            'spawn_id': str(spawn_id),
            'strategy': s,
            'status': new_status,
            'prerequisites': prereq,
        }

    # Retire any prior Track C champion for this strategy (max 1 champion per strategy)
    db.batch([
        (
            "UPDATE adaptive_tracks SET status='retired',updated_at=? WHERE namespace=? AND track='C' AND strategy=?",
            (stamp, namespace, s),
        ),
        (
            "UPDATE adaptive_tracks SET status='retired',updated_at=? WHERE namespace=? AND track='B' AND spawn_id=?",
            (stamp, namespace, str(spawn_id)),
        ),
        (
            "INSERT OR REPLACE INTO adaptive_tracks("
            "namespace,track,strategy,slot_index,spawn_id,status,params_json,allocated_capital_paise,cash_paise,"
            "starting_equity_net_haircut_paise,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                namespace, 'C', s, 1, str(spawn_id), 'champion',
                sp['params_json'], ae.SLEEVE_CAPITAL_PAISE,
                ae.SLEEVE_CAPITAL_PAISE, sp['starting_equity_net_haircut_paise'],
                stamp, stamp,
            ),
        ),
        (
            "UPDATE adaptive_spawns SET track='C',status='PROMOTED',decided_at=? WHERE namespace=? AND spawn_id=?",
            (stamp, namespace, str(spawn_id)),
        ),
    ])

    alog.append_log(
        db, namespace, correlation_id=sp['correlation_id'], spawn_id=str(spawn_id),
        track='C', strategy=s,
        category='PROMOTION',
        decision_type='PROMOTE',
        named_cause='challenger_promoted_to_track_c_champion',
        rule_fired='all_gates_calibration_ci_lookahead_and_observation_passed',
        conditions=prereq,
        execution_details={
            'promoted': True,
            'track': 'C',
            'strategy': s,
            'params': json.loads(sp['params_json']),
        },
        source_at=stamp,
    )
    return {
        'promoted': True,
        'spawn_id': str(spawn_id),
        'strategy': s,
        'track': 'C',
        'status': 'PROMOTED',
        'prerequisites': prereq,
    }
