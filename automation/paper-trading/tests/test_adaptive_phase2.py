"""Adaptive Completion Plan v2, Phase 2 (library correctness) regressions. Isolated: in-memory SQLite, no network
(GitHub API is mocked), no live runtime, no broker.

C-05/E11 policy provenance + literal scan, C-02/E5 one liquidation-net equity basis, E6/C-07 CI proof without a
settable flag, C-09/E7 HAC + block-bootstrap calibration with date-enforced partitions and spent-vault lockout,
E8 observation sessions from logs, G-05 harness_hash, C-06/E12 isolation guards, C-11 cumulative K_cand.
"""
import ast, copy, inspect, json, shutil, subprocess, sys, tempfile, unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import adaptive_ci
import adaptive_engine as ae
import adaptive_guard as guard
import adaptive_observation as obs
import adaptive_policy as apol
import adaptive_promotion as ap
import atomic_gates as ag
import calibration as cal
import pit_archive as pit
import trial_counting as tc
from adaptive_fixtures import (calibration_partitions, ci_success_payload, fresh_db, gate8_cache_from_series, make_market, seed_observation_evidence,
                               seed_pit, trading_days)
from test_atomic_gates import _build_trades

AP = apol.POLICY
IST = guard.IST

# ---- C-05 literal scan ------------------------------------------------------------------------------------------
SCANNED_MODULES = guard.ADAPTIVE_ENTRY_MODULES
# Structural constants: not thresholds. Each has a stated reason; anything else in adaptive code must come from policy.
STRUCTURAL = {
    0: 'zero / empty', 1: 'unit, increment, ddof=1', 2: 'pairs, halves, two-sided, ddof', -1: 'last element / sign',
    0.5: 'half (rounding, median split)', 3: 'minimum n for OLS with intercept+slope (n-2 >= 1 dof)',
    100: 'percent and paise-per-rupee unit conversion', 10000: 'basis points unit conversion', 40: 'git SHA-1 hex length',
    64: 'SHA-256 hex length', 1e-9: 'float equality tolerance', 1e-12: 'float equality tolerance', 1e-15: 'float zero guard',
    1e-16: 'float zero guard', 1e6: 'saturated t-stat sentinel for a zero-variance non-zero mean (not a threshold)',
    0.5772156649015329: 'Euler-Mascheroni constant (expected max of null t-stats)',
}


def _literal_violations(module):
    tree = ast.parse((ROOT / f'{module}.py').read_text())
    exempt = set()
    for node in ast.walk(tree):
        # Frozen Track A indicator definitions (parity-tested against strategy_indicators.py), not adaptive numbers.
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'BASELINE_PARAMS' for t in node.targets):
            exempt |= {id(n) for n in ast.walk(node.value)}
        # Strategy-id dispatch `if s == 3:` compares an identifier, not a threshold.
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id in ('s', 'strategy'):
            exempt |= {id(c) for c in node.comparators}
        # Slice bounds are id/hash truncation for display (e.g. [:12]), not thresholds.
        if isinstance(node, ast.Slice):
            exempt |= {id(n) for n in ast.walk(node)}
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float) and id(node) not in exempt:
            v = node.value
            if v not in STRUCTURAL and -v not in STRUCTURAL:
                out.append((module, node.lineno, v))
    return out


