"""Undo safety, as approved on 28 Oct together with the ±% exemption for undo.

(1) An undo takes its values only from the "before" snapshot in the audit log,
    never from input.
(2) An undo is refused when the live values differ from the "after" values of
    the change being undone.
(3) The ranges still apply to an undo: bids Rp20,000-400,000, budget
    Rp300,000-1,800,000.
"""

import pytest

from ads_actions.engine import Engine, Session
from ads_actions.errors import Refused
from ads_actions.guardrails import Guardrails
from ads_actions.store import MemoryStore
from tests.fake_ads import CAMPAIGN, CUSTOMER, FakeAds

M = 1_000_000
INTEGRATION, AUTOMATION = "201460749979", "201460750019"


@pytest.fixture
def env():
    fake = FakeAds()
    engine = Engine(Guardrails.load(), MemoryStore())
    return engine, Session("ardhofortana@gmail.com", lambda cid: fake), fake


async def propose(env, change_type, **request):
    engine, session, _ = env
    return await engine.propose(session, CUSTOMER, change_type, {"customer_id": CUSTOMER, **request})


async def execute(env, change_id):
    return await env[0].execute(env[1], CUSTOMER, change_id)


async def undo(env, change_id):
    return await env[0].propose_undo(env[1], CUSTOMER, change_id)


def manual(env):
    env[2].state["campaign"].update(bidding_strategy_type="MANUAL_CPC", cpc_bid_ceiling_micros=None)


# (1) values only from the audit log ---------------------------------------------


async def test_1_propose_undo_tool_takes_only_the_change_id():
    from ads_actions import server

    tools = {t.name: t for t in await server.build_server(Engine(Guardrails.load(), MemoryStore())).list_tools()}
    assert set(tools["propose_undo"].parameters["properties"]) == {"customer_id", "change_id"}


async def test_1_undo_request_is_the_before_snapshot(env):
    manual(env)
    r = await propose(env, "set_ad_group_cpc", ad_group_id=INTEGRATION, cpc_idr=140_000)
    await execute(env, r["change_id"])
    u = await undo(env, r["change_id"])
    stored = await env[0].get_change(env[1], CUSTOMER, u["change_id"])
    original = await env[0].get_change(env[1], CUSTOMER, r["change_id"])
    assert stored["request"]["cpc_idr"] * M == original["before"]["cpc_bid_micros"] == 270_000 * M
    assert stored["undo_of"] == r["change_id"]


@pytest.mark.parametrize("flags", [{"restore": True}, {"only_listed_ad_groups": True, "restore": True}, {"undo_of": "chg_XXXX"}])
async def test_1_flags_in_input_do_not_unlock_the_exemption(env, flags):
    manual(env)
    env[2].state["ad_groups"][INTEGRATION]["cpc_bid_micros"] = 140_000 * M
    with pytest.raises(Refused, match=r"\+93% change"):
        await propose(env, "set_ad_group_cpc", ad_group_id=INTEGRATION, cpc_idr=270_000, **flags)
    with pytest.raises(Refused, match="60% change"):
        await propose(env, "set_daily_budget", campaign_id=CAMPAIGN, amount_idr=1_440_000, **flags)
    with pytest.raises(Refused, match="ad_group_default_cpc_idr is required"):
        await propose(env, "set_bidding", campaign_id=CAMPAIGN, strategy="MANUAL_CPC", ad_group_bids={INTEGRATION: 190_000}, **flags)


async def test_1_tampered_undo_values_are_refused_at_execute(env):
    manual(env)
    engine, _, fake = env
    r = await propose(env, "set_ad_group_cpc", ad_group_id=INTEGRATION, cpc_idr=140_000)
    await execute(env, r["change_id"])
    u = await undo(env, r["change_id"])
    # Someone edits the stored undo proposal to restore a different value.
    engine.store.docs[u["change_id"]]["request"]["cpc_idr"] = 300_000
    with pytest.raises(Refused, match="don't match the 'before' snapshot"):
        await execute(env, u["change_id"])
    assert fake.state["ad_groups"][INTEGRATION]["cpc_bid_micros"] == 140_000 * M
    assert engine.store.docs[u["change_id"]]["failure_kind"] == "undo_mismatch"


async def test_1_tampered_before_snapshot_is_refused_too(env):
    """Editing the original's "before" after the undo was proposed doesn't slip through."""
    manual(env)
    engine, _, fake = env
    r = await propose(env, "set_ad_group_cpc", ad_group_id=INTEGRATION, cpc_idr=140_000)
    await execute(env, r["change_id"])
    u = await undo(env, r["change_id"])
    engine.store.docs[r["change_id"]]["before"]["cpc_bid_micros"] = 400_000 * M
    with pytest.raises(Refused, match="don't match the 'before' snapshot"):
        await execute(env, u["change_id"])
    assert fake.state["ad_groups"][INTEGRATION]["cpc_bid_micros"] == 140_000 * M


# (2) live must still equal the "after" of the change being undone ----------------


async def test_2_budget_changed_since(env):
    r = await propose(env, "set_daily_budget", campaign_id=CAMPAIGN, amount_idr=1_000_000)
    await execute(env, r["change_id"])
    env[2].state["campaign"]["budget_amount_micros"] = 950_000 * M  # edited in the UI
    with pytest.raises(Refused, match="no longer match what .* left behind"):
        await undo(env, r["change_id"])
    assert env[2].state["campaign"]["budget_amount_micros"] == 950_000 * M


