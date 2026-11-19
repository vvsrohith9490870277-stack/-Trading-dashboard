"""Trial-mode rules (Task 10): namespace resolution, login gate, trial caps,
plus the trial lifecycle (init / compare / purge) that needs the state store.

The resolve/login/caps core stays pure (no I/O) so the resolver and its
tests never touch the network or the database; the lifecycle helpers below
take an explicit store/db handle.
"""
from __future__ import annotations
import logging
import os
import re
import time
from datetime import date

log = logging.getLogger('render.trial')

TRIAL_RE = re.compile(r'^paper_render_trial_[a-z0-9_]{1,24}$')
PROTECTED = frozenset({
    'paper_v1', 'paper_v2', 'paper_demo_v1', 'paper_exit_test_v1', 'adaptive_v1',
})
# Hard trial Fyers caps (brief section 3). Env may lower, never raise.
TRIAL_DAY_CAP = 1000
TRIAL_MINUTE_CAP = 8
TRIAL_SECOND_CAP = 1


class NamespaceRefused(Exception):
    """Raised when a mode/namespace/override combination breaks section 3."""


def is_trial_namespace(name):
    return bool(name) and bool(TRIAL_RE.match(str(name)))


def resolve(mode, cfg_namespace, override, runtime_is_render):
    """Return the namespace to run under, or raise NamespaceRefused.

    mode: 'live' or 'trial'. cfg_namespace: config.json namespace.
    override: PAPER_NAMESPACE_OVERRIDE or None. runtime_is_render: whether
    PAPER_RUNTIME=render is set. When the runtime is not render, the override
    is ignored and logged and the config namespace is returned unchanged.
    Refusal happens before any DB write; callers exit with code 3.
    """
    if not runtime_is_render:
        if override:
            log.warning('PAPER_NAMESPACE_OVERRIDE ignored: PAPER_RUNTIME is not render '
                        '(override=%s, namespace=%s)', override, cfg_namespace)
        return cfg_namespace
    if mode == 'live':
        if override:
            raise NamespaceRefused(
                'live mode refuses PAPER_NAMESPACE_OVERRIDE (%s)' % override)
        if is_trial_namespace(cfg_namespace):
            raise NamespaceRefused(
                'live mode refuses trial namespace %s' % cfg_namespace)
        if cfg_namespace in ('paper_v1', 'paper_demo_v1'):  # refused: archived namespaces
            # A Render deploy of a main that still carries the archive
            # namespace must never start (brief section 3).
            raise NamespaceRefused(
                'live mode refuses archived namespace %s' % cfg_namespace)
        return cfg_namespace
    if mode == 'trial':
        if not override:
            raise NamespaceRefused(
                'trial mode requires PAPER_NAMESPACE_OVERRIDE')
        if override in PROTECTED or override == cfg_namespace:
            raise NamespaceRefused(
                'trial mode refuses protected/config namespace %s' % override)
        if not TRIAL_RE.match(override):
            raise NamespaceRefused(
                'trial mode refuses namespace %r (must match %s)'
                % (override, TRIAL_RE.pattern))
        return override
    raise NamespaceRefused('unknown RENDER_MODE %r' % (mode,))


def login_allowed(mode, env=None):
    """True only in live mode with FYERS_AUTO_LOGIN=1 and all login creds present.

    Always False in trial mode, even with credentials in the environment.
    """
    env = os.environ if env is None else env
    if mode != 'live':
        return False
    if str(env.get('FYERS_AUTO_LOGIN', '')) != '1':
        return False
    return all(env.get(k) for k in (
        'FYERS_CLIENT_ID', 'FYERS_SECRET_ID', 'FYERS_PIN', 'FYERS_TOTP_SECRET'))


def trial_caps(env=None):
    """Trial Fyers caps. RENDER_TRIAL_DAY_CAP may lower the day cap, never raise it."""
    env = os.environ if env is None else env
    try:
        day = int(env.get('RENDER_TRIAL_DAY_CAP') or TRIAL_DAY_CAP)
    except (TypeError, ValueError):
        day = TRIAL_DAY_CAP
    # Literal 1000 (= TRIAL_DAY_CAP): the hard maximum, never raisable by env.
    return {
        'day': min(1000, max(0, day)),
        'minute': TRIAL_MINUTE_CAP,
        'second': TRIAL_SECOND_CAP,
        'reserve': 0,
    }


