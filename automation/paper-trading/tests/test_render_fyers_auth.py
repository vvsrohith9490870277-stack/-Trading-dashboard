"""Tests for the Fyers TOTP auto-refresh on Render (brief 23 Nov 2026).

All Fyers HTTP is faked (FakeFyersSession); no test ever touches Fyers.
No real secret is used: every credential and token below is a fake.
"""
import base64
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fyers_auth as fa
import render_fyers_auth as rfa
import render_health as rh
import render_state as rs

IST = rfa.IST

CREDS = {
    'FYERS_CLIENT_ID': 'CLIENT1',
    'FYERS_APP_ID': 'APPID-100',
    'FYERS_SECRET_ID': 'SECRET1',
    'FYERS_PIN': '1234',
    'FYERS_TOTP_SECRET': 'JBSWY3DPEHPK3PXP',
    'FYERS_REDIRECT_URI': 'https://trade.fyers.in/api-login/redirect-uri/index.html',
    'FYERS_AUTO_LOGIN': '1',
}

# Only the login / token endpoints are ever called. Any other URL
# (notably a broker endpoint) is a failure of the module.
ALLOWED_URL_TAILS = (
    'send_login_otp_v2', 'send_login_otp_v3', 'verify_otp',
    'verify_pin_v2', 'token', 'validate-authcode',
    'validate-refresh-token',
)


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeNonJsonResp:
    """A Fyers error page: .json() raises, .text is HTML."""

    def __init__(self, text='<html>error</html>', status=502):
        self.status_code = status
        self.text = text

    def json(self):
        raise ValueError('no JSON here')


class FakeFyersSession:
    """Records every login call; emulates the Fyers login endpoints.

    fail_v2_otp: v2 answers without a request_key (the Render
    failure); v3 answers with one, so the fallback can be exercised.
    fail_v2_non_json: v2 answers a non-JSON error page; v3 succeeds.
    fail_both_otp: both v2 and v3 answer without a request_key (the
    v3 message is distinct, so the recorded detail is provably the
    last attempt's).
    """

    def __init__(self, fail_step=None, fail_refresh_token=False,
                 fail_transport=False, access_token='AT-NEW',
                 refresh_token='RT-NEW', rt_access_token='AT-RT',
                 fail_v2_otp=False, fail_v2_non_json=False,
                 fail_both_otp=False):
        self.calls = []
        self.headers = {}
        self.fail_step = fail_step
        self.fail_refresh_token = fail_refresh_token
        self.fail_transport = fail_transport
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.rt_access_token = rt_access_token
        self.fail_v2_otp = fail_v2_otp
        self.fail_v2_non_json = fail_v2_non_json
        self.fail_both_otp = fail_both_otp

    def post(self, url, json=None, timeout=None, **kw):
        self.calls.append({'url': url, 'json': json})
        if self.fail_transport:
            raise ConnectionError('boom')
        step = url.rsplit('/', 1)[-1]
        if self.fail_step == step:
            return FakeResp({'s': 'error', 'code': -1,
                             'message': 'boom'}, 400)
        if step == 'send_login_otp_v2':
            if self.fail_v2_non_json:
                return FakeNonJsonResp()
            if self.fail_v2_otp or self.fail_both_otp:
                return FakeResp({'s': 'error', 'code': -110,
                                 'message': 'otp v2 unavailable'}, 400)
            return FakeResp({'request_key': 'rk1', 's': 'ok'})
        if step == 'send_login_otp_v3':
            if self.fail_both_otp:
                return FakeResp({'s': 'error', 'code': -111,
                                 'message': 'otp v3 unavailable'}, 400)
            return FakeResp({'request_key': 'rk1-v3', 's': 'ok'})
        if step == 'verify_otp':
            return FakeResp({'request_key': 'rk2', 's': 'ok'})
        if step == 'verify_pin_v2':
            return FakeResp({'data': {'access_token': 'sess1'}, 's': 'ok'})
        if step == 'token':
            return FakeResp(
                {'Url': 'https://trade.fyers.in/api-login/redirect-uri/'
                        'index.html?auth_code=AC1', 's': 'ok'})
        if step == 'validate-authcode':
            return FakeResp({'access_token': self.access_token,
                             'refresh_token': self.refresh_token,
                             's': 'ok'})
        if step == 'validate-refresh-token':
            if self.fail_refresh_token:
                return FakeResp({'s': 'error', 'code': -1,
                                 'message': 'bad refresh token'}, 400)
            return FakeResp({'access_token': self.rt_access_token,
                             's': 'ok'})
        raise AssertionError('unexpected url ' + url)


