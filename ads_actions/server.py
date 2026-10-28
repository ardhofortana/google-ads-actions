"""MCP server: OAuth, the user allowlist and the tool definitions."""

import json
import os
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.dependencies import get_access_token
from mcp import types as mcp_types
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler
from pydantic import Field
from starlette.middleware import Middleware

from key_value.aio.wrappers.prefix_collections import PrefixCollectionsWrapper

from ads_actions import oauth_hardening
from ads_actions.engine import Engine, Session
from ads_actions.errors import Forbidden, Refused
from ads_actions.gateway import GoogleAdsGateway
from ads_actions.guardrails import Guardrails
from ads_actions.store import FirestoreStore

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/adwords",
]

OAUTH_COLLECTION_PREFIX = "gads-actions"

INSTRUCTIONS = """Write tools for one Google Ads account, with a two-step rule.
Every change is made with a propose_* tool first. It validates the change with
Google (nothing changes) and returns a change_id and a before -> after table.
Show that table to the user. Call execute_change(change_id) only for changes
the user has explicitly approved. emergency_pause_campaign is the only one-step
tool and the server refuses it unless live data shows runaway spend or likely
invalid traffic; report it to the user immediately if it runs."""


def email_from_claims(claims: dict | None) -> str | None:
    """The verified email on the token, or None."""
    claims = claims or {}
    verified = claims.get("email_verified")
    if verified is False or str(verified).lower() == "false":
        return None
    email = claims.get("email")
    return email.strip().lower() if isinstance(email, str) else None


class AllowlistMiddleware:
    """Answers 403 to any authenticated request from a user not on the allowlist.

    Runs after FastMCP's auth middleware, so ``scope["user"]`` is set. Requests
    with no valid token pass through and get FastMCP's 401, which starts the
    OAuth flow.
    """

    def __init__(self, app, guardrails: Guardrails):
        self.app = app
        self.g = guardrails

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            user = scope.get("user")
            if isinstance(user, AuthenticatedUser):
                email = email_from_claims(getattr(user.access_token, "claims", None))
                if email not in self.g.allowed_user_emails:
                    body = json.dumps({"error": "forbidden", "error_description": "This Google account may not use this server."}).encode()
                    await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
                    await send({"type": "http.response.body", "body": body})
                    return
        await self.app(scope, receive, send)


def _ensure_subscriptions_listen(server: FastMCP) -> None:
    """Same compatibility shim as googleads/google-ads-mcp (coordinator.py)."""
    low = server._mcp_server
    if "subscriptions/listen" not in low._request_handlers:
        low.add_request_handler("subscriptions/listen", mcp_types.SubscriptionsListenRequestParams, ListenHandler(InMemorySubscriptionBus()))


def build_auth() -> GoogleProvider:
    client_id = os.environ.get("GOOGLE_ADS_MCP_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET")
    if not (client_id and client_secret):
        raise SystemExit("Refusing to start: GOOGLE_ADS_MCP_OAUTH_CLIENT_ID and GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET are required.")
    from ads_actions.auth_storage import create_client_storage

    kwargs: dict[str, Any] = {
        "client_id": client_id,
        "client_secret": client_secret,
        "base_url": os.environ.get("GOOGLE_ADS_MCP_BASE_URL", "http://localhost:8080"),
        "required_scopes": SCOPES,
    }
    if os.environ.get("GOOGLE_ADS_MCP_JWT_SIGNING_KEY"):
        kwargs["jwt_signing_key"] = os.environ["GOOGLE_ADS_MCP_JWT_SIGNING_KEY"]
    storage = create_client_storage()
    if storage is not None:
        # Own collections, so this service never shares OAuth state with the
        # read-only google-ads-mcp service in the same Firestore database.
        kwargs["client_storage"] = PrefixCollectionsWrapper(key_value=storage, prefix=OAUTH_COLLECTION_PREFIX)
    oauth_hardening.install()
    return GoogleProvider(**kwargs)


