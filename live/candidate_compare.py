"""Side-by-side comparison report (B4): frozen seven vs shadow challengers.

Writes compare.json and COMPARE.md into <report dir>/adaptive-compare/<day>/.

Per strategy (the seven and the candidates, same table): trades, win rate,
average net return per trade (%), average R, max drawdown, total net P&L
after modeled charges and slippage, average holding days, exposure, trade
overlap with the seven (by (symbol, entry date)), sample size.

R definition (plan §4.4, brief §6 Q3 answered by Grok, plan §9): 1R per
trade = qty x 2 x ATR(14) at entry, in paise; R-multiple per trade = net
P&L (paise) / 1R. No pass/fail on R. The frozen seven have no fixed stops
(indicator-based exits), so entry-minus-stop is not computable for them
and one definition must cover baseline and challengers alike. Raw net
P&L in paise is shown alongside the INR figure in every row.

Honesty rules (brief §4 B4):
- Two clearly separated panels: historical replay (simulated) and forward
  shadow days (observed), each with its day and trade counts.
- Any row with fewer than 30 trades is labelled insufficient_sample and gets
  no ordering by performance.
- No claim of "better" or "promote": the report states "shadow, simulated,
  observation only, no promotion".
- The report is read-only: it reads trades and writes only its own out dir.

Inputs: --offline-fixture DIR (trades_historical.json + trades_forward.json,
as written by the committed fixture) or --db PATH (sidecar adaptive.sqlite:
adaptive_candidate_trades for 101-106 and adaptive_sim_trades track='A' for
the seven). In --db mode ATR(14) at entry is not stored in either table, so
avg_r is null with reason atr14_at_entry_not_stored.
"""
import argparse
import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
LIB = HERE.parent / 'automation' / 'paper-trading'
for p in (str(HERE), str(LIB)):
    if p not in sys.path:
        sys.path.insert(0, p)

IST = ZoneInfo('Asia/Kolkata')
MODE = 'shadow_simulated_observation_only'
MIN_TRADES_FOR_SAMPLE = 30
SEVEN = [str(s) for s in range(1, 8)]
DEFAULT_CAPITAL_PAISE = 50_000_000  # sleeve capital (capital_policy.json)


def _parse_day(s):
    return date.fromisoformat(str(s)[:10])


def _hold_days(t):
    return (_parse_day(t['exit_date']) - _parse_day(t['entry_date'])).days


def _r_multiple(t):
    """R-multiple = net P&L / 1R, with 1R = qty x 2 x ATR(14) at entry
    (Grok Q3 answer, plan §9)."""
    atr = t.get('atr14_at_entry_paise')
    if atr is None or float(atr) <= 0:
        return None
    return float(t['net_pnl_paise']) / (int(t['qty']) * 2.0 * float(atr))


def _max_drawdown_pct(trades, capital_paise):
    """Max peak-to-trough drop of the cumulative net P&L curve (ordered by
    exit date), as a percent of the starting capital."""
    if not trades:
        return 0.0
    ordered = sorted(trades, key=lambda t: (str(t['exit_date']), str(t['symbol'])))
    peak = 0
    worst = 0
    cum = 0
    for t in ordered:
        cum += int(t['net_pnl_paise'])
        peak = max(peak, cum)
        worst = min(worst, cum - peak)
    return round(abs(worst) / float(capital_paise) * 100, 6)


