"""Fyers token auto-refresh for the Render service (brief 23 Nov 2026).

The supervisor owns one FyersTokenManager. It keeps the access token in
memory only: it never writes a token to disk, to .env, or to Turso. The
engine's own on-demand refresh (MarketData.refresh) is unchanged and
remains the safety net for a token that expires mid-session; it still
writes the refreshed token to the encrypted token store, as before.

How the token is obtained (in order):
  1. the in-memory cache, when the token was obtained today IST;
  2. FYERS_ACCESS_TOKEN from the environment (the hand-pasted token,
     kept for backward compatibility);
  3. the refresh-token grant: POST validate-refresh-token with the
     stored FYERS_REFRESH_TOKEN and the PIN (the refresh token is
     valid 15 days, so this avoids a full login most days);
  4. the full TOTP+PIN login via fyers_auth.refresh_token.

A login is only ever attempted when render_trial.login_allowed says so
(live mode, FYERS_AUTO_LOGIN=1, and the login secrets present); in
trial mode, or with no secrets, the manager stays in legacy mode and
never touches Fyers. Attempts are bounded (at most 3 per hour, with
backoff) and an auth failure opens a 30-minute circuit, so a bad
secret can never hammer the login or lock the Fyers account. Every
log line and exception text passes through the redactor, and the
/health status never carries a token value.
"""
from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import requests

import fyers_auth as fa

IST = ZoneInfo('Asia/Kolkata')

BASE_API = 'https://api-t1.fyers.in/api/v3'
VALIDATE_REFRESH_URL = BASE_API + '/validate-refresh-token'

# Daily refresh slot (IST) and its retry window.
REFRESH_AT = dtime(8, 30)
REFRESH_WINDOW_END = dtime(9, 5)
# Auth/lockout circuit: how long a failed login blocks further attempts.
CIRCUIT_OPEN_SECONDS = 1800
# At most this many login attempts in any rolling hour.
MAX_ATTEMPTS_PER_HOUR = 3
# On-demand refreshes (from get_access_token) are throttled to at most
# one attempt per this many seconds; scheduled refreshes are not.
ON_DEMAND_MIN_INTERVAL_S = 600
# A TOTP code is only used when at least this many seconds remain in
# its 30-second window.
TOTP_MIN_REMAINING_S = 5

LOGIN_CREDS = ('FYERS_CLIENT_ID', 'FYERS_APP_ID', 'FYERS_SECRET_ID',
               'FYERS_PIN', 'FYERS_TOTP_SECRET')

# Error codes that mean the credentials (not the network) failed. Only
# these open the 30-minute circuit; a transport failure does not.
AUTH_ERROR_CODES = frozenset({
    'send_otp_failed', 'verify_otp_failed', 'verify_pin_failed',
    'no_auth_code', 'no_access_token', 'refresh_token_failed',
})


def redact(text, secrets):
    """Remove every secret from `text` (fyers_auth.redact, same rule)."""
    return fa.redact(str(text), list(secrets or []))


def _app_id_hash(app_id_full, app_secret):
    return hashlib.sha256(
        ('%s:%s' % (app_id_full, app_secret)).encode()).hexdigest()


def refresh_via_refresh_token(creds, session=None):
    """One access token from a stored refresh token + PIN.

    POST validate-refresh-token with grant_type refresh_token, the
    appIdHash, the refresh token and the raw PIN. Returns
    {'access_token', 'refresh_token'?}. Raises FyersAuthError with a
    classified code (never a secret) on any failure. The refresh
    token is static for 15 days, so a new one is only returned when
    Fyers rotates it; the caller keeps whatever comes back.
    """
    app_id_full = creds['FYERS_APP_ID']
    app_secret = creds['FYERS_SECRET_ID']
    refresh_token = creds['FYERS_REFRESH_TOKEN']
    pin = creds['FYERS_PIN']
    if session is None:
        session = requests.Session()
    try:
        r = session.post(
            VALIDATE_REFRESH_URL,
            json={'grant_type': 'refresh_token',
                  'appIdHash': _app_id_hash(app_id_full, app_secret),
                  'refresh_token': refresh_token, 'pin': pin},
            timeout=30)
    except Exception:
        raise fa.FyersAuthError('token_refresh_failed') from None
    try:
        data = r.json()
    except Exception:
        raise fa.FyersAuthError('refresh_token_failed') from None
    access_token = (data or {}).get('access_token')
    if not access_token:
        raise fa.FyersAuthError('refresh_token_failed')
    out = {'access_token': access_token}
    if (data or {}).get('refresh_token'):
        out['refresh_token'] = data['refresh_token']
    return out