def build_server(engine: Engine, auth=None, make_gateway=None) -> FastMCP:
    """Builds the MCP server. ``make_gateway(customer_id, token)`` is swappable for tests."""
    mcp = FastMCP("Google Ads Actions", instructions=INSTRUCTIONS, auth=auth)
    _ensure_subscriptions_listen(mcp)
    make_gateway = make_gateway or (lambda cid, token: GoogleAdsGateway(cid, token))

    def session() -> Session:
        token = get_access_token()
        email = email_from_claims(token.claims) if token is not None else None
        if email not in engine.g.allowed_user_emails:
            raise Forbidden()
        return Session(email=email, make_gateway=lambda cid: make_gateway(cid, token.token))

    async def run(coro_fn, *args):
        try:
            return await coro_fn(session(), *args)
        except Refused as e:
            raise ToolError(str(e))

    async def propose(change_type: str, customer_id: str, request: dict):
        return await run(engine.propose, customer_id, change_type, {"customer_id": customer_id, **request})

    CustomerId = Annotated[str, Field(description="Google Ads customer ID (only 6775198830 is allowed).")]
    CampaignId = Annotated[str, Field(description="Campaign ID (must be in the allowlist).")]
    ChangeId = Annotated[str, Field(description="Change ID such as chg_7K2Q.")]
    Status = Literal["ENABLED", "PAUSED"]
    write = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}
    dry = {"readOnlyHint": True, "openWorldHint": True}

    # -- housekeeping -----------------------------------------------------

    @mcp.tool(annotations=dry)
    async def list_pending_changes(customer_id: CustomerId) -> list[dict]:
        """Proposals waiting for approval (status 'proposed'), newest first."""
        return await run(engine.list_pending, customer_id)

    @mcp.tool(annotations=dry)
    async def get_change(customer_id: CustomerId, change_id: ChangeId) -> dict:
        """Full record of one proposal or executed change, including before/after and the exact operations."""
        return await run(engine.get_change, customer_id, change_id)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
    async def cancel_change(customer_id: CustomerId, change_id: ChangeId) -> dict:
        """Cancels a pending proposal. Changes nothing in Google Ads."""
        return await run(engine.cancel, customer_id, change_id)

    @mcp.tool(annotations=dry)
    async def get_change_log(
        customer_id: CustomerId,
        days: int = 30,
        include_failed: Annotated[bool, Field(description="Also list refused or failed executions (drift, guardrail re-check, Google errors) with the reason and live values.")] = False,
    ) -> list[dict]:
        """Executed changes (including emergency pauses) in the last N days, with before/after values."""
        return await run(engine.change_log, customer_id, days, include_failed)

    @mcp.tool(annotations=dry)
    async def propose_undo(customer_id: CustomerId, change_id: ChangeId) -> dict:
        """Proposes restoring the 'before' values of an executed change. Still needs execute_change after approval."""
        return await run(engine.propose_undo, customer_id, change_id)

    @mcp.tool(annotations={**write, "destructiveHint": True})
    async def execute_change(customer_id: CustomerId, change_id: ChangeId) -> dict:
        """Executes a proposal the user has explicitly approved.

        Refuses if the proposal is missing, expired, cancelled or already executed,
        if the live values changed since the proposal, or if it now breaks a guardrail.
        """
        return await run(engine.execute, customer_id, change_id)

    # -- negative keywords -----------------------------------------------

    @mcp.tool(annotations=dry)
    async def propose_add_campaign_negatives(
        customer_id: CustomerId,
        campaign_id: CampaignId,
        keywords: Annotated[list[str], Field(description="Up to 50 negative keywords. Lowercased and trimmed.")],
        match_type: Literal["PHRASE", "EXACT"],
    ) -> dict:
        """Proposes campaign-level negative keywords. Negatives that would block an active keyword are rejected with a warning; existing ones are skipped."""
        return await propose("add_campaign_negatives", customer_id, {"campaign_id": campaign_id, "keywords": keywords, "match_type": match_type})

    @mcp.tool(annotations=dry)
    async def propose_remove_campaign_negatives(customer_id: CustomerId, campaign_id: CampaignId, criterion_ids: list[int]) -> dict:
        """Proposes removing campaign-level negative keywords by criterion ID."""
        return await propose("remove_campaign_negatives", customer_id, {"campaign_id": campaign_id, "criterion_ids": [str(i) for i in criterion_ids]})

    # -- keywords ---------------------------------------------------------

    @mcp.tool(annotations=dry)
    async def propose_add_keywords(
        customer_id: CustomerId,
        ad_group_id: str,
        keywords: Annotated[list[dict], Field(description='Up to 20 items like {"text": "ai integration agency", "match_type": "PHRASE"}. PHRASE or EXACT only; broad is always rejected.')],
    ) -> dict:
        """Proposes adding keywords to an ad group in an allowed campaign."""
        return await propose("add_keywords", customer_id, {"ad_group_id": ad_group_id, "keywords": keywords})

    @mcp.tool(annotations=dry)
    async def propose_set_keyword_status(customer_id: CustomerId, ad_group_id: str, criterion_id: str, status: Status) -> dict:
        """Proposes enabling or pausing one keyword."""
        return await propose("set_keyword_status", customer_id, {"ad_group_id": ad_group_id, "criterion_id": str(criterion_id), "status": status})

    # -- status -----------------------------------------------------------

    @mcp.tool(annotations=dry)
    async def propose_set_ad_group_status(customer_id: CustomerId, ad_group_id: str, status: Status) -> dict:
        """Proposes enabling or pausing an ad group."""
        return await propose("set_ad_group_status", customer_id, {"ad_group_id": ad_group_id, "status": status})

    @mcp.tool(annotations=dry)
    async def propose_set_campaign_status(customer_id: CustomerId, campaign_id: CampaignId, status: Status) -> dict:
        """Proposes enabling or pausing the campaign."""
        return await propose("set_campaign_status", customer_id, {"campaign_id": campaign_id, "status": status})

    # -- budget and bidding ----------------------------------------------

    @mcp.tool(annotations=dry)
    async def propose_set_daily_budget(customer_id: CustomerId, campaign_id: CampaignId, amount_idr: Annotated[int, Field(description="New daily budget in whole rupiah, e.g. 1000000.")]) -> dict:
        """Proposes a new daily budget. Must be within the guardrail range and at most +/-50% from today's value."""
        return await propose("set_daily_budget", customer_id, {"campaign_id": campaign_id, "amount_idr": amount_idr})

    @mcp.tool(annotations=dry)
    async def propose_set_bidding(
        customer_id: CustomerId,
        campaign_id: CampaignId,
        strategy: Literal["MAXIMIZE_CLICKS", "MAXIMIZE_CONVERSIONS", "MANUAL_CPC"],
        cpc_ceiling_idr: Annotated[int | None, Field(description="Max CPC for MAXIMIZE_CLICKS, in rupiah. None for no ceiling.")] = None,
        target_cpa_idr: Annotated[int | None, Field(description="Target CPA for MAXIMIZE_CONVERSIONS, in rupiah. None for no target.")] = None,
        ad_group_default_cpc_idr: Annotated[int | None, Field(description="MANUAL_CPC only, required: new default max CPC for every enabled ad group, in rupiah (Rp20,000 to Rp400,000, at most ±50% from each current bid).")] = None,
        ad_group_bids: Annotated[dict[str, int] | None, Field(description='MANUAL_CPC only: {ad_group_id: cpc_idr} overrides for listed ad groups. The only way to change a paused ad group.')] = None,
    ) -> dict:
        """Proposes a bidding change: Maximize clicks (optional CPC ceiling), Maximize conversions (optional target CPA), or Manual CPC.

        MANUAL_CPC sets Enhanced CPC off and changes the campaign strategy and every enabled ad group's default bid in ONE atomic change, so Manual CPC never runs on the old bids.
        """
        return await propose(
            "set_bidding",
            customer_id,
            {
                "campaign_id": campaign_id, "strategy": strategy, "cpc_ceiling_idr": cpc_ceiling_idr, "target_cpa_idr": target_cpa_idr,
                "ad_group_default_cpc_idr": ad_group_default_cpc_idr, "ad_group_bids": ad_group_bids,
            },
        )

    @mcp.tool(annotations=dry)
    async def propose_set_ad_group_cpc(customer_id: CustomerId, ad_group_id: str, cpc_idr: Annotated[int, Field(description="New default max CPC in rupiah (Rp20,000 to Rp400,000, at most ±50% from the current bid).")]) -> dict:
        """Proposes a new default max CPC for one ad group. Only while the campaign uses Manual CPC."""
        return await propose("set_ad_group_cpc", customer_id, {"ad_group_id": ad_group_id, "cpc_idr": cpc_idr})

    @mcp.tool(annotations=dry)
    async def propose_set_keyword_cpc(
        customer_id: CustomerId,
        items: Annotated[list[dict] | None, Field(description='Up to 20 items like {"ad_group_id": "...", "criterion_id": "...", "cpc_idr": 250000}. cpc_idr 0 or null removes the keyword\'s own bid so it uses the ad group default.')] = None,
        ad_group_id: Annotated[str | None, Field(description="Single-keyword form: use instead of items.")] = None,
        criterion_id: Annotated[str | None, Field(description="Single-keyword form.")] = None,
        cpc_idr: Annotated[int | None, Field(description="Single-keyword form. 0 or null removes the keyword's own bid.")] = None,
    ) -> dict:
        """Proposes keyword-level max CPC bids. Only while the campaign uses Manual CPC. Each bid Rp20,000 to Rp400,000 and at most ±50% from the current bid (the ad group default if the keyword has none)."""
        if items is None:
            items = [{"ad_group_id": ad_group_id, "criterion_id": criterion_id, "cpc_idr": cpc_idr}]
        return await propose("set_keyword_cpc", customer_id, {"items": items})

    @mcp.tool(annotations=dry)
    async def propose_set_device_bid_adjustment(
        customer_id: CustomerId,
        campaign_id: CampaignId,
        device: Literal["DESKTOP", "MOBILE", "TABLET"],
        percent: Annotated[int, Field(description="Bid adjustment from -50 to +50 (0 removes it).")],
    ) -> dict:
        """Proposes a campaign device bid adjustment. Only while the campaign uses Manual CPC."""
        return await propose("set_device_bid_adjustment", customer_id, {"campaign_id": campaign_id, "device": device, "percent": percent})

    # -- ad schedule ------------------------------------------------------

    @mcp.tool(annotations=dry)
    async def propose_set_ad_schedule(
        customer_id: CustomerId,
        campaign_id: CampaignId,
        slots: Annotated[list[dict], Field(description='Full new schedule, e.g. [{"day": "MONDAY", "start_hour": 19, "end_hour": 24}]. Whole hours in the account time zone (WIB); end_hour 24 allowed.')],
    ) -> dict:
        """Proposes replacing the campaign's whole ad schedule."""
        return await propose("set_ad_schedule", customer_id, {"campaign_id": campaign_id, "slots": slots})

    @mcp.tool(annotations=dry)
    async def propose_shift_ad_schedule(customer_id: CustomerId, campaign_id: CampaignId, hours: Annotated[int, Field(description="Hours to shift every slot by, e.g. 1 or -1.")]) -> dict:
        """Proposes shifting every ad schedule slot by N hours, wrapping across midnight (e.g. +1 when US daylight saving ends)."""
        return await propose("shift_ad_schedule", customer_id, {"campaign_id": campaign_id, "hours": hours})

    # -- ads and assets ---------------------------------------------------

    @mcp.tool(annotations=dry)
    async def propose_update_rsa(
        customer_id: CustomerId,
        ad_id: str,
        headlines: Annotated[list[str] | None, Field(description="3 to 15 headlines, max 30 characters each. Replaces all headlines.")] = None,
        descriptions: Annotated[list[str] | None, Field(description="2 to 4 descriptions, max 90 characters each. Replaces all descriptions.")] = None,
        pin_first_headline: Annotated[bool | None, Field(description="True pins the first headline to position 1; False removes that pin; None keeps existing pins.")] = None,
    ) -> dict:
        """Proposes new copy for an existing responsive search ad. Final URL and paths never change. New or changed lines may not contain dashes, exclamation marks, AI-provider names or prices; lines identical to the live ad are kept as they are."""
        return await propose("update_rsa", customer_id, {"ad_id": ad_id, "headlines": headlines, "descriptions": descriptions, "pin_first_headline": pin_first_headline})

    @mcp.tool(annotations=dry)
    async def propose_add_campaign_assets(
        customer_id: CustomerId,
        campaign_id: CampaignId,
        type: Literal["SITELINK", "CALLOUT", "STRUCTURED_SNIPPET"],
        items: Annotated[list[Any], Field(description='SITELINK: {"link_text", "final_url" (https://djavaweb.com/...), "description1", "description2"}. CALLOUT: "text". STRUCTURED_SNIPPET: {"header": a Google header such as "Service catalog", "values": [...]}.')],
    ) -> dict:
        """Proposes new sitelink, callout or structured snippet assets on the campaign (up to 10)."""
        return await propose("add_campaign_assets", customer_id, {"campaign_id": campaign_id, "type": type, "items": items})

    @mcp.tool(annotations=dry)
    async def propose_remove_campaign_asset(customer_id: CustomerId, campaign_id: CampaignId, asset_id: str, field_type: Literal["SITELINK", "CALLOUT", "STRUCTURED_SNIPPET"]) -> dict:
        """Proposes unlinking one asset from the campaign. The asset itself stays in the account."""
        return await propose("remove_campaign_assets", customer_id, {"campaign_id": campaign_id, "links": [{"asset_id": str(asset_id), "field_type": field_type}]})

    # -- emergency --------------------------------------------------------

    @mcp.tool(annotations={**write, "destructiveHint": True})
    async def emergency_pause_campaign(customer_id: CustomerId, campaign_id: CampaignId, reason: str) -> dict:
        """Pauses the campaign immediately, without a proposal. The server allows it only if live data for today shows cost above 2x the daily budget, or more than 40 clicks in the last 2 hours with 0 conversions and CTR above 20%. Otherwise use propose_set_campaign_status."""
        return await run(engine.emergency_pause, customer_id, campaign_id, reason)

    return mcp


def run_server() -> None:
    auth = build_auth()  # exits unless OAuth is configured
    g = Guardrails.load()
    engine = Engine(g, FirestoreStore(project=os.environ.get("GOOGLE_ADS_MCP_STORAGE_FIRESTORE_PROJECT"), database=os.environ.get("GOOGLE_ADS_MCP_STORAGE_FIRESTORE_DATABASE")))
    mcp = build_server(engine, auth=auth)
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        middleware=[Middleware(AllowlistMiddleware, guardrails=g)],
        uvicorn_config={"access_log": False},
    )


if __name__ == "__main__":
    run_server()