class FakeClock:
    """Controllable IST clock; FakeSleep records waits without waiting."""

    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now = self.now + timedelta(**kw)


class FakeSleep:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


def make_manager(env_extra=None, session=None, clock=None, sleep=None,
                 stored_refresh_token=None, mode='live', nudge=True):
    """A manager with fakes. The clock starts Monday 2026-11-23 08:00
    IST with at least 10 s left in the TOTP window, so no test waits
    unless it sets the clock itself (nudge=False keeps the clock
    exactly as given)."""
    clock = clock or FakeClock(datetime(2026, 11, 23, 8, 0, 0, tzinfo=IST))
    if nudge:
        ts = clock.now.timestamp()
        remaining = 30 - (ts % 30)
        if remaining < 10:
            clock.advance(seconds=(10 - remaining) + 1)
    sleep = sleep if sleep is not None else FakeSleep()
    env = dict(CREDS)
    if env_extra is not None:
        env.update(env_extra)
    session = session if session is not None else FakeFyersSession()
    return rfa.FyersTokenManager(
        env=env, mode=mode, session_factory=lambda: session,
        now_fn=clock, sleep_fn=sleep,
        stored_refresh_token=stored_refresh_token), session, clock, sleep


def make_store(tmpdir=None):
    tmp = Path(tempfile.mkdtemp(dir=tmpdir)) / 'store.sqlite'
    store = rs.SqliteStore(str(tmp))
    rs.migrate(store)
    return store


class RefreshTokenGrantTests(unittest.TestCase):
    def test_refresh_token_grant_shape(self):
        sess = FakeFyersSession()
        creds = dict(CREDS, FYERS_REFRESH_TOKEN='RT-OLD')
        out = rfa.refresh_via_refresh_token(creds, session=sess)
        self.assertEqual(out, {'access_token': 'AT-RT'})
        self.assertEqual(len(sess.calls), 1)
        call = sess.calls[0]
        self.assertTrue(call['url'].endswith('validate-refresh-token'))
        expect_hash = hashlib.sha256(b'APPID-100:SECRET1').hexdigest()
        self.assertEqual(call['json'], {
            'grant_type': 'refresh_token', 'appIdHash': expect_hash,
            'refresh_token': 'RT-OLD', 'pin': '1234'})
        # The refresh-token call carries the raw PIN (per the Fyers
        # endpoint), never the TOTP code.
        self.assertNotIn('otp', json.dumps(call['json']))

    def test_refresh_token_grant_failure_classified(self):
        sess = FakeFyersSession(fail_refresh_token=True)
        creds = dict(CREDS, FYERS_REFRESH_TOKEN='RT-OLD')
        with self.assertRaises(fa.FyersAuthError) as cm:
            rfa.refresh_via_refresh_token(creds, session=sess)
        self.assertEqual(str(cm.exception), 'refresh_token_failed')
        self.assertNotIn('1234', str(cm.exception))

    def test_refresh_token_grant_transport_failure(self):
        sess = FakeFyersSession(fail_transport=True)
        creds = dict(CREDS, FYERS_REFRESH_TOKEN='RT-OLD')
        with self.assertRaises(fa.FyersAuthError) as cm:
            rfa.refresh_via_refresh_token(creds, session=sess)
        self.assertEqual(str(cm.exception), 'token_refresh_failed')


