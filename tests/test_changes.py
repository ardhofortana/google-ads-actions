"""Execute + undo round trips for every change type, and offline API checks."""

import re

import pytest
from google.ads.googleads.v26.services.types.google_ads_service import GoogleAdsRow

from ads_actions.engine import Engine, Session
from ads_actions.errors import Refused
from ads_actions.gateway import ALL_QUERIES
from ads_actions.guardrails import Guardrails
from ads_actions.store import MemoryStore
from tests.fake_ads import CAMPAIGN, CUSTOMER, FakeAds


@pytest.fixture
def env():
    fake = FakeAds()
    engine = Engine(Guardrails.load(), MemoryStore())
    return engine, Session("ardhofortana@gmail.com", lambda cid: fake), fake


async def roundtrip(env, change_type, **request):
    """Propose, execute, undo, and check the account is back where it started."""
    engine, session, fake = env
    import copy

    start = copy.deepcopy(fake.state)
    p = await engine.propose(session, CUSTOMER, change_type, {"customer_id": CUSTOMER, **request})
    assert fake.state == start, "propose must not change anything"
    await engine.execute(session, CUSTOMER, p["change_id"])
    changed = copy.deepcopy(fake.state)
    assert changed != start
    u = await engine.propose_undo(session, CUSTOMER, p["change_id"])
    await engine.execute(session, CUSTOMER, u["change_id"])
    return start, changed, fake.state


def _strip_ids(state):
    """Re-created criteria and assets get new IDs; compare by content."""
    s = dict(state)
    s["negatives"] = sorted((v["text"], v["match_type"]) for v in state["negatives"].values())
    s["schedule"] = sorted(tuple(sorted(v.items(), key=str)) for v in state["schedule"].values())
    s["keywords"] = sorted((k[0], v["text"], v["match_type"], v["status"]) for k, v in state["keywords"].items())
    s.pop("next_id")
    return s


@pytest.mark.parametrize(
    "change_type,request_",
    [
        ("set_daily_budget", {"campaign_id": CAMPAIGN, "amount_idr": 1_200_000}),
        ("set_bidding", {"campaign_id": CAMPAIGN, "strategy": "MAXIMIZE_CLICKS", "cpc_ceiling_idr": 200_000}),
        ("set_bidding", {"campaign_id": CAMPAIGN, "strategy": "MAXIMIZE_CONVERSIONS", "target_cpa_idr": 3_000_000}),
        ("set_campaign_status", {"campaign_id": CAMPAIGN, "status": "PAUSED"}),
        ("set_ad_group_status", {"ad_group_id": "201460750259", "status": "ENABLED"}),
        ("shift_ad_schedule", {"campaign_id": CAMPAIGN, "hours": 1}),
        ("set_ad_schedule", {"campaign_id": CAMPAIGN, "slots": [{"day": "MON", "start_hour": 8, "end_hour": 17}]}),
        ("update_rsa", {"ad_id": "700000000001", "descriptions": ["We connect AI to the tools you already use.", "Senior engineers and a clear scope."]}),
        ("update_rsa", {"ad_id": "700000000001", "pin_first_headline": False}),
        ("add_keywords", {"ad_group_id": "201460750019", "keywords": [{"text": "workflow automation ai", "match_type": "PHRASE"}]}),
        ("remove_campaign_negatives", {"campaign_id": CAMPAIGN, "criterion_ids": ["__first_negative__"]}),
    ],
)
async def test_roundtrip(env, change_type, request_):
    if request_.get("criterion_ids") == ["__first_negative__"]:
        request_ = {**request_, "criterion_ids": list(env[2].state["negatives"])[:1]}
    start, _, end = await roundtrip(env, change_type, **request_)
    assert _strip_ids(end) == _strip_ids(start)


async def test_keyword_status_roundtrip(env):
    crit = next(c for (ag, c) in env[2].state["keywords"] if ag == "201460750019")
    start, changed, end = await roundtrip(env, "set_keyword_status", ad_group_id="201460750019", criterion_id=crit, status="PAUSED")
    assert changed["keywords"][("201460750019", crit)]["status"] == "PAUSED"
    assert end == start


async def test_assets_add_remove_relink(env):
    engine, session, fake = env
    items = [{"link_text": "Case Studies", "final_url": "https://djavaweb.com/case-studies/", "description1": "See recent AI projects", "description2": "Results from real clients"}]
    p = await engine.propose(session, CUSTOMER, "add_campaign_assets", {"customer_id": CUSTOMER, "campaign_id": CAMPAIGN, "type": "SITELINK", "items": items})
    e = await engine.execute(session, CUSTOMER, p["change_id"])
    (link,) = e["after"]["added"]
    assert len(fake.state["campaign_assets"]) == 1

    r = await engine.propose(session, CUSTOMER, "remove_campaign_assets", {"customer_id": CUSTOMER, "campaign_id": CAMPAIGN, "links": [{"asset_id": link["asset_id"], "field_type": "SITELINK"}]})
    await engine.execute(session, CUSTOMER, r["change_id"])
    assert fake.state["campaign_assets"] == {}
    u = await engine.propose_undo(session, CUSTOMER, r["change_id"])
    await engine.execute(session, CUSTOMER, u["change_id"])
    assert list(fake.state["campaign_assets"]) == [(CAMPAIGN, link["asset_id"], "SITELINK")]

    with pytest.raises(Refused, match="already on the campaign"):
        await engine.propose(session, CUSTOMER, "add_campaign_assets", {"customer_id": CUSTOMER, "campaign_id": CAMPAIGN, "type": "SITELINK", "items": items})
    with pytest.raises(Refused, match="must start with https://djavaweb.com/"):
        await engine.propose(session, CUSTOMER, "add_campaign_assets", {"customer_id": CUSTOMER, "campaign_id": CAMPAIGN, "type": "SITELINK", "items": [{**items[0], "link_text": "Other", "final_url": "https://evil.example/"}]})
    with pytest.raises(Refused, match="26 characters"):
        await engine.propose(session, CUSTOMER, "add_campaign_assets", {"customer_id": CUSTOMER, "campaign_id": CAMPAIGN, "type": "CALLOUT", "items": ["A" * 26]})


