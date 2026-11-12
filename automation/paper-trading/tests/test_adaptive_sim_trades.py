"""Persist the Track B simulation trade tape (closed trades only). Never invent prices or P&L."""
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import adaptive_sim_trades as tape


def _db():
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row

    class DB:
        def batch(self, stm):
            with c:
                for sql, args in stm:
                    c.execute(sql, args)

        def query(self, sql, args=()):
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    db = DB()
    tape.ensure_table(db)
    return db


def _trade(**kw):
    row = {
        'symbol': 'AAA-EQ',
        'side': 'BUY',
        'entry_date': '2026-11-09',
        'entry_price': 100.25,
        'exit_date': '2026-11-11',
        'exit_price': 101.10,
        'reason': 'daily_exit',
        'qty': 10,
        'net_pnl_paise': 850,
    }
    row.update(kw)
    return row


class PersistSimTrades(unittest.TestCase):
    def test_writes_only_fields_from_the_simulation_record(self):
        db = _db()
        t = _trade()
        out = tape.persist_sim_trades(
            db, 'adaptive_v1', 'spawn-b1', 1, '2026-11-12', [t],
            recorded_at='2026-11-12T18:30:00+05:30', track='B',
        )
        self.assertEqual(out['written'], 1)
        self.assertEqual(out['skipped_incomplete'], 0)
        rows = db.query('SELECT * FROM adaptive_sim_trades')
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r['namespace'], 'adaptive_v1')
        self.assertEqual(r['spawn_id'], 'spawn-b1')
        self.assertEqual(r['strategy'], 1)
        self.assertEqual(r['session_day'], '2026-11-12')
        self.assertEqual(r['symbol'], 'AAA-EQ')
        self.assertEqual(r['side'], 'BUY')
        self.assertEqual(r['entry_date'], '2026-11-09')
        self.assertEqual(r['entry_price'], 100.25)
        self.assertEqual(r['exit_date'], '2026-11-11')
        self.assertEqual(r['exit_price'], 101.10)
        self.assertEqual(r['exit_reason'], 'daily_exit')
        self.assertEqual(r['qty'], 10)
        self.assertEqual(r['pnl_paise'], 850)
        self.assertEqual(r['track'], 'B')

    def test_rerun_of_the_same_trade_is_idempotent(self):
        db = _db()
        t = _trade()
        tape.persist_sim_trades(db, 'adaptive_v1', 'spawn-b1', 1, '2026-11-12', [t], 'stamp-1', track='B')
        tape.persist_sim_trades(db, 'adaptive_v1', 'spawn-b1', 1, '2026-11-13', [t], 'stamp-2', track='B')
        rows = db.query('SELECT * FROM adaptive_sim_trades')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['session_day'], '2026-11-13')
        self.assertEqual(rows[0]['recorded_at'], 'stamp-2')
        self.assertEqual(rows[0]['pnl_paise'], 850)

    def test_skips_incomplete_trades_instead_of_inventing_values(self):
        db = _db()
        missing_exit = _trade()
        del missing_exit['exit_price']
        missing_pnl = _trade(symbol='BBB-EQ', net_pnl_paise=None)
        ok = _trade(symbol='CCC-EQ', net_pnl_paise=12)
        out = tape.persist_sim_trades(
            db, 'adaptive_v1', 'spawn-b1', 2, '2026-11-12',
            [missing_exit, missing_pnl, ok], 'stamp',
        )
        self.assertEqual(out['written'], 1)
        self.assertEqual(out['skipped_incomplete'], 2)
        rows = db.query('SELECT symbol, exit_price, pnl_paise FROM adaptive_sim_trades')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['symbol'], 'CCC-EQ')
        self.assertEqual(rows[0]['exit_price'], 101.10)
        self.assertEqual(rows[0]['pnl_paise'], 12)

    def test_side_is_left_null_when_the_simulation_did_not_provide_it(self):
        db = _db()
        t = _trade()
        del t['side']
        tape.persist_sim_trades(db, 'adaptive_v1', 'spawn-b1', 1, '2026-11-12', [t], 'stamp')
        rows = db.query('SELECT side FROM adaptive_sim_trades')
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['side'])

    def test_closed_trades_from_simulate_track_window_persist_without_invention(self):
        from adaptive_fixtures import fresh_db, make_market, seed_pit
        import adaptive_engine as ae
        db = fresh_db(ROOT)
        tape.ensure_table(db)
        days, df, idx = make_market()
        syms = sorted(df.symbol.unique())
        seed_pit(db, 'adaptive_v1', days, syms, series='EQ')
        res = ae.simulate_track_window(db, 'adaptive_v1', 3, {}, df, idx, days[-105:-45], days[-45:])
        self.assertGreater(len(res['closed_trades']), 0)
        out = tape.persist_sim_trades(
            db, 'adaptive_v1', 'spawn-sim', 3, '2026-11-12', res['closed_trades'], 'stamp', track='B',
        )
        self.assertEqual(out['skipped_incomplete'], 0)
        self.assertEqual(out['written'], len(res['closed_trades']))
        rows = db.query('SELECT symbol, side, entry_date, entry_price, exit_date, exit_price, exit_reason, qty, pnl_paise FROM adaptive_sim_trades ORDER BY entry_date, symbol')
        self.assertEqual(len(rows), len(res['closed_trades']))
        for rec, src in zip(rows, sorted(res['closed_trades'], key=lambda t: (t['entry_date'], t['symbol']))):
            self.assertEqual(rec['symbol'], src['symbol'])
            self.assertEqual(rec['entry_date'], src['entry_date'])
            self.assertEqual(rec['exit_date'], src['exit_date'])
            self.assertEqual(rec['entry_price'], src['entry_price'])
            self.assertEqual(rec['exit_price'], src['exit_price'])
            self.assertEqual(rec['exit_reason'], src.get('reason'))
            self.assertEqual(rec['qty'], src['qty'])
            self.assertEqual(rec['pnl_paise'], src['net_pnl_paise'])
            if src.get('side') is None:
                self.assertIsNone(rec['side'])
            else:
                self.assertEqual(rec['side'], src['side'])


if __name__ == '__main__':
    unittest.main()
