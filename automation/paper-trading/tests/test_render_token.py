"""Task 3 render token/auth-circuit/budget tests (offline, local SQLite store)."""
import base64
import hashlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fyers_auth as fa
import render_state as rs
import token_store as ts
from token_store import TokenStore

CREDS = {
    'FYERS_CLIENT_ID': 'CLIENT1',
    'FYERS_APP_ID': 'APPID-100',
    'FYERS_SECRET_ID': 'SECRET1',
    'FYERS_PIN': '1234',
    'FYERS_TOTP_SECRET': 'JBSWY3DPEHPK3PXP',
}


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeFyersSession:
    """Records every login call; emulates the 5-step Fyers flow."""

    def __init__(self, fail_step=None):
        self.calls = []
        self.headers = {}
        self.fail_step = fail_step

    def post(self, url, json=None, timeout=None, headers=None, allow_redirects=True):
        self.calls.append({'url': url, 'json': json})
        step = url.rsplit('/', 1)[-1]
        if self.fail_step == step:
            return FakeResp({'s': 'error', 'code': -1, 'message': 'boom'}, 400)
        if step == 'send_login_otp_v2':
            return FakeResp({'request_key': 'rk1', 's': 'ok'})
        if step == 'verify_otp':
            return FakeResp({'request_key': 'rk2', 's': 'ok'})
        if step == 'verify_pin_v2':
            return FakeResp({'data': {'access_token': 'sess1'}, 's': 'ok'})
        if step == 'token':
            return FakeResp({'Url': 'https://trade.fyers.in/api-login/redirect-uri/index.html?auth_code=AC1', 's': 'ok'})
        if step == 'validate-authcode':
            return FakeResp({'access_token': 'AT-NEW', 'refresh_token': 'RT-NEW', 's': 'ok'})
        raise AssertionError('unexpected url ' + url)


def make_store(tmpdir=None):
    tmp = Path(tempfile.mkdtemp(dir=tmpdir)) / 'store.sqlite'
    store = rs.SqliteStore(str(tmp))
    rs.migrate(store)
    return store


def fixed_time(monkey_time=1000000.0):
    # 1000000 % 30 == 10 -> 20 s remain in the TOTP window, no sleep.
    return mock.patch.object(fa.time, 'time', return_value=monkey_time)


class RefreshFlowTests(unittest.TestCase):
    def test_refresh_flow_matches_legacy_requests(self):
        sess = FakeFyersSession()
        with fixed_time():
            out = fa.refresh_token(CREDS, session=sess)
        self.assertEqual(out, {'access_token': 'AT-NEW', 'refresh_token': 'RT-NEW'})
        urls = [c['url'] for c in sess.calls]
        # Only the first OTP URL is ever tried.
        self.assertEqual([u for u in urls if 'send_login_otp' in u],
                         ['https://api-t2.fyers.in/vagator/v2/send_login_otp_v2'])
        self.assertNotIn('https://api-t2.fyers.in/vagator/v2/send_login_otp_v3', urls)
        # Only the base64 PIN variant is tried.
        pin_calls = [c for c in sess.calls if c['url'].endswith('verify_pin_v2')]
        self.assertEqual(len(pin_calls), 1)
        self.assertEqual(pin_calls[0]['json']['identifier'],
                         base64.b64encode(b'1234').decode())
        self.assertEqual(pin_calls[0]['json']['identity_type'], 'pin')
        # OTP step used the TOTP for the configured secret.
        otp_calls = [c for c in sess.calls if c['url'].endswith('verify_otp')]
        self.assertEqual(len(otp_calls), 1)
        self.assertEqual(otp_calls[0]['json']['request_key'], 'rk1')
        # Token step used the session token as Bearer and the app id hash.
        token_calls = [c for c in sess.calls if c['url'].endswith('/token')]
        self.assertEqual(len(token_calls), 1)
        validate_calls = [c for c in sess.calls if c['url'].endswith('validate-authcode')]
        self.assertEqual(len(validate_calls), 1)
        expect_hash = hashlib.sha256(b'APPID-100:SECRET1').hexdigest()
        self.assertEqual(validate_calls[0]['json']['appIdHash'], expect_hash)
        self.assertEqual(validate_calls[0]['json']['code'], 'AC1')

    def test_refresh_failure_classified_no_secret(self):
        sess = FakeFyersSession(fail_step='verify_pin_v2')
        with fixed_time():
            with self.assertRaises(fa.FyersAuthError) as cm:
                fa.refresh_token(CREDS, session=sess)
        self.assertEqual(str(cm.exception), 'verify_pin_failed')
        self.assertNotIn('1234', str(cm.exception))


