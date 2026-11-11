"""CLI wiring for tonight's routines, Support Agent 403/gap detection, isolation, calendar days, circuit parse."""
import json, os, sys, tempfile, unittest, zipfile, io
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / 'automation' / 'paper-trading'))

import adaptive_runner as ar
import bhavcopy_backfill as bb
import circuit_bands as cb
import support_runner as sr
import support_agent as sa

IST = ZoneInfo('Asia/Kolkata')


class RoutineCommandLines(unittest.TestCase):
    def test_scripts_exist_and_are_executable(self):
        for name in ('run_adaptive.sh', 'run_support.sh'):
            p = ROOT / name
            self.assertTrue(p.is_file(), name)
            self.assertTrue(os.access(p, os.X_OK), name)
            text = p.read_text()
            self.assertIn('Asia/Kolkata', text)
            self.assertTrue((Path('/workspace/sahil-fyers-turso/.venv/bin/python')).exists() or 'ADAPTIVE_PYTHON' in text)

    def test_eod_and_backfill_argv_used_by_routines(self):
        p = ar.build_parser()
        eod = p.parse_args(['eod', '--session', '2026-11-11'])
        self.assertEqual(eod.phase, 'eod')
        self.assertEqual(eod.session, '2026-11-11')
        self.assertFalse(eod.no_wait)
        eod2 = p.parse_args(['eod', '--session', '2026-11-11', '--no-wait'])
        self.assertTrue(eod2.no_wait)
        bf = p.parse_args(['backfill', '--session', '2026-11-11', '--from', '2026-06-02', '--to', '2026-11-11'])
        self.assertEqual(bf.phase, 'backfill')
        self.assertEqual(bf.frm, '2026-06-02')
        self.assertEqual(bf.to, '2026-11-11')
        self.assertEqual(bf.session, '2026-11-11')
        st = p.parse_args(['status'])
        self.assertEqual(st.phase, 'status')
        pf = p.parse_args(['preflight'])
        self.assertEqual(pf.phase, 'preflight')

    def test_support_argv(self):
        a = sr.build_parser().parse_args(['--session', '2026-11-11'])
        self.assertEqual(a.session, '2026-11-11')
        self.assertIsNone(a.now)
        b = sr.build_parser().parse_args(['--session', '2026-11-11', '--now', '2026-11-11T15:10:00+05:30'])
        self.assertEqual(b.now, '2026-11-11T15:10:00+05:30')

    def test_backfill_requires_from_and_to_in_help(self):
        # runner itself refuses a backfill without --from/--to rather than passing None through
        self.assertIn('--from', ar.build_parser().format_help())


class Isolation(unittest.TestCase):
    def test_quiet_window_0900_1535(self):
        inside = datetime(2026, 11, 11, 12, 0, tzinfo=IST)
        edge_open = datetime(2026, 11, 11, 9, 0, tzinfo=IST)
        before = datetime(2026, 11, 11, 8, 59, tzinfo=IST)
        after = datetime(2026, 11, 11, 15, 35, tzinfo=IST)
        self.assertTrue(ar.in_market_quiet_window(inside))
        self.assertTrue(ar.in_market_quiet_window(edge_open))
        self.assertFalse(ar.in_market_quiet_window(before))
        self.assertFalse(ar.in_market_quiet_window(after))
        with self.assertRaises(RuntimeError) as ctx:
            ar.assert_not_market_hours('eod', now=inside)
        self.assertIn('0900_1535', str(ctx.exception))
        ar.assert_not_market_hours('eod', now=after)  # does not raise


