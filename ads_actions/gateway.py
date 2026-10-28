"""Thin layer over the Google Ads API.

The change engine only talks to this interface, so tests can swap in an
in-memory fake. Reads return plain dicts; writes go through a single atomic
``GoogleAdsService.Mutate`` call with ``partial_failure=false``.
"""

import os
from typing import Any, Protocol

import grpc
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google.ads.googleads.v26.services.types.google_ads_service import (
    MutateGoogleAdsRequest,
    MutateOperation,
)
from google.oauth2.credentials import Credentials
from google.protobuf import json_format

from ads_actions import ops
from ads_actions.errors import AdsApiError, Refused

API_VERSION = "v26"


class Gateway(Protocol):
    customer_id: str

    def campaign(self, campaign_id: str) -> dict | None: ...
    def campaign_negatives(self, campaign_id: str) -> list[dict]: ...
    def campaign_keywords(self, campaign_id: str) -> list[dict]: ...
    def ad_group(self, ad_group_id: str) -> dict | None: ...
    def campaign_ad_groups(self, campaign_id: str) -> list[dict]: ...
    def device_criteria(self, campaign_id: str) -> list[dict]: ...
    def ad_schedule(self, campaign_id: str) -> list[dict]: ...
    def rsa(self, ad_id: str) -> dict | None: ...
    def campaign_assets(self, campaign_id: str) -> list[dict]: ...
    def time_zone(self) -> str: ...
    def today_metrics(self, campaign_id: str) -> dict: ...
    def hourly_metrics(self, campaign_id: str, start_date: str, end_date: str) -> list[dict]: ...
    def mutate(self, operations: list[dict], validate_only: bool) -> dict: ...


# GAQL used by GoogleAdsGateway. Kept at module level so tests can check every
# selected field against the GoogleAdsRow proto. A field of another resource
# used in WHERE (e.g. campaign.id) must also be selected: campaign_asset rejects
# the query otherwise (EXPECTED_REFERENCED_FIELD_IN_SELECT_CLAUSE).
Q_CAMPAIGN = """
SELECT campaign.id, campaign.name, campaign.status, campaign.campaign_budget,
  campaign.bidding_strategy_type, campaign.bidding_strategy,
  campaign.target_spend.cpc_bid_ceiling_micros,
  campaign.maximize_conversions.target_cpa_micros,
  campaign.manual_cpc.enhanced_cpc_enabled,
  campaign_budget.resource_name, campaign_budget.amount_micros,
  campaign_budget.explicitly_shared, campaign_budget.reference_count
FROM campaign WHERE campaign.id = {campaign_id}"""

Q_NEGATIVES = """
SELECT campaign.id, campaign_criterion.criterion_id, campaign_criterion.keyword.text,
  campaign_criterion.keyword.match_type
FROM campaign_criterion
WHERE campaign.id = {campaign_id} AND campaign_criterion.type = KEYWORD
  AND campaign_criterion.negative = TRUE AND campaign_criterion.status != REMOVED"""

Q_KEYWORDS = """
SELECT campaign.id, ad_group.id, ad_group.name, ad_group.status, ad_group.cpc_bid_micros,
  ad_group_criterion.criterion_id, ad_group_criterion.keyword.text,
  ad_group_criterion.keyword.match_type, ad_group_criterion.status,
  ad_group_criterion.cpc_bid_micros
FROM ad_group_criterion
WHERE campaign.id = {campaign_id} AND ad_group_criterion.type = KEYWORD
  AND ad_group_criterion.negative = FALSE AND ad_group_criterion.status != REMOVED
  AND ad_group.status != REMOVED"""

Q_AD_GROUP = """
SELECT ad_group.id, ad_group.name, ad_group.status, ad_group.cpc_bid_micros, campaign.id
FROM ad_group WHERE ad_group.id = {ad_group_id}"""

Q_AD_GROUPS = """
SELECT campaign.id, ad_group.id, ad_group.name, ad_group.status, ad_group.cpc_bid_micros
FROM ad_group WHERE campaign.id = {campaign_id} AND ad_group.status != REMOVED"""

Q_DEVICES = """
SELECT campaign.id, campaign_criterion.criterion_id, campaign_criterion.device.type,
  campaign_criterion.bid_modifier
FROM campaign_criterion
WHERE campaign.id = {campaign_id} AND campaign_criterion.type = DEVICE
  AND campaign_criterion.status != REMOVED"""

