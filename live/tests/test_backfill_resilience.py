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


if __name__ == '__main__':
    unittest.main()
