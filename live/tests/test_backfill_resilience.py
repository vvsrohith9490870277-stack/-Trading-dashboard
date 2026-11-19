"""Task 6 tests: backfill resilience and per-date reporting (offline, fake fetchers).

Fixtures are tiny synthetic zip/csv files, clearly labelled synthetic; no NSE
network is used. fetch_official is monkeypatched with fakes that raise
urllib HTTPError(403/404) or write a synthetic legacy zip.
"""
import io, json, sys, tempfile, unittest, urllib.error, zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / 'automation' / 'paper-trading'))
import bhavcopy_backfill as bb

HDR = ['TradDt', 'FinInstrmTp', 'TckrSymb', 'SctySrs', 'ISIN', 'OpnPric', 'HghPric', 'LwPric', 'ClsPric', 'TtlTradgVol', 'TtlTrfVal']


def udiff_zip(day, symbol='AAA'):
    s = io.StringIO(); s.write(','.join(HDR) + '\n')
    s.write(','.join([day, 'STK', symbol, 'EQ', 'IN1', '100', '101', '99', '100.5', '10', '20']) + '\n')
    b = io.BytesIO()
    with zipfile.ZipFile(b, 'w') as z: z.writestr('BhavCopy.csv', s.getvalue())
    return b.getvalue()


def legacy_zip(day, symbol='AAA'):
    csv_text = ('SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN\n'
                f'{symbol},EQ,100,101,99,100.5,100.5,99,10,1000,{day},1,IN1\n')
    b = io.BytesIO()
    with zipfile.ZipFile(b, 'w') as z:
        d = day[8:10] + {'01': 'JAN', '02': 'FEB', '03': 'MAR', '04': 'APR', '05': 'MAY', '06': 'JUN',
                         '07': 'JUL', '08': 'AUG', '09': 'SEP', '10': 'OCT', '11': 'NOV', '12': 'DEC'}[day[5:7]] + day[:4]
        z.writestr(f'cm{d}bhav.csv', csv_text)
    return b.getvalue()


def http_error(url, code):
    return urllib.error.HTTPError(url, code, f'http_{code}', hdrs=None, fp=None)


def _day_from_dest(dest):
    name = Path(dest).name
    if name.startswith('udiff-'):
        return name[6:16]
    if name.startswith('cm'):
        months = {'JAN': '01', 'FEB': '02', 'MAR': '03', 'APR': '04', 'MAY': '05', 'JUN': '06',
                  'JUL': '07', 'AUG': '08', 'SEP': '09', 'OCT': '10', 'NOV': '11', 'DEC': '12'}
        return f"{name[7:11]}-{months[name[4:7]]}-{name[2:4]}"
    return '2026-07-23'


class FakeSession:
    def warmup(self): return self


