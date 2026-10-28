"""Change request of 28 Oct: Manual CPC, ad group / keyword bids, device adjustments.

Tests 1-8 of the request, plus execute/undo/drift on the fake account.
"""

import copy

import pytest

from ads_actions.engine import Engine, Session
from ads_actions.errors import Refused
from ads_actions.guardrails import Guardrails
from ads_actions.store import MemoryStore
from tests.fake_ads import CAMPAIGN, CUSTOMER, FakeAds

M = 1_000_000
INTEGRATION, AUTOMATION, CONSULTING = "201460749979", "201460750019", "201460750059"
PAUSED = ("201460750219", "201460750259")


@pytest.fixture
def env():
    fake = FakeAds()
    engine = Engine(Guardrails.load(), MemoryStore())
    return engine, Session("ardhofortana@gmail.com", lambda cid: fake), fake


async def propose(env, change_type, **request):
    engine, session, _ = env
    return await engine.propose(session, CUSTOMER, change_type, {"customer_id": CUSTOMER, **request})


async def execute(env, change_id):
    engine, session, _ = env
    return await engine.execute(session, CUSTOMER, change_id)


def manual(env):
    """Puts the fake campaign on Manual CPC directly (as if switched earlier)."""
    c = env[2].state["campaign"]
    c.update(bidding_strategy_type="MANUAL_CPC", cpc_bid_ceiling_micros=None, enhanced_cpc_enabled=False)


# -- the request's tests ---------------------------------------------------------


async def test_1_switch_to_manual_cpc_one_atomic_proposal(env):
    _, _, fake = env
    r = await propose(env, "set_bidding", campaign_id=CAMPAIGN, strategy="MANUAL_CPC", ad_group_default_cpc_idr=190_000)
    t = r["before_after"]
    assert "| Maximize clicks, CPC ceiling Rp270,000 | Manual CPC (Enhanced CPC off) |" in t
    for name in ("AI Integration Services", "AI Automation", "AI Consulting"):
        assert f"Ad group {name}" in t and "| Rp270,000 | Rp190,000 (-30%) |" in t
    assert "Custom AI Solutions" not in t and "AI Development Partner" not in t
    assert any("Custom AI Solutions (paused) not changed" in s for s in r["skipped"])
    ops_sent, validate_only = fake.mutate_calls[-1]
    assert validate_only is True and len(ops_sent) == 4  # 3 ad groups + campaign, one call
    assert ops_sent[-1]["campaign_operation"]["update"]["manual_cpc"] == {"enhanced_cpc_enabled": False}
    await env[0].cancel(env[1], CUSTOMER, r["change_id"])


async def test_2_manual_cpc_requires_default(env):
    with pytest.raises(Refused, match="ad_group_default_cpc_idr is required"):
        await propose(env, "set_bidding", campaign_id=CAMPAIGN, strategy="MANUAL_CPC")


async def test_3_default_above_cap(env):
    with pytest.raises(Refused, match="outside the allowed range Rp20,000 to Rp400,000"):
        await propose(env, "set_bidding", campaign_id=CAMPAIGN, strategy="MANUAL_CPC", ad_group_default_cpc_idr=450_000)


async def test_4_override_more_than_50_pct(env):
    with pytest.raises(Refused, match=r"AI Consulting: Rp270,000 to Rp100,000 is a -63% change"):
        await propose(env, "set_bidding", campaign_id=CAMPAIGN, strategy="MANUAL_CPC", ad_group_default_cpc_idr=190_000, ad_group_bids={CONSULTING: 100_000})


async def test_5_ad_group_cpc_needs_manual(env):
    with pytest.raises(Refused, match="only allowed while the campaign uses Manual CPC"):
        await propose(env, "set_ad_group_cpc", ad_group_id=INTEGRATION, cpc_idr=200_000)


async def test_6_keyword_cpc_needs_manual(env):
    crit = next(c for (ag, c) in env[2].state["keywords"] if ag == INTEGRATION)
    with pytest.raises(Refused, match="only allowed while the campaign uses Manual CPC"):
        await propose(env, "set_keyword_cpc", items=[{"ad_group_id": INTEGRATION, "criterion_id": crit, "cpc_idr": 200_000}])


