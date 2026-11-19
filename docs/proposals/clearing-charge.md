# Clearing charge for cash delivery — proposal (PROPOSE ONLY, not implemented)

Status: **PROPOSAL ONLY — no file in the repo was changed.** This document records what the public
sources actually say, what would change if a clearing charge were ever introduced, and the
recommendation. It waits for the user's approval, which comes via the senior.

## Finding: the audit's number does NOT confirm for delivery

The audit said Fyers' public charges list shows a delivery clearing charge of 0.0005% (about
₹0.30 per ₹50,000 order including GST). Checked against the live public pages on 2026-11-19, that
number is the **Futures** column, not delivery.

**Fyers charges list** — https://fyers.in/charges-list (page crawled 2026-11-18 18:33 UTC), "Other
charges" section, Clearing Charges row, columns DELIVERY / INTRADAY / FUTURES / OPTIONS:

| Delivery | Intraday | Futures | Options |
|---|---|---|---|
| 0 | 0 | 0.0005% | 0.009% |

Delivery clearing is **0**. The 0.0005% the audit quoted is the Futures clearing charge. The Fyers
page carries **no effective date** for the clearing row, so none can be quoted for it.

The same page confirms the rates the policy already uses: NSE exchange transaction 0.0030699%,
SEBI turnover fee ₹10/crore, NSE IPFT ₹0.01/crore, stamp 0.015% on buy, DP ₹12.50 + GST. It also
states GST as "18% on (Brokerage + Exchange Transaction charges + Clearing charges + SEBI Turnover
Fee + NSE IPFT)" for all four columns — i.e. **clearing is in the GST base** on the Fyers page,
while `charge_rates.json` lists the GST base as brokerage, exchange, SEBI, IPFT only.

**NSE Clearing equities page** — https://www.nseclearing.in/equities (crawled 2026-11-18 18:33 UTC):
describes clearing and settlement mechanics (T+1 settlement, multilateral netting, direct payout
to client demat accounts) and lists no client clearing-charge rate for the cash market at all. It
does not support a delivery clearing charge either.

**Recommendation: do NOT introduce a delivery clearing charge.** The current
`clearing: {num: 0, den: 1, source_text: "0 assumed"}` in `charge_rates.json` and
`cash_clearing: "0 assumed; separate clearing charges shown for derivatives, not cash"` in
`charge_policy.json` are correct for delivery and confirmed by the Fyers page. The only wording
gap is the GST base (see option B below), which is numerically nil while clearing is 0.

## Current state (for the record)

- `charge_policy.json` version `FYERS_STANDARD_NSE_DELIVERY_ESTIMATE_2026_11_09_v2`,
  effective_from 2026-11-07, valid_through 2026-12-31, retrieved_on_IST 2026-11-07.
- `charge_rates.json` `charge_policy_version` matches that version; `clearing` is 0; GST base is
  `["brokerage", "exchange", "sebi", "ipft"]`.
- Frozen digests (`freeze_lock.json`, locked_on_IST 2026-11-11):
  `capital_policy.json` dea4ef3b9a48191a3f94249466f232656671cd772c3fb04eb0d59e4b8cb82732,
  `charge_policy.json` 9a8c3955edd4091cbc8e025afe9b3996fd5c41262a03dd89ab322155f3445fea,
  `charge_rates.json` 40c6b5a812670307ad7a0f7be9394fc81e24057aa12327e14d7f1a74159b1baa;
  `capital_policy_version` freeze_capital_v2.

## Option A (NOT recommended): introduce a 0.0005% delivery clearing charge

Use only if Fyers or NSE Clearing ever publishes a non-zero delivery clearing charge. The numbers
below use the audit's 0.0005% purely as a **hypothetical illustration** — it is not confirmed and
not recommended. The impact math was computed read-only on throwaway copies in `/tmp`
(`/tmp/clearing-impact`), never in the repo, using the real `fees.mjs` `estimate()`.

Exact JSON diff (`charge_rates.json`):

```diff
-  "charge_policy_version": "FYERS_STANDARD_NSE_DELIVERY_ESTIMATE_2026_11_09_v2",
+  "charge_policy_version": "FYERS_STANDARD_NSE_DELIVERY_ESTIMATE_2026_11_19_v3",
-  "clearing": {"num": 0, "den": 1, "source_text": "0 assumed"},
+  "clearing": {"num": 5, "den": 1000000, "source_text": "0.0005% both sides"},
-  "gst": {"num": 18, "den": 100, "base": ["brokerage", "exchange", "sebi", "ipft"], "source_text": "18% brokerage+exchange+SEBI+IPFT; DP GST separate"},
+  "gst": {"num": 18, "den": 100, "base": ["brokerage", "exchange", "sebi", "ipft", "clearing"], "source_text": "18% brokerage+exchange+SEBI+IPFT+clearing; DP GST separate"},
```

Exact JSON diff (`charge_policy.json`):

```diff
- "version":"FYERS_STANDARD_NSE_DELIVERY_ESTIMATE_2026_11_09_v2",
+ "version":"FYERS_STANDARD_NSE_DELIVERY_ESTIMATE_2026_11_19_v3",
- "effective_from":"2026-11-07","valid_through":"2026-12-31","retrieved_on_IST":"2026-11-07",
+ "effective_from":"2026-11-19","valid_through":"2026-12-31","retrieved_on_IST":"2026-11-19",
- "gst":"18% brokerage+exchange+SEBI+IPFT; DP GST separate",
+ "gst":"18% brokerage+exchange+SEBI+IPFT+clearing; DP GST separate",
- "cash_clearing":"0 assumed; separate clearing charges shown for derivatives, not cash"},
+ "cash_clearing":"0.0005% both sides"},
```

