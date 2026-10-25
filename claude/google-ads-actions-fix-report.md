# Fix report: asset tools bug and RSA price lock

**Date:** 25 October 2026
**Spec:** `google-ads-actions-asset-tools-bug.md` (23 October 2026)
**Service:** `google-ads-actions`, project `djavaweb-ads-mcp`, `us-central1`
**Code:** `ardhofortana/google-ads-actions`, commit noted at the bottom
**Status:** both problems fixed and tested offline; **deploy and live tests pending**

## Summary

| # | Problem | Cause | Fix |
|---|---|---|---|
| 1 | Every asset tool failed with `EXPECTED_REFERENCED_FIELD_IN_SELECT_CLAUSE ... 'campaign.id'` | The one `campaign_asset` query (used by the duplicate check, the "before" snapshot, the after-read and the drift check) filtered on `campaign.id` without selecting it | `campaign.id` added to that SELECT. Also added to the negative-keyword, keyword and ad-schedule queries, which filter on it too (harmless there, and keeps the rule uniform). A new test fails if any query filters on another resource's field without selecting it. |
| 2 | `propose_update_rsa` refused every headline edit on ads with a human-added price (`Projects From $1,500`) | Copy rules ran on every line, including unchanged live lines | Copy rules run only on new or changed lines (details below) |

No tools, guardrails, allowlist or audit-log behaviour changed beyond this.

## Problem 2: rule as implemented

- The live headlines and descriptions are read for the "before" snapshot, as before.
- A line whose text, trimmed, **exactly** matches a live line of the same type (headline vs description) is kept as it is and skips the copy rules. Its pin may change. The match is case-sensitive, so `projects from $1,500` counts as changed.
- Every new or changed line goes through every rule: price, 30/90 characters, no em or en dashes, no `!`, no OpenAI/ChatGPT/Claude/Gemini/GPT.
- Count (3 to 15 headlines, 2 to 4 descriptions), uniqueness and pin checks still cover every line.
- The before → after table shows kept lines as `(unchanged, existing)`.
- The drift check at execute time is unchanged: if the live ad changed after the proposal, execution is refused.
- No general price allowlist was added.

## Table wording (asset tools)

Add: `not linked` → `new asset, linked`. Remove: `linked` → `unlinked (asset stays in the library)`. Re-link (undo of a removal): `not linked` → `linked`.

## Tests

Offline results are on the in-memory fake account (`tests/test_fix_assets_rsa.py`, `tests/test_changes.py`). Live results come from Claude on the real account after deploy, with nothing executed.

| # | Test | Expected | Offline | Live |
|---|---|---|---|---|
| 1 | `propose_add_campaign_assets` CALLOUT `["Test callout one"]` | Proposal with table, then cancel | ✅ | Pending |
| 2 | `propose_add_campaign_assets` SITELINK to `https://djavaweb.com/ai-consultation/#faq` | Proposal, then cancel | ✅ | Pending |
| 3 | `propose_remove_campaign_asset` on an existing callout | Proposal, linked → unlinked, then cancel | ✅ | Pending |
| 4 | `propose_update_rsa` ad `828606487337`, all 15 live headlines, one non-price headline changed | Proposal; `Projects From $1,500` shown `(unchanged, existing)`; cancel | ✅ | Pending |
| 5 | Same ad, `Projects From $1,500` → `Projects From $2,000` | Refused (price, changed line) | ✅ | Pending |
| 6 | Same ad, `Only $99 Today` replacing another headline | Refused (price, new line) | ✅ | Pending |
| 7 | Same ad, all identical but one new headline of 31 characters | Refused (length, new line) | ✅ | Pending |
| — | `list_pending_changes` at the end | Empty | ✅ | Pending |

Extra offline checks: a lower-case copy of the price headline is refused (exact match); an em dash in a new line is refused; executing a headline change writes the existing price line back verbatim; a description with a live price can be kept; a pin-only change on an ad with a price line is allowed. The query rule test fails on the old `campaign_asset` query.

Offline suite: 64 tests, all passing.

## Not covered

The GAQL error comes from Google, so the fake account can't reproduce it. Live tests 1 to 3 are the real check.

**Commit:** see the deploy output (image tag = commit).
