# 2027 holiday calendar — procedure and checklist (PROPOSE ONLY, do not edit config.json)

Status: **PROPOSAL ONLY — `config.json` was not changed.** This is the procedure to follow once
NSE publishes the 2027 holiday list. No 2027 dates are guessed anywhere in this document.

## Current state

`config.json` calendar section today:

- `calendar_valid_from`: 2026-11-01, `calendar_valid_through`: 2026-12-31.
- `holidays`: 2026-11-10, 2026-11-24, 2026-12-25.
- `calendar_sources`:
  - https://www.nseindia.com/resources/exchange-communication-holidays
  - https://nsearchives.nseindia.com/content/circulars/CMTR76941.pdf (the NSE circular for the
    2026 trading holidays).

## What happens after 2026-12-31 if nothing is done

The calendar fails closed. `session()` (`engine.py:79`) returns `None` for any date outside
`calendar_valid_from` … `calendar_valid_through`, so `is_open()` is False, the monitor returns
`market_closed_or_calendar_unverified` and takes no fills, and the scan returns
`calendar_unverified`. `next_session` / `completed_session` raise
`calendar_unverified_next_session` / `calendar_unverified_completed_session`. **No trading
happens until the calendar is extended** — that is the safe behaviour, but it means a missed
update costs every session from 2027-01-01 until it is fixed.

`config.json` is not in `freeze_lock.json` (`LOCKED_FILES` in `capital.py` are only
`capital_policy.json`, `charge_policy.json`, `charge_rates.json`), so a calendar update does not
need a lock refresh — but it gates trading, so it still needs review and a test run before it
goes live.

## Procedure (when NSE publishes the 2027 list)

1. **Watch for the circular.** From early December 2026, check the NSE holidays page
   (https://www.nseindia.com/resources/exchange-communication-holidays) weekly. NSE publishes
   the next year's trading holidays as a circular on that page and in the archives. The 2027
   circular number is not known yet — **do not guess it, and do not copy the 2026 circular
   number**. The circular to use is the one the holidays page links to for 2027.
2. **Download the circular and extract the dates.** Take the holiday dates from the circular PDF
   itself (not from a news article or a broker's summary). Record the circular number, its date,
   and its URL.
3. **Cross-check.** Compare the circular's dates against the NSE holidays page listing for 2027;
   they must agree. If they disagree, stop and ask — do not pick one silently.
4. **Edit `config.json` only.** Update exactly these keys, nothing else:
   - `holidays`: the 2027 holiday dates from the circular (ISO `YYYY-MM-DD`, sorted).
   - `calendar_valid_from`: 2027-01-01.
   - `calendar_valid_through`: 2027-12-31.
   - `calendar_sources`: keep the holidays page URL and replace the 2026 circular URL with the
     2027 circular's URL.
   - Do NOT touch any other key (in particular, do not touch `indicator_excluded_sessions` or
     any strategy, prefilter, capital or charge number).
5. **Verify before it goes live.** Run the calendar/session tests and confirm: a known 2027
   holiday is not a session, the first 2027 trading day is a session, and a date after
   2027-12-31 still fails closed. Confirm the engine starts and the scan reports a valid
   calendar. Only then deploy.
6. **Record it.** Note the circular number, its URL, and the date the update was applied in the
   report for that change, so the next year's update has a trail to follow.

## Checklist

- [ ] NSE 2027 holiday circular found on the holidays page (number + URL recorded, not guessed).
- [ ] Dates extracted from the circular PDF and cross-checked against the holidays page listing.
- [ ] `config.json`: `holidays`, `calendar_valid_from` (2027-01-01), `calendar_valid_through`
      (2027-12-31), `calendar_sources` updated; nothing else changed.
- [ ] Calendar/session tests pass; a 2027 holiday is not a session; 2027-01-01 onwards trades;
      beyond 2027-12-31 still fails closed.
- [ ] Change reviewed (a calendar change gates all trading) and deployed before 2026-12-31.
- [ ] Circular number, URL and application date recorded.

## If the circular is late

If NSE has not published the 2027 list by mid-December 2026, escalate: the operator checks the
holidays page every day from 15 Dec, and if there is still no circular by the last trading session
of 2026, the system stays fail-closed into January (no trading) rather than trading on guessed
dates. Never invent a holiday list.
