"""Official NSE CM Price Band complete list (sec_list_DDMMYYYY.csv) helper.

Adaptive never calls Fyers. Live paper_price_bands in Turso currently stores only ASM/GSM
surveillance flags — no upper_price/lower_price — so the shadow engine has no official
circuit status today. This module parses the NSE band-percent file when a copy is on disk
(or when a small probe download succeeds) and can infer UPPER/LOWER from previous close
and the day's high/low for names that have a static percent band. F&O names typically
have no static band (dynamic 10% flex) and stay unknown.
"""
import csv, json, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG = json.loads((HERE / 'adaptive_live_config.json').read_text())
CIR = CFG.get('circuit_bands') or {}


def parse_sec_list(text_or_path):
    """Return rows {symbol, series, name, band_pct, remarks, static_band}.

    Band values like '2', '5', '10', '20' are percents. '-', 'NA', 'No Band', '' mean no static band.
    """
    if isinstance(text_or_path, (str, Path)) and Path(str(text_or_path)).exists() and not str(text_or_path).lstrip().startswith('Symbol'):
        raw = Path(text_or_path).read_text(encoding='utf8', errors='replace')
    else:
        raw = str(text_or_path)
    rows = list(csv.DictReader(raw.splitlines()))
    out = []
    for r in rows:
        # column names vary slightly: Symbol / SYMBOL, Band / Price Band, Series
        sym = (r.get('Symbol') or r.get('SYMBOL') or r.get('TckrSymb') or '').strip().upper()
        if not sym:
            continue
        series = (r.get('Series') or r.get('SERIES') or r.get('SctySrs') or '').strip().upper()
        name = (r.get('Security Name') or r.get('SecurityName') or r.get('NAME') or '').strip()
        band_raw = (r.get('Band') or r.get('Price Band') or r.get('PRICE_BAND') or r.get('Applicable Price Band') or '').strip()
        remarks = (r.get('Remarks') or r.get('REMARKS') or '').strip()
        static, pct = False, None
        try:
            pct = float(band_raw)
            if pct > 0:
                static = True
        except (TypeError, ValueError):
            static = False
        out.append({
            'symbol': sym,
            'series': series or 'EQ',
            'name': name,
            'band_raw': band_raw,
            'band_pct': pct,
            'static_band': static,
            'remarks': remarks,
        })
    return out


def infer_circuit(band_pct, prev_close, high=None, low=None, close=None, tol=0.002):
    """Infer UPPER/LOWER/NONE from a static percent band and the day's range.

    Returns None if there is no static band or no previous close (cannot invent a level).
    """
    if not band_pct or not prev_close:
        return None
    upper = round(float(prev_close) * (1 + float(band_pct) / 100.0), 2)
    lower = round(float(prev_close) * (1 - float(band_pct) / 100.0), 2)
    state = 'NONE'
    def hit(px, level):
        return px is not None and level > 0 and abs(float(px) - float(level)) / float(level) <= tol
    if high is not None and (float(high) >= upper or hit(high, upper) or hit(close, upper)):
        state = 'UPPER'
    if low is not None and (float(low) <= lower or hit(low, lower) or hit(close, lower)):
        state = 'LOWER' if state != 'UPPER' else 'BOTH'
    return {'upper': upper, 'lower': lower, 'circuit': state, 'band_pct': float(band_pct)}


def document():
    return {
        'source': CIR.get('url_template_ddmmyyyy'),
        'status': CIR.get('status'),
        'note': CIR.get('note'),
        'fyers': 'not used (adaptive isolation; live Fyers quotes have upper_ckt/lower_ckt but are not snapshotted into paper_price_bands with prices)',
        'turso_paper_price_bands': 'ASM/GSM flags only as of 11 Nov 2026 (source=NSE_ASM_GSM, upper_price/lower_price null)',
    }


if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print(json.dumps(document(), indent=2))
        raise SystemExit(0)
    rows = parse_sec_list(path)
    static = sum(1 for r in rows if r['static_band'])
    print(json.dumps({'file': path, 'rows': len(rows), 'with_static_band': static, 'without_static_band': len(rows) - static, 'document': document()}, indent=2))