class PolicyGovernanceTests(unittest.TestCase):
    def test_every_value_key_has_exactly_one_provenance_and_recount_matches(self):
        prov = AP['provenance']
        labelled = [k for keys in prov.values() for k in keys]
        self.assertEqual(len(labelled), len(set(labelled)))
        self.assertEqual(set(labelled), set(AP) - apol.META_KEYS)
        # Recount (C-05): the v1 audit placeholders, the plan-v2 "New in v2" keys and the two plan examples.
        self.assertEqual(len(prov['placeholder_pending_review']), 23)
        self.assertEqual(len(prov['new_pending_review']), 22)  # plan v2 table said 20; fresh-holdout and window split out
        self.assertEqual(sorted(prov['plan_example']), ['alpha', 'track_c_superiority_bps_per_day'])
        self.assertEqual(sorted(prov['implementer_pending_review']), ['ci_api_timeout_seconds', 'ci_local_truncation_timeout_seconds', 'heavy_work_window_start_ist'])
        pending = apol.pending_review_keys()
        self.assertEqual(sum(len(v) for v in pending.values()), 23 + 22 + 2 + 3)

    def test_plan_v2_proposed_values_are_used_verbatim(self):
        expect = {'backfill_window_start_ist': '18:00', 'backfill_window_end_ist': '07:00', 'backfill_max_requests_per_minute': 20,
                  'archive_depth_months': 36, 'archive_min_coverage': 0.99, 'placebo_jitter_sessions': 5, 'gate8_null_count': 500,
                  'gate8_null_point_max': 0.12, 'gate8_null_ucb_max': 0.15, 'placebo_cache_equivalence_samples': 10,
                  'parity_equity_tolerance_paise': 100, 'parity_min_consecutive_sessions': 10, 'runner_memory_max_mb': 2048,
                  'runner_cpu_quota_pct': 200, 'hac_bootstrap_max_disagreement': 0.25, 'fresh_holdout_min_sessions_fast_swing': 45,
                  'fresh_holdout_min_sessions_trend': 75, 'eod_marker_deadline_ist': '19:00', 'gate9_min_effect_size_daily': 0.0002,
                  'gate2_cost_slippage_hurdle_daily': 0.0001, 'observation_min_sessions': 15, 'gate8_ucb_confidence': 0.95}
        for k, v in expect.items():
            self.assertEqual(AP[k], v, k)
        self.assertEqual(AP['hac_bandwidth_grid'], [0.5, 1.0, 1.5])  # "auto, +/-50%"
        self.assertEqual(AP['plateau_aggregation']['statistic'], 'median')

    def test_loader_rejects_double_label_phantom_label_and_unknown_group(self):
        good = json.loads(apol.POLICY_PATH.read_text())
        for mutate in (lambda p: p['provenance']['plan'].append('gate8_null_count'),
                       lambda p: p['provenance']['plan'].append('no_such_key'),
                       lambda p: p['provenance'].setdefault('made_up', []).append('alpha'),
                       lambda p: p.update(new_unlabelled_threshold=3),
                       lambda p: p.update(gate8_null_point_max=0.2),
                       lambda p: p['plateau_step_sizes'].pop('7'),
                       lambda p: p.update(backfill_window_start_ist='25:00')):
            bad = copy.deepcopy(good)
            mutate(bad)
            with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
                json.dump(bad, f)
            with self.assertRaises(RuntimeError):
                apol.load(f.name)

    def test_literal_scan_adaptive_modules_have_no_unlisted_numbers(self):
        violations = [v for m in SCANNED_MODULES for v in _literal_violations(m)]
        self.assertEqual(violations, [], 'numeric literals outside adaptive_policy.json')

    def test_literal_scan_catches_a_planted_threshold(self):
        with tempfile.TemporaryDirectory() as d:
            src = (ROOT / 'atomic_gates.py').read_text() + '\nPLANTED = 0.0003\n'
            p = Path(d) / 'planted_mod.py'
            p.write_text(src)
            tree = ast.parse(src)
            vals = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == 0.0003]
            self.assertEqual(vals, [0.0003])
            self.assertNotIn(0.0003, STRUCTURAL)

    def test_plateau_steps_cover_every_tunable_parameter(self):
        for s, params in ae.BASELINE_PARAMS.items():
            self.assertEqual(set(AP['plateau_step_sizes'][str(s)]), set(params), s)


