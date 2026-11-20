# Proposal: keep the real reasons behind the 23 unclassified 19-Nov events

**Status: proposal only — do NOT implement from this doc without senior approval.**
`audit.py` and every Track A runtime file are frozen for brief
adaptive-support-tuning-01. This doc is the exact change the senior would
make, so the Support Agent can stop working around the rewrite.

## Root cause

`audit.py` `clean()` (line ~33) rewrites any `reason` / `reasons` /
`error_code` not in the `REASONS` allow-list (`audit.py:11`) to
`'unclassified_condition'`. Three emitters use reasons that are not
allow-listed, so on 19 Nov 2026 the Support Agent saw 23 events it could
not classify (finding `error_taxonomy_gap:2026-11-19`, namespace `paper_v2`):

| Emitter | Reason as emitted | Events 19 Nov |
|---|---|---|
| `automation.py:495,503` (`host_freeze_detected`) | `host_vm_paused` | 11 (outcome `failed`, WARN) |
| `sessiond.py:110` (`sessiond_wake`) | `box_resumed_catching_up` | 11 (outcome `ok`, INFO) |
| `automation.py:379` (`session_outage_recorded`) | outage `kind`: `no_monitoring`, `monitor_coverage_low`, `scan_missing`, `eod_missing` | 1 (outcome `failed`, ERROR) |

The same box-sleep windows also produced 9 `monitor_staleness` gaps (up to
970 s) that the Support Agent could not attribute to the pause.

## Exact allow-list additions (`audit.py:11`, `REASONS`)

Add these six reason strings to the `REASONS` set (they are plain reason
words, not patterns, so they need no regex change):

```
host_vm_paused
box_resumed_catching_up
no_monitoring
monitor_coverage_low
scan_missing
eod_missing
```

No change to `allowed_reason()` is needed: plain membership in `REASONS`
is enough. The `auth_circuit_open_*` regex precedent shows the pattern
for families; these six are fixed words, so set membership is the right
shape.

## What must change with it

1. **Nothing else in `audit.py` is required for the reasons to survive.**
   `clean()` keeps a `reason` that `allowed_reason()` accepts, and the
   `data_json` payloads keep `started` / `stopped` / `frozen_seconds` /
   `coverage` / `checks` (all already in the `FIELDS` filter).
2. **One gap remains even after this change:** `session_outage_recorded`
   also passes `missed_session=<date>`, but `missed_session` is not in the
   `FIELDS` filter in `clean()`, so that field is still dropped from
   `data_json`. If the senior wants the missed date preserved, add
   `'missed_session'` to `FIELDS` as well. It is not needed for the Support
   Agent (the outage `kind` in `reason` is enough to tell a missed session
   from a coverage failure), so this is optional.
3. **Support Agent impact (no code change needed):** `support_taxonomy.py`
   (A1) classifies these events from `(event_type, outcome)` today, so it
   keeps working either way. After the allow-list lands, the same events
   will also carry their real reasons, and `error_taxonomy_gap` will stop
   seeing them as candidates at all (they will no longer match
   `ERROR_TAXONOMY_PATTERN`). The `box_host_pause` finding and the
   `explained_by_host_pause` gap tag keep working unchanged.
4. **No Track A behaviour change:** the allow-list only affects what
   `clean()` writes into `data_json`; no decision, fill, or ordering path
   reads these reasons. The change is safe to land on its own branch and
   needs no migration (old spool rows keep `unclassified_condition`; only
   new events carry the real reasons).

## Verification after landing

Re-run the A1 gate check (G7a) against a fresh session with a host freeze:
`error_taxonomy_gap` must not fire for `host_freeze_detected` /
`sessiond_wake` / `session_outage_recorded`, and a `box_host_pause`
finding must still appear with the same `total_paused_seconds`.