async def test_bidding_and_status_guardrails(env):
    engine, session, _ = env
    for req, why in [
        ({"strategy": "MAXIMIZE_CLICKS", "cpc_ceiling_idr": 500_000}, "outside the allowed range"),
        ({"strategy": "MAXIMIZE_CONVERSIONS", "target_cpa_idr": 500_000}, "outside the allowed range"),
        ({"strategy": "TARGET_ROAS"}, "MAXIMIZE_CONVERSIONS or MANUAL_CPC"),
        ({"strategy": "MAXIMIZE_CLICKS", "cpc_ceiling_idr": 270_000}, "already"),
    ]:
        with pytest.raises(Refused, match=why):
            await engine.propose(session, CUSTOMER, "set_bidding", {"customer_id": CUSTOMER, "campaign_id": CAMPAIGN, **req})
    with pytest.raises(Refused, match="already ENABLED"):
        await engine.propose(session, CUSTOMER, "set_campaign_status", {"customer_id": CUSTOMER, "campaign_id": CAMPAIGN, "status": "ENABLED"})
    with pytest.raises(Refused, match="ENABLED or PAUSED"):
        await engine.propose(session, CUSTOMER, "set_campaign_status", {"customer_id": CUSTOMER, "campaign_id": CAMPAIGN, "status": "REMOVED"})


async def test_limits_per_proposal(env):
    engine, session, _ = env
    with pytest.raises(Refused, match="limit is 50"):
        await engine.propose(session, CUSTOMER, "add_campaign_negatives", {"customer_id": CUSTOMER, "campaign_id": CAMPAIGN, "keywords": [f"kw {i}" for i in range(51)], "match_type": "EXACT"})
    with pytest.raises(Refused, match="limit is 20"):
        await engine.propose(session, CUSTOMER, "add_keywords", {"customer_id": CUSTOMER, "ad_group_id": "201460749979", "keywords": [{"text": f"kw {i}", "match_type": "EXACT"} for i in range(21)]})


async def test_propose_rejected_by_google_is_not_stored(env):
    engine, session, fake = env
    fake.state["campaign"]["budget_resource_name"] = "customers/6775198830/campaignBudgets/1"
    from ads_actions.errors import AdsApiError

    def boom(operations, validate_only):
        raise AdsApiError(["rangeError=TOO_LOW: test"], "req-1")

    fake.mutate = boom
    with pytest.raises(AdsApiError, match="TOO_LOW"):
        await engine.propose(session, CUSTOMER, "set_daily_budget", {"customer_id": CUSTOMER, "campaign_id": CAMPAIGN, "amount_idr": 1_000_000})
    assert engine.store.docs == {}


def test_gaql_fields_exist_in_v26_proto():
    desc = GoogleAdsRow()._pb.DESCRIPTOR
    bad = []
    for q in ALL_QUERIES:
        select = re.search(r"SELECT(.*?)FROM", q, re.S).group(1)
        where = q.split("FROM", 1)[1]
        fields = [f.strip() for f in select.split(",")]
        fields += re.findall(r"\b([a-z_]+(?:\.[a-z_0-9]+)+)\s*(?:=|!=|DURING|BETWEEN)", where)
        for f in fields:
            d = desc
            for part in f.split("."):
                fd = d.fields_by_name.get(part) or d.fields_by_name.get(part + "_")
                if fd is None:
                    bad.append(f)
                    break
                d = fd.message_type
    assert bad == []


def test_where_fields_of_other_resources_are_selected():
    """campaign_asset (and others) reject a WHERE on campaign.id unless it is selected."""
    missing = []
    for q in ALL_QUERIES:
        select = {f.strip() for f in re.search(r"SELECT(.*?)FROM", q, re.S).group(1).split(",")}
        main = re.search(r"FROM\s+(\w+)", q).group(1)
        where = q.split("WHERE", 1)[1] if "WHERE" in q else ""
        for f in re.findall(r"\b([a-z_]+(?:\.[a-z_0-9]+)+)\s*(?:=|!=)", where):
            if not f.startswith(main + ".") and not f.startswith("segments.") and f not in select:
                missing.append((main, f))
    assert missing == []