class EquityBasisTests(unittest.TestCase):
    POS = [{'symbol': 'AAA-EQ', 'qty': 40, 'mark_price': 1234.5, 'series': 'EQ', 'locked_days': 0, 'adv_paise': 3_000_000_000},
           {'symbol': 'BBB-EQ', 'qty': 12, 'mark_price': 4021.1, 'series': 'BE', 'locked_days': 2, 'adv_paise': 900_000_000}]

    def test_flat_day_returns_zero_in_both_arms(self):
        snap = {'cash_paise': 41_000_000, 'positions': self.POS}
        a1 = ae.remark_ledger_liquidation_net(snap)['equity_paise']
        a2 = ae.remark_ledger_liquidation_net(copy.deepcopy(snap))['equity_paise']
        b1, _ = ae.liquidation_net_equity_paise(41_000_000, copy.deepcopy(self.POS), {p['symbol']: p['mark_price'] for p in self.POS},
                                                {p['symbol']: p['series'] for p in self.POS}, {p['symbol']: p['locked_days'] for p in self.POS},
                                                {p['symbol']: p['adv_paise'] for p in self.POS})
        self.assertEqual(a1, b1)  # Track A re-mark and challenger valuation are the same function/basis
        self.assertEqual(ae.daily_returns_from_equity([a2], a1), [0.0])
        self.assertEqual(ae.daily_returns_from_equity([b1, b1], b1), [0.0, 0.0])
        self.assertLess(a1, snap['cash_paise'] + sum(p['qty'] * round(p['mark_price'] * 100) for p in self.POS))  # haircut applied

    def test_identical_arms_give_zero_d_t_and_mixed_basis_is_refused(self):
        arm = {'basis': 'liquidation_net', 'e0_paise': 50_000_000, 'equity_paise': [50_100_000, 49_900_000, 50_250_000]}
        self.assertEqual(ae.paired_active_returns_same_basis(arm, copy.deepcopy(arm)), [0.0, 0.0, 0.0])
        with self.assertRaises(ValueError):
            ae.paired_active_returns_same_basis(arm, {**arm, 'basis': 'gross_mtm'})

    def test_simulator_uses_one_basis_for_e0_and_every_eod_and_identical_runs_give_zero_d_t(self):
        db, ns = fresh_db(ROOT), 'eq'
        days, df, idx = make_market()
        seed_pit(db, ns, days, sorted(df.symbol.unique()))
        r1 = ae.simulate_track_window(db, ns, 3, {}, df, idx, days[-105:-45], days[-45:])
        r2 = ae.simulate_track_window(db, ns, 3, {}, df, idx, days[-105:-45], days[-45:])
        self.assertEqual(r1['equity_basis'], 'liquidation_net')
        recs = r1['daily_records']
        self.assertTrue(any(r['open_positions'] > 0 for r in recs))
        for r in recs:
            self.assertEqual(r['equity_basis'], 'liquidation_net')
            self.assertEqual(r['equity_paise'], r['gross_equity_paise'] - r['exit_haircut_paise'])
            if r['open_positions'] > 0:
                self.assertGreater(r['exit_haircut_paise'], 0)
        e0 = r1['starting_equity']['starting_equity_net_haircut_paise']
        np.testing.assert_allclose([r['return'] for r in recs], ae.daily_returns_from_equity([r['equity_paise'] for r in recs], e0), rtol=0, atol=0)
        arm = lambda res: {'basis': res['equity_basis'], 'e0_paise': res['starting_equity']['starting_equity_net_haircut_paise'],
                           'equity_paise': [r['equity_paise'] for r in res['daily_records']]}
        self.assertEqual(ae.paired_active_returns_same_basis(arm(r1), arm(r2)), [0.0] * len(recs))


