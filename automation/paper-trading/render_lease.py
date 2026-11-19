"""Runner lease, handover flag and box guard (Task 4, brief render-deploy-01).

One supervisor at a time owns the lease for a namespace. The lease row lives
in Turso (paper_runner_lease); the TTL is 90 s and the holder renews every
20 s. A takeover increments the epoch so a stale holder can detect it. The
handover flag (paper_runtime_kv key 'active_runner', scope = the namespace)
names the active runner: 'box' or 'render'.
"""
import os
import socket
import time

LEASE_TTL_SECONDS = 90
LEASE_RENEW_SECONDS = 20
LEASE_LOST_SECONDS = 60  # renewal failing this long -> the lease is lost


def make_owner_id(kind='render'):
    """owner_id = render:<RENDER_INSTANCE_ID or hostname>:<pid>:<start epoch>"""
    host = os.environ.get('RENDER_INSTANCE_ID') or socket.gethostname()
    return '%s:%s:%d:%d' % (kind, host, os.getpid(), int(time.time()))


_NOW = "CAST(strftime('%s','now') AS INTEGER)"  # server time, never local time

_ACQUIRE_SQL = (
    'INSERT INTO paper_runner_lease'
    '(namespace, owner_id, owner_kind, epoch, acquired_unix, expires_unix,'
    ' heartbeat_at, detail_json) '
    'VALUES(?, ?, ?, 1, %s, %s + ?, ?, ?) '
    'ON CONFLICT(namespace) DO UPDATE SET '
    'owner_id = excluded.owner_id, '
    'owner_kind = excluded.owner_kind, '
    'epoch = CASE WHEN paper_runner_lease.owner_id = excluded.owner_id '
    'THEN paper_runner_lease.epoch ELSE paper_runner_lease.epoch + 1 END, '
    'acquired_unix = CASE WHEN paper_runner_lease.owner_id = excluded.owner_id '
    'THEN paper_runner_lease.acquired_unix ELSE %s END, '
    'expires_unix = %s + ?, '
    'heartbeat_at = excluded.heartbeat_at, '
    'detail_json = excluded.detail_json '
    'WHERE paper_runner_lease.expires_unix < %s '
    'OR paper_runner_lease.owner_id = excluded.owner_id'
    % (_NOW, _NOW, _NOW, _NOW, _NOW))


class Lease:
    """One lease row for one namespace, held by one owner_id."""

    def __init__(self, store, namespace, owner_id, owner_kind='render'):
        self.store = store
        self.namespace = namespace
        self.owner_id = owner_id
        self.owner_kind = owner_kind
        self.held = False

    def _try_acquire(self, detail_json=''):
        now_iso = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())
        n = self.store.affected_rows(
            _ACQUIRE_SQL,
            (self.namespace, self.owner_id, self.owner_kind,
             LEASE_TTL_SECONDS, now_iso, detail_json, LEASE_TTL_SECONDS))
        self.held = n == 1
        return self.held

    def acquire(self, detail_json=''):
        """Take the lease (first holder) or take it over after expiry."""
        return self._try_acquire(detail_json)

    def renew(self, detail_json=''):
        """Renew the lease I hold. False means another owner took it over."""
        return self._try_acquire(detail_json)

    def release(self):
        """Give the lease up now so a standby can take over immediately."""
        # expires_unix is set strictly in the past: a takeover in the same
        # second must see the lease as expired.
        n = self.store.affected_rows(
            'UPDATE paper_runner_lease SET expires_unix = %s - 1 '
            'WHERE namespace=? AND owner_id=?' % _NOW,
            (self.namespace, self.owner_id))
        self.held = False
        return n == 1


def get_lease(store, namespace):
    rows = store.execute(
        'SELECT namespace, owner_id, owner_kind, epoch, acquired_unix,'
        ' expires_unix, heartbeat_at, detail_json FROM paper_runner_lease'
        ' WHERE namespace=?', (namespace,))
    return rows[0] if rows else None


def get_active_runner(store, namespace):
    """The handover flag for a namespace: 'box', 'render', or None (unset)."""
    rows = store.execute(
        'SELECT value FROM paper_runtime_kv WHERE scope=? AND key=?',
        (namespace, 'active_runner'))
    return rows[0]['value'] if rows else None


def set_active_runner(store, namespace, value):
    """Set the handover flag. Only render_ctl handoff writes this."""
    now_iso = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())
    store.execute(
        'INSERT INTO paper_runtime_kv(scope, key, value, updated_at)'
        ' VALUES(?, ?, ?, ?)'
        ' ON CONFLICT(scope, key) DO UPDATE SET value=excluded.value,'
        ' updated_at=excluded.updated_at',
        (namespace, 'active_runner', value, now_iso))