class ManagerRefreshTests(unittest.TestCase):
    def test_totp_login_success(self):
        mgr, sess, clock, sleep = make_manager()
        token = mgr.refresh()
        self.assertEqual(token, 'AT-NEW')
        st = mgr.status()
        self.assertEqual(st['source'], 'totp')
        self.assertTrue(st['valid'])
        self.assertEqual(st['obtained_at_ist'],
                         clock.now.isoformat())
        self.assertEqual(st['attempts_today'], 1)
        self.assertIsNone(st['last_error_code'])
        self.assertFalse(st['circuit_open'])
        # The full 5-step TOTP flow ran, and no refresh-token call.
        tails = [c['url'].rsplit('/', 1)[-1] for c in sess.calls]
        self.assertEqual(tails, ['send_login_otp_v2', 'verify_otp',
                                 'verify_pin_v2', 'token',
                                 'validate-authcode'])
        self.assertEqual(sleep.calls, [])

    def test_refresh_token_path_first(self):
        mgr, sess, clock, sleep = make_manager(
            env_extra={'FYERS_REFRESH_TOKEN': 'RT-OLD'})
        token = mgr.refresh()
        self.assertEqual(token, 'AT-RT')
        self.assertEqual(mgr.status()['source'], 'refresh_token')
        # Only the refresh-token grant ran: no TOTP flow at all.
        self.assertEqual(len(sess.calls), 1)
        self.assertTrue(sess.calls[0]['url'].endswith(
            'validate-refresh-token'))
        expect_hash = hashlib.sha256(b'APPID-100:SECRET1').hexdigest()
        self.assertEqual(sess.calls[0]['json'], {
            'grant_type': 'refresh_token', 'appIdHash': expect_hash,
            'refresh_token': 'RT-OLD', 'pin': '1234'})

    def test_stored_refresh_token_used(self):
        mgr, sess, clock, sleep = make_manager(
            env_extra={'FYERS_REFRESH_TOKEN': ''},
            stored_refresh_token=lambda: 'RT-STORED')
        token = mgr.refresh()
        self.assertEqual(token, 'AT-RT')
        self.assertEqual(mgr.status()['source'], 'refresh_token')
        self.assertEqual(sess.calls[0]['json']['refresh_token'],
                         'RT-STORED')

    def test_refresh_token_failure_falls_back_to_totp(self):
        mgr, sess, clock, sleep = make_manager(
            env_extra={'FYERS_REFRESH_TOKEN': 'RT-BAD'},
            session=FakeFyersSession(fail_refresh_token=True))
        token = mgr.refresh()
        self.assertEqual(token, 'AT-NEW')
        self.assertEqual(mgr.status()['source'], 'totp')
        tails = [c['url'].rsplit('/', 1)[-1] for c in sess.calls]
        self.assertEqual(tails, ['validate-refresh-token',
                                 'send_login_otp_v2', 'verify_otp',
                                 'verify_pin_v2', 'token',
                                 'validate-authcode'])

    def test_env_token_fallback_no_login(self):
        mgr, sess, clock, sleep = make_manager(
            env_extra={'FYERS_ACCESS_TOKEN': 'AT-ENV',
                       'FYERS_CLIENT_ID': '', 'FYERS_APP_ID': '',
                       'FYERS_SECRET_ID': '', 'FYERS_PIN': '',
                       'FYERS_TOTP_SECRET': '', 'FYERS_AUTO_LOGIN': ''})
        token = mgr.get_access_token()
        self.assertEqual(token, 'AT-ENV')
        self.assertEqual(mgr.status()['source'], 'env')
        self.assertTrue(mgr.status()['valid'])
        # No Fyers call at all: the env token is used as-is.
        self.assertEqual(sess.calls, [])

    def test_totp_env_missing_legacy(self):
        mgr, sess, clock, sleep = make_manager(env_extra={
            k: '' for k in ('FYERS_CLIENT_ID', 'FYERS_APP_ID',
                            'FYERS_SECRET_ID', 'FYERS_PIN',
                            'FYERS_TOTP_SECRET', 'FYERS_AUTO_LOGIN',
                            'FYERS_REFRESH_TOKEN')})
        self.assertIsNone(mgr.get_access_token())
        self.assertIsNone(mgr.refresh())
        self.assertEqual(sess.calls, [])
        st = mgr.status()
        self.assertEqual(st, {'source': None, 'valid': False,
                              'obtained_at_ist': None,
                              'next_refresh_ist': None,
                              'last_error_code': None,
                              'last_error_detail': None,
                              'circuit_open': False,
                              'attempts_today': 0})

    def test_login_not_allowed_in_trial(self):
        mgr, sess, clock, sleep = make_manager(mode='trial')
        self.assertIsNone(mgr.get_access_token())
        self.assertIsNone(mgr.refresh())
        self.assertEqual(sess.calls, [])
        self.assertFalse(mgr.status()['valid'])

    def test_cached_token_valid_today_no_second_login(self):
        mgr, sess, clock, sleep = make_manager()
        self.assertEqual(mgr.refresh(), 'AT-NEW')
        calls_after_first = len(sess.calls)
        self.assertEqual(mgr.get_access_token(), 'AT-NEW')
        self.assertEqual(mgr.refresh(), 'AT-NEW')
        self.assertEqual(len(sess.calls), calls_after_first)

    def test_token_not_valid_next_day(self):
        mgr, sess, clock, sleep = make_manager()
        self.assertEqual(mgr.refresh(), 'AT-NEW')
        calls_after_first = len(sess.calls)
        clock.advance(days=1)
        # Day 2 uses the refresh token from day 1 (valid 15 days),
        # not a full TOTP login: exactly one more call, the grant.
        self.assertEqual(mgr.get_access_token(), 'AT-RT')
        self.assertEqual(len(sess.calls), calls_after_first + 1)
        self.assertTrue(sess.calls[-1]['url'].endswith(
            'validate-refresh-token'))
        self.assertEqual(mgr.status()['source'], 'refresh_token')
        self.assertEqual(mgr.status()['attempts_today'], 1)