Q_SCHEDULE = """
SELECT campaign.id, campaign_criterion.criterion_id, campaign_criterion.ad_schedule.day_of_week,
  campaign_criterion.ad_schedule.start_hour, campaign_criterion.ad_schedule.start_minute,
  campaign_criterion.ad_schedule.end_hour, campaign_criterion.ad_schedule.end_minute,
  campaign_criterion.bid_modifier
FROM campaign_criterion
WHERE campaign.id = {campaign_id} AND campaign_criterion.type = AD_SCHEDULE
  AND campaign_criterion.status != REMOVED"""

Q_RSA = """
SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status,
  ad_group_ad.ad.final_urls, ad_group_ad.ad.responsive_search_ad.headlines,
  ad_group_ad.ad.responsive_search_ad.descriptions,
  ad_group_ad.ad.responsive_search_ad.path1, ad_group_ad.ad.responsive_search_ad.path2,
  ad_group.id, campaign.id
FROM ad_group_ad WHERE ad_group_ad.ad.id = {ad_id} AND ad_group_ad.status != REMOVED"""

Q_ASSETS = """
SELECT campaign.id, campaign_asset.resource_name, campaign_asset.field_type, campaign_asset.status,
  asset.id, asset.type, asset.final_urls, asset.sitelink_asset.link_text,
  asset.sitelink_asset.description1, asset.sitelink_asset.description2,
  asset.callout_asset.callout_text, asset.structured_snippet_asset.header,
  asset.structured_snippet_asset.values
FROM campaign_asset
WHERE campaign.id = {campaign_id} AND campaign_asset.status != REMOVED"""

Q_TIME_ZONE = "SELECT customer.time_zone FROM customer LIMIT 1"

Q_TODAY = """
SELECT metrics.cost_micros, metrics.clicks, metrics.impressions, metrics.conversions
FROM campaign WHERE campaign.id = {campaign_id} AND segments.date DURING TODAY"""

Q_HOURLY = """
SELECT segments.date, segments.hour, metrics.clicks, metrics.impressions,
  metrics.conversions
FROM campaign WHERE campaign.id = {campaign_id}
  AND segments.date BETWEEN '{start_date}' AND '{end_date}'"""

ALL_QUERIES = [Q_CAMPAIGN, Q_NEGATIVES, Q_KEYWORDS, Q_AD_GROUP, Q_AD_GROUPS, Q_DEVICES, Q_SCHEDULE, Q_RSA, Q_ASSETS, Q_TIME_ZONE, Q_TODAY, Q_HOURLY]


class _RequestIdRecorder(grpc.UnaryUnaryClientInterceptor):
    """Collects the ``request-id`` trailing metadata of every successful call."""

    def __init__(self):
        self.request_ids: list[str] = []

    def intercept_unary_unary(self, continuation, client_call_details, request):
        response = continuation(client_call_details, request)
        try:
            for key, value in response.trailing_metadata() or ():
                if key == "request-id":
                    self.request_ids.append(value)
        except Exception:
            pass
        return response


def _digits(value: Any) -> str:
    s = str(value)
    if not s.isdigit():
        raise Refused(f"{value!r} is not a numeric ID.")
    return s


def _enum(v) -> str:
    return getattr(v, "name", str(v))


def _ads_error(ex: GoogleAdsException) -> AdsApiError:
    messages = []
    for err in ex.failure.errors:
        code = json_format.MessageToDict(type(err.error_code).pb(err.error_code))
        code_s = ",".join(f"{k}={v}" for k, v in code.items())
        loc = ".".join(
            e.field_name + (f"[{e.index}]" if "index" in e else "")
            for e in err.location.field_path_elements
        )
        messages.append(f"{code_s}: {err.message}" + (f" (at {loc})" if loc else ""))
    return AdsApiError(messages, ex.request_id)