class FyersTokenManager:
    """In-memory Fyers token for one supervisor process.

    `env` is the environment to read secrets from (defaults to
    os.environ). `mode` is the render mode ('live' or 'trial'); a
    login is only attempted when render_trial.login_allowed allows
    it. `session_factory` builds the HTTP session (injectable for
    tests). `now_fn` returns the current aware IST datetime and
    `sleep_fn` sleeps (both injectable so tests never wait).
    `stored_refresh_token` is an optional callable returning the
    refresh token held in the encrypted token store, or None; the
    manager never writes to the store, it only reads that one value
    through the callable, and a failure there fails closed to None.
    """

    def __init__(self, env=None, mode='live', session_factory=None,
                 now_fn=None, sleep_fn=None, stored_refresh_token=None):
        import os
        self._env = dict(os.environ) if env is None else dict(env)
        self._mode = mode
        self._session_factory = session_factory or requests.Session
        self._now_fn = now_fn or (lambda: datetime.now(IST))
        self._sleep_fn = sleep_fn or time.sleep
        self._stored_refresh_token = stored_refresh_token
        self._lock = threading.Lock()
        self._token = None
        self._refresh_token = self._env.get('FYERS_REFRESH_TOKEN')
        self._source = None
        self._obtained_at = None
        self._attempts = []
        self._attempts_today = 0
        self._attempts_date = None
        self._last_error_code = None
        self._circuit_open_until = None
        self._last_attempt_at = None
        self._last_success_date = None

    # -- configuration -----------------------------------------------
    def credentials_present(self):
        return all(self._env.get(k) for k in LOGIN_CREDS)

    def login_allowed(self):
        import render_trial as rt
        return bool(rt.login_allowed(self._mode, self._env))

    def _can_login(self):
        return self.login_allowed() and self.credentials_present()

    def _secrets(self):
        return [self._env.get(k, '') for k in LOGIN_CREDS] + [
            self._env.get('FYERS_ACCESS_TOKEN', ''),
            self._env.get('FYERS_REFRESH_TOKEN', ''),
            self._token or '', self._refresh_token or '']

    # -- state ---------------------------------------------------------
    def _today(self):
        return self._now_fn().date()

    def _circuit_open(self, now):
        return (self._circuit_open_until is not None
                and now < self._circuit_open_until)

    def _open_circuit(self, now):
        self._circuit_open_until = now + timedelta(
            seconds=CIRCUIT_OPEN_SECONDS)

    def _prune_attempts(self, now):
        hour_ago = now - timedelta(hours=1)
        self._attempts = [t for t in self._attempts if t > hour_ago]

    def _cached_token(self, now):
        if (self._token and self._obtained_at is not None
                and self._obtained_at.date() == now.date()):
            return self._token
        return None

    def last_success_date(self):
        with self._lock:
            return self._last_success_date

    # -- token ---------------------------------------------------------
    def get_access_token(self):
        """The token for today's IST session, refreshing if needed.

        Returns the cached token when it was obtained today, else the
        FYERS_ACCESS_TOKEN env value when set, else triggers one
        on-demand refresh (throttled to one attempt per 10 minutes).
        Returns None when no token is available; never raises.
        """
        now = self._now_fn()
        with self._lock:
            token = self._cached_token(now)
            if token:
                return token
            env_token = self._env.get('FYERS_ACCESS_TOKEN')
            if env_token:
                self._token = env_token
                self._source = 'env'
                self._obtained_at = now
                return env_token
        return self.refresh(kind='on_demand')

    def refresh(self, kind='scheduled'):
        """Obtain a fresh token (refresh-token path, then full login).

        `kind` is 'scheduled' (startup / 08:30) or 'on_demand' (from
        get_access_token). Returns the access token, or None when no
        token could be obtained. Never raises, never logs a secret,
        and never writes a token anywhere: the token stays in memory
        only. Bounded: at most 3 attempts per rolling hour, a
        30-minute circuit after an auth failure, and on-demand
        attempts at most once per 10 minutes.
        """
        now = self._now_fn()
        with self._lock:
            token = self._cached_token(now)
            if token:
                return token
            if self._circuit_open(now):
                return None
            self._prune_attempts(now)
            if len(self._attempts) >= MAX_ATTEMPTS_PER_HOUR:
                self._last_error_code = 'login_attempts_exceeded'
                return None
            if (kind == 'on_demand' and self._last_attempt_at is not None
                    and (now - self._last_attempt_at).total_seconds()
                    < ON_DEMAND_MIN_INTERVAL_S):
                return None
            if not self._can_login():
                return None
            self._attempts.append(now)
            if self._attempts_date != now.date():
                self._attempts_date = now.date()
                self._attempts_today = 0
            self._attempts_today += 1
            self._last_attempt_at = now
        return self._do_refresh()

    def _do_refresh(self):
        """One login attempt (no lock held: this does network I/O)."""
        session = self._session_factory()
        creds = {k: self._env.get(k) for k in LOGIN_CREDS}
        creds['FYERS_REDIRECT_URI'] = self._env.get(
            'FYERS_REDIRECT_URI',
            'https://trade.fyers.in/api-login/redirect-uri/index.html')
        refresh_token = self._refresh_token
        if not refresh_token and self._stored_refresh_token is not None:
            try:
                refresh_token = self._stored_refresh_token()
            except Exception:
                refresh_token = None
        if refresh_token:
            creds['FYERS_REFRESH_TOKEN'] = refresh_token
        error_code = None
        try:
            result = self._attempt_login(creds, session)
        except fa.FyersAuthError as ex:
            error_code = str(ex) or 'token_refresh_failed'
            result = None
        except Exception as ex:
            error_code = 'token_refresh_failed'
            result = None
        with self._lock:
            if result is not None:
                self._token = result['access_token']
                self._source = result['source']
                self._obtained_at = self._now_fn()
                self._last_success_date = self._obtained_at.date()
                if result.get('refresh_token'):
                    self._refresh_token = result['refresh_token']
                self._circuit_open_until = None
                self._last_error_code = None
                return self._token
            self._last_error_code = error_code
            if error_code in AUTH_ERROR_CODES:
                self._open_circuit(self._now_fn())
            return None

    def _attempt_login(self, creds, session):
        """Refresh-token path, then the full TOTP login. One attempt.

        The refresh-token failure never opens the circuit here: the
        full login is still tried. Only a failed full login (or a
        refresh-token failure with no login available) is an auth
        failure for the circuit.
        """
        if creds.get('FYERS_REFRESH_TOKEN'):
            try:
                out = refresh_via_refresh_token(creds, session=session)
            except fa.FyersAuthError as ex:
                refresh_error = str(ex) or 'refresh_token_failed'
            else:
                out['source'] = 'refresh_token'
                return out
        else:
            refresh_error = None
        if not self.credentials_present():
            raise fa.FyersAuthError(
                refresh_error or 'refresh_token_failed')
        self._wait_for_totp_window()
        out = fa.refresh_token(creds, session=session)
        out['source'] = 'totp'
        return out

    def _wait_for_totp_window(self):
        """Wait until at least 5 s remain in the 30 s TOTP window, so
        the code the login uses is inside its window."""
        remaining = 30 - (self._now_fn().timestamp() % 30)
        if remaining < TOTP_MIN_REMAINING_S:
            self._sleep_fn(remaining + 0.15)

    # -- /health ---------------------------------------------------------
    def status(self):
        """The /health.fyers_auth payload. Never carries a token."""
        now = self._now_fn()
        with self._lock:
            valid = (self._token is not None
                     and self._obtained_at is not None
                     and self._obtained_at.date() == now.date())
            if self._attempts_date != now.date():
                attempts_today = 0
            else:
                attempts_today = self._attempts_today
            circuit_open = self._circuit_open(now)
            if not self._can_login():
                next_refresh = None
            elif self._last_success_date == now.date():
                nxt = datetime.combine(now.date() + timedelta(days=1),
                                       REFRESH_AT, tzinfo=IST)
                next_refresh = nxt.isoformat()
            else:
                nxt = datetime.combine(now.date(), REFRESH_AT, tzinfo=IST)
                if now >= nxt:
                    nxt = datetime.combine(
                        now.date() + timedelta(days=1), REFRESH_AT,
                        tzinfo=IST)
                next_refresh = nxt.isoformat()
            return {
                'source': self._source,
                'valid': valid,
                'obtained_at_ist': (
                    self._obtained_at.isoformat()
                    if self._obtained_at is not None else None),
                'next_refresh_ist': next_refresh,
                'last_error_code': self._last_error_code,
                'circuit_open': circuit_open,
                'attempts_today': attempts_today,
            }
