# Fix report: Google Ads connectors keep losing their login

**Date:** 23 October 2026
**Spec:** `google-ads-connectors-auth-fix-brief.md`
**Services:** `google-ads-mcp` (read-only) and `google-ads-actions` (write), project `djavaweb-ads-mcp`, `us-central1`
**Code:** `ardhofortana/google-ads-actions`, commit noted at the bottom
**Status:** cause confirmed from Cloud Run logs; fix built and tested offline; **deploy and acceptance tests pending**

## Summary

Three separate problems, all confirmed in the Cloud Run logs for 23 Oct 01:15 to 02:40 UTC. None of them is shared storage.

1. **A refresh lost during a cold start logs the connector out.** FastMCP makes every refresh token single-use. When a refresh is slow (37.6 s during a cold start), claude.ai gives up and retries with the old token. The server has already deleted it and answers `invalid_grant`, which claude.ai shows as needs-auth. Two sessions refreshing the same token cause the same failure.
2. **Cloud Run cut the MCP connection every 5 minutes.** The default request timeout is 300 s. Both services' long-lived MCP streams were cut in the same second, about 30 s before the chat showed needs-auth. No token was rejected at that moment.
3. **Cold starts take 22 to 37 seconds** on both services (`min-instances 0`). A first call that waits that long fails on claude.ai's side before the server can answer.

## Evidence (UTC; WIB = UTC+7)

| Time (UTC) | Service | Log | Meaning |
|---|---|---|---|
| 01:20:44 | mcp | Cold start; `POST /token` 200 after **27.8 s** | 08:20 WIB report: refresh worked, slowly |
| 01:37:58 | actions | Cold start; `POST /mcp` 401 after **22.5 s**; no `/token` follows | 08:37 WIB report: Actions "needs reconnect" |
| 02:20:56 | mcp | Cold start; `POST /token` 200 after **37.6 s**, server rotates refresh token at 02:21:34 | Client stopped waiting |
| 02:22:02 | mcp | `POST /token` 401; `WARNING Refresh token not found (token_hash=078a6853); it was already rotated ... Rejecting with invalid_grant` | **Cause 1 confirmed**: the old refresh token was replayed after rotation |
| 02:22:02 | actions | Cold start; `POST /mcp` 401 after **21.8 s**; no `/token` follows | Same pattern as 01:37 |
| 02:22:19 to 02:23:09 | both | `/authorize` → `/consent` → `/auth/callback` → `/token` 200 | Ardho reconnected (09:22 to 09:23 WIB) |
| 02:28:32 | **both** | `WARNING Truncated response body` (same second on both) | **Cause 2**: 300 s after the sessions opened at ~02:23:32; chat showed needs-auth at 09:29 WIB |
| 02:30:59 to 02:32:02 | both | `POST /mcp` 401, then full reconnect | Reconnect from the second chat (09:31 to 09:32 WIB) |

Checks against the brief's suspected causes:

| Suspected cause | Finding |
|---|---|
| 1. Refresh doesn't work | **Confirmed, with a twist.** Refresh itself works (01:21:12 and 02:21:34 succeeded). It fails when a rotated refresh token is used again, which happened after a 37.6 s cold-start refresh. Google does return a refresh token: `GoogleProvider` sends `access_type=offline` and `prompt=consent`. |
| 2. Shared storage | **Not the cause.** Both services write the same collection names (`mcp-jti-mappings`, `mcp-refresh-tokens`, `mcp-upstream-tokens`, `mcp-oauth-transactions`), but the keys are random per token, and no client records are stored (`mcp-oauth-proxy-clients` doesn't exist), so nothing collides. Separated anyway, as required. |
| 3. State lost on cold start | **Not lost**: tokens issued before a cold start still refresh after it (01:21:12). Each service uses Firestore and its own stable JWT key (`gads-mcp-jwt-key`, `gads-actions-jwt-key`). The **latency** of cold starts is a problem (see cause 3 above). |
| 4. Consent screen in Testing | Not visible in logs. If it is in Testing, Google ends the login every 7 days. Ardho to check and publish (below). |

## What changed

| Change | Where | Effect |
|---|---|---|
| Rotated-out refresh tokens stay valid for 30 days (`OAUTH_REFRESH_REUSE_SECONDS`), never past their own expiry | `ads_actions/oauth_hardening.py`, both services | A lost or replayed refresh no longer logs the connector out. Each use still returns a new token pair. Revoke still ends a token. |
| Access token given to claude.ai lasts 8 hours instead of 1 (`OAUTH_ACCESS_TOKEN_SECONDS`) | same | claude.ai refreshes a few times a day instead of hourly. The server renews the Google token itself (FastMCP's built-in transparent refresh). |
| Cloud Run request timeout 300 s → 3600 s | both deploy scripts | The MCP stream is no longer cut every 5 minutes |
| Startup CPU boost; optional `MIN_INSTANCES=1` | both deploy scripts | Shorter or no cold starts |
| Actions OAuth state in its own collections (`gads-actions__…`) | `ads_actions/server.py` | No shared OAuth storage. **Actions needs one reconnect after this deploy.** |
| Read-only gets the same module without code changes: a thin image on top of its current image loads it at startup | `readonly-patch/`, `scripts/deploy-google-ads-mcp-auth-patch.sh` | Existing read-only logins stay valid. The script prints a rollback command. |

Unchanged: tools, guardrails, user allowlist, audit log, OAuth client and client secret (still only in Secret Manager as `gads-mcp-client-secret`).

Offline tests (51, all passing) include the failure itself, reproduced against FastMCP's real `GoogleProvider`. Without the fix, a second refresh with the same token fails with the same `Refresh token not found` warning as in production. With the fix, both sessions keep working, and a revoked token stays revoked.

## Actions for Ardho

1. Run the two deploy scripts (commands in the chat).
2. Reconnect **Google Ads Actions** once.
3. Consent screen: https://console.cloud.google.com/auth/audience?project=djavaweb-ads-mcp. If the status is **Testing**, click **Publish app**.

## Acceptance tests

| # | Test | Pass condition | Result |
|---|---|---|---|
| 1 | Connect both, idle 2 h, call one read tool on each from a new message | Works, no reconnect | Pending |
| 2 | Same, from a new chat session in the project | Works | Pending |
| 3 | Chat + `fire_trigger` on the daily report at the same time, then the chat again | All calls work | Pending |
| 4 | Disconnect → Connect only `google-ads-actions`, then call `google-ads-mcp` | Read-only still works | Pending |
| 5 | Deploy a new revision of each service, call both | Works | Pending (the fix deploy itself counts) |
| 6 | Next 08:20 WIB scheduled report | Proposal IDs in the report | Pending |

## Remaining risk

- With a 3600 s timeout, a long-lived MCP stream is still cut once an hour. Test 1 (2 h idle) shows whether claude.ai reconnects on its own. If it doesn't, the next step is FastMCP's stateless HTTP mode (`FASTMCP_STATELESS_HTTP=true`), which has no long-lived stream.
- Without `MIN_INSTANCES=1`, cold starts remain (shorter with CPU boost). The reuse window makes a slow refresh harmless, but a slow first call can still time out on claude.ai's side.

**Commit:** see `git log` (`claude/google-ads-connectors-auth-fix-report.md` is updated with results after the acceptance tests).
