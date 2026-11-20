"""Fix 1 (Grok box test, render-deploy-03): the real quote error must be
recorded, not masked as 'RuntimeError'.

The Render worker had 0 successful Fyers quotes since 09:22 IST, 199
quote_batch_failed events, all with error_code 'RuntimeError' — the
real exception message was discarded twice: MarketData.get() re-raised
a transport failure as bare RuntimeError('market_data_transport_failed')
with `from None`, and classified_error() returns only the type name when
the message is not an allowlisted reason. These tests pin the fix:
error_detail() carries the real (redacted) message alongside the
unchanged error_code, in quote_batch_failed events and in the worker
backoff heartbeat.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit as al
import engine as e
import test_engine as fixtures


class ErrorDetailTests(unittest.TestCase):
    def test_redact_keeps_diagnostic_text(self):
        # A transport message with a URL path is diagnostic, not a
        # secret: it must survive (the old text() would blank the
        # whole value because it contains two slashes).
        msg = ("HTTPSConnectionPool(host='api-t1.fyers.in', port=443): "
               "Max retries exceeded with url: /data/quotes "
               "(Caused by NewConnectionError('failed to establish "
               "a new connection'))")
        out = al.redact_error_detail(msg)
        self.assertIn('api-t1.fyers.in', out)
        self.assertIn('/data/quotes', out)
        self.assertIn('Max retries', out)

    def test_redact_removes_tokens_and_codes(self):
        jwt = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature'
        out = al.redact_error_detail('token refresh failed for ' + jwt)
        self.assertNotIn(jwt, out)
        self.assertNotIn('eyJ', out)
        self.assertIn('[redacted]', out)
        out = al.redact_error_detail('bad pin 123456 in request')
        self.assertNotIn('123456', out)
        self.assertIn('[redacted]', out)
        out = al.redact_error_detail('Authorization: Bearer abcdef123456')
        self.assertNotIn('abcdef123456', out)
        self.assertIn('[redacted]', out)

    def test_redact_truncates(self):
        out = al.redact_error_detail('x' * 500)
        self.assertLessEqual(len(out), 160)

    def test_error_detail_prefers_attached_detail(self):
        err = RuntimeError('market_data_transport_failed')
        err.detail = 'connection refused by api-t1.fyers.in'
        self.assertEqual(al.error_detail(err),
                         'connection refused by api-t1.fyers.in')

    def test_error_detail_walks_cause_chain(self):
        try:
            try:
                raise ConnectionError('connection reset by peer')
            except ConnectionError as cause:
                raise RuntimeError('market_data_transport_failed') from cause
        except RuntimeError as ex:
            self.assertIn('connection reset by peer', al.error_detail(ex))

    def test_error_detail_falls_back_to_message(self):
        self.assertEqual(al.error_detail(RuntimeError('something broke')),
                         'something broke')
        self.assertIsNone(al.error_detail(RuntimeError('')))

    def test_classified_error_still_masks_code(self):
        # The fix ADDS error_detail; it does not change the error_code
        # taxonomy (classified_error still returns the type name).
        err = RuntimeError('market_data_transport_failed')
        self.assertEqual(al.classified_error(err),
                         'market_data_transport_failed')
        masked = RuntimeError(
            "HTTPSConnectionPool(host='x'): Max retries exceeded")
        self.assertEqual(al.classified_error(masked), 'RuntimeError')
        # ...but the detail is still recoverable from the same object.
        self.assertIn('Max retries', al.error_detail(masked))

    def test_clean_keeps_error_detail(self):
        payload = al.clean({'error_code': 'RuntimeError',
                            'error_detail': 'connection refused',
                            'duration_ms': 12})
        self.assertEqual(payload['error_detail'], 'connection refused')
        self.assertEqual(payload['error_code'], 'RuntimeError')
        # A secret inside the detail is redacted by clean(), not dropped.
        payload = al.clean({'error_detail': 'token eyJabc.def.ghi bad'})
        self.assertNotIn('eyJabc', payload['error_detail'])


class QuoteErrorDetailTests(unittest.TestCase):
    def _api(self):
        api = e.MarketData.__new__(e.MarketData)
        api.env = {'FYERS_APP_ID': 'fixture',
                   'FYERS_ACCESS_TOKEN': 'fixture'}
        api.last = 0
        api.refreshed = False
        tmp = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        api.budget = e.RequestBudget(tmp.name)
        return api

    def test_get_attaches_real_error_detail(self):
        api = self._api()
        real = ("HTTPSConnectionPool(host='api-t1.fyers.in', port=443): "
                "Max retries exceeded with url: /data/quotes")
        with patch.object(e.requests, 'get',
                          side_effect=ConnectionError(real)), \
             patch.object(e.time, 'sleep'):
            try:
                api.get('quotes', {'symbols': 'NSE:RELIANCE-EQ'})
                self.fail('expected RuntimeError')
            except RuntimeError as caught:
                detail = al.error_detail(caught)
                code = al.classified_error(caught)
                message = str(caught)
        self.assertEqual(message, 'market_data_transport_failed')
        self.assertEqual(code, 'market_data_transport_failed')
        self.assertIn('api-t1.fyers.in', detail)
        self.assertIn('/data/quotes', detail)

    def test_quotes_failed_event_carries_error_detail(self):
        api = self._api()
        real = 'connection reset by peer while reading /data/quotes'
        notes = []
        with patch.object(e.requests, 'get',
                          side_effect=ConnectionError(real)), \
             patch.object(e.time, 'sleep'), \
             patch.object(e.al, 'note',
                          side_effect=lambda kind, **kw: notes.append(
                              (kind, kw))):
            with self.assertRaises(RuntimeError):
                api.quotes(['RELIANCE-EQ'])
        failed = [kw for kind, kw in notes if kind == 'quote_batch_failed']
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]['error_code'],
                         'market_data_transport_failed')
        self.assertIn('connection reset by peer',
                      failed[0]['error_detail'])
        self.assertIn('/data/quotes', failed[0]['error_detail'])


class WorkerBackoffErrorDetailTests(unittest.TestCase):
    """The worker loop's backoff heartbeat (the record Grok cited:
    status=backoff, failures=153) must carry the real error, not just
    error_code 'RuntimeError'."""

    @classmethod
    def setUpClass(cls):
        fixtures.PaperTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixtures.PaperTests.tearDownClass.__func__(cls)

    setUp = fixtures.PaperTests.setUp

    def test_backoff_heartbeat_carries_error_detail(self):
        # The worker loop's failure record (the backoff heartbeat Grok
        # cited: status=backoff, failures=153) must carry the real
        # error, not just error_code 'RuntimeError'. The heartbeat row
        # is overwritten by 'stopped' when the worker exits, so the
        # durable proof is the worker_failure audit event, which the
        # loop emits with the same error_detail.
        import automation as a
        err = RuntimeError('market_data_transport_failed')
        err.detail = 'connection reset by peer while reading /data/quotes'
        with patch.object(e, 'monitor', side_effect=err):
            a.worker(e, self.db, sleep=lambda _: None, max_polls=1,
                     api_factory=lambda: object())
        rows = self.db.query(
            "SELECT data_json FROM paper_events WHERE namespace=?"
            " AND event_type='worker_failure' ORDER BY recorded_at DESC",
            (e.NS,))
        self.assertTrue(rows, 'no worker_failure event recorded')
        data = json.loads(rows[0]['data_json'])
        self.assertEqual(data['error_code'],
                         'market_data_transport_failed')
        self.assertEqual(
            data['error_detail'],
            'connection reset by peer while reading /data/quotes')
        self.assertEqual(data['failures'], 1)


if __name__ == '__main__':
    unittest.main()