async def test_8_device_adjustment_needs_manual(env):
    with pytest.raises(Refused, match="only allowed while the campaign uses Manual CPC"):
        await propose(env, "set_device_bid_adjustment", campaign_id=CAMPAIGN, device="MOBILE", percent=-30)


# -- execute, undo, drift ----------------------------------------------------------


async def test_execute_switch_then_undo_restores_strategy_and_bids_atomically(env):
    _, _, fake = env
    start = copy.deepcopy(fake.state)
    r = await propose(env, "set_bidding", campaign_id=CAMPAIGN, strategy="MANUAL_CPC", ad_group_default_cpc_idr=190_000, ad_group_bids={CONSULTING: 150_000})
    await execute(env, r["change_id"])
    c, groups = fake.state["campaign"], fake.state["ad_groups"]
    assert c["bidding_strategy_type"] == "MANUAL_CPC" and c["cpc_bid_ceiling_micros"] is None
    assert [groups[g]["cpc_bid_micros"] // M for g in (INTEGRATION, AUTOMATION, CONSULTING)] == [190_000, 190_000, 150_000]
    assert all(groups[g]["cpc_bid_micros"] == 270_000 * M for g in PAUSED)

    u = await env[0].propose_undo(env[1], CUSTOMER, r["change_id"])
    assert "| Manual CPC (Enhanced CPC off) | Maximize clicks, CPC ceiling Rp270,000 |" in u["before_after"]
    assert u["before_after"].count("| Rp270,000 (+") == 3
    assert len(fake.mutate_calls[-1][0]) == 4  # one atomic undo
    await execute(env, u["change_id"])
    assert fake.state["campaign"] == start["campaign"] and fake.state["ad_groups"] == start["ad_groups"]


async def test_drift_on_an_ad_group_bid_refuses_the_switch(env):
    r = await propose(env, "set_bidding", campaign_id=CAMPAIGN, strategy="MANUAL_CPC", ad_group_default_cpc_idr=190_000)
    env[2].state["ad_groups"][AUTOMATION]["cpc_bid_micros"] = 250_000 * M  # changed in the UI meanwhile
    with pytest.raises(Refused, match="no longer match"):
        await execute(env, r["change_id"])
    assert env[2].state["campaign"]["bidding_strategy_type"] == "TARGET_SPEND"


async def test_paused_group_changes_only_when_listed(env):
    r = await propose(env, "set_bidding", campaign_id=CAMPAIGN, strategy="MANUAL_CPC", ad_group_default_cpc_idr=190_000, ad_group_bids={PAUSED[0]: 200_000})
    assert "Ad group Custom AI Solutions" in r["before_after"] and "AI Development Partner" not in r["before_after"]


async def test_bid_lower_bound_and_unknown_group(env):
    with pytest.raises(Refused, match="outside the allowed range"):
        await propose(env, "set_bidding", campaign_id=CAMPAIGN, strategy="MANUAL_CPC", ad_group_default_cpc_idr=190_000, ad_group_bids={INTEGRATION: 15_000})
    with pytest.raises(Refused, match="not in campaign"):
        await propose(env, "set_bidding", campaign_id=CAMPAIGN, strategy="MANUAL_CPC", ad_group_default_cpc_idr=190_000, ad_group_bids={"999": 190_000})
    with pytest.raises(Refused, match="only applies to MANUAL_CPC"):
        await propose(env, "set_bidding", campaign_id=CAMPAIGN, strategy="MAXIMIZE_CLICKS", cpc_ceiling_idr=200_000, ad_group_bids={INTEGRATION: 190_000})


async def test_back_to_maximize_clicks_from_manual(env):
    manual(env)
    r = await propose(env, "set_bidding", campaign_id=CAMPAIGN, strategy="MAXIMIZE_CLICKS", cpc_ceiling_idr=250_000)
    assert "| Manual CPC (Enhanced CPC off) | Maximize clicks, CPC ceiling Rp250,000 |" in r["before_after"]
    await execute(env, r["change_id"])
    assert env[2].state["campaign"]["bidding_strategy_type"] == "TARGET_SPEND"
    u = await env[0].propose_undo(env[1], CUSTOMER, r["change_id"])
    await execute(env, u["change_id"])
    assert env[2].state["campaign"]["bidding_strategy_type"] == "MANUAL_CPC"


async def test_ad_group_cpc_under_manual(env):
    manual(env)
    with pytest.raises(Refused, match=r"-56% change"):
        await propose(env, "set_ad_group_cpc", ad_group_id=INTEGRATION, cpc_idr=120_000)
    r = await propose(env, "set_ad_group_cpc", ad_group_id=INTEGRATION, cpc_idr=200_000)
    assert "| Rp270,000 | Rp200,000 (-26%) |" in r["before_after"]
    await execute(env, r["change_id"])
    assert env[2].state["ad_groups"][INTEGRATION]["cpc_bid_micros"] == 200_000 * M
    u = await env[0].propose_undo(env[1], CUSTOMER, r["change_id"])
    await execute(env, u["change_id"])
    assert env[2].state["ad_groups"][INTEGRATION]["cpc_bid_micros"] == 270_000 * M


async def test_keyword_cpc_set_and_remove_under_manual(env):
    manual(env)
    _, _, fake = env
    crit = next(c for (ag, c) in fake.state["keywords"] if ag == INTEGRATION)
    # No own bid yet: compared with the ad group default (Rp270,000).
    with pytest.raises(Refused, match=r"-63% change"):
        await propose(env, "set_keyword_cpc", items=[{"ad_group_id": INTEGRATION, "criterion_id": crit, "cpc_idr": 100_000}])
    r = await propose(env, "set_keyword_cpc", items=[{"ad_group_id": INTEGRATION, "criterion_id": crit, "cpc_idr": 300_000}])
    assert "| ad group default Rp270,000 | Rp300,000 (own bid, +11%) |" in r["before_after"]
    await execute(env, r["change_id"])
    assert fake.state["keywords"][(INTEGRATION, crit)]["cpc_bid_micros"] == 300_000 * M

    r = await propose(env, "set_keyword_cpc", items=[{"ad_group_id": INTEGRATION, "criterion_id": crit, "cpc_idr": 0}])
    assert "| Rp300,000 (own bid) | ad group default Rp270,000 (-10%) |" in r["before_after"]
    await execute(env, r["change_id"])
    assert fake.state["keywords"][(INTEGRATION, crit)]["cpc_bid_micros"] is None
    u = await env[0].propose_undo(env[1], CUSTOMER, r["change_id"])
    await execute(env, u["change_id"])
    assert fake.state["keywords"][(INTEGRATION, crit)]["cpc_bid_micros"] == 300_000 * M


async def test_keyword_cpc_limits(env):
    manual(env)
    crit = next(c for (ag, c) in env[2].state["keywords"] if ag == INTEGRATION)
    item = {"ad_group_id": INTEGRATION, "criterion_id": crit, "cpc_idr": 300_000}
    with pytest.raises(Refused, match="listed twice"):
        await propose(env, "set_keyword_cpc", items=[item, item])
    with pytest.raises(Refused, match="limit is 20"):
        await propose(env, "set_keyword_cpc", items=[{**item, "criterion_id": str(i)} for i in range(21)])
    with pytest.raises(Refused, match="no own bid"):
        await propose(env, "set_keyword_cpc", items=[{**item, "cpc_idr": None}])


async def test_switch_warns_about_keywords_with_own_bids(env):
    crit = next(c for (ag, c) in env[2].state["keywords"] if ag == INTEGRATION)
    env[2].state["keywords"][(INTEGRATION, crit)]["cpc_bid_micros"] = 350_000 * M
    r = await propose(env, "set_bidding", campaign_id=CAMPAIGN, strategy="MANUAL_CPC", ad_group_default_cpc_idr=190_000)
    assert any("own bid and keep it" in w and "Rp350,000" in w for w in r["warnings"])


async def test_device_adjustment_under_manual(env):
    manual(env)
    with pytest.raises(Refused, match="outside the allowed range -50% to \\+50%"):
        await propose(env, "set_device_bid_adjustment", campaign_id=CAMPAIGN, device="MOBILE", percent=-60)
    r = await propose(env, "set_device_bid_adjustment", campaign_id=CAMPAIGN, device="MOBILE", percent=-30)
    assert "| +0% | -30% (bid x0.7) |" in r["before_after"]
    op = env[2].mutate_calls[-1][0][0]["campaign_criterion_operation"]
    assert op["update"]["resource_name"].endswith(f"{CAMPAIGN}~30001") and op["update"]["bid_modifier"] == 0.7
    await execute(env, r["change_id"])
    u = await env[0].propose_undo(env[1], CUSTOMER, r["change_id"])
    assert "| -30% | +0% (bid x1) |" in u["before_after"]


async def test_undo_of_a_large_cut_restores_even_past_the_change_limit(env):
    manual(env)
    r = await propose(env, "set_ad_group_cpc", ad_group_id=INTEGRATION, cpc_idr=140_000)  # -48%
    await execute(env, r["change_id"])
    u = await env[0].propose_undo(env[1], CUSTOMER, r["change_id"])  # +93%, allowed for undo
    await execute(env, u["change_id"])
    assert env[2].state["ad_groups"][INTEGRATION]["cpc_bid_micros"] == 270_000 * M
    # A normal proposal still can't jump that far.
    await execute(env, (await propose(env, "set_ad_group_cpc", ad_group_id=INTEGRATION, cpc_idr=140_000))["change_id"])
    with pytest.raises(Refused, match=r"\+93% change"):
        await propose(env, "set_ad_group_cpc", ad_group_id=INTEGRATION, cpc_idr=270_000)


async def test_undo_still_respects_the_bid_range(env):
    manual(env)
    env[2].state["ad_groups"][INTEGRATION]["cpc_bid_micros"] = 450_000 * M  # set outside our range in the UI
    r = await propose(env, "set_ad_group_cpc", ad_group_id=INTEGRATION, cpc_idr=300_000)
    await execute(env, r["change_id"])
    with pytest.raises(Refused, match="outside the allowed range"):
        await env[0].propose_undo(env[1], CUSTOMER, r["change_id"])


async def test_tools_cannot_set_the_undo_only_flags():
    from ads_actions import server

    mcp = server.build_server(Engine(Guardrails.load(), MemoryStore()))
    for tool in await mcp.list_tools():
        props = set((tool.parameters or {}).get("properties", {}))
        assert not props & {"restore", "only_listed_ad_groups"}, tool.name


async def test_manual_cpc_through_the_mcp_tools(env, monkeypatch):
    from fastmcp import Client
    from fastmcp.exceptions import ToolError
    from fastmcp.server.auth.auth import AccessToken

    from ads_actions import server

    engine, _, fake = env
    token = AccessToken(token="t", client_id="c", scopes=[], claims={"email": "ardhofortana@gmail.com", "email_verified": True})
    monkeypatch.setattr(server, "get_access_token", lambda: token)
    mcp = server.build_server(engine, make_gateway=lambda cid, tok: fake)
    async with Client(mcp) as c:
        r = (await c.call_tool("propose_set_bidding", {
            "customer_id": CUSTOMER, "campaign_id": CAMPAIGN, "strategy": "MANUAL_CPC",
            "ad_group_default_cpc_idr": 190000, "ad_group_bids": {CONSULTING: 180000},
        })).data
        assert "Rp180,000 (-33%)" in r["before_after"]
        with pytest.raises(ToolError, match="only allowed while the campaign uses Manual CPC"):
            await c.call_tool("propose_set_keyword_cpc", {"customer_id": CUSTOMER, "ad_group_id": INTEGRATION, "criterion_id": "900001", "cpc_idr": 200000})
        await c.call_tool("cancel_change", {"customer_id": CUSTOMER, "change_id": r["change_id"]})
        assert (await c.call_tool("list_pending_changes", {"customer_id": CUSTOMER})).structured_content["result"] == []