class BackfillResilienceTests(unittest.TestCase):
    def setUp(self):
        self._orig_fetch = bb.fetch_official
        self._orig_session = bb.NseBrowserSession
        bb.NseBrowserSession = lambda timeout=30: FakeSession()
        self.calls = []

    def tearDown(self):
        bb.fetch_official = self._orig_fetch
        bb.NseBrowserSession = self._orig_session

    def _fake_fetch(self, behaviour):
        def fetch(url, dest, timeout, retries=3, gap=1.0, session=None):
            self.calls.append(url)
            form = 'udiff' if 'BhavCopy_NSE_CM' in url else ('legacy_cm' if '/cm' in url or 'bhav.csv.zip' in url else 'sec_bhavdata_full')
            action = behaviour.get(form)
            if action == '403':
                raise http_error(url, 403)
            if action == '404':
                raise http_error(url, 404)
            if action == 'ok_legacy':
                Path(dest).write_bytes(legacy_zip(_day_from_dest(dest)))
                return Path(dest)
            if action == 'ok_udiff':
                Path(dest).write_bytes(udiff_zip(_day_from_dest(dest)))
                return Path(dest)
            raise http_error(url, 403)
        return fetch

    def test_udiff_403_then_legacy_success_ingested_with_attempts(self):
        bb.fetch_official = self._fake_fetch({'udiff': '403', 'legacy_cm': 'ok_legacy', 'sec_bhavdata_full': '404'})
        with tempfile.TemporaryDirectory() as t:
            got = bb.obtain_session_file('2026-07-23', Path(t), offline=False, gap=0)
        self.assertEqual(got['status'], 'ok')
        self.assertEqual(got['form'], 'legacy_cm')
        attempts = got.get('attempts') or []
        self.assertEqual([a['form'] for a in attempts], ['udiff', 'legacy_cm'])
        self.assertEqual(attempts[0]['http_status'], 403)
        self.assertEqual(attempts[1]['http_status'], 200)
        self.assertEqual(attempts[1]['outcome'], 'success')

    def test_all_forms_403_is_fetch_failed_with_attempts(self):
        bb.fetch_official = self._fake_fetch({'udiff': '403', 'legacy_cm': '403', 'sec_bhavdata_full': '403'})
        with tempfile.TemporaryDirectory() as t:
            got = bb.obtain_session_file('2026-07-23', Path(t), offline=False, gap=0)
        self.assertEqual(got['status'], 'fetch_failed')
        self.assertEqual([a['http_status'] for a in got['attempts']], [403, 403, 403])

    def test_all_forms_404_is_no_session(self):
        bb.fetch_official = self._fake_fetch({'udiff': '404', 'legacy_cm': '404', 'sec_bhavdata_full': '404'})
        with tempfile.TemporaryDirectory() as t:
            got = bb.obtain_session_file('2026-07-23', Path(t), offline=False, gap=0)
        self.assertEqual(got['status'], 'no_session')

    def test_date_mismatch_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / 'udiff-2026-07-23.csv.zip').write_bytes(udiff_zip('2026-07-22'))
            got = bb.obtain_session_file('2026-07-23', Path(t), offline=True, gap=0)
        self.assertEqual(got['status'], 'no_session')

    def test_run_continues_after_failures_and_reports_honest_coverage(self):
        bb.fetch_official = self._fake_fetch({'udiff': '403', 'legacy_cm': '403', 'sec_bhavdata_full': '403'})
        with tempfile.TemporaryDirectory() as t:
            out = bb.main(['--dates', '2026-07-23,2026-07-24', '--probe', '--scratch-dir', t])
        self.assertEqual(out['coverage'], 0)
        self.assertLess(out['coverage'], 0.99)
        self.assertEqual([r['status'] for r in out['detail']], ['fetch_failed', 'fetch_failed'])
        self.assertEqual(len(out['failures']), 2)
        self.assertTrue(all(r.get('attempts') for r in out['detail']))

    def test_retry_missing_only_touches_missing_dates_and_second_run_is_noop(self):
        bb.fetch_official = self._fake_fetch({'udiff': '403', 'legacy_cm': 'ok_legacy', 'sec_bhavdata_full': '404'})
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            # 2026-07-23 already has a valid local file; 2026-07-24 does not.
            (td / 'udiff-2026-07-23.csv.zip').write_bytes(udiff_zip('2026-07-23'))
            out = bb.main(['--dates', '2026-07-23,2026-07-24', '--probe', '--scratch-dir', t, '--retry-missing'])
            self.assertEqual([r['date'] for r in out['detail']], ['2026-07-24'])
            self.assertTrue(all('20260723' not in u and '23JUL2026' not in u and '23072026' not in u for u in self.calls))
            self.calls.clear()
            out2 = bb.main(['--dates', '2026-07-23,2026-07-24', '--probe', '--scratch-dir', t, '--retry-missing'])
            self.assertEqual(out2['detail'], [])
            self.assertEqual(self.calls, [])

    def test_recent_404_is_not_recorded_but_old_404_is(self):
        # R9b: NSE publishes the Bhavcopy in the evening, so a 404 for
        # yesterday may just mean "not published yet" — it must NOT be
        # recorded; a 404 for a date 10 days ago IS recorded. Both still
        # report no_session for the run itself; only persistence is gated.
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        bb.fetch_official = self._fake_fetch({'udiff': '404', 'legacy_cm': '404', 'sec_bhavdata_full': '404'})
        today = datetime.now(ZoneInfo('Asia/Kolkata')).date()
        yesterday = (today - timedelta(days=1)).isoformat()
        old = (today - timedelta(days=10)).isoformat()
        with tempfile.TemporaryDirectory() as t:
            record = Path(t) / 'bhavcopy_no_session.json'
            old_record = bb.nsr.NO_SESSION_RECORD
            bb.nsr.NO_SESSION_RECORD = record
            try:
                out = bb.main(['--dates', f'{yesterday},{old}', '--probe', '--scratch-dir', t, '--retry-missing'])
                self.assertEqual([r['status'] for r in out['detail']], ['no_session', 'no_session'])
                recorded = bb.load_no_session_dates()
                self.assertNotIn(yesterday, recorded)
                self.assertIn(old, recorded)
            finally:
                bb.nsr.NO_SESSION_RECORD = old_record

    def test_recordable_no_session_date_boundary(self):
        # R9b boundary: "at least 3 days before today (IST)" — exactly
        # 3 days before is recordable, 2 days before is not.
        from datetime import date
        today = date(2026, 11, 18)
        self.assertFalse(bb.nsr.recordable_no_session_date('2026-11-18', today=today))
        self.assertFalse(bb.nsr.recordable_no_session_date('2026-11-17', today=today))
        self.assertFalse(bb.nsr.recordable_no_session_date('2026-11-16', today=today))
        self.assertTrue(bb.nsr.recordable_no_session_date('2026-11-15', today=today))
        self.assertTrue(bb.nsr.recordable_no_session_date('2026-11-08', today=today))
        self.assertFalse(bb.nsr.recordable_no_session_date('not-a-date', today=today))

    def test_session_days_agree_with_expected_session_days(self):
        # Optional (review 2): the backfill calendar and the freshness
        # gate's calendar must agree, or R9b/R10 would disagree about
        # which days are sessions.
        import adaptive_daily as ad
        self.assertEqual(bb.session_days('2026-01-01', '2026-12-31'),
                         ad.expected_session_days('2026-01-01', '2026-12-31'))
        self.assertEqual(bb.session_days('2026-11-01', '2026-11-30'),
                         ad.expected_session_days('2026-11-01', '2026-11-30'))

    def test_all_404_date_makes_zero_fetch_calls_on_second_retry_run(self):
        # R9: an all-404 (no_session) date is persisted under state/ and
        # skipped on the next --retry-missing run — no more NSE calls for it.
        bb.fetch_official = self._fake_fetch({'udiff': '404', 'legacy_cm': '404', 'sec_bhavdata_full': '404'})
        with tempfile.TemporaryDirectory() as t:
            record = Path(t) / 'bhavcopy_no_session.json'
            old_record = bb.nsr.NO_SESSION_RECORD
            bb.nsr.NO_SESSION_RECORD = record
            try:
                out = bb.main(['--dates', '2026-07-23', '--probe', '--scratch-dir', t, '--retry-missing'])
                self.assertEqual([r['status'] for r in out['detail']], ['no_session'])
                first_calls = len(self.calls)
                self.assertEqual(first_calls, 3)  # one per archive form, then stop
                self.assertTrue(record.exists())
                self.assertIn('2026-07-23', bb.load_no_session_dates())
                self.calls.clear()
                out2 = bb.main(['--dates', '2026-07-23', '--probe', '--scratch-dir', t, '--retry-missing'])
                self.assertEqual(self.calls, [])
                self.assertEqual([r['status'] for r in out2['detail']], ['no_session_known'])
                self.assertEqual(out2['skipped_known_no_session'], ['2026-07-23'])
            finally:
                bb.nsr.NO_SESSION_RECORD = old_record


if __name__ == '__main__':
    unittest.main()