Clearing **does** join the GST base: the Fyers page puts clearing in the GST base, and `fees.mjs`
`estimate()` already computes `clearing` as a component and takes GST over `rates.gst.base`, so
adding `"clearing"` to the base is the only change needed for GST to apply to it.

New digests for this hypothetical (from `python3 capital.py --print-lock` on the throwaway copy;
`capital_policy.json` untouched, `capital_policy_version` still `freeze_capital_v2`):

```json
{
  "files": {
    "capital_policy.json": "dea4ef3b9a48191a3f94249466f232656671cd772c3fb04eb0d59e4b8cb82732",
    "charge_policy.json": "6e2c4e7bc4c541aa3058a9db313f5e79f6ffac16ce914757d719190e25854d57",
    "charge_rates.json": "56eecedf666556fa83ca30f52d802dd81495c7a5331042b5f8e9a94374a3c6fa"
  },
  "capital_policy_version": "freeze_capital_v2"
}
```

Steps to refresh `freeze_lock.json` if this is ever adopted: edit the two JSON files (new version
`..._2026_11_19_v3`), run `python3 capital.py --print-lock` in `automation/paper-trading`, paste
its output into `freeze_lock.json` (update `locked_on_IST` too), and commit the JSON files and the
lock together. A mismatch makes the engine and `db.mjs` refuse to start (fail closed).

Impact, read-only, real `estimate()` on the throwaway copy (turnover ₹50,000, single order, no
prior, no DP):

| | Current | Hypothetical 0.0005% | Difference |
|---|---|---|---|
| BUY ₹50,000 total | ₹82.96 (8296 paise) | ₹83.26 (8326 paise) | **+₹0.30** (clearing ₹0.25 + GST on it ₹0.05) |
| SELL ₹50,000 total | ₹75.46 (7546 paise) | ₹75.76 (7576 paise) | **+₹0.30** (same split) |

Synthetic 23-fill example (no production data exists; clearly synthetic): 23 fills of ₹50,000
turnover each → +₹0.30 each → **+₹6.90** for 23 one-sided fills; a 23-fill round trip (23 buys +
23 sells) → **+₹13.80**. The audit's "about ₹0.30 per ₹50k order including GST" is arithmetically
right for a 0.0005% rate — the rate itself is the part that does not confirm for delivery.

## Option B (optional wording fix, zero numeric effect): add clearing to the GST base only

Change only the GST base list (and its `source_text`) in `charge_rates.json` and the `gst` text in
`charge_policy.json`, keeping `clearing` at 0. This matches the Fyers page's GST wording. It
changes no number: verified on a throwaway copy (`/tmp/clearing-gstonly`) that the ₹50,000 BUY and
SELL estimates are identical (8296 / 7546 paise). It still needs a new version
(`..._2026_11_19_v3`) and a lock refresh, because the files' digests change:

```json
{
  "files": {
    "capital_policy.json": "dea4ef3b9a48191a3f94249466f232656671cd772c3fb04eb0d59e4b8cb82732",
    "charge_policy.json": "8aefebde261bcd157cfdaf2eddc620424881e3dfd3ee52a59c6c7fed93e8e3f5",
    "charge_rates.json": "7dc1309b71d3def7cbd7b81e728cfe74de074b1e1bdc31c4a4ab6349b7fbaccd"
  },
  "capital_policy_version": "freeze_capital_v2"
}
```

Recommendation: do this only together with the next genuine policy change, to avoid spending a
version bump and lock refresh on a zero-effect wording fix. Or do nothing — while clearing is 0,
the GST base list cannot change any number.

## What else must change with either option

- **Version-mismatch check**: `fees.mjs:7` throws `charge_rates_policy_mismatch` unless
  `charge_rates.json`'s `charge_policy_version` equals `charge_policy.json`'s `version`. Both must
  move together.
- **Mixed daily policy**: `db.mjs:134` throws `mixed_daily_policy` if a day's `paper_fill_costs`
  rows carry different `policy_version`s. A mid-day change would block fills for the rest of that
  day; a version change must take effect from a session boundary (use `effective_from` on a new
  session day).
- **Dashboard bundled policy**: `server/exit-plan.mjs:13` imports `charge_policy.json`, so the
  dashboard serves the new version once deployed; no code change there, but the deployed bundle
  must be refreshed with the repo.
- **Adaptive fee mirror**: `adaptive_engine.py:373` `fill_charges_paise()` mirrors `fees.mjs`
  `estimate()`. Any live change here needs the same change in the adaptive replica (follow-up; do
  NOT edit adaptive files in this branch).
- **Historical days stay on the old version**: fills already recorded under
  `FYERS_STANDARD_NSE_DELIVERY_ESTIMATE_2026_11_09_v2` keep that `policy_version` in
  `paper_fill_costs`; only fills on or after `effective_from` use the new version. The
  `paper_charge_policies` table records each version's `policy_json` immutably.

## Decision needed

No approval is needed to keep the current 0 — it is confirmed correct. Approval is needed only if
the user wants option A (introduce a clearing charge if a real published rate appears) or option B
(GST-base wording fix). If neither is approved, this document stands as the record that the
audit's 0.0005% was the Futures column, and delivery clearing stays 0.