async def test_2_ad_group_bid_changed_since(env):
    manual(env)
    r = await propose(env, "set_ad_group_cpc", ad_group_id=INTEGRATION, cpc_idr=200_000)
    await execute(env, r["change_id"])
    env[2].state["ad_groups"][INTEGRATION]["cpc_bid_micros"] = 210_000 * M
    with pytest.raises(Refused, match="no longer match"):
        await undo(env, r["change_id"])


async def test_2_manual_cpc_switch_then_one_bid_or_the_strategy_changed(env):
    _, _, fake = env
    r = await propose(env, "set_bidding", campaign_id=CAMPAIGN, strategy="MANUAL_CPC", ad_group_default_cpc_idr=190_000)
    await execute(env, r["change_id"])
    fake.state["ad_groups"][AUTOMATION]["cpc_bid_micros"] = 185_000 * M
    with pytest.raises(Refused, match="no longer match"):
        await undo(env, r["change_id"])
    fake.state["ad_groups"][AUTOMATION]["cpc_bid_micros"] = 190_000 * M
    fake.state["campaign"].update(bidding_strategy_type="TARGET_SPEND", cpc_bid_ceiling_micros=200_000 * M)
    with pytest.raises(Refused, match="no longer match"):
        await undo(env, r["change_id"])
    # Changes the switch didn't touch (a paused ad group's bid) don't block the undo.
    fake.state["campaign"].update(bidding_strategy_type="MANUAL_CPC", cpc_bid_ceiling_micros=None)
    fake.state["ad_groups"]["201460750219"]["cpc_bid_micros"] = 100_000 * M
    await undo(env, r["change_id"])


async def test_2_change_between_undo_proposal_and_execute(env):
    manual(env)
    r = await propose(env, "set_ad_group_cpc", ad_group_id=INTEGRATION, cpc_idr=200_000)
    await execute(env, r["change_id"])
    u = await undo(env, r["change_id"])
    env[2].state["ad_groups"][INTEGRATION]["cpc_bid_micros"] = 210_000 * M
    with pytest.raises(Refused, match="no longer match"):
        await execute(env, u["change_id"])
    assert env[0].store.docs[u["change_id"]]["failure_kind"] == "undo_live_changed"
    assert env[2].state["ad_groups"][INTEGRATION]["cpc_bid_micros"] == 210_000 * M


async def test_2_keyword_bid_and_device_changed_since(env):
    manual(env)
    _, _, fake = env
    crit = next(c for (ag, c) in fake.state["keywords"] if ag == INTEGRATION)
    r = await propose(env, "set_keyword_cpc", items=[{"ad_group_id": INTEGRATION, "criterion_id": crit, "cpc_idr": 300_000}])
    await execute(env, r["change_id"])
    fake.state["keywords"][(INTEGRATION, crit)]["cpc_bid_micros"] = 310_000 * M
    with pytest.raises(Refused, match="no longer match"):
        await undo(env, r["change_id"])

    d = await propose(env, "set_device_bid_adjustment", campaign_id=CAMPAIGN, device="MOBILE", percent=-30)
    await execute(env, d["change_id"])
    fake.state["devices"]["MOBILE"]["bid_modifier"] = 0.8
    with pytest.raises(Refused, match="no longer match"):
        await undo(env, d["change_id"])


async def test_2_negative_removed_by_hand_since(env):
    _, _, fake = env
    r = await propose(env, "add_campaign_negatives", campaign_id=CAMPAIGN, keywords=["free template download"], match_type="PHRASE")
    await execute(env, r["change_id"])
    for k, v in list(fake.state["negatives"].items()):
        if v["text"] == "free template download":
            del fake.state["negatives"][k]
    with pytest.raises(Refused, match="no longer match"):
        await undo(env, r["change_id"])


# (3) ranges still apply to an undo -----------------------------------------------


async def test_3_budget_undo_may_exceed_50_pct_but_not_the_range(env):
    _, _, fake = env
    r = await propose(env, "set_daily_budget", campaign_id=CAMPAIGN, amount_idr=450_000)  # -50%
    await execute(env, r["change_id"])
    u = await undo(env, r["change_id"])  # +100%, allowed for an undo
    await execute(env, u["change_id"])
    assert fake.state["campaign"]["budget_amount_micros"] == 900_000 * M

    fake.state["campaign"]["budget_amount_micros"] = 250_000 * M  # below range, set in the UI
    r = await propose(env, "set_daily_budget", campaign_id=CAMPAIGN, amount_idr=300_000)
    await execute(env, r["change_id"])
    with pytest.raises(Refused, match="outside the allowed range Rp300,000 to Rp1,800,000"):
        await undo(env, r["change_id"])


async def test_3_bid_undo_outside_range_refused(env):
    manual(env)
    env[2].state["ad_groups"][INTEGRATION]["cpc_bid_micros"] = 15_000 * M  # below range, set in the UI
    r = await propose(env, "set_ad_group_cpc", ad_group_id=INTEGRATION, cpc_idr=20_000)
    await execute(env, r["change_id"])
    with pytest.raises(Refused, match="outside the allowed range Rp20,000 to Rp400,000"):
        await undo(env, r["change_id"])