class TokenStoreTests(unittest.TestCase):
    def test_token_roundtrip_encrypted(self):
        store = make_store()
        key = ts.generate_key()
        toks = TokenStore(store, enc_key=key)
        toks.put_token({'access_token': 'AT-SECRET-XYZ', 'refresh_token': 'RT-SECRET'})
        raw = toks._kv_get(ts.TOKEN_KEY)
        self.assertNotIn('AT-SECRET-XYZ', raw)
        self.assertNotIn('RT-SECRET', raw)
        got = toks.get_token()
        self.assertEqual(got['access_token'], 'AT-SECRET-XYZ')
        self.assertEqual(got['refresh_token'], 'RT-SECRET')
        self.assertIn('issued_at', got)

    def test_wrong_key_fails_closed(self):
        store = make_store()
        TokenStore(store, enc_key=ts.generate_key()).put_token({'access_token': 'AT1'})
        other = TokenStore(store, enc_key=ts.generate_key())
        self.assertIsNone(other.get_token())

    def test_circuit_survives_restart(self):
        store = make_store()
        TokenStore(store).put_circuit(True, '2026-11-19T10:00:00+05:30')
        again = TokenStore(rs.SqliteStore(store.path))
        self.assertEqual(again.get_circuit(),
                         {'open': True, 'at': '2026-11-19T10:00:00+05:30'})

    def test_put_token_without_key_fails(self):
        store = make_store()
        with self.assertRaises(ts.TokenStoreError):
            TokenStore(store, enc_key=None, env={}).put_token({'access_token': 'AT1'})


class EngineIntegrationTests(unittest.TestCase):
    def _engine_with_store(self, token=None, enc_key=None, env_extra=None):
        import engine as e
        store = make_store()
        key = enc_key or ts.generate_key()
        if token is not None:
            TokenStore(store, enc_key=key).put_token(token)
        env = {'PAPER_RUNTIME': 'render', 'PAPER_TEST_DATABASE': store.path,
               'TOKEN_ENC_KEY': key, 'FYERS_APP_ID': 'APPID-100'}
        if env_extra:
            env.update(env_extra)
        return e, store, key, env

    def test_cold_start_uses_stored_token_no_refresh(self):
        import engine as e
        _, store, key, env = self._engine_with_store(
            token={'access_token': 'AT-STORED', 'refresh_token': 'RT-STORED'})
        with mock.patch.dict(os.environ, env, clear=False):
            got_env = e.load_env()
            self.assertEqual(got_env.get('FYERS_ACCESS_TOKEN'), 'AT-STORED')
            md = e.MarketData()
            self.assertEqual(md.env.get('FYERS_ACCESS_TOKEN'), 'AT-STORED')
            with mock.patch.object(e.MarketData, 'refresh') as refresh_spy:
                with mock.patch.object(e.requests, 'get') as get_spy:
                    get_spy.return_value = FakeResp({'s': 'ok', 'd': []})
                    out = md.get('quotes', {'symbols': 'NSE:RELIANCE-EQ'})
                self.assertEqual(out, {'s': 'ok', 'd': []})
            refresh_spy.assert_not_called()

    def test_single_refresh_then_circuit_open(self):
        import engine as e
        _, store, key, env = self._engine_with_store(token=None, env_extra={
            'FYERS_CLIENT_ID': 'CLIENT1', 'FYERS_SECRET_ID': 'SECRET1',
            'FYERS_PIN': '1234', 'FYERS_TOTP_SECRET': 'JBSWY3DPEHPK3PXP',
            'FYERS_AUTO_LOGIN': '1'})
        sess = FakeFyersSession()
        with mock.patch.dict(os.environ, env, clear=False):
            md = e.MarketData()
            with mock.patch.object(fa, 'refresh_token', return_value={'access_token': 'AT-NEW'}) as rt_spy:
                with mock.patch.object(fa.requests, 'Session', return_value=sess):
                    md.refresh()
                rt_spy.assert_called_once()
            self.assertEqual(md.env.get('FYERS_ACCESS_TOKEN'), 'AT-NEW')
            # Second refresh in the same process: single-refresh rule.
            with self.assertRaises(RuntimeError) as cm:
                md.refresh()
            self.assertEqual(str(cm.exception), 'auth_failed_after_single_refresh')
            # Circuit is open in kv now.
            toks = TokenStore(store, enc_key=key)
            self.assertTrue(toks.get_circuit()['open'])

    def test_login_not_allowed_in_trial_no_refresh(self):
        import engine as e
        _, store, key, env = self._engine_with_store(token=None, env_extra={
            'RENDER_MODE': 'trial', 'PAPER_NAMESPACE_OVERRIDE': 'paper_render_trial_v1',
            'FYERS_CLIENT_ID': 'CLIENT1', 'FYERS_SECRET_ID': 'SECRET1',
            'FYERS_PIN': 'CANARY_PIN', 'FYERS_TOTP_SECRET': 'CANARY_TOTP',
            'FYERS_AUTO_LOGIN': '1'})
        with mock.patch.dict(os.environ, env, clear=False):
            md = e.MarketData()
            with mock.patch.object(fa, 'refresh_token') as rt_spy:
                with self.assertRaises(RuntimeError) as cm:
                    md.refresh()
                self.assertEqual(str(cm.exception), 'token_unavailable_no_login')
            rt_spy.assert_not_called()
            # No circuit change on the no-login path.
            self.assertFalse(TokenStore(store, enc_key=key).get_circuit()['open'])