def strategy_metrics(strategies_trades, seven_keys, window_days, capital_paise):
    """Per-strategy metrics for one panel. strategies_trades: {key: [trades]}."""
    seven_pairs = set()
    for k in seven_keys:
        for t in strategies_trades.get(k, []):
            seven_pairs.add((str(t['symbol']), str(t['entry_date'])[:10]))
    rows = {}
    for key in sorted(strategies_trades, key=lambda k: (int(k) if str(k).isdigit() else 999, str(k))):
        trades = strategies_trades.get(key, []) or []
        n = len(trades)
        wins = sum(1 for t in trades if int(t['net_pnl_paise']) > 0)
        rets = []
        rs = []
        holds = []
        net = 0
        for t in trades:
            ep = int(round(float(t['entry_price']) * 100))
            notion = int(t['qty']) * ep
            rets.append(int(t['net_pnl_paise']) / notion * 100 if notion else None)
            r = _r_multiple(t)
            if r is not None:
                rs.append(r)
            holds.append(_hold_days(t))
            net += int(t['net_pnl_paise'])
        overlap = sum(1 for t in trades
                      if (str(t['symbol']), str(t['entry_date'])[:10]) in seven_pairs
                      and str(key) not in seven_keys)
        # For a seven strategy, overlap is against the OTHER six.
        if str(key) in seven_keys:
            other_pairs = set()
            for k in seven_keys:
                if k == str(key):
                    continue
                for t in strategies_trades.get(k, []):
                    other_pairs.add((str(t['symbol']), str(t['entry_date'])[:10]))
            overlap = sum(1 for t in trades
                          if (str(t['symbol']), str(t['entry_date'])[:10]) in other_pairs)
        rows[str(key)] = {
            'trades': n,
            'sample_size': n,
            'sample_label': 'sufficient' if n >= MIN_TRADES_FOR_SAMPLE else 'insufficient_sample',
            'win_rate': round(wins / n, 4) if n else None,
            'avg_net_return_pct': round(sum(r for r in rets if r is not None) / len([r for r in rets if r is not None]), 4) if any(r is not None for r in rets) else None,
            'avg_r': round(sum(rs) / len(rs), 4) if rs else None,
            'avg_r_note': None if rs else ('atr14_at_entry_not_stored' if trades else 'no_trades'),
            'max_drawdown_pct': _max_drawdown_pct(trades, capital_paise),
            'net_pnl_inr': round(net / 100, 2),
            'net_pnl_paise': net,
            'avg_hold_days': round(sum(holds) / len(holds), 2) if holds else None,
            'exposure': round(sum(holds) / float(window_days), 4) if window_days else None,
            'overlap_with_seven': {'count': overlap,
                                   'pct': round(overlap / n, 4) if n else None},
        }
    return rows


def build_report(historical, forward, session_day):
    """historical / forward: {'window_days': int, 'starting_capital_paise': int,
    'strategies': {key: [trades]}}. Returns the compare.json dict."""
    hist_rows = strategy_metrics(historical['strategies'], SEVEN,
                                 historical.get('window_days'), historical.get('starting_capital_paise') or DEFAULT_CAPITAL_PAISE)
    fwd_rows = strategy_metrics(forward['strategies'], SEVEN,
                                forward.get('window_days'), forward.get('starting_capital_paise') or DEFAULT_CAPITAL_PAISE)
    merged_trades = {}
    for key in set(historical['strategies']) | set(forward['strategies']):
        merged_trades[key] = historical['strategies'].get(key, []) + forward['strategies'].get(key, [])
    merged = strategy_metrics(
        merged_trades, SEVEN,
        (historical.get('window_days') or 0) + (forward.get('window_days') or 0),
        historical.get('starting_capital_paise') or DEFAULT_CAPITAL_PAISE)
    return {
        'mode': MODE,
        'generated_at_ist': datetime.now(IST).isoformat(),
        'session_day': session_day,
        'statement': 'shadow, simulated, observation only, no promotion',
        'r_definition': '1R per trade = qty x 2 x ATR(14) at entry (paise); R-multiple = net P&L / 1R. No pass/fail on R.',
        'min_trades_for_sample': MIN_TRADES_FOR_SAMPLE,
        'historical_replay': {'days': historical.get('window_days'),
                              'trades': sum(len(v) for v in historical['strategies'].values()),
                              'strategies': hist_rows},
        'forward_shadow': {'days': forward.get('window_days'),
                           'trades': sum(len(v) for v in forward['strategies'].values()),
                           'strategies': fwd_rows},
        'strategies': merged,
    }


def _fmt(v, nd=2):
    return 'n/a' if v is None else f'{v:.{nd}f}'