class V3FallbackTests(unittest.TestCase):
    """send_login_otp_v3 fallback (brief 23 Nov 2026): Fyers no longer
    issues a request_key on v2 for this client, so the login must try
    v3 before failing, exactly as the box script does."""

    def test_v2_no_key_falls_back_to_v3(self):
        mgr, sess, clock, sleep = make_manager(
            session=FakeFyersSession(fail_v2_otp=True))
        token = mgr.refresh()
        self.assertEqual(token, 'AT-NEW')
        self.assertEqual(mgr.status()['source'], 'totp')
        self.assertTrue(mgr.status()['valid'])
        tails = [c['url'].rsplit('/', 1)[-1] for c in sess.calls]
        self.assertEqual(tails, ['send_login_otp_v2',
                                 'send_login_otp_v3', 'verify_otp',
                                 'verify_pin_v2', 'token',
                                 'validate-authcode'])
        # Only the client ID (base64) and app_id "2" are sent at
        # step 1, on both URLs.
        expect = {'fy_id': base64.b64encode(b'CLIENT1').decode('ascii'),
                  'app_id': '2'}
        self.assertEqual(sess.calls[0]['json'], expect)
        self.assertEqual(sess.calls[1]['json'], expect)
        self.assertIsNone(mgr.status()['last_error_detail'])

    def test_v2_non_json_falls_back_to_v3(self):
        mgr, sess, clock, sleep = make_manager(
            session=FakeFyersSession(fail_v2_non_json=True))
        self.assertEqual(mgr.refresh(), 'AT-NEW')
        tails = [c['url'].rsplit('/', 1)[-1] for c in sess.calls]
        self.assertEqual(tails[:2], ['send_login_otp_v2',
                                     'send_login_otp_v3'])

    def test_both_otp_fail_raises_send_otp_failed(self):
        sess = FakeFyersSession(fail_both_otp=True)
        with self.assertRaises(fa.FyersAuthError) as cm:
            fa.refresh_token(dict(CREDS), session=sess)
        self.assertEqual(str(cm.exception), 'send_otp_failed')
        # The detail is the LAST attempt's (v3's), not v2's.
        self.assertEqual(cm.exception.detail,
                         {'s': 'error', 'code': -111,
                          'message': 'otp v3 unavailable'})
        tails = [c['url'].rsplit('/', 1)[-1] for c in sess.calls]
        self.assertEqual(tails, ['send_login_otp_v2',
                                 'send_login_otp_v3'])

    def test_last_error_detail_in_health(self):
        mgr, sess, clock, sleep = make_manager(
            session=FakeFyersSession(fail_both_otp=True))
        self.assertIsNone(mgr.refresh())
        st = mgr.status()
        self.assertEqual(st['last_error_code'], 'send_otp_failed')
        self.assertEqual(st['last_error_detail'],
                         {'s': 'error', 'code': -111,
                          'message': 'otp v3 unavailable'})
        self.assertTrue(st['circuit_open'])
        # The health payload never carries a secret.
        blob = json.dumps(st)
        for secret in ('1234', 'JBSWY3DPEHPK3PXP', 'CLIENT1', 'SECRET1'):
            self.assertNotIn(secret, blob)

    def test_last_error_detail_message_truncated_to_160(self):
        long_msg = 'x' * 500
        sess = FakeFyersSession(fail_both_otp=True)
        orig_post = sess.post

        def post(url, json=None, timeout=None, **kw):
            if url.endswith('send_login_otp_v3'):
                sess.calls.append({'url': url, 'json': json})
                return FakeResp({'s': 'error', 'code': -111,
                                 'message': long_msg}, 400)
            return orig_post(url, json=json, timeout=timeout, **kw)

        sess.post = post
        with self.assertRaises(fa.FyersAuthError) as cm:
            fa.refresh_token(dict(CREDS), session=sess)
        self.assertEqual(cm.exception.detail['message'], 'x' * 160)
        self.assertEqual(len(cm.exception.detail['message']), 160)

    def test_last_error_detail_redacts_secrets(self):
        sess = FakeFyersSession(fail_both_otp=True)
        orig_post = sess.post

        def post(url, json=None, timeout=None, **kw):
            if url.endswith('send_login_otp_v3'):
                sess.calls.append({'url': url, 'json': json})
                return FakeResp({'s': 'error', 'code': -111,
                                 'message': 'bad pin 1234 totp '
                                            'JBSWY3DPEHPK3PXP'}, 400)
            return orig_post(url, json=json, timeout=timeout, **kw)

        sess.post = post
        with self.assertRaises(fa.FyersAuthError) as cm:
            fa.refresh_token(dict(CREDS), session=sess)
        detail = cm.exception.detail
        self.assertNotIn('1234', detail['message'])
        self.assertNotIn('JBSWY3DPEHPK3PXP', detail['message'])
        self.assertIn('<redacted>', detail['message'])

    def test_last_error_detail_cleared_on_success(self):
        sess = FakeFyersSession(fail_both_otp=True)
        mgr, sess, clock, sleep = make_manager(session=sess)
        self.assertIsNone(mgr.refresh())
        self.assertEqual(mgr.status()['last_error_code'],
                         'send_otp_failed')
        self.assertIsNotNone(mgr.status()['last_error_detail'])
        # 31 minutes on: the circuit has closed. The fake now
        # answers v2 fine, so the retry succeeds.
        clock.advance(minutes=31)
        sess.fail_both_otp = False
        self.assertEqual(mgr.refresh(), 'AT-NEW')
        self.assertIsNone(mgr.status()['last_error_code'])
        self.assertIsNone(mgr.status()['last_error_detail'])
        self.assertTrue(mgr.status()['valid'])


