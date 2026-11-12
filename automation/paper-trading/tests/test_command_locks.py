import sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import engine as e

class CommandLockTests(unittest.TestCase):
    def test_read_only_reporting_skips_engine_lock(self):
        for cmd in ('trades-today', 'status', 'positions', 'pnl', 'charges', 'finance', 'overlap', 'funnel'):
            self.assertFalse(e.uses_engine_lock(cmd), cmd)

    def test_reference_feeds_skips_engine_lock(self):
        self.assertFalse(e.uses_engine_lock('reference-feeds'))
        self.assertFalse(e.uses_engine_lock('exit-selftest'))

    def test_scan_and_other_writers_still_take_engine_lock(self):
        for cmd in ('scan', 'monitor-once', 'probe', 'eod', 'sync-history', 'bootstrap', 'screen-only'):
            self.assertTrue(e.uses_engine_lock(cmd), cmd)

    def test_supervision_commands_remain_unlocked(self):
        for cmd in ('worker', 'ensure-monitor', 'health', 'preflight', 'ensure-session', 'guardian', 'replay-audit', 'export-logs', 'support'):
            self.assertFalse(e.uses_engine_lock(cmd), cmd)

if __name__ == '__main__':
    unittest.main()
