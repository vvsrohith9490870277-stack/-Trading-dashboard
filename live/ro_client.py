"""Python wrapper around turso_ro.mjs (SELECT-only, read transaction). Lives outside the adaptive library so the
adaptive modules never import a DB client (adaptive_guard forbidden-import rule)."""
import json, subprocess, time
from pathlib import Path
HERE = Path(__file__).resolve().parent

class TursoRO:
    """SELECT-only reads. A failed or timed-out read is retried (reads are side-effect free), so one transient
    Turso/network error no longer aborts a whole EOD shadow run (seen 11 Nov: turso_read_failed in wait_for_eod)."""
    def __init__(self, timeout=180, retries=2, backoff_seconds=3.0, sleep=time.sleep, run=subprocess.run):
        self.timeout, self.retries, self.backoff_seconds, self.sleep, self.run = timeout, retries, backoff_seconds, sleep, run
    def _once(self, req):
        try:
            r = self.run(['node', str(HERE / 'turso_ro.mjs')], input=json.dumps(req), text=True,
                         capture_output=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError('turso_read_timeout') from None
        if r.returncode:
            try: code = json.loads(r.stderr.strip().splitlines()[-1]).get('code', 'turso_read_failed')
            except Exception: code = 'turso_read_failed'
            raise RuntimeError(code)
        return json.loads(r.stdout)
    def many(self, queries):
        req = {'queries': [{'sql': s, 'args': list(a)} for s, a in queries]}
        for attempt in range(self.retries + 1):
            try:
                return self._once(req)
            except RuntimeError as ex:
                if attempt >= self.retries or str(ex).startswith('ro_'):  # local guard refusals are not transient
                    raise
                self.sleep(self.backoff_seconds * (attempt + 1))
    def query(self, sql, args=()):
        return self.many([(sql, args)])[0]
    def batch(self, statements):
        raise PermissionError('turso_read_only_client')
