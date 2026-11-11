import csv, io, json, os, tempfile, unittest, zipfile
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import official_close as oc
import engine as e
import automation as a
import audit as al
import test_engine as fixtures
T = fixtures.T

def zip_of(rows, name='cm.csv'):
    buf = io.BytesIO()
    fields = ['FinInstrmTp', 'SctySrs', 'TradDt', 'TckrSymb', 'ClsPric']
    with zipfile.ZipFile(buf, 'w') as z:
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in fields})
        z.writestr(name, out.getvalue())
    return buf.getvalue()

ROW = dict(FinInstrmTp='STK', SctySrs='EQ', TradDt='2026-11-11', TckrSymb='RELIANCE', ClsPric='110.50')

class OfficialCloseParseTests(unittest.TestCase):
    def test_parse_maps_hyphenated_ticker_and_prefers_eq(self):
        raw = zip_of([
            dict(FinInstrmTp='STK', SctySrs='BE', TradDt='2026-11-11', TckrSymb='BAJAJ-AUTO', ClsPric='1.00'),
            dict(FinInstrmTp='STK', SctySrs='EQ', TradDt='2026-11-11', TckrSymb='BAJAJ-AUTO', ClsPric='9000.15'),
            dict(FinInstrmTp='FUT', SctySrs='EQ', TradDt='2026-11-11', TckrSymb='RELIANCE', ClsPric='1.00'),
            ROW,
        ])
        closes, n = oc.parse_udiff(raw, '2026-11-11')
        self.assertEqual(n, 4)
        self.assertEqual(closes['BAJAJ-AUTO-EQ'], Decimal('9000.15'))
        self.assertEqual(closes['RELIANCE-EQ'], Decimal('110.50'))
        self.assertNotIn('RELIANCE-FUT', closes)
        # Each series keeps its own close; EQ is preferred only if a later caller asks for a single ticker.
        self.assertEqual(closes['BAJAJ-AUTO-BE'], Decimal('1.00'))

    def test_trad_dt_mismatch_is_unavailable(self):
        raw = zip_of([dict(ROW, TradDt='2026-11-10')])
        with self.assertRaises(ValueError) as ctx:
            oc.parse_udiff(raw, '2026-11-11')
        self.assertIn('udiff_trade_date_mismatch', str(ctx.exception))

    def test_closes_for_incomplete_or_failed_stays_unavailable(self):
        e_ = type('E', (), {'CFG': json.loads((Path(__file__).resolve().parents[1] / 'config.json').read_text()), 'ROOT': Path('.')})()
        raw = zip_of([ROW])
        r = oc.closes_for(e_, '2026-11-11', ['RELIANCE-EQ', 'DLF-EQ'], fetch=lambda *a, **k: raw)
        self.assertEqual(r['status'], 'unavailable')
        self.assertEqual(r['reason'], 'official_close_incomplete')
        self.assertEqual(r['missing'], ['DLF-EQ'])
        self.assertEqual(r['source'], 'NSE_BHAVCOPY_UDIFF')
        r2 = oc.closes_for(e_, '2026-11-11', ['RELIANCE-EQ'], fetch=lambda *a, **k: (_ for _ in ()).throw(RuntimeError('official_close_http_404')))
        self.assertEqual(r2['status'], 'unavailable')
        self.assertEqual(r2['closes'], {})
        self.assertIsNone(r2['source'])
        r3 = oc.closes_for(e_, '2026-11-11', ['RELIANCE-EQ'], fetch=lambda *a, **k: zip_of([dict(ROW, TradDt='2026-11-10')]))
        self.assertEqual(r3['status'], 'unavailable')
        self.assertTrue(str(r3['reason']).startswith('udiff_trade_date_mismatch'))

    def test_closes_for_returns_labelled_official_prices(self):
        e_ = type('E', (), {'CFG': json.loads((Path(__file__).resolve().parents[1] / 'config.json').read_text()), 'ROOT': Path('.')})()
        r = oc.closes_for(e_, '2026-11-11', ['RELIANCE-EQ'], fetch=lambda *a, **k: zip_of([ROW]))
        self.assertEqual(r['status'], 'ok')
        self.assertEqual(r['source'], 'NSE_BHAVCOPY_UDIFF')
        self.assertEqual(r['closes']['RELIANCE-EQ'], Decimal('110.50'))

class OfficialCloseEodTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.PaperTests.setUpClass.__func__(cls)
    def setUp(self):
        fixtures.PaperTests.setUp(self)
    seed_adv=fixtures.PaperTests.seed_adv
    signal=fixtures.PaperTests.signal
    quote=fixtures.PaperTests.quote
    def test_stale_marks_use_official_close_and_do_not_mutate_positions(self):
        e.fill(self.db, self.signal(), self.quote(100), T)
        before = self.db.query('SELECT mark_price,mark_at FROM paper_positions WHERE namespace=?', (e.NS,))[0]
        e.now = lambda: T.replace(hour=16, minute=10)
        raw = zip_of([dict(ROW, TradDt=str(T.date()), TckrSymb='RELIANCE', ClsPric='110')])
        with patch.object(a.oc, 'fetch_zip', side_effect=lambda *a, **k: raw):
            r = a.eod(e, self.db)
        pos = self.db.query('SELECT qty,entry_price FROM paper_positions WHERE namespace=?', (e.NS,))[0]
        self.assertTrue(r['official_close'])
        self.assertEqual(r['official_close_source'], 'NSE_BHAVCOPY_UDIFF')
        self.assertEqual(r['official_close_status'], 'applied')
        self.assertTrue(r['closing_valuation_complete'])
        expected=int(((Decimal('110')-Decimal(str(pos['entry_price'])))*int(pos['qty'])*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
        self.assertEqual(r['daily_gross_paise'], expected)
        self.assertGreater(r['daily_gross_paise'], 0)
        after = self.db.query('SELECT mark_price,mark_at FROM paper_positions WHERE namespace=?', (e.NS,))[0]
        self.assertEqual(after['mark_price'], before['mark_price'])
        self.assertEqual(after['mark_at'], before['mark_at'])
        self.assertTrue(all(x['source'] == 'NSE_BHAVCOPY_UDIFF' for x in r['mark_observations']))
        self.assertEqual(len(self.db.query("SELECT * FROM paper_eod WHERE namespace=? AND day=?", (e.NS, str(T.date())))), 1)
        with patch.object(a.oc, 'fetch_zip', side_effect=lambda *a, **k: raw):
            second = a.eod(e, self.db)
        self.assertEqual(len(self.db.query("SELECT * FROM paper_eod WHERE namespace=? AND day=?", (e.NS, str(T.date())))), 1)
        self.assertEqual(second['daily_gross_paise'], r['daily_gross_paise'])
        self.assertTrue(second.get('previous_snapshot_as_of'))
    def test_unavailable_bhavcopy_leaves_daily_pnl_unavailable(self):
        e.fill(self.db, self.signal(), self.quote(100), T)
        e.now = lambda: T.replace(hour=16, minute=10)
        with patch.object(a.oc, 'closes_for', return_value={'status': 'unavailable', 'reason': 'official_close_http_404', 'closes': {}, 'source': None}):
            r = a.eod(e, self.db)
        self.assertFalse(r['official_close'])
        self.assertFalse(r['closing_valuation_complete'])
        self.assertIsNone(r['daily_gross_paise'])
        self.assertEqual(r['official_close_status'], 'unavailable')

class AuditExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name); (self.root / 'out').mkdir(); (self.root / 'logs').mkdir()
        self.e = type('E', (), {})(); self.e.ROOT = self.root; self.e.NS = 'test_export'
        self.e.now = lambda: datetime(2026, 11, 11, 16, 40, tzinfo=e.IST)
        self.e.session = lambda d: None
        self.e.CFG = {'poll_seconds': 30}
        self.db = _Mem()
        al.init(self.db)
    def tearDown(self):
        self.tmp.cleanup()
    def test_recreates_if_missing_and_does_not_invent_events(self):
        path = self.root / 'out' / 'audit-2026-11-11.jsonl'
        self.assertFalse(path.exists())
        r = al.ensure_daily_export(self.e, self.db, '2026-11-11')
        self.assertEqual(r['status'], 'ok')
        self.assertTrue(r['rebuilt'])
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(), '')  # no invented events; empty source
        self.assertEqual(r['events'], 0)
        al.write(self.e, self.db, [al.event(self.e, 'monitor_idle')])
        # existing non-empty file is left alone unless force
        unchanged = path.read_text()
        r2 = al.ensure_daily_export(self.e, self.db, '2026-11-11')
        self.assertFalse(r2['rebuilt'])
        self.assertEqual(path.read_text(), unchanged)
        r3 = al.ensure_daily_export(self.e, self.db, '2026-11-11', force=True)
        self.assertTrue(r3['rebuilt'])
        self.assertEqual(r3['events'], 1)
        line = json.loads(path.read_text().splitlines()[0])
        self.assertEqual(line['event_type'], 'monitor_idle')
        self.assertEqual(line['namespace'], 'test_export')
    def test_export_failure_is_marked_unavailable(self):
        class Boom:
            def query(self, *a, **k):
                raise RuntimeError('TURSO_OPERATION_FAILED')
        r = al.ensure_daily_export(self.e, Boom(), '2026-11-11')
        self.assertEqual(r['status'], 'unavailable')
        self.assertFalse((self.root / 'out' / 'audit-2026-11-11.jsonl').exists())
        marker = Path(r['marker'])
        self.assertTrue(marker.exists())
        self.assertEqual(json.loads(marker.read_text())['error_code'], 'TURSO_OPERATION_FAILED')

class TaxonomyTests(unittest.TestCase):
    def test_classified_error_keeps_known_tokens(self):
        self.assertEqual(al.classified_error(RuntimeError('market_data_rate_limit')), 'market_data_rate_limit')
        self.assertEqual(al.classified_error(RuntimeError('Turso_operation_failed')), 'Turso_operation_failed')
        self.assertEqual(al.classified_error(RuntimeError('TURSO_OPERATION_FAILED')), 'TURSO_OPERATION_FAILED')
        self.assertEqual(al.clean('Turso_operation_failed', 'error_code'), 'Turso_operation_failed')
        self.assertEqual(al.clean('a mystery with spaces', 'error_code'), 'unclassified_condition')

class _Mem:
    def __init__(self):
        import sqlite3
        self.c = sqlite3.connect(':memory:')
        self.c.row_factory = sqlite3.Row
        self.test_database = 'memory'
    def batch(self, stmts):
        for s, a in stmts:
            self.c.execute(s, a)
        self.c.commit()
    def query(self, sql, args=()):
        return [dict(r) for r in self.c.execute(sql, args).fetchall()]

if __name__ == '__main__':
    unittest.main()