class CircuitAndBoundsTests(unittest.TestCase):
    def test_auth_error_opens_circuit_blocks_30min(self):
        mgr, sess, clock, sleep = make_manager(
            session=FakeFyersSession(fail_step='verify_pin_v2'))
        self.assertIsNone(mgr.refresh())
        st = mgr.status()
        self.assertEqual(st['last_error_code'], 'verify_pin_failed')
        self.assertTrue(st['circuit_open'])
        calls_after_fail = len(sess.calls)
        # Blocked inside the 30-minute circuit: no new Fyers call.
        self.assertIsNone(mgr.refresh())
        self.assertIsNone(mgr.get_access_token())
        self.assertEqual(len(sess.calls), calls_after_fail)
        clock.advance(minutes=29)
        self.assertIsNone(mgr.refresh())
        self.assertEqual(len(sess.calls), calls_after_fail)
        # 31 minutes on: the circuit has closed, a retry is allowed.
        clock.advance(minutes=2)
        self.assertIsNone(mgr.refresh())
        self.assertGreater(len(sess.calls), calls_after_fail)

    def test_transport_error_no_circuit_but_throttled(self):
        mgr, sess, clock, sleep = make_manager(
            session=FakeFyersSession(fail_transport=True),
            env_extra={'FYERS_REFRESH_TOKEN': ''})
        self.assertIsNone(mgr.get_access_token())
        st = mgr.status()
        self.assertEqual(st['last_error_code'], 'token_refresh_failed')
        self.assertFalse(st['circuit_open'])
        calls_after_fail = len(sess.calls)
        # On-demand attempts are throttled to one per 10 minutes.
        self.assertIsNone(mgr.get_access_token())
        self.assertEqual(len(sess.calls), calls_after_fail)
        clock.advance(minutes=9)
        self.assertIsNone(mgr.get_access_token())
        self.assertEqual(len(sess.calls), calls_after_fail)
        clock.advance(minutes=2)
        self.assertIsNone(mgr.get_access_token())
        self.assertGreater(len(sess.calls), calls_after_fail)

    def test_attempts_bounded_3_per_hour(self):
        mgr, sess, clock, sleep = make_manager(
            session=FakeFyersSession(fail_transport=True),
            env_extra={'FYERS_REFRESH_TOKEN': ''})
        for _ in range(3):
            self.assertIsNone(mgr.refresh(kind='scheduled'))
        self.assertEqual(len(sess.calls), 3)
        self.assertEqual(mgr.status()['attempts_today'], 3)
        # The 4th attempt inside the hour is refused, no Fyers call.
        self.assertIsNone(mgr.refresh(kind='scheduled'))
        self.assertEqual(len(sess.calls), 3)
        self.assertEqual(mgr.status()['last_error_code'],
                         'login_attempts_exceeded')
        # An hour on: attempts are allowed again.
        clock.advance(minutes=61)
        self.assertIsNone(mgr.refresh(kind='scheduled'))
        self.assertEqual(len(sess.calls), 4)

    def test_totp_window_wait(self):
        clock = FakeClock(datetime(2026, 11, 23, 8, 0, 0, tzinfo=IST))
        # Put the clock where under 5 s remain in the TOTP window.
        ts = clock.now.timestamp()
        remaining = 30 - (ts % 30)
        clock.advance(seconds=remaining + 26)
        sleep = FakeSleep()
        mgr, sess, clock, sleep = make_manager(
            clock=clock, sleep=sleep, nudge=False)
        self.assertEqual(mgr.refresh(), 'AT-NEW')
        self.assertEqual(len(sleep.calls), 1)
        self.assertGreater(sleep.calls[0], 0)