class GoogleAdsGateway:
    """Calls the Google Ads API as the signed-in user."""

    def __init__(self, customer_id: str, access_token: str):
        self.customer_id = customer_id
        self.recorder = _RequestIdRecorder()
        args = {"credentials": Credentials(token=access_token), "use_proto_plus": True, "version": API_VERSION}
        # Optional, as in googleads/google-ads-mcp: API access can come from the
        # Cloud project instead of a developer token.
        if os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN"):
            args["developer_token"] = os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"]
        self._client = GoogleAdsClient(**args)
        self._ga = self._client.get_service("GoogleAdsService", interceptors=[self.recorder])

    # -- reads ---------------------------------------------------------------

    def _search(self, query: str) -> list:
        try:
            return list(self._ga.search(customer_id=self.customer_id, query=query))
        except GoogleAdsException as ex:
            raise _ads_error(ex)

    def campaign(self, campaign_id):
        rows = self._search(Q_CAMPAIGN.format(campaign_id=_digits(campaign_id)))
        if not rows:
            return None
        r = rows[0]
        c, b = r.campaign, r.campaign_budget
        return {
            "id": str(c.id),
            "name": c.name,
            "status": _enum(c.status),
            "budget_resource_name": c.campaign_budget,
            "budget_amount_micros": int(b.amount_micros),
            "budget_explicitly_shared": bool(b.explicitly_shared),
            "budget_reference_count": int(b.reference_count),
            "bidding_strategy_type": _enum(c.bidding_strategy_type),
            "portfolio_bidding_strategy": c.bidding_strategy or None,
            "cpc_bid_ceiling_micros": int(c.target_spend.cpc_bid_ceiling_micros) or None,
            "target_cpa_micros": int(c.maximize_conversions.target_cpa_micros) or None,
            "enhanced_cpc_enabled": bool(c.manual_cpc.enhanced_cpc_enabled),
        }

    def campaign_negatives(self, campaign_id):
        rows = self._search(Q_NEGATIVES.format(campaign_id=_digits(campaign_id)))
        return [
            {
                "criterion_id": str(r.campaign_criterion.criterion_id),
                "text": r.campaign_criterion.keyword.text,
                "match_type": _enum(r.campaign_criterion.keyword.match_type),
            }
            for r in rows
        ]

    def campaign_keywords(self, campaign_id):
        rows = self._search(Q_KEYWORDS.format(campaign_id=_digits(campaign_id)))
        return [
            {
                "ad_group_id": str(r.ad_group.id),
                "ad_group_name": r.ad_group.name,
                "ad_group_status": _enum(r.ad_group.status),
                "criterion_id": str(r.ad_group_criterion.criterion_id),
                "text": r.ad_group_criterion.keyword.text,
                "match_type": _enum(r.ad_group_criterion.keyword.match_type),
                "status": _enum(r.ad_group_criterion.status),
                "cpc_bid_micros": int(r.ad_group_criterion.cpc_bid_micros) or None,  # own bid; None = ad group default
                "ad_group_cpc_bid_micros": int(r.ad_group.cpc_bid_micros) or None,
            }
            for r in rows
        ]

    def ad_group(self, ad_group_id):
        rows = self._search(Q_AD_GROUP.format(ad_group_id=_digits(ad_group_id)))
        if not rows:
            return None
        return self._ad_group_row(rows[0])

    @staticmethod
    def _ad_group_row(r) -> dict:
        return {
            "id": str(r.ad_group.id),
            "name": r.ad_group.name,
            "status": _enum(r.ad_group.status),
            "campaign_id": str(r.campaign.id),
            "cpc_bid_micros": int(r.ad_group.cpc_bid_micros) or None,
        }

    def campaign_ad_groups(self, campaign_id):
        rows = self._search(Q_AD_GROUPS.format(campaign_id=_digits(campaign_id)))
        return [self._ad_group_row(r) for r in rows]

    def device_criteria(self, campaign_id):
        rows = self._search(Q_DEVICES.format(campaign_id=_digits(campaign_id)))
        out = []
        for r in rows:
            cc = r.campaign_criterion
            mod = cc.bid_modifier if "bid_modifier" in cc else None
            out.append({"criterion_id": str(cc.criterion_id), "device": _enum(cc.device.type_), "bid_modifier": float(mod) if mod else None})
        return out

    def ad_schedule(self, campaign_id):
        rows = self._search(Q_SCHEDULE.format(campaign_id=_digits(campaign_id)))
        out = []
        for r in rows:
            cc = r.campaign_criterion
            s = cc.ad_schedule
            mod = cc.bid_modifier if "bid_modifier" in cc else None
            out.append(
                {
                    "criterion_id": str(cc.criterion_id),
                    "day": _enum(s.day_of_week),
                    "start_hour": int(s.start_hour),
                    "start_minute": _enum(s.start_minute),
                    "end_hour": int(s.end_hour),
                    "end_minute": _enum(s.end_minute),
                    "bid_modifier": None if mod in (None, 0.0, 1.0) else float(mod),
                }
            )
        return out

    def rsa(self, ad_id):
        rows = self._search(Q_RSA.format(ad_id=_digits(ad_id)))
        if not rows:
            return None
        r = rows[0]
        ad = r.ad_group_ad.ad
        rsa = ad.responsive_search_ad

        def assets(items):
            out = []
            for a in items:
                d = {"text": a.text}
                pin = _enum(a.pinned_field)
                if pin not in ("UNSPECIFIED", "UNKNOWN"):
                    d["pinned_field"] = pin
                out.append(d)
            return out

        return {
            "ad_id": str(ad.id),
            "type": _enum(ad.type_),
            "status": _enum(r.ad_group_ad.status),
            "ad_group_id": str(r.ad_group.id),
            "campaign_id": str(r.campaign.id),
            "final_urls": list(ad.final_urls),
            "path1": rsa.path1,
            "path2": rsa.path2,
            "headlines": assets(rsa.headlines),
            "descriptions": assets(rsa.descriptions),
        }

    def campaign_assets(self, campaign_id):
        rows = self._search(Q_ASSETS.format(campaign_id=_digits(campaign_id)))
        out = []
        for r in rows:
            a, ca = r.asset, r.campaign_asset
            out.append(
                {
                    "resource_name": ca.resource_name,
                    "asset_id": str(a.id),
                    "field_type": _enum(ca.field_type),
                    "status": _enum(ca.status),
                    "asset_type": _enum(a.type_),
                    "final_urls": list(a.final_urls),
                    "link_text": a.sitelink_asset.link_text,
                    "description1": a.sitelink_asset.description1,
                    "description2": a.sitelink_asset.description2,
                    "callout_text": a.callout_asset.callout_text,
                    "header": a.structured_snippet_asset.header,
                    "values": list(a.structured_snippet_asset.values),
                }
            )
        return out

    def time_zone(self):
        rows = self._search(Q_TIME_ZONE)
        return rows[0].customer.time_zone

    def today_metrics(self, campaign_id):
        rows = self._search(Q_TODAY.format(campaign_id=_digits(campaign_id)))
        tot = {"cost_micros": 0, "clicks": 0, "impressions": 0, "conversions": 0.0}
        for r in rows:
            tot["cost_micros"] += int(r.metrics.cost_micros)
            tot["clicks"] += int(r.metrics.clicks)
            tot["impressions"] += int(r.metrics.impressions)
            tot["conversions"] += float(r.metrics.conversions)
        return tot

    def hourly_metrics(self, campaign_id, start_date, end_date):
        for d in (start_date, end_date):
            if len(d) != 10 or not d.replace("-", "").isdigit():
                raise Refused(f"Bad date {d!r}.")
        rows = self._search(
            Q_HOURLY.format(campaign_id=_digits(campaign_id), start_date=start_date, end_date=end_date)
        )
        return [
            {
                "date": r.segments.date,
                "hour": int(r.segments.hour),
                "clicks": int(r.metrics.clicks),
                "impressions": int(r.metrics.impressions),
                "conversions": float(r.metrics.conversions),
            }
            for r in rows
        ]

    # -- writes --------------------------------------------------------------

    def mutate(self, operations, validate_only):
        before = len(self.recorder.request_ids)
        request = MutateGoogleAdsRequest(
            customer_id=self.customer_id,
            mutate_operations=[MutateOperation.wrap(ops.to_proto(o)) for o in operations],
            partial_failure=False,
            validate_only=validate_only,
        )
        try:
            response = self._ga.mutate(request=request)
        except GoogleAdsException as ex:
            raise _ads_error(ex)
        results = []
        for r in response.mutate_operation_responses:
            d = json_format.MessageToDict(type(r).pb(r), preserving_proto_field_name=True)
            results.append(next(iter(d.values()), {}).get("resource_name"))
        return {"resource_names": results, "request_ids": self.recorder.request_ids[before:]}

    def request_ids(self) -> list[str]:
        return list(self.recorder.request_ids)
