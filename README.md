# google-ads-actions

An MCP server that gives Claude a small, guarded set of **write** tools for the
Djavaweb Google Ads account (customer `6775198830`, campaign `24378039028`).
It runs next to the existing read-only `google-ads-mcp` service and doesn't
replace it.

Every change goes **propose → execute**:

1. A `propose_*` tool checks the request against `guardrails.yaml`, reads the
   live "before" values, sends the mutate to Google with `validate_only=true`,
   and stores a proposal (`chg_XXXX`, expires in 24 h) in Firestore. It returns a
   before → after table.
2. `execute_change(change_id)` re-runs the same planner against the live account.
   It refuses if the proposal is missing, expired, cancelled or already executed,
   if the "before" values or the operations differ from the proposal (drift), or
   if a guardrail now fails. Otherwise it sends the stored operations in one
   atomic `GoogleAdsService.Mutate` (`partial_failure=false`), reads the "after"
   values back, and writes the audit entry.

The only one-step tool is `emergency_pause_campaign`. The server allows it only
when live data for today shows cost above 2× the daily budget, or more than 40
clicks in the last 2 hours with 0 conversions and CTR above 20%.

## Layout

| Path | What it does |
|---|---|
| `ads_actions/guardrails.yaml` | All limits. Bundled into the image, read at startup. Changing it needs a redeploy. |
| `ads_actions/server.py` | FastMCP server: Google OAuth proxy (same setup as upstream `coordinator.py`), the 403 allowlist middleware, and the tool definitions |
| `ads_actions/engine.py` | Propose / execute / undo / cancel / change log / emergency pause |
| `ads_actions/planners.py` | One planner per change type: checks, before snapshot, exact operations, after read, inverse for undo |
| `ads_actions/gateway.py` | Google Ads API calls (GAQL reads, atomic mutate), made as the signed-in user |
| `ads_actions/schedule.py` | Ad schedule validation and shifting across midnight |
| `ads_actions/text_rules.py` | Ad and asset text rules |
| `ads_actions/store.py` | Firestore `ads_actions` collection (never deletes) |
| `ads_actions/auth_storage.py` | OAuth client storage, copied from googleads/google-ads-mcp |
| `deploy-google-ads-actions.sh` | Cloud Run deploy |

## How access is restricted

- **User:** a Starlette middleware placed after FastMCP's auth answers **HTTP 403**
  to any valid token whose verified Google email isn't `ardhofortana@gmail.com`.
  Every tool checks the email again.
- **Account and campaign:** every tool takes `customer_id` and refuses anything
  but `6775198830`. Campaign IDs must be in `allowed_campaign_ids`. Ad groups,
  keywords, ads and assets are looked up and must belong to an allowed campaign.
- **Operations:** the only code that sends a real mutate is `execute_change` (on
  stored, re-verified operations) and the emergency pause. There are no tools for
  anything in brief section 5.10 (campaign create/remove, conversions, billing,
  users, targeting, networks, AI Max, broad match, auto-apply, final URLs).
- **Calls run as the user:** Google Ads requests use the signed-in user's OAuth
  token. The service account only reads secrets and Firestore.
- **No OAuth, no start:** the server exits if the OAuth client ID or secret is missing.

## Tools