class RedactionAndHealthTests(unittest.TestCase):
    def test_redactor_removes_secrets(self):
        mgr, sess, clock, sleep = make_manager(
            env_extra={'FYERS_ACCESS_TOKEN': 'AT-ENV-SECRET',
                       'FYERS_REFRESH_TOKEN': 'RT-OLD-SECRET'})
        secrets = ['1234', 'JBSWY3DPEHPK3PXP', 'AT-ENV-SECRET',
                   'RT-OLD-SECRET', 'CLIENT1', 'SECRET1']
        text = ('pin 1234 totp JBSWY3DPEHPK3PXP access AT-ENV-SECRET '
                'refresh RT-OLD-SECRET client CLIENT1 secret SECRET1')
        out = rfa.redact(text, mgr._secrets())
        for secret in secrets:
            self.assertNotIn(secret, out)
        self.assertIn('<redacted>', out)
        # A classified auth error carries no secret either.
        mgr2, sess2, _, _ = make_manager(
            session=FakeFyersSession(fail_step='verify_pin_v2'))
        self.assertIsNone(mgr2.refresh())
        self.assertNotIn('1234', mgr2.status()['last_error_code'])
        self.assertNotIn('JBSWY3DPEHPK3PXP',
                         mgr2.status()['last_error_code'])

    def test_health_never_contains_token(self):
        mgr, sess, clock, sleep = make_manager(
            session=FakeFyersSession(access_token='AT-FAKE-XYZ'))
        self.assertEqual(mgr.refresh(), 'AT-FAKE-XYZ')
        st = mgr.status()
        self.assertEqual(set(st), {'source', 'valid', 'obtained_at_ist',
                                   'next_refresh_ist', 'last_error_code',
                                   'last_error_detail',
                                   'circuit_open', 'attempts_today'})
        self.assertNotIn('AT-FAKE-XYZ', json.dumps(st))
        snap = rh.build_snapshot(
            mode='live', namespace='paper_v2', active_runner='render',
            lease_held=True, lease={'owner_id': 'render:h:1:1',
                                    'epoch': 1},
            worker={'state': 'running', 'pid': 123, 'rss_mb': 200},
            jobs={}, uptime_seconds=60, memory_mb=150, fyers_auth=st)
        self.assertEqual(snap['fyers_auth'], st)
        self.assertNotIn('AT-FAKE-XYZ', json.dumps(snap))

    def test_health_default_unknown(self):
        snap = rh.build_snapshot(
            mode='live', namespace='paper_v2', active_runner='render',
            lease_held=True, lease={'owner_id': 'render:h:1:1',
                                    'epoch': 1},
            worker={'state': 'running', 'pid': 123, 'rss_mb': 200},
            jobs={}, uptime_seconds=60, memory_mb=150)
        self.assertEqual(snap['fyers_auth'], {
            'source': None, 'valid': False, 'obtained_at_ist': None,
            'next_refresh_ist': None, 'last_error_code': None,
            'last_error_detail': None,
            'circuit_open': None, 'attempts_today': 0})

    def test_next_refresh_ist(self):
        mgr, sess, clock, sleep = make_manager()
        # Before 08:30 with no success today: next is 08:30 today.
        self.assertEqual(mgr.status()['next_refresh_ist'],
                         '2026-11-23T08:30:00+05:30')
        self.assertEqual(mgr.refresh(), 'AT-NEW')
        # After a success today: next is 08:30 tomorrow.
        self.assertEqual(mgr.status()['next_refresh_ist'],
                         '2026-11-24T08:30:00+05:30')

    def test_no_order_endpoint_called(self):
        # Full TOTP login plus a refresh-token login: assert on the
        # fake's URL list that no broker endpoint was ever called.
        sess = FakeFyersSession()
        mgr, sess, clock, sleep = make_manager(session=sess)
        self.assertEqual(mgr.refresh(), 'AT-NEW')
        mgr2, sess2, _, _ = make_manager(
            env_extra={'FYERS_REFRESH_TOKEN': 'RT-OLD'})
        self.assertEqual(mgr2.refresh(), 'AT-RT')
        for call in sess.calls + sess2.calls:
            url = call['url']
            for bad in ('orders', 'place', 'modify', 'cancel'):
                self.assertNotIn(bad, url)
            tail = url.rsplit('/', 1)[-1]
            self.assertIn(tail, ALLOWED_URL_TAILS)