class BudgetStoreTests(unittest.TestCase):
    def _budget(self, trial=False, clock=None, store=None):
        import engine as e
        store = store or make_store()
        return e.RequestBudget(token_store=TokenStore(store), trial=trial, clock=clock), store

    def test_budget_persists_across_restart_and_adds_margin(self):
        import engine as e
        store = make_store()
        clock_val = [datetime(2026, 11, 19, 10, 0, tzinfo=e.IST)]

        def clock():
            return clock_val[0]

        b1, _ = self._budget(clock=clock, store=store)
        for _ in range(5):
            b1.record('quotes')
        b1.flush()
        # Simulate a cold start 60 s later: stored state older than 10 s.
        clock_val[0] = datetime(2026, 11, 19, 10, 1, tzinfo=e.IST)
        b2, _ = self._budget(clock=clock, store=store)
        snap = b2.snapshot()
        self.assertEqual(snap['requests'], 25)  # 5 + restart_safety_requests 20
        self.assertEqual(snap['day'], '2026-11-19')

    def test_budget_day_rollover_ist(self):
        import engine as e
        store = make_store()
        # Stored state from yesterday IST; clock is 00:30 IST (= 19:00 UTC, so
        # under TZ=UTC the local day is still yesterday - IST must win).
        clock_val = [datetime(2026, 11, 19, 0, 30, tzinfo=e.IST)]

        def clock():
            return clock_val[0]

        b1, _ = self._budget(clock=clock, store=store)
        b1.record('quotes')
        b1.flush()
        # Move to the next IST day, 00:30 IST on 20 Nov.
        clock_val[0] = datetime(2026, 11, 20, 0, 30, tzinfo=e.IST)
        old_tz = os.environ.get('TZ')
        os.environ['TZ'] = 'UTC'
        try:
            time.tzset()
            b2, _ = self._budget(clock=clock, store=store)
            snap = b2.snapshot()
        finally:
            if old_tz is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = old_tz
            time.tzset()
        self.assertEqual(snap['day'], '2026-11-20')
        self.assertEqual(snap['requests'], 0)

    def test_budget_limits_same_as_config(self):
        import engine as e
        b, _ = self._budget()
        snap = b.snapshot()
        self.assertEqual(snap['limits'], {'per_second': 4, 'per_minute': 40,
                                          'per_day': 4000,
                                          'reserve_for_session_monitor': 1500})
        # Trial caps are the min of config and the hard trial caps.
        bt, _ = self._budget(trial=True)
        snap_t = bt.snapshot()
        self.assertEqual(snap_t['limits']['per_day'], 1000)
        self.assertEqual(snap_t['limits']['per_minute'], 8)
        self.assertEqual(snap_t['limits']['per_second'], 1)
        self.assertEqual(snap_t['limits']['reserve_for_session_monitor'], 0)

    def test_trial_budget_uses_own_key_never_box_key(self):
        import engine as e
        store = make_store()
        bt, _ = self._budget(trial=True, store=store)
        bt.record('quotes')
        bt.flush()
        toks = TokenStore(store)
        self.assertIsNotNone(toks.get_budget(trial=True))
        self.assertIsNone(toks.get_budget(trial=False))

    def test_no_secret_in_logs_or_exceptions(self):
        import engine as e
        canary = 'CANARY_PIN_1234'
        store = make_store()
        key = ts.generate_key()
        env = {'PAPER_RUNTIME': 'render', 'PAPER_TEST_DATABASE': store.path,
               'TOKEN_ENC_KEY': key, 'FYERS_PIN': canary,
               'FYERS_TOTP_SECRET': 'CANARY_TOTP_5678',
               'FYERS_CLIENT_ID': 'C', 'FYERS_SECRET_ID': 'S',
               'FYERS_AUTO_LOGIN': '1', 'RENDER_MODE': 'trial',
               'PAPER_NAMESPACE_OVERRIDE': 'paper_render_trial_v1'}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch('sys.stdout', buf):
                md = e.MarketData()
                try:
                    md.refresh()
                except RuntimeError as ex:
                    err = str(ex)
                else:
                    err = ''
        out = buf.getvalue() + err
        self.assertNotIn(canary, out)
        self.assertNotIn('CANARY_TOTP_5678', out)
        # The stored token ciphertext must not contain the canary either.
        raw = TokenStore(store, enc_key=key)._kv_get(ts.TOKEN_KEY)
        self.assertTrue(raw is None or canary not in raw)


if __name__ == '__main__':
    unittest.main()