# ---------------------------------------------------------------------------
# Task 10: trial lifecycle (init / compare / purge)
# ---------------------------------------------------------------------------

def _trial_now_iso():
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())


class BarRefused(Exception):
    """A --bar that is not a verified completed NSE session."""


def _read_watermarks(db, namespace):
    """{(strategy, symbol): bar_date} from paper_watermarks."""
    rows = db.query(
        'SELECT strategy, symbol, bar_date FROM paper_watermarks'
        ' WHERE namespace=?', (namespace,))
    return {(r['strategy'], r['symbol']): r['bar_date'] for r in rows}


def compare_watermarks(db, trial_ns, live_ns='paper_v2'):
    """Compare bar_date per (strategy, symbol) between two namespaces.

    Read-only. `rows_compared` counts every (strategy, symbol) key in
    either namespace; `differences` lists the first 10 mismatches
    (a key missing on one side counts as a mismatch, with None).
    """
    live = _read_watermarks(db, live_ns)
    trial = _read_watermarks(db, trial_ns)
    diffs = []
    for key in sorted(set(live) | set(trial)):
        if live.get(key) != trial.get(key):
            diffs.append({'strategy': key[0], 'symbol': key[1],
                          'live_bar_date': live.get(key),
                          'trial_bar_date': trial.get(key)})
    return {'watermarks_match_paper_v2': not diffs,
            'rows_compared': len(set(live) | set(trial)),
            'differences': diffs[:10]}


def validate_bar(bar, latest_completed_day):
    """Raise BarRefused unless `bar` is a verified completed NSE session
    on or before `latest_completed_day` (both 'YYYY-MM-DD').

    A verified completed session is a day the engine calendar has a
    session for (not a weekend, holiday or unverified date) that is not
    later than the real completed day.
    """
    import engine as e
    try:
        day = date.fromisoformat(bar)
    except (TypeError, ValueError):
        raise BarRefused('bar %r is not YYYY-MM-DD' % (bar,))
    if not e.session(day):
        raise BarRefused(
            'bar %s is not a verified NSE session day' % bar)
    if bar > latest_completed_day:
        raise BarRefused(
            'bar %s is later than the latest completed day %s'
            % (bar, latest_completed_day))
    return day


class _NoNetworkApi:
    """Stand-in for MarketData in bootstrap: with do_sync=False the
    bootstrap path must never touch the network, so any api call is a
    bug and fails loudly instead of reaching Fyers."""

    def __getattr__(self, name):
        raise AssertionError(
            'trial-init bootstrap must not call api.%s' % name)


def _run_bootstrap(db, namespace, bar):
    """Run the real `bootstrap` code path for `namespace` at `bar`.

    Patches `engine.completed_day` (and `engine.NS`) in this process
    only to return the bar; no engine code changes, no --sync, no
    Fyers or network call (the api object above raises on any use).
    The patch is restored even when bootstrap raises.
    """
    import engine as e
    target = date.fromisoformat(bar)
    old_ns, old_completed = e.NS, e.completed_day
    try:
        e.NS = namespace
        e.completed_day = lambda t: target
        return e.scan(db, _NoNetworkApi(), do_sync=False, bootstrap=True)
    finally:
        e.NS, e.completed_day = old_ns, old_completed