class EngineOnDemandTests(unittest.TestCase):
    def test_expired_token_quote_one_refresh_one_retry(self):
        import engine as e
        store = make_store()
        key = 'test-key-not-a-real-fernet-key'
        env = {'PAPER_RUNTIME': 'render',
               'PAPER_TEST_DATABASE': store.path,
               'TOKEN_ENC_KEY': key, 'FYERS_APP_ID': 'APPID-100',
               'FYERS_ACCESS_TOKEN': 'AT-OLD'}
        with mock.patch.dict(os.environ, env, clear=False):
            md = e.MarketData()
            calls = {'n': 0}

            def fake_get(url, params=None, headers=None, timeout=None):
                calls['n'] += 1
                if calls['n'] == 1:
                    return FakeResp({'s': 'error', 'code': -8}, 401)
                return FakeResp({'s': 'ok', 'd': [{'lp': 100.0}]}, 200)

            refreshed = {'n': 0}

            def fake_refresh():
                refreshed['n'] += 1
                md.env['FYERS_ACCESS_TOKEN'] = 'AT-NEW'

            with mock.patch.object(e.requests, 'get',
                                   side_effect=fake_get):
                with mock.patch.object(md, 'refresh',
                                       side_effect=fake_refresh):
                    out = md.get('quotes',
                                 {'symbols': 'NSE:RELIANCE-EQ'})
            self.assertEqual(out, {'s': 'ok', 'd': [{'lp': 100.0}]})
            # Exactly one refresh and one retry of the quote.
            self.assertEqual(refreshed['n'], 1)
            self.assertEqual(calls['n'], 2)