class CalendarAndParse(unittest.TestCase):
    def test_session_days_skips_weekend_holiday_muhurat(self):
        days = bb.session_days('2026-11-06', '2026-11-11')
        self.assertEqual(days, ['2026-11-06', '2026-11-09', '2026-11-11'])  # 7-8 weekend/Muhurat, 10 holiday

    def test_parse_udiff_fixture_and_date_mismatch_is_no_session(self):
        # reuse the existing wiring helper shape
        hdr = ['TradDt', 'FinInstrmTp', 'TckrSymb', 'SctySrs', 'ISIN', 'OpnPric', 'HghPric', 'LwPric', 'ClsPric', 'TtlTradgVol', 'TtlTrfVal']
        def z(rows):
            s = io.StringIO(); s.write(','.join(hdr) + '\n')
            for r in rows: s.write(','.join(r) + '\n')
            b = io.BytesIO()
            with zipfile.ZipFile(b, 'w') as zf: zf.writestr('BhavCopy.csv', s.getvalue())
            return b.getvalue()
        ok = z([['2026-11-06', 'STK', 'RELIANCE', 'EQ', 'INE002A01018', '100', '101', '99', '100.5', '10', '20']])
        rows, raw = bb.parse_udiff(ok, '2026-11-06')
        self.assertEqual(raw, 1)
        self.assertEqual(rows[0]['TckrSymb'], 'RELIANCE')
        bad = z([['2026-11-05', 'STK', 'RELIANCE', 'EQ', 'INE002A01018', '100', '101', '99', '100.5', '10', '20']])
        with self.assertRaises(ValueError) as ctx:
            bb.parse_udiff(bad, '2026-11-06')
        self.assertIn('udiff_trade_date_mismatch', str(ctx.exception))

    def test_official_archive_candidate_urls_are_nse_only(self):
        cands = bb.official_archive_candidates('2026-07-23')
        forms = [c[0] for c in cands]
        self.assertEqual(forms, ['udiff', 'legacy_cm', 'sec_bhavdata_full'])
        urls = [c[1] for c in cands]
        self.assertTrue(all(u.startswith('https://nsearchives.nseindia.com/') for u in urls))
        self.assertIn('BhavCopy_NSE_CM_0_0_0_20260723_F_0000.csv.zip', urls[0])
        self.assertEqual(urls[1], 'https://nsearchives.nseindia.com/content/historical/EQUITIES/2026/JUL/cm23JUL2026bhav.csv.zip')
        self.assertEqual(urls[2], 'https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_23072026.csv')

    def test_parse_legacy_cm_and_sec_bhavdata_full_and_reject_mismatch(self):
        legacy_csv = (
            'SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN\n'
            'RELIANCE,EQ,1400,1410,1390,1405,1405,1399,1000,14000,23-JUL-2026,10,INE002A01018\n'
            'RELIANCE,BE,1,1,1,1,1,1,1,1,23-JUL-2026,1,INE002A01018\n'
        )
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, 'w') as zf:
            zf.writestr('cm23JUL2026bhav.csv', legacy_csv)
        with tempfile.TemporaryDirectory() as td:
            zp = Path(td) / 'cm23JUL2026bhav.csv.zip'
            zp.write_bytes(zbuf.getvalue())
            rows, raw, form, source = bb.parse_official_file(zp, '2026-07-23')
        self.assertEqual(form, 'legacy_cm')
        self.assertEqual(source, 'NSE_BHAVCOPY_LEGACY_CM')
        self.assertEqual(raw, 2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['symbol'], 'RELIANCE-EQ')
        self.assertEqual(rows[0]['trade_date'], '2026-07-23')
        self.assertEqual(rows[0]['close'], 1405.0)

        full = (
            'SYMBOL,SERIES,DATE1,PREV_CLOSE,OPEN_PRICE,HIGH_PRICE,LOW_PRICE,LAST_PRICE,CLOSE_PRICE,AVG_PRICE,TTL_TRD_QNTY,TURNOVER_LACS,NO_OF_TRADES,DELIV_QTY,DELIV_PER\n'
            'HFCL,EQ,14-AUG-2026,10,10,11,9,10.2,10.5,10.3,50,0.05,3,20,40\n'
        )
        with tempfile.TemporaryDirectory() as td:
            cp = Path(td) / 'sec_bhavdata_full_14082026.csv'
            cp.write_text(full)
            rows, raw, form, source = bb.parse_official_file(cp, '2026-08-14')
        self.assertEqual(form, 'sec_bhavdata_full')
        self.assertEqual(source, 'NSE_BHAVCOPY_SEC_BHAVDATA_FULL')
        self.assertEqual(rows[0]['symbol'], 'HFCL-EQ')
        self.assertEqual(rows[0]['trade_date'], '2026-08-14')
        self.assertEqual(rows[0]['close'], 10.5)

        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / 'cm21AUG2026bhav.csv.zip'
            zbuf = io.BytesIO()
            with zipfile.ZipFile(zbuf, 'w') as zf:
                zf.writestr('cm21AUG2026bhav.csv', legacy_csv)  # TradDt 23-JUL, requested 21-AUG
            bad.write_bytes(zbuf.getvalue())
            with self.assertRaises(ValueError) as ctx:
                bb.parse_official_file(bad, '2026-08-21')
            self.assertIn('trade_date_mismatch', str(ctx.exception))

    def test_obtain_offline_parses_cached_legacy_and_never_synthesizes(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            missing = bb.obtain_session_file('2026-07-23', td, offline=True)
            self.assertEqual(missing['status'], 'missing_offline')
            # Cached official file is used without a network call.
            csv_text = (
                'SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN\n'
                'TCS,EQ,3000,3010,2990,3005,3005,2999,10,30,23-JUL-2026,1,INE467B01029\n'
            )
            zbuf = io.BytesIO()
            with zipfile.ZipFile(zbuf, 'w') as zf:
                zf.writestr('cm23JUL2026bhav.csv', csv_text)
            (td / 'cm23JUL2026bhav.csv.zip').write_bytes(zbuf.getvalue())
            got = bb.obtain_session_file('2026-07-23', td, offline=True)
            self.assertEqual(got['status'], 'ok')
            self.assertEqual(got['form'], 'legacy_cm')
            self.assertTrue(got['from_cache'])
            self.assertEqual(got['rows'][0]['symbol'], 'TCS-EQ')


class SupportDetectors(unittest.TestCase):
    def test_reference_feed_403_from_engine_shape(self):
        ev = {
            'event_type': 'reference_feeds',
            'source_at': '2026-11-11T09:07:53+05:30',
            'data_json': json.dumps({
                'kind': 'reference_feeds',
                'trade_day': '2026-11-11',
                'sources': {
                    'corporate_actions': {'status': 'unavailable', 'error': 'http_403'},
                    'asm': {'status': 'unavailable', 'error': 'http_403'},
                    'gsm': {'status': 'unavailable', 'error': 'http_403'},
                    'series': {'status': 'ok', 'universe_symbols_not_eq': 0},
                },
            }),
        }
        hits = sa.reference_feed_403_hits(ev)
        self.assertEqual(sorted(h['feed'] for h in hits), ['asm', 'corporate_actions', 'gsm'])
        old = {'event_type': 'reference_feed_error', 'data_json': json.dumps({'feed': 'nse_asm', 'http_status': 403, 'error': 'forbidden'})}
        self.assertEqual(sa.reference_feed_403_hits(old)[0]['feed'], 'nse_asm')
        clean = {'event_type': 'reference_feeds', 'data_json': json.dumps({'sources': {'series': {'status': 'ok'}}})}
        self.assertEqual(sa.reference_feed_403_hits(clean), [])

    def test_engine_log_loader_filters_day_and_strips_secrets(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / 'engine.jsonl'
            rows = [
                {'at_IST': '2026-11-11T08:29:05+05:30', 'namespace': 'paper_v1', 'kind': 'reference_feeds',
                 'sources': {'asm': {'status': 'unavailable', 'error': 'http_403'}}, 'access_token': 'SHOULD_NOT_LEAK'},
                {'at_IST': '2026-11-09T17:03:13+05:30', 'namespace': 'paper_v1', 'kind': 'reference_feeds',
                 'sources': {'gsm': {'status': 'unavailable', 'error': 'http_403'}}},
            ]
            p.write_text(''.join(json.dumps(r) + '\n' for r in rows))
            evs = sr.engine_log_events(p, '2026-11-11', 'paper_v1')
            self.assertEqual(len(evs), 1)
            self.assertEqual(evs[0]['event_type'], 'reference_feeds')
            self.assertNotIn('SHOULD_NOT_LEAK', evs[0]['data_json'])
            self.assertNotIn('access_token', evs[0]['data_json'])
            hits = sa.reference_feed_403_hits(evs[0])
            self.assertEqual([h['feed'] for h in hits], ['asm'])

    def test_monitor_staleness_lists_each_gap(self):
        import tempfile as tf
        day = '2026-11-11'
        with tf.TemporaryDirectory() as t:
            trading = sa.SupportStore(Path(t) / 't.sqlite')  # has paper? no - use a trading-like store
        # Build a minimal trading sqlite with paper_events only via sa.ReadOnlyTradingDB needs the table.
        # Use SupportStore schema? Run v3 against a tiny sqlite created here.
        import sqlite3
        with tf.TemporaryDirectory() as t:
            path = Path(t) / 'tr.sqlite'
            c = sqlite3.connect(path)
            c.executescript(sa.SCHEMA)  # support schema only - need paper_events
            c.execute('CREATE TABLE paper_events(namespace TEXT,event_id TEXT,run_id TEXT,session_day TEXT,source_at TEXT,recorded_at TEXT,event_type TEXT,severity TEXT,outcome TEXT,strategy INTEGER,symbol TEXT,bar_date TEXT,signal_id TEXT,policy_version TEXT,input_policy_version TEXT,data_json TEXT,PRIMARY KEY(namespace,event_id))')
            c.execute('CREATE TABLE paper_decisions(namespace TEXT,seq INTEGER,correlation_id TEXT,decision_type TEXT,strategy INTEGER,symbol TEXT,signal_id TEXT,cause TEXT,rule TEXT,execution_path TEXT,source_at TEXT,recorded_at TEXT,prev_hash TEXT,entry_hash TEXT,payload_json TEXT)')
            c.execute('CREATE TABLE paper_fills(id INTEGER PRIMARY KEY,namespace TEXT,signal_id TEXT,strategy INTEGER,symbol TEXT,side TEXT,qty INTEGER,price REAL,quote_at TEXT,filled_at TEXT,reason TEXT,realized_pnl REAL,cashflow REAL,execution_path TEXT)')
            c.execute('CREATE TABLE paper_signals(id TEXT,namespace TEXT,created_at TEXT)')
            # late scan (completed 09:13) + three monitor gaps matching today's incidents
            evs = [
                ('scan_started', '2026-11-11T08:51:55+05:30'),
                ('scan_completed', '2026-11-11T09:13:40+05:30'),
                ('monitor_completed', '2026-11-11T09:17:14+05:30'),
                ('monitor_completed', '2026-11-11T09:18:49+05:30'),
                ('monitor_completed', '2026-11-11T09:22:54+05:30'),  # 245s after 09:18:49
                ('monitor_completed', '2026-11-11T09:33:13+05:30'),
                ('monitor_completed', '2026-11-11T09:35:19+05:30'),
                ('monitor_completed', '2026-11-11T09:41:17+05:30'),  # 358s
                ('monitor_completed', '2026-11-11T11:08:48+05:30'),
                ('monitor_completed', '2026-11-11T11:15:46+05:30'),  # 418s
                ('reference_feeds', '2026-11-11T09:07:53+05:30'),
            ]
            for i, (et, at) in enumerate(evs):
                data = '{}'
                if et == 'reference_feeds':
                    data = json.dumps({'sources': {'corporate_actions': {'status': 'unavailable', 'error': 'http_403'},
                                                   'asm': {'status': 'unavailable', 'error': 'http_403'},
                                                   'gsm': {'status': 'unavailable', 'error': 'http_403'}}})
                c.execute('INSERT INTO paper_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                          ('paper_v1', f'e{i}', 'r', day, at, at, et, 'INFO', 'ok', None, None, None, None, None, None, data))
            c.commit(); c.close()
            trading = sa.ReadOnlyTradingDB(path)
            store = sa.SupportStore(Path(t) / 'findings.sqlite')
            out = sa.run_v3_production_detectors(
                store, 'paper_v1', day, now_at='2026-11-11T15:10:00+05:30',
                trading_db=trading, observation_only=True,
            )
            dets = {r['detector'] for r in out['results'] if r.get('detector')}
            self.assertIn('scan_missed', dets)
            self.assertIn('monitor_staleness', dets)
            self.assertIn('feed_http_403', dets)
            findings = sa.current_findings(store, 'paper_v1', day)
            by = {f['detector']: f for f in findings}
            gaps = by['monitor_staleness']['details']['gaps']
            starts = {g['from_ist'] for g in gaps}
            self.assertIn('09:18:49', starts)
            self.assertIn('09:35:19', starts)
            self.assertIn('11:08:48', starts)
            feeds = {f['details']['feed'] for f in findings if f['detector'] == 'feed_http_403'}
            self.assertEqual(feeds, {'corporate_actions', 'asm', 'gsm'})
            # eod_missed must not fire at 15:10
            self.assertNotIn('eod_missed', dets)


class CircuitBands(unittest.TestCase):
    def test_parse_sec_list_and_infer(self):
        csv = 'Symbol,Series,Security Name,Band,Remarks\nAAA,EQ,Aaa Ltd,5,"-"\nBBB,EQ,Bbb Ltd,No Band,"F&O"\n'
        rows = cb.parse_sec_list(csv)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]['static_band'] and rows[0]['band_pct'] == 5)
        self.assertFalse(rows[1]['static_band'])
        inferred = cb.infer_circuit(5, 100, high=105, low=99, close=105)
        self.assertEqual(inferred['upper'], 105.0)
        self.assertEqual(inferred['circuit'], 'UPPER')
        self.assertIsNone(cb.infer_circuit(None, 100, high=105, low=95))
        doc = cb.document()
        self.assertIn('fyers', doc)
        self.assertIn('not used', doc['fyers'])


if __name__ == '__main__':
    unittest.main()