def init_trial(store, db, namespace, bar, live_ns='paper_v2',
               latest_completed_day=None):
    """Baseline a trial namespace at `bar` (brief 01 Task 10.5).

    One code path for the real and the --fixture modes; only the store
    (and the trading DB handle) differ. Steps, in order:
    1. refuse anything that is not a trial namespace (before any write);
    2. refuse a bar that is not a verified completed session on or
       before the latest completed day (the real completed day, unless
       the caller passes one — the fixture passes its own);
    3. run the real bootstrap for the trial namespace, unless it is
       already bootstrapped (watermarks exist) — a second run still
       runs step 5;
    4. record the init marker in kv (trial scope) and zero the trial
       budget (its own key; the box budget is never touched);
    5. read the live namespace's paper_watermarks and compare bar_date
       per strategy and symbol with the trial's.
    Writes only the trial namespace's rows (plus the shared audit and
    qualifier rows the bootstrap itself writes under that namespace).
    Returns {namespace, bar, bootstrapped, already_bootstrapped,
    watermarks_match_paper_v2, rows_compared, differences}.
    The caller exits 2 on a mismatch.
    """
    if not is_trial_namespace(namespace):
        raise NamespaceRefused(
            'trial-init refuses non-trial namespace %s' % namespace)
    if bar is None:
        raise BarRefused('trial-init needs --bar YYYY-MM-DD')
    if latest_completed_day is None:
        import engine as e
        latest_completed_day = e.completed_day(e.now()).isoformat()
    validate_bar(bar, latest_completed_day)
    already = bool(_read_watermarks(db, namespace))
    bootstrapped = False
    if not already:
        _run_bootstrap(db, namespace, bar)
        bootstrapped = True
    now_iso = _trial_now_iso()
    store.execute(
        'INSERT INTO paper_runtime_kv(scope, key, value, updated_at)'
        ' VALUES(?, ?, ?, ?)'
        ' ON CONFLICT(scope, key) DO UPDATE SET value=excluded.value,'
        ' updated_at=excluded.updated_at',
        (namespace, 'trial_init', now_iso, now_iso))
    # zero the trial budget (own key; the box budget is never touched)
    import token_store as ts
    try:
        enc = ts.get_enc_key()
        if enc is not None:
            tstore = ts.TokenStore(store, enc, {})
            tstore.put_budget(
                {'day': now_iso[:10], 'requests': 0, 'by_route': {},
                 'times': []}, trial=True)
    except Exception:
        pass  # no enc key yet: the budget row is created on first use
    out = {'namespace': namespace, 'bar': bar,
           'bootstrapped': bootstrapped,
           'already_bootstrapped': already}
    out.update(compare_watermarks(db, namespace, live_ns))
    return out


def compare_trial(db, trial_ns, live_ns, day):
    """Compare trial vs live for one session day.

    `db` is an engine DB (or a fake with .query). Counts are read-only and
    scoped to each namespace, so the comparison can never leak one book
    into the other.
    """
    def _count(table, ns, day_col=None):
        if day_col == 'filled_at':
            # filled_at is a full timestamp; compare its date part
            rows = db.query(
                'SELECT COUNT(*) AS n FROM %s WHERE namespace=?'
                ' AND substr(filled_at, 1, 10)=?' % table, (ns, day))
        elif day_col:
            rows = db.query(
                'SELECT COUNT(*) AS n FROM %s WHERE namespace=? AND %s=?'
                % (table, day_col), (ns, day))
        else:
            rows = db.query(
                'SELECT COUNT(*) AS n FROM %s WHERE namespace=?' % table,
                (ns,))
        return int(rows[0]['n']) if rows else 0

    out = {'day': day}
    for label, ns in (('trial', trial_ns), ('live', live_ns)):
        out[label] = {
            'signals': _count('paper_signals', ns, 'bar_date'),
            'fills': _count('paper_fills', ns, 'filled_at'),
            'positions': _count('paper_positions', ns),
        }
    return out


def purge_trial(store, namespace, db=None):
    """Delete everything a trial run wrote, and nothing else.

    Removes the trial namespace's kv rows, lease row and job runs from the
    state store, and (when `db` is given) its trading rows from the paper
    tables. Refuses any namespace that is not a trial namespace.
    """
    if not is_trial_namespace(namespace):
        raise NamespaceRefused(
            'trial-purge refuses non-trial namespace %s' % namespace)
    deleted = {}
    store.execute(
        'DELETE FROM paper_runtime_kv WHERE scope=?', (namespace,))
    deleted['kv'] = True
    store.execute(
        'DELETE FROM paper_runner_lease WHERE namespace=?', (namespace,))
    deleted['lease'] = True
    store.execute(
        'DELETE FROM paper_job_runs WHERE namespace=?', (namespace,))
    deleted['job_runs'] = True
    if db is not None:
        for table in ('paper_signals', 'paper_positions', 'paper_fills',
                      'paper_pnl', 'paper_runs', 'paper_quote_audit',
                      'paper_scan_batches', 'paper_watermarks',
                      'paper_qualifier_states', 'paper_prefilter_states',
                      'paper_meta', 'paper_outages'):
            try:
                db.batch([('DELETE FROM %s WHERE namespace=?' % table,
                           (namespace,))])
            except Exception:
                pass  # table may not exist on an old DB; purge continues
        deleted['trading_tables'] = True
    return {'ok': True, 'namespace': namespace, 'deleted': deleted}