class CIProofTests(unittest.TestCase):
    SHA = 'b' * 40

    def test_public_check_runs_success_and_each_failure_mode_fails_closed(self):
        ok = ci_success_payload(self.SHA)
        with mock.patch.object(adaptive_ci, '_http_get_json', return_value=ok):
            self.assertTrue(adaptive_ci.public_check_runs_status(self.SHA)['passed'])
        bad_cases = []
        failing = copy.deepcopy(ok); failing['check_runs'][0]['conclusion'] = 'failure'; bad_cases.append(failing)
        missing = copy.deepcopy(ok); missing['check_runs'].pop(); bad_cases.append(missing)
        other_sha = copy.deepcopy(ok); other_sha['check_runs'][1]['head_sha'] = 'c' * 40; bad_cases.append(other_sha)
        running = copy.deepcopy(ok); running['check_runs'][2]['status'] = 'in_progress'; bad_cases.append(running)
        bad_cases.append({'unexpected': True})
        for payload in bad_cases:
            with mock.patch.object(adaptive_ci, '_http_get_json', return_value=payload):
                self.assertFalse(adaptive_ci.public_check_runs_status(self.SHA)['passed'], payload)
        with mock.patch.object(adaptive_ci, '_http_get_json', side_effect=TimeoutError()):
            r = adaptive_ci.public_check_runs_status(self.SHA)
        self.assertFalse(r['passed'])
        self.assertIn('unreachable', r['reason'])
        self.assertFalse(adaptive_ci.public_check_runs_status('abc123')['passed'])

    def test_request_is_unauthenticated(self):
        captured = {}

        class Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"check_runs": []}'

        def fake_urlopen(req, timeout):
            captured['headers'] = {k.lower(): v for k, v in req.header_items()}
            captured['url'] = req.full_url
            captured['timeout'] = timeout
            return Resp()
        with mock.patch.object(adaptive_ci, 'urlopen', fake_urlopen):
            adaptive_ci.public_check_runs_status(self.SHA)
        self.assertNotIn('authorization', captured['headers'])
        self.assertIn(f"/repos/{AP['ci_github_repo']}/commits/{self.SHA}/check-runs", captured['url'])
        self.assertEqual(captured['timeout'], AP['ci_api_timeout_seconds'])

    def test_local_truncation_failure_blocks_even_with_green_public_checks(self):
        with mock.patch.object(adaptive_ci, '_http_get_json', return_value=ci_success_payload(self.SHA)), \
                mock.patch.object(adaptive_ci, 'run_local_truncation_suite', return_value={'passed': False}):
            self.assertFalse(adaptive_ci.verify_ci_for_promotion(self.SHA)['passed'])

    def test_local_truncation_suite_really_runs_and_passes(self):
        self.assertTrue(adaptive_ci.run_local_truncation_suite()['passed'])

    def test_no_public_setter_or_stored_flag_for_ci(self):
        for mod in ('adaptive_promotion', 'adaptive_ci', 'calibration', 'atomic_gates', 'adaptive_engine'):
            src = (ROOT / f'{mod}.py').read_text()
            self.assertNotIn("'adaptive_ci_lookahead_passed'", src, mod)
            self.assertNotRegex(src, r'def (record|set|mark)_ci', mod)

    def test_workflow_job_names_match_required_checks(self):
        path = REPO / '.github' / 'workflows' / 'adaptive-ci.yml'
        if not path.exists():  # promotion still fails closed: no workflow => no successful check-runs for the SHA
            self.skipTest('adaptive-ci.yml not yet on this checkout (needs a push with workflow scope)')
        wf = path.read_text()
        names = [l.split(':', 1)[1].strip() for l in wf.splitlines() if l.startswith('    name: ')]
        self.assertEqual(sorted(names), sorted(AP['ci_required_check_names']))
        self.assertNotIn('secrets.', wf)
        self.assertIn('test_adaptive_truncation_ci', wf)


