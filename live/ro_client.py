"""Python wrapper around turso_ro.mjs (SELECT-only, read transaction). Lives outside the adaptive library so the
adaptive modules never import a DB client (adaptive_guard forbidden-import rule)."""
import json, subprocess
from pathlib import Path
HERE = Path(__file__).resolve().parent

class TursoRO:
    def __init__(self, timeout=180):
        self.timeout = timeout
    def many(self, queries):
        req = {'queries': [{'sql': s, 'args': list(a)} for s, a in queries]}
        r = subprocess.run(['node', str(HERE / 'turso_ro.mjs')], input=json.dumps(req), text=True,
                           capture_output=True, timeout=self.timeout)
        if r.returncode:
            try: code = json.loads(r.stderr.strip().splitlines()[-1]).get('code', 'turso_read_failed')
            except Exception: code = 'turso_read_failed'
            raise RuntimeError(code)
        return json.loads(r.stdout)
    def query(self, sql, args=()):
        return self.many([(sql, args)])[0]
    def batch(self, statements):
        raise PermissionError('turso_read_only_client')
