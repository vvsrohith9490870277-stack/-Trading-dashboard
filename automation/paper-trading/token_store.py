"""Fyers token, auth-circuit and budget storage in Turso kv (Task 3).

No secret or counter lives only on local disk in render mode. The access /
refresh token is Fernet-encrypted with the key from env TOKEN_ENC_KEY
(generated once with `render_ctl.py gen-key`; never stored in Turso) and kept
in paper_runtime_kv (scope '_global', key 'fyers_token') with an IST
issued_at. The auth circuit and the Fyers budget use the same table with
their own keys; the trial budget uses its own key ('fyers_budget_trial')
and never touches the box budget key. Reading a token never logs it.
"""
from __future__ import annotations
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken

import render_state as rs

IST = ZoneInfo('Asia/Kolkata')
GLOBAL_SCOPE = '_global'
TOKEN_KEY = 'fyers_token'
CIRCUIT_KEY = 'fyers_auth_circuit'
BUDGET_KEY = 'fyers_budget'
TRIAL_BUDGET_KEY = 'fyers_budget_trial'


class TokenStoreError(RuntimeError):
    """Classified token-store failure (never contains a secret)."""


def generate_key():
    return Fernet.generate_key().decode()


def get_enc_key(env=None):
    env = os.environ if env is None else env
    return env.get('TOKEN_ENC_KEY')


class TokenStore:
    """Token/circuit/budget kv access over a StateStore."""

    def __init__(self, store, enc_key=None, env=None):
        self.store = store
        self.enc_key = enc_key if enc_key is not None else get_enc_key(env)

    def _kv_get(self, key):
        rows = self.store.execute(
            "SELECT value FROM paper_runtime_kv WHERE scope=? AND key=?",
            (GLOBAL_SCOPE, key))
        return rows[0]['value'] if rows else None

    def _kv_put(self, key, value, updated_at):
        self.store.execute(
            "INSERT INTO paper_runtime_kv(scope,key,value,updated_at) VALUES(?,?,?,?)"
            " ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value,"
            " updated_at=excluded.updated_at",
            (GLOBAL_SCOPE, key, value, updated_at))

    # -- token --
    def get_token(self):
        """Decrypted token dict, or None (missing, no key, wrong key, corrupt).

        Fails closed: never raises for a bad key, never returns plaintext.
        """
        raw = self._kv_get(TOKEN_KEY)
        if raw is None or not self.enc_key:
            return None
        try:
            plain = Fernet(self.enc_key.encode()).decrypt(raw.encode()).decode()
            data = json.loads(plain)
        except (InvalidToken, ValueError, TypeError, Exception):
            return None
        if not isinstance(data, dict) or not data.get('access_token'):
            return None
        return data

    def put_token(self, token):
        if not self.enc_key:
            raise TokenStoreError('token_enc_key_missing')
        try:
            fernet = Fernet(self.enc_key.encode())
        except Exception:
            raise TokenStoreError('token_enc_key_invalid') from None
        data = dict(token)
        data['issued_at'] = datetime.now(IST).isoformat()
        raw = fernet.encrypt(json.dumps(data).encode()).decode()
        self._kv_put(TOKEN_KEY, raw, data['issued_at'])
        return data

    # -- auth circuit --
    def get_circuit(self):
        raw = self._kv_get(CIRCUIT_KEY)
        if raw is None:
            return {'open': False, 'at': None}
        try:
            data = json.loads(raw)
        except ValueError:
            return {'open': False, 'at': None}
        return {'open': bool(data.get('open')), 'at': data.get('at')}

    def put_circuit(self, is_open, at=None):
        at = at or datetime.now(IST).isoformat()
        self._kv_put(CIRCUIT_KEY, json.dumps({'open': bool(is_open), 'at': at}), at)
        return {'open': bool(is_open), 'at': at}

    # -- budget --
    def get_budget(self, trial=False):
        raw = self._kv_get(TRIAL_BUDGET_KEY if trial else BUDGET_KEY)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def put_budget(self, state, trial=False):
        updated = state.get('updated_at') or datetime.now(IST).isoformat()
        self._kv_put(TRIAL_BUDGET_KEY if trial else BUDGET_KEY,
                     json.dumps(state), updated)


def open_token_store(env=None):
    """TokenStore over the render store (PAPER_TEST_DATABASE or Turso env)."""
    return TokenStore(rs.open_store_from_env(env))