class CalibrationC09Tests(unittest.TestCase):
    def setUp(self):
        self.db, self.ns = fresh_db(ROOT), 'cal9'

    @staticmethod
    def _ar1(rho, n, seed=4, sd=0.001):
        r = np.random.default_rng(seed)
        e, y = r.normal(0, sd, n), np.zeros(n)
        for i in range(n):
            y[i] = (rho * y[i - 1] if i else 0.0) + e[i]
        return y

    def test_hac_sensitivity_grid_and_autocorrelation_raises_sigma_above_iid(self):
        y = self._ar1(0.6, 250)
        h = cal.hac_sigma_with_sensitivity(y)
        self.assertEqual([r['bandwidth_multiplier'] for r in h['bandwidth_sensitivity']], AP['hac_bandwidth_grid'])
        lags = [r['max_lag'] for r in h['bandwidth_sensitivity']]
        self.assertEqual(lags, sorted(lags))
        self.assertLess(lags[0], lags[-1])
        self.assertGreater(h['sigma_hac'], 1.3 * h['sigma_iid'])
        xc = cal.cross_check_sigma(y)
        self.assertTrue(xc['agree'], xc['relative_disagreement'])
        self.assertEqual(xc['sigma_used'], max(xc['sigma_hac'], xc['sigma_bootstrap']))

    def test_hac_bootstrap_disagreement_fails_closed_to_no_go(self):
        d = np.array([0.0002, -0.0001, 0.0003, -0.0002, 0.0001] * 24)  # degenerate: block length == period
        parts, old, _, _ = calibration_partitions(len(d))
        res = cal.run_calibration_go_no_go(self.db, self.ns, 3, d, 0.0005, old, parts, window_days=45)
        self.assertFalse(res['sigma_cross_check_agree'])
        self.assertEqual(res['go_no_go'], 'NO_GO_MONITOR_ONLY')
        self.assertEqual(res['go_no_go_reason'], 'hac_bootstrap_sigma_disagreement_fail_closed')

    def test_power_uses_block_bootstrap_and_is_lower_under_autocorrelation(self):
        iid, ar = self._ar1(0.0, 240, seed=9), self._ar1(0.6, 240, seed=9)
        p_iid = cal.estimate_power_at_injected_edge(iid, 0.0004, 45)
        p_ar = cal.estimate_power_at_injected_edge(ar * (np.std(iid) / np.std(ar)), 0.0004, 45)
        self.assertEqual(p_ar['empirical_method'], 'circular_block_bootstrap_hac_t')
        self.assertLess(p_ar['measured_power'], p_iid['measured_power'])

    def test_partitions_are_date_enforced(self):
        d = np.random.default_rng(1).normal(0, 0.0008, 120)
        parts, old, mid, hold = calibration_partitions(120)
        with self.assertRaises(PermissionError):
            cal.run_calibration_go_no_go(self.db, self.ns, 3, d, 0.0005, old[:-1] + [hold[0]], parts, window_days=45)
        overlap = copy.deepcopy(parts); overlap[1]['start'] = old[-1]
        with self.assertRaises(ValueError):
            cal.validate_partitions(overlap)
        with self.assertRaises(ValueError):
            cal.validate_partitions(list(reversed(parts)))
        with self.assertRaises(ValueError):
            cal.validate_partitions(parts[:1])

    def test_spent_vault_blocks_promotion_until_fresh_holdout_of_required_length_passes(self):
        rng = np.random.default_rng(2)
        parts, old, _, hold = calibration_partitions(120)
        nogo = cal.run_calibration_go_no_go(self.db, self.ns, 3, rng.normal(0, 0.008, 120), 0.00005, old, parts, window_days=45)
        with self.assertRaises(PermissionError):  # a holdout is never touched to rescue a NO-GO
            cal.touch_holdout_vault_once(self.db, self.ns, 'v0', nogo['calibration_id'], True, holdout_dates=hold)
        run1 = cal.run_calibration_go_no_go(self.db, self.ns, 3, rng.normal(0, 0.0008, 120), 0.0005, old, parts, window_days=45)
        cal.touch_holdout_vault_once(self.db, self.ns, 'v1', run1['calibration_id'], False, holdout_dates=hold)
        el = cal.holdout_eligibility(self.db, self.ns, 3)
        self.assertEqual(el, {'eligible': False, 'reason': 'holdout_vault_spent_monitor_only_until_fresh_holdout', 'vault_id': 'v1'})

        run2 = cal.run_calibration_go_no_go(self.db, self.ns, 3, rng.normal(0, 0.0008, 120), 0.0005, old, parts, window_days=45)
        need = AP['fresh_holdout_min_sessions_fast_swing']
        after = trading_days(need + 5, start=date.fromisoformat(hold[-1]) + (date(2000, 1, 2) - date(2000, 1, 1)))
        with self.assertRaises(PermissionError):  # overlaps the spent holdout
            cal.touch_holdout_vault_once(self.db, self.ns, 'v2', run2['calibration_id'], True, holdout_dates=hold[-3:] + after[:need])
        with self.assertRaises(PermissionError):  # too short
            cal.touch_holdout_vault_once(self.db, self.ns, 'v2', run2['calibration_id'], True, holdout_dates=after[:need - 1])
        self.assertFalse(cal.holdout_eligibility(self.db, self.ns, 3)['eligible'])
        cal.touch_holdout_vault_once(self.db, self.ns, 'v2', run2['calibration_id'], True, holdout_dates=after[:need])
        self.assertTrue(cal.holdout_eligibility(self.db, self.ns, 3)['eligible'])

    def test_promotion_lists_spent_vault_as_blocker(self):
        rng = np.random.default_rng(5)
        parts, old, _, hold = calibration_partitions(120)
        run = cal.run_calibration_go_no_go(self.db, self.ns, 3, rng.normal(0, 0.0008, 120), 0.0005, old, parts, window_days=45)
        cal.touch_holdout_vault_once(self.db, self.ns, 'v1', run['calibration_id'], False, holdout_dates=hold)
        sp = ae.spawn_candidate(self.db, self.ns, 3, {'rsi_entry': 36.0})
        pre = ap.check_promotion_prerequisites(self.db, self.ns, 3, sp['spawn_id'], observation_only=False)
        self.assertIn('holdout_vault_spent_monitor_only_until_fresh_holdout', pre['blockers'])
        self.assertIn('ci_lookahead_truncation_test_not_passed', pre['blockers'])
        self.assertEqual(pre['ci']['reason'], 'not_checked_other_blockers_present')