class SupervisorWiringTests(unittest.TestCase):
    def _sup(self, store, manager=None):
        import render_app as ra
        sup = ra.Supervisor.__new__(ra.Supervisor)
        sup.store = store
        sup.namespace = 'paper_v2'
        sup.mode = 'live'
        sup.owner_id = 'render:h:1:1'
        sup.lease_held = False
        sup.breakers = {}
        sup._job_thread = None
        sup._boot_actions_done = False
        if manager is not None:
            sup._fyers_auth = manager
        return sup

    def test_child_env_injects_token(self):
        store = make_store()
        try:
            mgr, sess, clock, sleep = make_manager(
                env_extra={'FYERS_ACCESS_TOKEN': 'AT-ENV'})
            sup = self._sup(store, manager=mgr)
            with mock.patch.dict(os.environ, {}, clear=True):
                env = sup._child_env()
            self.assertEqual(env['FYERS_ACCESS_TOKEN'], 'AT-ENV')
            self.assertEqual(env['PAPER_RUNTIME'], 'render')
            self.assertEqual(env['RENDER_MODE'], 'live')
            self.assertEqual(env['RENDER_OWNER_ID'], 'render:h:1:1')
        finally:
            store.close()

    def test_child_env_legacy_no_token(self):
        store = make_store()
        try:
            mgr, sess, clock, sleep = make_manager(env_extra={
                k: '' for k in ('FYERS_CLIENT_ID', 'FYERS_APP_ID',
                                'FYERS_SECRET_ID', 'FYERS_PIN',
                                'FYERS_TOTP_SECRET', 'FYERS_AUTO_LOGIN',
                                'FYERS_REFRESH_TOKEN')})
            sup = self._sup(store, manager=mgr)
            with mock.patch.dict(os.environ, {}, clear=True):
                env = sup._child_env()
            self.assertNotIn('FYERS_ACCESS_TOKEN', env)
            self.assertEqual(sess.calls, [])
        finally:
            store.close()

    def test_boot_refresh_calls_manager(self):
        store = make_store()
        try:
            mgr, sess, clock, sleep = make_manager()
            sup = self._sup(store, manager=mgr)
            with mock.patch.dict(os.environ, {}, clear=True):
                sup._run_boot_actions()
            # The startup refresh ran the full TOTP login.
            self.assertEqual(mgr.status()['source'], 'totp')
            self.assertTrue(mgr.status()['valid'])
            self.assertGreater(len(sess.calls), 0)
        finally:
            store.close()

    def test_0830_tick_triggers_refresh(self):
        import render_schedule as rsch
        store = make_store()
        try:
            mgr, sess, clock, sleep = make_manager()
            sup = self._sup(store, manager=mgr)
            sup.lease_held = True
            orig_day = rsch.is_trading_day
            rsch.is_trading_day = lambda day: True
            try:
                sup._maybe_refresh_token(
                    now=datetime(2026, 11, 23, 8, 30, tzinfo=IST))
            finally:
                rsch.is_trading_day = orig_day
            self.assertEqual(mgr.status()['source'], 'totp')
            self.assertTrue(mgr.status()['valid'])
        finally:
            store.close()

    def test_tick_outside_window_no_refresh(self):
        import render_schedule as rsch
        store = make_store()
        try:
            mgr, sess, clock, sleep = make_manager()
            sup = self._sup(store, manager=mgr)
            sup.lease_held = True
            orig_day = rsch.is_trading_day
            rsch.is_trading_day = lambda day: True
            try:
                sup._maybe_refresh_token(
                    now=datetime(2026, 11, 23, 10, 0, tzinfo=IST))
            finally:
                rsch.is_trading_day = orig_day
            self.assertEqual(sess.calls, [])
            self.assertFalse(mgr.status()['valid'])
        finally:
            store.close()

    def test_tick_no_refresh_when_already_done_today(self):
        import render_schedule as rsch
        store = make_store()
        try:
            mgr, sess, clock, sleep = make_manager()
            sup = self._sup(store, manager=mgr)
            sup.lease_held = True
            orig_day = rsch.is_trading_day
            rsch.is_trading_day = lambda day: True
            try:
                sup._maybe_refresh_token(
                    now=datetime(2026, 11, 23, 8, 30, tzinfo=IST))
                calls_after_first = len(sess.calls)
                sup._maybe_refresh_token(
                    now=datetime(2026, 11, 23, 8, 45, tzinfo=IST))
            finally:
                rsch.is_trading_day = orig_day
            self.assertEqual(len(sess.calls), calls_after_first)
        finally:
            store.close()

    def test_tick_no_refresh_when_lease_not_held(self):
        import render_schedule as rsch
        store = make_store()
        try:
            mgr, sess, clock, sleep = make_manager()
            sup = self._sup(store, manager=mgr)
            sup.lease_held = False
            orig_day = rsch.is_trading_day
            rsch.is_trading_day = lambda day: True
            try:
                sup._maybe_refresh_token(
                    now=datetime(2026, 11, 23, 8, 30, tzinfo=IST))
            finally:
                rsch.is_trading_day = orig_day
            self.assertEqual(sess.calls, [])
        finally:
            store.close()


if __name__ == '__main__':
    unittest.main()
