# Change report: Manual CPC and ad group / keyword bids

**Date:** 28 October 2026
**Spec:** `google-ads-actions-manual-cpc-request.md` (28 October 2026)
**Service:** `google-ads-actions`, project `djavaweb-ads-mcp`, `us-central1`
**Code:** `ardhofortana/google-ads-actions`, commit noted at the bottom
**Status:** built and tested offline (100 tests); **deploy and live tests pending**. Nothing has been executed on the account.

## What was added

| # | Item | Behaviour |
|---|---|---|
| 1 | `propose_set_bidding` with `MANUAL_CPC` | `ad_group_default_cpc_idr` required; applied to every **enabled** ad group. `ad_group_bids {ad_group_id: cpc_idr}` overrides it and is the only way to change a paused ad group. Sets `manual_cpc.enhanced_cpc_enabled = false`. Ad group bids and the campaign switch go in **one** `GoogleAdsService.Mutate` (`validate_only` at propose). Keywords that have their own bid keep it, and the proposal warns about them. |
| 1 | Drift check | The "before" snapshot holds the strategy type, CPC ceiling, target CPA, Enhanced CPC flag and every affected ad group's `status` and `cpc_bid_micros`. Execute refuses if any of them changed. |
| 1 | Undo | Restores the previous strategy **and** the previous bids of the ad groups that changed, in one atomic change. Example: Maximize clicks with a Rp270.000 ceiling, ad groups back to Rp270.000. |
| 1 | Reverse | `MAXIMIZE_CLICKS` from Manual CPC works as before. Its undo goes back to Manual CPC. |
| 2 | `propose_set_ad_group_cpc(ad_group_id, cpc_idr)` | Manual CPC only. |
| 3 | `propose_set_keyword_cpc(items)` | Manual CPC only, up to 20 keywords. `cpc_idr` 0 or null removes the keyword's own bid (the field is cleared, so it uses the ad group default). A single-keyword form (`ad_group_id`, `criterion_id`, `cpc_idr`) is also accepted. |
| 4 | `propose_set_device_bid_adjustment(campaign_id, device, percent)` | Manual CPC only. DESKTOP / MOBILE / TABLET, −50% to +50% → bid modifier 0.5 to 1.5, on the campaign's device criterion (IDs 30000 / 30001 / 30002). 0 removes the adjustment. |

## Guardrails added (`guardrails.yaml`)

```yaml
bid_idr: {min: 20000, max: 400000, max_change_pct_per_proposal: 50}
max_keyword_bids_per_proposal: 20
device_bid_adjustment_pct: {min: -50, max: 50}
```

Everything else is unchanged: propose → execute, drift check, user allowlist, audit log, emergency pause.

## One deliberate exception: undo and the ±50% limit

A ±50% limit can't undo itself: lowering a bid by 44% (Rp270.000 → Rp150.000) needs +80% to reverse. The same was already true for budgets. An **undo** is therefore exempt from the per-proposal **change** limit, for bids and budgets only. **Approved by Ardho on 28 Oct**, with these safeguards, each covered by tests (`tests/test_undo_safety.py`):

1. **Values only from the audit log.** `propose_undo` takes only `customer_id` and `change_id`. The undo's values are built from the "before" snapshot of that executed entry. The exemption depends on a server-side reference to that entry, never on anything in a request: flags such as `restore` or `only_listed_ad_groups` sent as input are ignored, and a normal +93% proposal is still refused. At execute, the undo request is re-derived from the audit log and must match the stored request exactly. A tampered request or a tampered "before" snapshot is refused with `failure_kind: undo_mismatch`.
2. **Live must equal the original "after".** At propose and again at execute, the live values are read the same way the original "after" was read and compared with it. Any difference (budget, strategy or ceiling, any bid the change set, keyword bid, device modifier, removed negative) refuses the undo with `failure_kind: undo_live_changed`. Things the change didn't touch, such as a paused ad group's bid, don't block it.
3. **Ranges still apply.** Bids Rp20.000 to Rp400.000, budget Rp300.000 to Rp1.800.000. An undo back to a value outside the range (set by hand in the UI earlier) is refused.

The drift check and `execute_change` after approval apply as for any proposal.

## Tests

Offline results come from the in-memory fake account, seeded with the five ad groups at Rp270.000 (`tests/test_manual_cpc.py`, `tests/test_undo_safety.py`). The full offline suite has 100 tests, all passing. Live tests are proposals only: **do not execute test 1**.

| # | Test | Expected | Offline | Live |
|---|---|---|---|---|
| 1 | `propose_set_bidding` MANUAL_CPC, default 190000 | One proposal: Maximize clicks (ceiling Rp270.000) → Manual CPC; 3 enabled ad groups Rp270.000 → Rp190.000 (−30%); 2 paused untouched (listed as skipped). Cancel. | ✅ 4 operations in one validate-only call | Pending |
| 2 | MANUAL_CPC without default | Refused (required) | ✅ | Pending |
| 3 | MANUAL_CPC, default 450000 | Refused (above Rp400.000) | ✅ | Pending |
| 4 | MANUAL_CPC 190000 + `ad_group_bids` {AI Consulting `201460750059`: 100000} | Refused: "AI Consulting: Rp270,000 to Rp100,000 is a -63% change" | ✅ | Pending |
| 5 | `propose_set_ad_group_cpc` on Maximize clicks | Refused ("only allowed while the campaign uses Manual CPC") | ✅ | Pending |
| 6 | `propose_set_keyword_cpc` on Maximize clicks | Refused (same) | ✅ | Pending |
| 7 | Test 1's validate-only accepted by Google | No `validate_only` errors | n/a (needs Google) | Pending |
| 8 | `propose_set_device_bid_adjustment` MOBILE −30% on Maximize clicks | Refused (Manual CPC only) | ✅ | Pending |
| — | `list_pending_changes` at the end | Empty | ✅ | Pending |

Extra offline checks: execute the switch, then undo restores the campaign and all 5 ad groups exactly, in one atomic change; an ad group bid changed between propose and execute → refused as drift, strategy unchanged; a paused ad group changes only when listed; bid below Rp20.000 refused; unknown ad group refused; `ad_group_bids` refused for non-Manual strategies; Manual CPC → Maximize clicks and its undo; ad group CPC set and undo under Manual CPC; keyword bid set (compared with the ad group default), removed and undone; duplicate keyword, more than 20 keywords, and removing a bid that doesn't exist all refused; warning for keywords with their own bids; device −60% refused, −30% sent as bid modifier 0.7 on criterion `~30001`, undo back to 0%; MCP tool layer passes `ad_group_bids` through; no tool exposes the undo-only flags.

## Not covered offline

Whether Google accepts the exact operations (test 7) can only be shown live. Device bid adjustments use the standard device criterion IDs; if Google rejects that for this campaign, test 8 after the switch will show it.

**Commit:** see the deploy output (image tag = commit).