class ObservationFromLogsTests(unittest.TestCase):
    def setUp(self):
        self.db, self.ns = fresh_db(ROOT), 'obs'

    def test_session_count_is_derived_and_cannot_be_passed_in(self):
        self.assertNotIn('sessions_observed', inspect.signature(ap.record_observation_period_completed).parameters)
        n = AP['observation_min_sessions']
        days = trading_days(n, start=date(2025, 9, 1))
        seed_observation_evidence(self.db, self.ns, 3, days)
        obs.log_shadow_session_completed(self.db, self.ns, 3, days[0])  # duplicate log counts once
        st = obs.observation_status(self.db, self.ns, 3)
        self.assertEqual(st['sessions'], n)
        self.assertEqual(st['parity_streak'], n)
        self.assertEqual(st['completed'], n >= AP['parity_min_consecutive_sessions'])
        self.assertEqual(obs.observation_status(self.db, self.ns, 5)['sessions'], 0)  # per strategy

    def test_sessions_without_bhavcopy_reconciled_eod_or_on_holidays_do_not_count(self):
        good = trading_days(3, start=date(2025, 9, 1))
        seed_observation_evidence(self.db, self.ns, 3, good)
        seed_observation_evidence(self.db, self.ns, 3, ['2025-09-08'], bhavcopy=False)
        seed_observation_evidence(self.db, self.ns, 3, ['2025-09-09'], reconciled=False)
        seed_observation_evidence(self.db, self.ns, 3, ['2026-11-10', '2026-11-08'])  # NSE holiday, Muhurat Sunday
        pit.ingest_bhavcopy_series(self.db, [{'symbol': 'AAA-EQ', 'series': 'EQ', 'close': 1.0}], trade_date='2025-09-10', source='FYERS_HISTORY')
        obs.record_eod_marker(self.db, self.ns, '2025-09-10', True, 'a', 'h')
        obs.log_shadow_session_completed(self.db, self.ns, 3, '2025-09-10')  # unofficial source only
        st = obs.count_observation_sessions(self.db, self.ns, 3)
        self.assertEqual(st['session_dates'], good)
        reasons = {e['trade_date']: e['reason'] for e in st['excluded']}
        self.assertEqual(reasons['2025-09-08'], 'no_official_nse_bhavcopy')
        self.assertEqual(reasons['2025-09-09'], 'track_a_eod_not_reconciled')
        self.assertEqual(reasons['2025-09-10'], 'no_official_nse_bhavcopy')
        self.assertEqual(reasons['2026-11-10'], 'not_a_regular_weekday_session')
        self.assertEqual(reasons['2026-11-08'], 'not_a_regular_weekday_session')

    def test_parity_failure_resets_streak_and_blocks_completion(self):
        n = AP['observation_min_sessions']
        days = trading_days(n + 2, start=date(2025, 9, 1))
        seed_observation_evidence(self.db, self.ns, 3, days)
        obs.record_parity(self.db, self.ns, 3, days[-5], True, True, True, AP['parity_equity_tolerance_paise'] + 1)
        st = obs.observation_status(self.db, self.ns, 3)
        self.assertEqual(st['sessions'], n + 2)
        self.assertEqual(st['parity_streak'], 4)
        self.assertFalse(st['completed'])
        self.assertTrue(obs.record_parity(self.db, self.ns, 3, days[-5], True, True, True, -AP['parity_equity_tolerance_paise']))
        self.assertFalse(obs.record_parity(self.db, self.ns, 3, days[-5], True, False, True, 0))

    def test_eod_marker_is_idempotent_and_never_downgraded(self):
        obs.record_eod_marker(self.db, self.ns, '2025-09-01', True, 'a', 'h1')
        r = obs.record_eod_marker(self.db, self.ns, '2025-09-01', False, 'a', 'h2')
        self.assertTrue(r['duplicate'])
        row = self.db.query('SELECT reconciled,source_hash FROM adaptive_eod_markers WHERE namespace=?', (self.ns,))[0]
        self.assertEqual((row['reconciled'], row['source_hash']), (1, 'h1'))
        with self.assertRaises(ValueError):
            obs.record_eod_marker(self.db, self.ns, '2025-09-02', True, 'a', '')


