"""Fix request of 23 Oct: asset tools and the RSA price lock.

Mirrors the request's tests 1-7 on the fake account. The GAQL part of the
asset bug is covered by test_where_fields_of_other_resources_are_selected.
"""

import pytest

from ads_actions.engine import Engine, Session
from ads_actions.errors import Refused
from ads_actions.guardrails import Guardrails
from ads_actions.store import MemoryStore
from tests.fake_ads import CAMPAIGN, CUSTOMER, FakeAds

PRICE_AD = "828606487337"
LIVE_HEADLINES = [
    "AI Integration Services", "Custom AI For Your Stack", "Talk To An Engineer Today",
    "Senior AI Engineers", "Fixed Scope, Clear Timelines", "Connect AI To Your Tools",
    "AI Automation For Teams", "Book A Free Consultation", "Projects From $1,500",
    "US Clients Since 2015", "Secure By Design", "Fast Prototypes In Weeks",
    "Integrations That Last", "From Idea To Production", "Trusted Engineering Partner",
]
CALLOUT_ID = "431824070724"


@pytest.fixture
def env():
    fake = FakeAds()
    fake.state["ads"][PRICE_AD] = {
        "ad_group_id": "201460749979",
        "type": "RESPONSIVE_SEARCH_AD",
        "status": "ENABLED",
        "final_urls": ["https://djavaweb.com/ai-integration/"],
        "path1": "ai",
        "path2": "integration",
        "headlines": [{"text": t, **({"pinned_field": "HEADLINE_1"} if i == 0 else {})} for i, t in enumerate(LIVE_HEADLINES)],
        "descriptions": [{"text": "We connect AI to your existing tools and data."}, {"text": "Projects from $1,500. Senior engineers, clear scope."}],
    }
    fake.state["assets"][CALLOUT_ID] = {"callout_asset": {"callout_text": "Senior Engineers Only"}}
    fake.state["campaign_assets"][(CAMPAIGN, CALLOUT_ID, "CALLOUT")] = {}
    engine = Engine(Guardrails.load(), MemoryStore())
    return engine, Session("ardhofortana@gmail.com", lambda cid: fake), fake


async def propose(env, change_type, **request):
    engine, session, _ = env
    return await engine.propose(session, CUSTOMER, change_type, {"customer_id": CUSTOMER, **request})


async def cancel(env, change_id):
    engine, session, _ = env
    await engine.cancel(session, CUSTOMER, change_id)


async def test_1_add_callout(env):
    r = await propose(env, "add_campaign_assets", campaign_id=CAMPAIGN, type="CALLOUT", items=["Test callout one"])
    assert 'Callout "Test callout one" | not linked | new asset, linked' in r["before_after"]
    await cancel(env, r["change_id"])


async def test_2_add_sitelink(env):
    item = {"link_text": "AI Consultation FAQ", "final_url": "https://djavaweb.com/ai-consultation/#faq"}
    r = await propose(env, "add_campaign_assets", campaign_id=CAMPAIGN, type="SITELINK", items=[item])
    assert "https://djavaweb.com/ai-consultation/#faq" in r["before_after"]
    await cancel(env, r["change_id"])


async def test_3_remove_existing_callout(env):
    r = await propose(env, "remove_campaign_assets", campaign_id=CAMPAIGN, links=[{"asset_id": CALLOUT_ID, "field_type": "CALLOUT"}])
    assert "| linked | unlinked (asset stays in the library) |" in r["before_after"]
    await cancel(env, r["change_id"])


async def test_4_change_one_headline_keeps_existing_price_line(env):
    engine, session, fake = env
    new = list(LIVE_HEADLINES)
    new[3] = "Senior AI Engineers On Call"
    r = await propose(env, "update_rsa", ad_id=PRICE_AD, headlines=new)
    assert "| Headline 9 | Projects From $1,500 | Projects From $1,500 (unchanged, existing) |" in r["before_after"]
    assert "| Headline 4 | Senior AI Engineers | Senior AI Engineers On Call |" in r["before_after"]
    assert "pinned headline 1" in r["before_after"]  # existing pin kept
    await cancel(env, r["change_id"])

    # Executing the same kind of proposal writes the price line back verbatim.
    r = await propose(env, "update_rsa", ad_id=PRICE_AD, headlines=new)
    await engine.execute(session, CUSTOMER, r["change_id"])
    assert [h["text"] for h in fake.state["ads"][PRICE_AD]["headlines"]] == new


async def test_4b_descriptions_with_existing_price_can_be_kept(env):
    r = await propose(env, "update_rsa", ad_id=PRICE_AD, descriptions=["We connect AI to the tools you already use.", "Projects from $1,500. Senior engineers, clear scope."])
    assert "(unchanged, existing)" in r["before_after"]
    await cancel(env, r["change_id"])


@pytest.mark.parametrize(
    "index,text,why",
    [
        (8, "Projects From $2,000", "price"),  # 5: price in a changed line
        (10, "Only $99 Today", "price"),  # 6: price in a new line
        (10, "A" * 31, "31 characters"),  # 7: length rule on a new line
        (8, "projects from $1,500", "price"),  # match is exact: different case counts as changed
        (10, "Fast — Reliable", "em dash"),
    ],
)
async def test_5_to_7_new_or_changed_lines_get_every_rule(env, index, text, why):
    new = list(LIVE_HEADLINES)
    new[index] = text
    with pytest.raises(Refused, match=why):
        await propose(env, "update_rsa", ad_id=PRICE_AD, headlines=new)


async def test_unchanged_copy_with_only_a_pin_change_is_allowed(env):
    r = await propose(env, "update_rsa", ad_id=PRICE_AD, pin_first_headline=False)
    assert "Projects From $1,500 (unchanged, existing)" in r["before_after"]
    await cancel(env, r["change_id"])


async def test_nothing_left_pending(env):
    engine, session, _ = env
    for t in (test_1_add_callout, test_2_add_sitelink, test_3_remove_existing_callout):
        await t(env)
    assert await engine.list_pending(session, CUSTOMER) == []
