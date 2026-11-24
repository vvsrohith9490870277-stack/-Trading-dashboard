"""Apply split, bonus, and dividend actions to open paper positions.

r = ratio_num / ratio_den is new shares per old share. Prices divide by r.
A non-integer result floors the quantity. The fractional new share is credited at the
post-adjustment open once that open exists. Rights, demergers, and mergers are not applied.
"""
import capital as cap

MANUAL = {'RIGHTS', 'DEMERGER', 'MERGER', 'EXTRAORDINARY_DIVIDEND_REVIEW', 'DIVIDEND_UNPARSED_REVIEW'}
# The freeze plan never defines 'extraordinary'; reference_feeds routes special/unparsed dividends here instead of inventing a threshold.
DIVIDEND_REVIEW = {'EXTRAORDINARY_DIVIDEND_REVIEW', 'DIVIDEND_UNPARSED_REVIEW'}
SCALED = {'SPLIT', 'BONUS', 'EXTRAORDINARY_DIVIDEND'}
ORDER = {'SPLIT': 0, 'BONUS': 1, 'EXTRAORDINARY_DIVIDEND': 2, 'CASH_DIVIDEND': 3, 'RIGHTS': 4, 'DEMERGER': 5, 'MERGER': 6}

def scale_paise(price_paise, num, den):
    """Divide a price by r = num/den and round half-up to paise."""
    return (int(price_paise) * int(den) + int(num) // 2) // int(num)

def back_adjust_frame(df, actions):
    """Back-adjust pre-ex-date OHLC prices for splits/bonuses, so a corporate
    action does not cause a false indicator exit.

    Only SCALED kinds (SPLIT, BONUS, EXTRAORDINARY_DIVIDEND) with a valid
    integer ratio are applied. For an action with ex_date <= the frame's last
    bar, bars with trade_date < ex_date have open/high/low/close divided by
    r = ratio_num / ratio_den and volume multiplied by r. Actions with
    ex_date after the last bar are never applied (zero look-ahead). Returns
    the frame unchanged when no action applies.
    """
    if df is None or len(df) == 0 or not actions:
        return df
    out = df.copy().sort_values('trade_date').reset_index(drop=True)
    as_of = str(out.iloc[-1]['trade_date'])
    applicable = []
    for a in actions:
        if a.get('kind') not in SCALED:
            continue
        ex_date = str(a.get('ex_date') or '')
        if not ex_date or ex_date > as_of:
            continue
        ratio = _ratio(a)
        if ratio is None:
            continue
        applicable.append((ex_date, ratio[0] / ratio[1]))
    if not applicable:
        return df
    applicable.sort(key=lambda x: x[0])
    for col in ('open', 'high', 'low', 'close'):
        if col in out.columns:
            out[col] = out[col].astype(float)
    if 'volume' in out.columns:
        out['volume'] = out['volume'].astype(float)
    for ex_date, r in applicable:
        mask = out['trade_date'].astype(str) < ex_date
        if mask.any():
            for col in ('open', 'high', 'low', 'close'):
                if col in out.columns:
                    out.loc[mask, col] = out.loc[mask, col] / r
            if 'volume' in out.columns:
                out.loc[mask, 'volume'] = out.loc[mask, 'volume'] * r
    return out

def inr(paise):
    return paise / 100

def _ratio(action):
    num, den = action.get('ratio_num'), action.get('ratio_den')
    if isinstance(num, bool) or isinstance(den, bool) or not isinstance(num, int) or not isinstance(den, int) or num <= 0 or den <= 0:
        return None
    return num, den

def _seen(db, table, namespace, strategy, symbol, day, kind):
    return bool(db.query(f'SELECT 1 FROM {table} WHERE namespace=? AND strategy=? AND symbol=? AND ex_date=? AND kind=?', (namespace, strategy, symbol, day, kind)))

def _credit(db, e, namespace, strategy, symbol, day, kind, amount, at):
    if amount <= 0 or _seen(db, 'paper_cash_credits', namespace, strategy, symbol, day, kind):
        return False
    db.batch([('INSERT INTO paper_cash_credits(namespace,strategy,symbol,ex_date,kind,amount_paise,recorded_at) VALUES(?,?,?,?,?,?,?)', (namespace, strategy, symbol, day, kind, int(amount), at))])
    return True

def _note(e, db, strategy, symbol, kind, cause, rule, payload):
    e.decide(db, {'id': e.stable_id('corp', e.NS, strategy, symbol, payload.get('ex_date'), kind), 'strategy': strategy, 'symbol': symbol}, e.now(), 'HOLD', cause, rule, payload)

def apply(e, db, day, opens=None):
    opens = opens or {}
    day = str(day)
    actions = db.query('SELECT * FROM paper_corporate_actions WHERE ex_date=?', (day,))
    actions.sort(key=lambda a: (a['symbol'], ORDER.get(a['kind'], 9), a['kind']))
    applied = []
    for action in actions:
        kind = action['kind']
        positions = db.query('SELECT * FROM paper_positions WHERE namespace=? AND symbol=? AND qty>0', (e.NS, action['symbol']))
        for p in positions:
            strategy, symbol, qty = p['strategy'], p['symbol'], int(p['qty'])
            at = e.now().isoformat()
            if kind in MANUAL:
                if not _seen(db, 'paper_manual_reviews', e.NS, strategy, symbol, day, kind):
                    db.batch([('INSERT INTO paper_manual_reviews(namespace,strategy,symbol,ex_date,kind,recorded_at) VALUES(?,?,?,?,?,?)', (e.NS, strategy, symbol, day, kind, at))])
                    _note(e, db, strategy, symbol, kind, 'CORP_ACTION_MANUAL_REVIEW', 'dividend_classification_requires_manual_review' if kind in DIVIDEND_REVIEW else 'rights_demerger_merger_not_applied', {'ex_date': day, 'kind': kind, 'quantity': qty})
                applied.append({'symbol': symbol, 'strategy': strategy, 'kind': kind, 'result': 'manual_review'})
                continue
            if kind == 'CASH_DIVIDEND':
                amount = int(action.get('cash_paise') or 0) * qty
                if _credit(db, e, e.NS, strategy, symbol, day, kind, amount, at):
                    _note(e, db, strategy, symbol, kind, 'CASH_CREDITED', 'cash_dividend_credited_without_state_scaling', {'ex_date': day, 'kind': kind, 'quantity': qty, 'amount_paise': amount})
                applied.append({'symbol': symbol, 'strategy': strategy, 'kind': kind, 'result': 'cash', 'amount_paise': amount})
                continue
            if kind not in SCALED:
                continue
            ratio = _ratio(action)
            if ratio is None:
                if not _seen(db, 'paper_manual_reviews', e.NS, strategy, symbol, day, kind):
                    db.batch([('INSERT INTO paper_manual_reviews(namespace,strategy,symbol,ex_date,kind,recorded_at) VALUES(?,?,?,?,?,?)', (e.NS, strategy, symbol, day, kind, at))])
                    _note(e, db, strategy, symbol, kind, 'CORP_ACTION_MANUAL_REVIEW', 'scaled_action_missing_ratio', {'ex_date': day, 'kind': kind, 'quantity': qty})
                applied.append({'symbol': symbol, 'strategy': strategy, 'kind': kind, 'result': 'manual_review'})
                continue
            num, den = ratio
            if kind == 'EXTRAORDINARY_DIVIDEND':
                amount = int(action.get('cash_paise') or 0) * qty
                if _credit(db, e, e.NS, strategy, symbol, day, kind, amount, at):
                    _note(e, db, strategy, symbol, kind, 'STATE_SCALING', 'extraordinary_dividend_credits_cash_and_scales_state', {'ex_date': day, 'kind': kind, 'quantity': qty, 'amount_paise': amount})
            if not _seen(db, 'paper_state_scales', e.NS, strategy, symbol, day, kind):
                new_qty = (qty * num) // den
                if new_qty < 1:
                    if not _seen(db, 'paper_manual_reviews', e.NS, strategy, symbol, day, kind):
                        db.batch([('INSERT INTO paper_manual_reviews(namespace,strategy,symbol,ex_date,kind,recorded_at) VALUES(?,?,?,?,?,?)', (e.NS, strategy, symbol, day, kind, at))])
                        _note(e, db, strategy, symbol, kind, 'CORP_ACTION_MANUAL_REVIEW', 'scaled_quantity_would_be_zero', {'ex_date': day, 'kind': kind, 'quantity': qty})
                    applied.append({'symbol': symbol, 'strategy': strategy, 'kind': kind, 'result': 'manual_review'})
                    continue
                fraction_num = (qty * num) % den
                def scaled(price):
                    return None if price is None else inr(scale_paise(cap.price_paise(price), num, den))
                entry, water, mark = scaled(p['entry_price']), scaled(p['high_water']), scaled(p['mark_price'])
                db.batch([('UPDATE paper_positions SET qty=?,entry_price=?,high_water=?,mark_price=?,updated_at=? WHERE namespace=? AND strategy=? AND symbol=?', (new_qty, entry, water, mark, at, e.NS, strategy, symbol))])
                state = db.query('SELECT * FROM paper_indicator_state WHERE namespace=? AND strategy=? AND symbol=?', (e.NS, strategy, symbol))
                if state:
                    row = state[0]
                    db.batch([('UPDATE paper_indicator_state SET upperband=?,lowerband=?,prev_close=?,updated_at=? WHERE namespace=? AND strategy=? AND symbol=?', (scaled(row['upperband']), scaled(row['lowerband']), scaled(row['prev_close']), at, e.NS, strategy, symbol))])
                db.batch([('INSERT INTO paper_state_scales(namespace,strategy,symbol,ex_date,kind,old_qty,new_qty,fraction_num,fraction_den,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?)', (e.NS, strategy, symbol, day, kind, qty, new_qty, fraction_num, den, at))])
                _note(e, db, strategy, symbol, kind, 'STATE_SCALING', 'split_bonus_scales_price_and_quantity', {'ex_date': day, 'kind': kind, 'old_qty': qty, 'new_qty': new_qty, 'fraction_num': fraction_num, 'fraction_den': den, 'entry_price': entry, 'high_water': water})
            scale = db.query('SELECT fraction_num,fraction_den FROM paper_state_scales WHERE namespace=? AND strategy=? AND symbol=? AND ex_date=? AND kind=?', (e.NS, strategy, symbol, day, kind))
            open_price = opens.get(symbol)
            if scale and scale[0]['fraction_num'] and open_price:
                credit = scale[0]['fraction_num'] * cap.price_paise(open_price) // scale[0]['fraction_den']
                credit_kind = 'FRACTIONAL_OPEN' if kind == 'SPLIT' else 'FRACTIONAL_' + kind
                if _credit(db, e, e.NS, strategy, symbol, day, credit_kind, credit, at):
                    _note(e, db, strategy, symbol, credit_kind, 'STATE_SCALING', 'fractional_remainder_credited_at_post_adjustment_open', {'ex_date': day, 'kind': kind, 'amount_paise': credit, 'open': open_price})
            applied.append({'symbol': symbol, 'strategy': strategy, 'kind': kind, 'result': 'scaled'})
    return applied