class HarnessAndGuardTests(unittest.TestCase):
    def test_harness_hash_is_g05_definition_over_git_blob_hashes(self):
        files = ae.HARNESS_FILES
        for need in ('adaptive_policy.json', 'freeze_lock.json', AP['harness_dashboard_read_module']):
            self.assertIn(need, files)
        for m in guard.ADAPTIVE_ENTRY_MODULES:
            self.assertIn(f'{m}.py', files)
        blob = subprocess.run(['git', 'hash-object', str(ROOT / 'adaptive_policy.json')], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(dict(ae.harness_manifest())['adaptive_policy.json'], blob)
        h = ae.compute_harness_hash()
        self.assertEqual(ae.verify_harness_hash(h), h)
        with self.assertRaises(RuntimeError):
            ae.verify_harness_hash('0' * 64)
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / 'automation' / 'paper-trading'
            shutil.copytree(ROOT, tmp, ignore=shutil.ignore_patterns('node_modules', 'tests', 'out', 'logs', '__pycache__', '*.sqlite'))
            (Path(d) / 'server').mkdir()
            shutil.copy(REPO / 'server' / 'automation-read.mjs', Path(d) / 'server' / 'automation-read.mjs')
            self.assertEqual(ae.compute_harness_hash(tmp), h)
            p = json.loads((tmp / 'adaptive_policy.json').read_text()); p['placebo_runs'] = 251
            (tmp / 'adaptive_policy.json').write_text(json.dumps(p))
            self.assertNotEqual(ae.compute_harness_hash(tmp), h)

    def test_heavy_work_only_after_close_in_ist(self):
        at = lambda hh, mm: datetime(2026, 11, 12, hh, mm, tzinfo=IST)
        for hh, mm, ok in ((9, 15, False), (12, 0, False), (15, 30, False), (15, 34, False), (15, 35, True), (23, 0, True),
                           (0, 30, True), (6, 59, True), (7, 0, False), (8, 30, False)):
            self.assertEqual(guard.can_run_heavy_work(at(hh, mm)), ok, (hh, mm))
        self.assertTrue(guard.can_run_heavy_work(datetime(2026, 11, 12, 10, 5, tzinfo=timezone.utc)))  # 15:35 IST
        with self.assertRaises(ValueError):
            guard.can_run_heavy_work(datetime(2026, 11, 12, 16, 0))
        self.assertFalse(guard.can_run_backfill(at(16, 0)))
        self.assertTrue(guard.can_run_backfill(at(18, 0)))

    def test_kill_switch_and_runnable_check(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(guard.kill_switch_engaged(d))
            self.assertTrue(guard.assert_runnable(datetime(2026, 11, 12, 16, 0, tzinfo=IST), root=d))
            with self.assertRaises(RuntimeError):
                guard.assert_runnable(datetime(2026, 11, 12, 11, 0, tzinfo=IST), root=d)
            ks = guard.kill_switch_path(d); ks.parent.mkdir(parents=True); ks.write_text('stop')
            with self.assertRaises(RuntimeError) as e:
                guard.assert_runnable(datetime(2026, 11, 12, 16, 0, tzinfo=IST), root=d)
            self.assertIn('kill_switch', str(e.exception))

    def test_env_scrub_drops_credentials_and_pins_tz(self):
        env = guard.scrubbed_env({'PATH': '/usr/bin', 'HOME': '/h', 'GH_TOKEN': 'x', 'FYERS_ACCESS_TOKEN': 'y', 'TURSO_AUTH_TOKEN': 'z',
                                  'AWS_SECRET_ACCESS_KEY': 'k', 'TZ': 'UTC'})
        self.assertEqual(env, {'PATH': '/usr/bin', 'HOME': '/h', 'TZ': 'Asia/Kolkata'})
        self.assertEqual(guard.credential_like_keys(env), [])

    def test_frozen_files_checked_against_freeze_lock(self):
        self.assertTrue(guard.assert_frozen_files())
        with tempfile.TemporaryDirectory() as d:
            for name in ('freeze_lock.json',) + tuple(ae.cap.LOCKED_FILES):
                shutil.copy(ROOT / name, Path(d) / name)
            p = json.loads((Path(d) / 'capital_policy.json').read_text()); p['adv_floor_inr'] = int(p['adv_floor_inr']) + 1
            (Path(d) / 'capital_policy.json').write_text(json.dumps(p))
            with self.assertRaises(RuntimeError):
                guard.assert_frozen_files(root=d)

    def test_systemd_limits_from_policy(self):
        cmd = guard.systemd_run_command(['python3', '-c', 'pass'])
        self.assertIn(f"MemoryMax={AP['runner_memory_max_mb']}M", cmd)
        self.assertIn(f"CPUQuota={AP['runner_cpu_quota_pct']}%", cmd)
        self.assertEqual(cmd[-3:], ['python3', '-c', 'pass'])

    def test_no_broker_order_or_live_engine_module_is_reachable(self):
        for m in guard.ADAPTIVE_ENTRY_MODULES:
            self.assertTrue((ROOT / f'{m}.py').exists(), m)
        res = guard.forbidden_import_violations()
        self.assertEqual(res['violations'], [])
        self.assertIn('capital', res['modules_scanned'])  # transitive local imports are walked
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'adaptive_x.py').write_text('import adaptive_y\n')
            (Path(d) / 'adaptive_y.py').write_text('import importlib\nm = importlib.import_module(NAME)\nfrom engine import run\n')
            v = guard.forbidden_import_violations(('adaptive_x',), root=d)['violations']
            self.assertIn(('adaptive_y', 'engine'), v)
            self.assertIn(('adaptive_y', '<dynamic_import>'), v)


class CumulativeKCandTests(unittest.TestCase):
    def test_gate2_reads_cumulative_k_cand_across_spawn_rounds(self):
        db, ns = fresh_db(ROOT), 'kc'
        ids = [ae.spawn_candidate(db, ns, 3, {'rsi_entry': v})['spawn_id'] for v in (35.0, 36.0, 37.0)]
        db.batch([("UPDATE adaptive_tracks SET status='retired' WHERE namespace=?", (ns,))])  # round 1 over
        ids.append(ae.spawn_candidate(db, ns, 3, {'rsi_entry': 38.0})['spawn_id'])
        ae.spawn_candidate(db, ns, 3, {'rsi_entry': 39.0}, candidate_kind='PLATEAU_NEIGHBOR')
        self.assertEqual(tc.cumulative_k_cand(db, ns, 3)['k_cand'], 4)
        sid = ids[-1]
        ae.evaluate_stopping_and_window(db, ns, sid, days_elapsed=45, new_discordant_trades=12, distinct_entry_weeks=6)
        rng = np.random.default_rng(1)
        r_idx = rng.normal(0.0004, 0.006, 60); r_base = 0.7 * r_idx + rng.normal(0.0001, 0.002, 60); r_var = r_base + 0.0005
        vt, bt = _build_trades(n_new=12, n_inherited=3)
        res = ag.evaluate_all_atomic_gates(db, ns, sid, 3, '2025-08-29', r_var, r_base, r_idx, vt, bt, '2025-05-30', [0.0004],
                                           r_var, k_cand=1, microstructure_checks=dict.fromkeys(ag.MICROSTRUCTURE_LOCKS, True),
                                           gate8_cache=gate8_cache_from_series(r_var, r_base))
        self.assertEqual(res['details']['k_cand_cumulative_from_spawns'], 4)
        self.assertEqual(res['details']['gate_2_and_4']['k_cand'], 4)


if __name__ == '__main__':
    unittest.main()