def render_markdown(report):
    lines = [
        '# Adaptive compare: frozen seven vs shadow challengers',
        '',
        'shadow, simulated, observation only, no promotion',
        '',
        f"Session day: {report['session_day']} | Generated (IST): {report['generated_at_ist']}",
        '',
        '## Historical replay (simulated)',
        '',
        f"Days: {report['historical_replay']['days']} | Trades: {report['historical_replay']['trades']}",
        '',
        '## Forward shadow days (observed)',
        '',
        f"Days: {report['forward_shadow']['days']} | Trades: {report['forward_shadow']['trades']}",
        '',
        '## Per-strategy table (both panels combined)',
        '',
        'R definition: 1R per trade = qty x 2 x ATR(14) at entry (paise); '
        'R-multiple = net P&L / 1R. No pass/fail on R.',
        'Rows with fewer than 30 trades are labelled insufficient_sample.',
        '',
        '| strategy | trades | win rate | avg net return % | avg R | max DD % | net P&L (INR) | net P&L (paise) | avg hold days | exposure | overlap with seven | sample |',
        '|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|',
    ]
    for key, r in sorted(report['strategies'].items(), key=lambda kv: int(kv[0])):
        ov = r['overlap_with_seven']
        lines.append(
            f"| {key} | {r['trades']} | {_fmt(r['win_rate'])} | {_fmt(r['avg_net_return_pct'])} | "
            f"{_fmt(r['avg_r'])} | {_fmt(r['max_drawdown_pct'], 4)} | {r['net_pnl_inr']:.2f} | "
            f"{r['net_pnl_paise']} | "
            f"{_fmt(r['avg_hold_days'], 1)} | {_fmt(r['exposure'])} | {ov['count']} ({_fmt(ov['pct'])}) | {r['sample_label']} |")
    lines += [
        '',
        'Notes:',
        '- Historical replay is simulated; forward shadow days are observed shadow runs.',
        '- Overlap counts trades sharing (symbol, entry date) with the frozen seven.',
        '- Exposure is total holding days divided by window days.',
        '',
    ]
    return '\n'.join(lines) + '\n'


def load_offline_fixture(fixture_dir):
    fixture_dir = Path(fixture_dir)
    out = {}
    for name in ('trades_historical.json', 'trades_forward.json'):
        p = fixture_dir / name
        if not p.exists():
            raise FileNotFoundError(f'fixture file missing: {p}')
        out[name] = json.loads(p.read_text())
    return out['trades_historical.json'], out['trades_forward.json']


def load_from_db(db_path, namespace='adaptive_v1'):
    """Read candidate trades (adaptive_candidate_trades) and Track A replica
    trades (adaptive_sim_trades track='A') from the sidecar. ATR(14) at entry
    is not stored in either table, so avg_r is null there."""
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    seven = {}
    try:
        for r in c.execute(
                "SELECT strategy, symbol, entry_date, exit_date, qty, entry_price, exit_price, pnl_paise AS net_pnl_paise "
                "FROM adaptive_sim_trades WHERE namespace=? AND track='A'", (namespace,)):
            d = dict(r)
            seven.setdefault(str(d.pop('strategy')), []).append(d)
    except sqlite3.Error:
        seven = {}
    cand = {}
    try:
        for r in c.execute(
                "SELECT strategy, symbol, entry_date, exit_date, qty, entry_price, exit_price, net_pnl_paise "
                "FROM adaptive_candidate_trades WHERE namespace=? AND mode='shadow'", (namespace,)):
            d = dict(r)
            cand.setdefault(str(d.pop('strategy')), []).append(d)
    except sqlite3.Error:
        cand = {}
    c.close()
    hist = {'window_days': None, 'starting_capital_paise': DEFAULT_CAPITAL_PAISE,
            'strategies': {**seven, **cand}}
    return hist, {'window_days': None, 'starting_capital_paise': DEFAULT_CAPITAL_PAISE, 'strategies': {}}


def write_report(report, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'compare.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    (out_dir / 'COMPARE.md').write_text(render_markdown(report))
    return out_dir


def main(argv=None):
    ap = argparse.ArgumentParser(description='Side-by-side compare: frozen seven vs shadow challengers (B4).')
    ap.add_argument('--offline-fixture', default=None)
    ap.add_argument('--db', default=None, help='sidecar adaptive.sqlite path')
    ap.add_argument('--namespace', default='adaptive_v1')
    ap.add_argument('--session', required=True, help='session day YYYY-MM-DD')
    ap.add_argument('--out', required=True, help='output directory')
    a = ap.parse_args(argv)
    if a.offline_fixture:
        historical, forward = load_offline_fixture(a.offline_fixture)
    elif a.db:
        historical, forward = load_from_db(a.db, a.namespace)
    else:
        raise ValueError('need --offline-fixture or --db')
    report = build_report(historical, forward, a.session)
    out_dir = write_report(report, a.out)
    print(json.dumps({'compare_json': str(out_dir / 'compare.json'),
                      'compare_md': str(out_dir / 'COMPARE.md'),
                      'mode': MODE,
                      'historical_trades': report['historical_replay']['trades'],
                      'forward_trades': report['forward_shadow']['trades']}, indent=2))
    return report


if __name__ == '__main__':
    main()
