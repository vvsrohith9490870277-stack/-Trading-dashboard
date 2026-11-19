# Exit slippage fallback when ADV is unavailable — proposal (Part B)

Status: **PROPOSAL ONLY — not implemented.** Part A (visibility) is done on branch
`fix/paper-exit-slippage`: when an exit gets no slippage, the fill decision payload and the
returned fill JSON (what the log records) now say `exit_slippage_unavailable` with the reason
`adv_below_floor` or `lagged_adv_history_short`. No price changed. This document asks the senior
to pick one option below; implementation happens only after that reply.

## Problem, with evidence

`db.mjs:128-131`: a SELL starts as `slip={applied:false, rule:'exit_slippage_unavailable'}` and
gets slippage only if `laggedAdv()` returns `ok` (median traded value at or above the ADV floor).
Otherwise an exit of an illiquid name, or one with a short ADV history, silently fills at the raw
reference price. Those are the names where real exits cost the most. BUY entries below the floor
are skipped (`ADV_FILTER_SKIP`), but a held position can still need an exit. BE/BZ exits already
use the stress multiplier (`db.mjs:129`).

Measured shortfall (EOD `performance.implementation_shortfall`): **+18.6 bps vs the signal close
and +3.2 bps vs the fill-day open on 23 fills**. Paper fills use the model's own price, so paper
data alone cannot prove the model is realistic. Do not claim any fallback below is "calibrated".

## The model today (frozen — nothing here changes a number)

`fees.mjs:38` `executionSlippage()`, all numbers from `capital_policy.json`:

- `bps = base + k × (order / ADV)`, where `k = slippage_participation_multiplier_bps / participation_rate = 10 / 0.005 = 2000`.
- `base = slippage_base_bps_through_20cr = 8` up to `slippage_tier_threshold_inr = 200,000,000` (₹20 cr), else `slippage_base_bps_above_20cr = 5`.
- Stress multiplies the whole bps by `slippage_stress_multiplier = 1.5`.
- BUY price = reference × (1 + bps/10000); SELL price = reference × (1 − bps/10000), rounded half-up to paise. Quantity stays on the reference price.

Worked example used for every option below — SELL 500 shares, reference ₹110, order notional
₹55,000, ADV unavailable (median ₹90,000,000, below the ₹200,000,000 floor; or history short).
For reference, if that ₹90M ADV *were* known, the full model would give
bps = 8 + 2000 × (55,000 / 90,000,000) = 8 + 1.222 = 9.222 bps → price ₹109.90, cost ₹50.
Today the same exit fills at ₹110.00, cost ₹0.

## Options

### (a) Apply the base rate only, with no participation term — RECOMMENDED

- Formula: SELL price = reference × (1 − `slippage_base_bps_through_20cr` / 10000), rounded half-up to paise. The participation term is dropped because it needs ADV, which is exactly what is missing.
- Worked example: 8 bps → 110 × (1 − 0.0008) = 109.912 → **₹109.91**, cost **₹45** (vs ₹50 the full model would charge at ₹90M ADV, vs ₹0 today).
- What changes if picked: `db.mjs` SELL branch — when `laggedAdv()` is not ok, compute the fill price from the base bps instead of leaving the reference price untouched, and record a new `slippage_rule` (e.g. `base_only_fallback`) with the reason. `executionSlippage()` itself needs no change. The same formula must be added to the adaptive mirror (see below).
- Why recommended: uses only numbers that already exist; no new parameter; no invented ADV. It charges the 8 bps the model already assigns to every sub-₹20 cr ADV name, so it is the smallest honest step toward the measured shortfall.
- Risks: still understates the full model by the participation term (~1.2 bps in the example, more for larger orders); an ADV-below-floor name may deserve *more* slippage, not less — this is a floor, not a cure.

### (b) Apply the stress multiplier on that base

- Formula: SELL price = reference × (1 − (`slippage_base_bps_through_20cr` × `slippage_stress_multiplier`) / 10000).
- Worked example: 8 × 1.5 = 12 bps → 110 × (1 − 0.0012) = 109.868 → **₹109.87**, cost **₹65**.
- What changes if picked: same place as (a), with the 1.5× multiplier.
- Why not recommended: the stress multiplier exists for BE/BZ series risk, not for missing ADV. Applying it to every illiquid exit conflates two different risks and overstates the cost for ordinary illiquid names. It would also make the fallback *harsher* than the full model (12 bps vs 9.222 bps in the example) without evidence.

### (c) Keep no slippage but flag it

- Formula: none. Price stays the reference price; the exit is recorded as `exit_slippage_unavailable` with its reason (Part A, already done).
- Worked example: **₹110.00**, cost **₹0**, flagged.
- What changes if picked: nothing. This is the status quo plus visibility.
- Why not recommended: it keeps the known bias the audit flagged — paper P&L looks better than reality on exactly the exits where real costs are highest. Visibility alone does not fix the number; it only labels it.

## What else must change with (a) or (b)

- **Adaptive mirror.** `adaptive_engine.py:399` `slippage_fill_paise()` mirrors `executionSlippage()`, and `tests/test_adaptive_independent_audit.py:67` checks the mirror. Any live change here needs the same fallback in the adaptive replica, and its mirror test extended. List that as a follow-up; do NOT edit adaptive files in the paper branch.
- **No frozen number changes.** All three options use only numbers already in `capital_policy.json` (8, 5, 1.5, 10, 0.005, 200,000,000). Do NOT add or change any slippage number: that would change the `freeze_capital_v2` lock. The code change is a new fallback path, not a new number.
- **Decision payload.** Whichever option is picked, the fill decision payload should keep recording `slippage_rule` and `slippage_reason` (Part A), so a fallback fill is distinguishable from a tiered one in the audit trail.
- **No fee change.** Slippage is a price adjustment, not a charge; `paper_fill_costs` and the charge policy are untouched by any option.

## Decision needed

Pick (a), (b), or (c). Implementation (paper branch + adaptive mirror + mirror test) happens only after the senior replies with an option.
