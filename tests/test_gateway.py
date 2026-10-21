"""Offline checks of GoogleAdsGateway's request building and error parsing."""

import pytest
from google.ads.googleads.errors import GoogleAdsException
from google.ads.googleads.v26.errors.types.errors import ErrorLocation, GoogleAdsError, GoogleAdsFailure
from google.ads.googleads.v26.services.types.google_ads_service import (
    GoogleAdsRow,
    MutateGoogleAdsResponse,
    MutateOperationResponse,
)

from ads_actions import gateway, ops
from ads_actions.errors import AdsApiError


class StubService:
    def __init__(self, rows=None, raise_on_mutate=None):
        self.rows = rows or []
        self.raise_on_mutate = raise_on_mutate
        self.requests = []

    def search(self, customer_id, query):
        return iter(self.rows)

    def mutate(self, request):
        self.requests.append(request)
        if self.raise_on_mutate:
            raise self.raise_on_mutate
        r = MutateOperationResponse()
        r.campaign_criterion_result.resource_name = "customers/1/campaignCriteria/2~3"
        return MutateGoogleAdsResponse(mutate_operation_responses=[r])


def make_gw(monkeypatch, svc):
    monkeypatch.setenv("GOOGLE_ADS_DEVELOPER_TOKEN", "dev")
    gw = gateway.GoogleAdsGateway("6775198830", "user-token")
    gw._ga = svc
    return gw


def test_mutate_builds_atomic_request(monkeypatch):
    svc = StubService()
    gw = make_gw(monkeypatch, svc)
    op = ops.create("campaign_criterion", {"campaign": "customers/6775198830/campaigns/1", "negative": True, "keyword": {"text": "jobs", "match_type": "PHRASE"}})
    out = gw.mutate([op], validate_only=True)
    req = svc.requests[0]
    assert req.validate_only is True and req.partial_failure is False
    assert req.customer_id == "6775198830"
    assert req.mutate_operations[0].campaign_criterion_operation.create.keyword.text == "jobs"
    assert out["resource_names"] == ["customers/1/campaignCriteria/2~3"]


def test_google_error_is_reported(monkeypatch):
    e = GoogleAdsError(message="Too low.")
    e.error_code.range_error = 3
    e.location = ErrorLocation(field_path_elements=[ErrorLocation.FieldPathElement(field_name="amount_micros")])
    failure = GoogleAdsFailure(errors=[e])
    ex = GoogleAdsException(error=None, call=None, failure=failure, request_id="abc123")
    gw = make_gw(monkeypatch, StubService(raise_on_mutate=ex))
    with pytest.raises(AdsApiError) as info:
        gw.mutate([ops.update("campaign", {"resource_name": "customers/6775198830/campaigns/1", "status": "PAUSED"}, ["status"])], False)
    assert "rangeError" in str(info.value) and "Too low." in str(info.value) and "abc123" in str(info.value)
    assert info.value.request_id == "abc123"


def test_reads_parse_rows(monkeypatch):
    row = GoogleAdsRow()
    row.campaign.id = 24378039028
    row.campaign.name = "Search | AI Integration | US"
    row.campaign.status = 2  # ENABLED
    row.campaign.campaign_budget = "customers/6775198830/campaignBudgets/15954093257"
    row.campaign.bidding_strategy_type = 9  # TARGET_SPEND
    row.campaign.target_spend.cpc_bid_ceiling_micros = 270_000_000_000
    row.campaign_budget.amount_micros = 900_000_000_000
    row.campaign_budget.reference_count = 1
    gw = make_gw(monkeypatch, StubService(rows=[row]))
    c = gw.campaign("24378039028")
    assert c["status"] == "ENABLED" and c["bidding_strategy_type"] == "TARGET_SPEND"
    assert c["budget_amount_micros"] == 900_000_000_000 and c["cpc_bid_ceiling_micros"] == 270_000_000_000
    assert c["target_cpa_micros"] is None and c["portfolio_bidding_strategy"] is None

    srow = GoogleAdsRow()
    srow.campaign_criterion.criterion_id = 5
    srow.campaign_criterion.ad_schedule.day_of_week = 2  # MONDAY
    srow.campaign_criterion.ad_schedule.start_hour = 0
    srow.campaign_criterion.ad_schedule.end_hour = 7
    srow.campaign_criterion.ad_schedule.start_minute = 2  # ZERO
    srow.campaign_criterion.ad_schedule.end_minute = 2
    gw._ga = StubService(rows=[srow])
    (s,) = gw.ad_schedule("24378039028")
    assert s == {"criterion_id": "5", "day": "MONDAY", "start_hour": 0, "start_minute": "ZERO", "end_hour": 7, "end_minute": "ZERO", "bid_modifier": None}