| Tool | Notes |
|---|---|
| `list_pending_changes`, `get_change`, `cancel_change`, `get_change_log(days=30)` | Housekeeping |
| `propose_undo(change_id)` | Builds a normal proposal that restores the "before" values. Still needs `execute_change`. Undoing an add removes what was created; undoing an asset removal re-links the same asset. |
| `execute_change(change_id)` | See above |
| `propose_add_campaign_negatives`, `propose_remove_campaign_negatives` | Max 50. Lowercased and trimmed. Existing negatives are skipped. A negative that would block an enabled keyword in an enabled ad group is rejected with a warning naming the keyword. Keywords in paused ad groups get a warning only. |
| `propose_add_keywords`, `propose_set_keyword_status` | PHRASE or EXACT only, max 20. Warns if a campaign negative would block the new keyword. |
| `propose_set_ad_group_status`, `propose_set_campaign_status` | ENABLED or PAUSED |
| `propose_set_daily_budget` | Rp300.000 to Rp1.800.000, at most ±50% per proposal, refused if the budget is shared |
| `propose_set_bidding` | Maximize clicks (CPC ceiling Rp50.000 to Rp400.000) or Maximize conversions (target CPA Rp1.000.000 to Rp8.000.000). Refused on portfolio strategies. |
| `propose_set_ad_schedule`, `propose_shift_ad_schedule` | Whole hours, account time zone (WIB). Shift merges the hour that spills past midnight into the next day's early slot. Bid modifiers are kept. |
| `propose_update_rsa` | Headlines 3 to 15 (≤30 chars), descriptions 2 to 4 (≤90). New or changed lines: no em or en dashes, no `!`, no OpenAI/ChatGPT/Claude/Gemini/GPT, no prices. A line whose trimmed text exactly matches a live line of the same type is kept as is and shown as `(unchanged, existing)`, so a price a person added in the UI doesn't lock the ad. Final URL and paths are never in the update mask. Plain-string headlines keep the existing pin of a headline with the same text. |
| `propose_add_campaign_assets`, `propose_remove_campaign_asset` | Sitelinks (text ≤25, descriptions ≤35, URL must start `https://djavaweb.com/`), callouts ≤25, structured snippets (Google's header list, 3 to 10 values ≤25). Max 10 per proposal. |
| `emergency_pause_campaign(campaign_id, reason)` | Logs the reason and the live numbers |

## Staying connected (OAuth)

claude.ai connects through FastMCP's OAuth proxy in front of Google. Two
additions in `ads_actions/oauth_hardening.py` keep the connector signed in:

- **Refresh tokens survive rotation.** FastMCP makes each refresh token
  single-use. When claude.ai holds the same token in two sessions (a chat and a
  scheduled task), the first refresh used to log the other session out with
  `invalid_grant`. A rotated-out refresh token now stays valid for
  `OAUTH_REFRESH_REUSE_SECONDS` (default 30 days, never past its own expiry).
  Each use still returns a new token pair, and revoking a token still ends it.
- **Longer access tokens.** The token claude.ai receives lasts
  `OAUTH_ACCESS_TOKEN_SECONDS` (default 8 hours) instead of Google's 1 hour.
  The server renews the Google token by itself when it has expired.

OAuth state is kept in Firestore under collections prefixed `gads-actions__`, so
it never mixes with the read-only `google-ads-mcp` service in the same database.
The JWT signing key (`gads-actions-jwt-key`) is a stable secret, so restarts and
new revisions keep existing logins.

The read-only service gets the same two additions without changing its code:
`scripts/deploy-google-ads-mcp-auth-patch.sh` builds a thin image on top of its
current image (`readonly-patch/`) that loads the module at startup. The script
prints the rollback command.

`scripts/collect-auth-evidence.sh` pulls the relevant Cloud Run logs and
Firestore collection names for both services, without printing tokens or
secrets. The key log line is FastMCP's `Refresh token not found ... already
rotated`, which marks a session that was logged out by another one.

Google expires refresh tokens after 7 days while the OAuth consent screen is in
**Testing**. Publish it once: Google Auth Platform → Audience → Publish app
(https://console.cloud.google.com/auth/audience?project=djavaweb-ads-mcp).

## Audit log

Firestore collection `ads_actions`, one document per proposal, with the fields
from brief section 7: `change_id`, `type`, `status`, `request`, `before`,
`after`, `operations`, `validate_only_result`, `created_at`, `expires_at`,
`executed_at`, `user_email`, `google_request_ids`, `error`. Some extra fields:
`summary`, `rows` (the before → after table), `warnings`, `skipped`,
`undo_of`/`undone_by`, and `reason`/`evidence` for emergency pauses.

An execution that is refused or fails keeps status `failed` and records
`failure_kind` (`drift`, `plan_changed`, `recheck_refused`, `read_error`,
`google_error` or `unknown_outcome`), `failed_at`, `failed_by`, and for drift the
live values found at execute time (`live_before`, `live_rows`). `get_change` shows
all of it, and `get_change_log(include_failed=true)` lists these entries alongside
executed changes.

For negative keywords, the "before" snapshot includes every existing negative
with the same text in any match type. So a negative added by hand in between,
even as BROAD, counts as drift.

Status `executing` is used briefly while a change is running. It is claimed in a
Firestore transaction so two instances can't run the same proposal. Queries use
single-field filters only, so no composite index is needed.

## Tests

```
uv venv -p 3.11 .venv && uv pip install -p .venv -e '.[dev]'
.venv/bin/python -m pytest -q
```

These tests run against an in-memory fake of the account, seeded from the
brief. `tests/test_brief.py` maps one-to-one onto the brief's section 9 checks.
`tests/test_changes.py` runs execute and undo for every change type, and checks
every GAQL field against the v26 `GoogleAdsRow` proto. `tests/test_gateway.py`
checks how the real gateway builds requests and parses errors, using a stubbed
service.

## Deploy

```
./deploy-google-ads-actions.sh
```

The script needs a committed tree and `gcloud` signed in as an owner or editor of
`djavaweb-ads-mcp`. It:

- enables the APIs;
- creates `gads-actions-jwt-key` if it's missing;
- builds the image with Cloud Build, tagged with the commit;
- grants the default compute service account `secretmanager.secretAccessor` on
  the secrets and `datastore.user` on the project, retrying on
  "concurrent policy changes";
- deploys with `--allow-unauthenticated` plus the OAuth settings, min 0 / max 2
  instances, and a `commit-sha` label;
- smoke-tests that `/mcp` answers 401 without a token.

No developer token is passed by default. Like the read-only service, API access
comes from the Cloud project. To pass one, set `DEV_TOKEN_SECRET_NAME` to a Secret
Manager secret. The image is built to
`us-central1-docker.pkg.dev/djavaweb-ads-mcp/mcp-servers/google-ads-actions:<commit>`.

After the first deploy:

1. Google Cloud console → APIs & Services → Credentials → OAuth client
   `google-ads-mcp` → **add** the redirect URI
   `https://google-ads-actions-602690127469.us-central1.run.app/auth/callback`.
   Keep the existing URI.
2. Claude → Settings → Connectors → Add custom connector: name **Google Ads
   Actions**, URL `https://google-ads-actions-602690127469.us-central1.run.app/mcp`.
   Leave the advanced client ID and secret empty. Sign in as
   ardhofortana@gmail.com and tick every permission box.

## Live handover checks (brief section 9)

Run these in Claude with the connector attached. Every step except 11 is
propose-only, so nothing changes. Cancel each proposal afterwards with
`cancel_change`.

1. Sign in to the connector with a different Google account. Expect HTTP 403 on
   every call. Remove that connection afterwards.
2. `propose_set_campaign_status` with `customer_id` `1234567890`, then with
   campaign `1`. Expect both refused.
3. `propose_add_keywords` on ad group `201460749979` with a BROAD keyword:
   refused. With one PHRASE and one EXACT: proposal returned.
4. `propose_set_daily_budget` with 2000000: refused (range). With 1440000:
   refused (+60%). With 1000000: proposal returned.
5. `propose_add_campaign_negatives(["jobs", "ai integration"], PHRASE)`. Expect a
   warning that "ai integration" blocks active keywords.
6. `propose_shift_ad_schedule(hours=1)`. Expect 10 slots: Mon 20 to 24; Tue to
   Fri 0 to 8 and 20 to 24; Sat 0 to 8; nothing on Sunday.
7. `propose_update_rsa` with a 31-character headline, then with an em dash.
   Expect both refused.
8. Cancel a proposal, then `execute_change` it: refused. Run `execute_change` on
   the already-executed change from step 11: refused. For expiry, run
   `execute_change` on any proposal more than 24 hours old.
9. Drift, without touching spend: propose adding the negative `"drift check
   test"` (phrase). Add the same negative by hand in the Google Ads UI, then
   `execute_change`. Expect a refusal. Delete the manual negative afterwards.
10. `emergency_pause_campaign`. Expect a refusal that shows today's numbers.
11. With Ardho present: propose and execute adding the phrase negative `"free
    template download"`, and check it appears in Google Ads. Then `propose_undo`
    and `execute_change`. Both should appear in `get_change_log`.
