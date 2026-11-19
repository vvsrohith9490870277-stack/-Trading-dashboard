# Persistent NSE 403 on reference feeds — proposal (PROPOSE ONLY, not implemented)

Status: **PROPOSAL ONLY — no code changed, and nothing here changes what counts as "loaded".**
The senior takes this to the user.

## What happened, with evidence

NSE's GSM and corporate-actions endpoints returned HTTP 403 on 12 Nov. This is not a new failure
mode: `reference_feeds.py` `NseFetcher` documents that "NSE's CDN refuses non-browser user agents
outright and refuses whole sessions at random (HTTP 403 for every request in the session, roughly
4 in 10 fresh sessions observed 11 Nov 2026)".

## Which fallbacks exist today (from the code and tests)

- **Session handling** (`reference_feeds.py`, `NseFetcher`): a browser-like session with a warm-up
  request (`nse_warmup_url`: https://www.nseindia.com/api/marketStatus). Up to `max_attempts: 6`
  tries with exponential backoff (`backoff_seconds: 2`, doubling, capped at
  `backoff_max_seconds: 15`). A refused session (401/403/429/500/502/503/504, or an HTML error
  page instead of data) is discarded and a fresh session is tried. HTML error pages are never
  parsed as data. Non-NSE URLs get the same bounded retries.
- **Corporate actions**: primary NSE API → http2 fetch → PR zip archive
  (`load_pr_corporate_actions`, `pr_zip_get`, `ca_from_pr_zip` — the archive at
  `https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR{ddmmyy}.zip`).
- **GSM**: `gsm_from_archive` — the NSE archive GSM CSV is parsed when the GSM API fails.
- **ASM**: `asm_from_archive` — same pattern for the ASM list.
- **Series**: `series_from_master` — the series comes from the NSE master file, not the
  per-symbol API.
- `reference_feeds.refresh()` reports per source: each source records `status: 'ok'` or
  `'unavailable'` with its error and which fallback (`via`) was used, and writes only what parsed.
  A dry run (`dry_run=True`) reports without writing.

## What happens on a day when every source 403s

- `refresh()` returns every source `unavailable` and writes nothing new. The session's reference
  status in `paper_meta` does not have all of `surveillance_required_sources`
  (`['asm', 'gsm', 'series']` in `config.json`, `surveillance_fail_closed: true`).
- `surveillance_ready()` (`engine.py:383`) therefore returns `ready: False`, and `_fill`
  (`engine.py`, BUY only) records a `SKIP` decision `SURVEILLANCE_DATA_MISSING`
  ("new_entries_blocked_until_asm_gsm_series_loaded_for_session") and returns without filling.
  The signal stays pending, so it is retried on the next poll if the feeds recover.
- **SELLs are not blocked**: the surveillance check is inside `if s['side']=='BUY':` — exits still
  run, so a held position can always be closed.
- The monitor and scan keep running (quotes, marks and exits do not need the reference feeds);
  only new entries stop.

## Is there an operator alert?

Partially. `_surveillance_alert()` (`engine.py`) emits a `surveillance_data_missing` audit event
(severity ERROR, with the reason and the unavailable sources) **once per session day**, the first
time a BUY is blocked. That event is in the audit log (`paper_events` / the decision chain), so
an operator who reads the log sees it. But there is **no push alert**: nothing sends a WhatsApp
message, an email, or any other notification when the feeds fail. If nobody opens the audit log,
a 403 day looks like a quiet day with no entries.

## Proposals (improvements only — do not change what counts as "loaded")

### (a) Keep as is

- The fail-closed gate is doing its job: no BUY is entered on unverified surveillance data, and
  exits still work. The 403s are absorbed by retries and archives most days.
- Risk: a full-403 day still blocks every entry with no push alert, and the operator may not
  notice until the session is over.

### (b) Add a push alert on `surveillance_data_missing` — RECOMMENDED

- What it is: when `_surveillance_alert()` fires, also send one push notification (WhatsApp or
  email — needs a channel decision, same as the session-checker proposal) saying which sources are
  unavailable. Once per session day, same as the audit event.
- Effect on frozen rules: none. It changes no gate, no source list, no threshold — it only
  notifies.
- What would be installed where: a notifier call in `_surveillance_alert()` (`engine.py`) plus
  the channel's credentials (environment variables on the box, never in the repo); a test that a
  blocked BUY produces exactly one alert per session day.
- Risks: needs an alert channel the box can reach (the box has no WhatsApp sender today); a
  misconfigured channel could spam or silently drop the alert.

### (c) Refresh the reference feeds earlier, with more retry runway

- What it is: run `refresh()` well before 09:15 (e.g. at 08:30, and again at 09:00 if any source
  is still unavailable), so a 403 has time for its 6 attempts plus archive fallbacks before the
  first BUY is attempted. Today the feeds are refreshed as part of the session flow, so a
  morning 403 can block the first entries of the day.
- Effect on frozen rules: none — same sources, same "loaded" definition, just earlier.
- Risks: small; the refresh already holds a lock (`reference_feeds.lock`) so a second run cannot
  overlap the first.

### (d) Do NOT do: cache the last good reference status, or drop a source from the gate

- Treating yesterday's ASM/GSM/series as "loaded" for today, or removing a source from
  `surveillance_required_sources`, would let BUYs through on unverified surveillance data. That
  changes what counts as "loaded" and is out of scope for this proposal. If the user ever wants
  it, it needs a separate decision with the frozen-rules implications spelled out.

## Recommendation

Do (b) — a push alert is the missing piece: the gate already fails closed correctly, but nobody is
told. Do (c) as well if the 403s keep costing morning entries. Do not do (d).
