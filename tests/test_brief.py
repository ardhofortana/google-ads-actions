"""The section 9 handover tests from the brief, run against the in-memory fake.

The same scenarios are repeated against the live account through the
connector once the service is deployed (README, "Live handover checks").
"""

import datetime as dt

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AccessToken
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from starlette.responses import JSONResponse

from ads_actions import schedule, server
from ads_actions.engine import Engine, Session
from ads_actions.errors import Forbidden, Refused
from ads_actions.guardrails import Guardrails
from ads_actions.store import MemoryStore
from tests.fake_ads import CAMPAIGN, CUSTOMER, FakeAds

ARDHO = "ardhofortana@gmail.com"


class Clock:
    def __init__(self):
        self.now = dt.datetime(2026, 10, 21, 1, 0, tzinfo=dt.timezone.utc)  # 08:00 WIB

    def __call__(self):
        return self.now


@pytest.fixture
def env():
    fake = FakeAds()
    clock = Clock()
    engine = Engine(Guardrails.load(), MemoryStore(), clock=clock)
    session = Session(email=ARDHO, make_gateway=lambda cid: fake if cid == CUSTOMER else FakeAds(cid))
    return engine, session, fake, clock


async def propose(env, change_type, **request):
    engine, session, _, _ = env
    return await engine.propose(session, CUSTOMER, change_type, {"customer_id": CUSTOMER, **request})


# 1 -----------------------------------------------------------------------------


async def test_1_other_user_gets_403_at_http_level():
    g = Guardrails.load()

    async def app(scope, receive, send):
        await JSONResponse({"ok": True})(scope, receive, send)

    def with_user(email):
        token = AccessToken(token="t", client_id="c", scopes=[], claims={"email": email, "email_verified": True})
        guarded = server.AllowlistMiddleware(app, g)

        async def asgi(scope, receive, send):
            scope["user"] = AuthenticatedUser(token)
            await guarded(scope, receive, send)

        return asgi

    async def status_for(asgi):
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": [], "query_string": b""}
        await asgi(scope, receive, send)
        return sent[0]["status"]

    for email, expected in [("someone@example.com", 403), ("ARDHOfortana@gmail.com", 200), (None, 403)]:
        assert await status_for(with_user(email)) == expected


async def test_1_other_user_refused_at_tool_level(env):
    engine, _, fake, _ = env
    mcp = server.build_server(engine, make_gateway=lambda cid, tok: fake)
    async with Client(mcp) as c:  # no token at all
        with pytest.raises(ToolError, match="403"):
            await c.call_tool("list_pending_changes", {"customer_id": CUSTOMER})
    with pytest.raises(Forbidden):
        await engine.list_pending(Session("mallory@example.com", lambda cid: fake), CUSTOMER)
    assert fake.mutate_calls == []


# 2 -----------------------------------------------------------------------------


async def test_2_wrong_customer_or_campaign_refused(env):
    engine, session, _, _ = env
    with pytest.raises(Refused, match="Customer"):
        await engine.propose(session, "1234567890", "set_campaign_status", {"campaign_id": CAMPAIGN, "status": "PAUSED"})
    with pytest.raises(Refused, match="allowlist"):
        await propose(env, "set_campaign_status", campaign_id="99999999", status="PAUSED")
    with pytest.raises(Refused, match="Customer"):
        await engine.list_pending(session, "111-222-3333")


# 3 -----------------------------------------------------------------------------


async def test_3_broad_refused_phrase_exact_pass(env):
    with pytest.raises(Refused, match="Broad"):
        await propose(env, "add_keywords", ad_group_id="201460749979", keywords=[{"text": "ai integration", "match_type": "BROAD"}])
    r = await propose(
        env, "add_keywords", ad_group_id="201460749979",
        keywords=[{"text": "ai integration partner", "match_type": "PHRASE"}, {"text": "ai integration agency", "match_type": "EXACT"}],
    )
    assert r["status"] == "proposed"
    _, _, fake, _ = env
    assert fake.mutate_calls[-1][1] is True  # validate_only


# 4 -----------------------------------------------------------------------------


async def test_4_budget_limits(env):
    with pytest.raises(Refused, match="outside the allowed range"):
        await propose(env, "set_daily_budget", campaign_id=CAMPAIGN, amount_idr=2_000_000)
    with pytest.raises(Refused, match="60% change"):
        await propose(env, "set_daily_budget", campaign_id=CAMPAIGN, amount_idr=1_440_000)
    r = await propose(env, "set_daily_budget", campaign_id=CAMPAIGN, amount_idr=1_000_000)
    assert "Rp900,000" in r["before_after"] and "Rp1,000,000" in r["before_after"]


async def test_4_shared_budget_refused(env):
    env[2].state["campaign"]["budget_reference_count"] = 2
    with pytest.raises(Refused, match="shared"):
        await propose(env, "set_daily_budget", campaign_id=CAMPAIGN, amount_idr=1_000_000)


# 5 -----------------------------------------------------------------------------


async def test_5_negative_that_blocks_active_keyword_warns(env):
    r = await propose(env, "add_campaign_negatives", campaign_id=CAMPAIGN, keywords=["Jobs ", "ai integration"], match_type="PHRASE")
    assert any('"ai integration"' in w and "ai integration services" in w for w in r["warnings"])
    assert '"jobs"' in r["before_after"] and '"ai integration"' not in r["before_after"]


# 6 -----------------------------------------------------------------------------


def test_6_shift_plus_one_pure():
    slots = [schedule.Slot(d, a, b) for d, a, b in __import__("tests.fake_ads", fromlist=["x"]).INITIAL_SCHEDULE]
    assert len(slots) == 10
    out = [(s.day, s.start_hour, s.end_hour) for s in schedule.shift_slots(slots, 1)]
    expected = [("MONDAY", 20, 24)] + [
        s for d in ("TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY") for s in ((d, 0, 8), (d, 20, 24))
    ] + [("SATURDAY", 0, 8)]
    assert out == expected
    assert not any(d == "SUNDAY" for d, _, _ in out)


async def test_6_shift_proposal_shows_every_slot(env):
    r = await propose(env, "shift_ad_schedule", campaign_id=CAMPAIGN, hours=1)
    assert "(10 slot(s) to 10 slot(s))" in r["summary"]
    assert "Monday | 19:00-24:00 | 20:00-24:00" in r["before_after"]
    assert "Tuesday | 00:00-07:00, 19:00-24:00 | 00:00-08:00, 20:00-24:00" in r["before_after"]
    assert "Saturday | 00:00-07:00 | 00:00-08:00" in r["before_after"]
    assert "Sunday" not in r["before_after"]


def test_6_shift_wraps_week_and_back():
    slots = [schedule.Slot("SUNDAY", 22, 24), schedule.Slot("MONDAY", 0, 2)]
    out = schedule.shift_slots(slots, 3)
    assert [(s.day, s.start_hour, s.end_hour) for s in out] == [("MONDAY", 1, 5)]
    back = schedule.shift_slots(out, -3)
    assert [(s.day, s.start_hour, s.end_hour) for s in back] == [("MONDAY", 0, 2), ("SUNDAY", 22, 24)]


# 7 -----------------------------------------------------------------------------


async def test_7_rsa_text_rules(env):
    ok = ["AI Integration Services", "Custom AI For Your Stack", "Talk To An Engineer Today"]
    with pytest.raises(Refused, match="31 characters"):
        await propose(env, "update_rsa", ad_id="700000000001", headlines=ok + ["A" * 31])
    with pytest.raises(Refused, match="em dash"):
        await propose(env, "update_rsa", ad_id="700000000001", headlines=ok + ["Fast — Reliable"])
    for bad, why in [("Book A Call Today!", "exclamation"), ("Built With ChatGPT", "ChatGPT"), ("From Rp5.000.000", "price"), ("Only $499", "price")]:
        with pytest.raises(Refused, match=why):
            await propose(env, "update_rsa", ad_id="700000000001", headlines=ok + [bad])
    r = await propose(env, "update_rsa", ad_id="700000000001", headlines=ok + ["Senior AI Engineers"])
    assert "pinned headline 1" in r["before_after"]  # existing pin carried over


# 8 -----------------------------------------------------------------------------


async def test_8_execute_refuses_expired_cancelled_executed(env):
    engine, session, fake, clock = env
    a = await propose(env, "set_ad_group_status", ad_group_id="201460750219", status="ENABLED")
    clock.now += dt.timedelta(hours=24, minutes=1)
    with pytest.raises(Refused, match="expired"):
        await engine.execute(session, CUSTOMER, a["change_id"])

    b = await propose(env, "set_ad_group_status", ad_group_id="201460750219", status="ENABLED")
    await engine.cancel(session, CUSTOMER, b["change_id"])
    with pytest.raises(Refused, match="cancelled"):
        await engine.execute(session, CUSTOMER, b["change_id"])

    c = await propose(env, "set_ad_group_status", ad_group_id="201460750219", status="ENABLED")
    await engine.execute(session, CUSTOMER, c["change_id"])
    with pytest.raises(Refused, match="executed"):
        await engine.execute(session, CUSTOMER, c["change_id"])
    with pytest.raises(Refused, match="not found"):
        await engine.execute(session, CUSTOMER, "chg_ZZZZ")
    assert [vo for _, vo in fake.mutate_calls].count(False) == 1


# 9 -----------------------------------------------------------------------------


async def test_9_drift_refused(env):
    engine, session, fake, _ = env
    r = await propose(env, "set_daily_budget", campaign_id=CAMPAIGN, amount_idr=1_000_000)
    fake.state["campaign"]["budget_amount_micros"] = 950_000 * 1_000_000  # someone edits in the UI
    with pytest.raises(Refused, match="no longer match"):
        await engine.execute(session, CUSTOMER, r["change_id"])
    assert fake.state["campaign"]["budget_amount_micros"] == 950_000 * 1_000_000
    assert (await engine.get_change(session, CUSTOMER, r["change_id"]))["status"] == "failed"


async def test_9_guardrail_recheck_on_execute(env):
    engine, session, fake, _ = env
    r = await propose(env, "add_campaign_negatives", campaign_id=CAMPAIGN, keywords=["consulting jobs"], match_type="PHRASE")
    # A keyword added after the proposal would now be blocked by the negative.
    fake.state["keywords"][("201460750059", "123")] = {"text": "ai consulting jobs board", "match_type": "PHRASE", "status": "ENABLED"}
    with pytest.raises(Refused, match="Refused on re-check"):
        await engine.execute(session, CUSTOMER, r["change_id"])


# 10 ----------------------------------------------------------------------------


async def test_10_emergency_pause_refused_on_normal_spend(env):
    engine, session, fake, _ = env
    with pytest.raises(Refused, match="Emergency pause refused"):
        await engine.emergency_pause(session, CUSTOMER, CAMPAIGN, "testing")
    assert fake.state["campaign"]["status"] == "ENABLED"
    assert not any(vo is False for _, vo in fake.mutate_calls)


async def test_10_emergency_pause_allowed_on_runaway_spend_and_invalid_traffic(env):
    engine, session, fake, clock = env
    fake.state["metrics_today"]["cost_micros"] = 1_900_000 * 1_000_000
    r = await engine.emergency_pause(session, CUSTOMER, CAMPAIGN, "cost spike")
    assert r["status"] == "emergency_executed" and fake.state["campaign"]["status"] == "PAUSED"
    log = await engine.change_log(session, CUSTOMER)
    assert log[0]["reason"] == "cost spike" and log[0]["evidence"]["over_budget"]

    # Re-enable through undo, then trip the invalid-traffic rule (08:00 WIB, window = 07:00 and 08:00).
    u = await engine.propose_undo(session, CUSTOMER, r["change_id"])
    await engine.execute(session, CUSTOMER, u["change_id"])
    fake.state["metrics_today"]["cost_micros"] = 0
    fake.state["hourly"] = [
        {"date": "2026-10-21", "hour": 7, "clicks": 25, "impressions": 100, "conversions": 0.0},
        {"date": "2026-10-21", "hour": 8, "clicks": 20, "impressions": 80, "conversions": 0.0},
        {"date": "2026-10-21", "hour": 5, "clicks": 500, "impressions": 500, "conversions": 0.0},  # outside window
    ]
    r2 = await engine.emergency_pause(session, CUSTOMER, CAMPAIGN, "click flood")
    assert r2["evidence"]["window_clicks"] == 45 and r2["evidence"]["invalid_traffic"]


# 11 ----------------------------------------------------------------------------


async def test_11_execute_then_undo_negative(env):
    engine, session, fake, clock = env
    p = await propose(env, "add_campaign_negatives", campaign_id=CAMPAIGN, keywords=["free template download"], match_type="PHRASE")
    e = await engine.execute(session, CUSTOMER, p["change_id"])
    added = e["after"]["added"]
    assert [a["text"] for a in added] == ["free template download"]
    assert any(n["text"] == "free template download" for n in fake.state["negatives"].values())

    clock.now += dt.timedelta(minutes=5)
    u = await engine.propose_undo(session, CUSTOMER, p["change_id"])
    assert u["undo_of"] == p["change_id"]
    await engine.execute(session, CUSTOMER, u["change_id"])
    assert not any(n["text"] == "free template download" for n in fake.state["negatives"].values())

    log = await engine.change_log(session, CUSTOMER)
    assert [l["change_id"] for l in log] == [u["change_id"], p["change_id"]]
    assert log[1]["undone_by"] == u["change_id"]
    with pytest.raises(Refused, match="already undone"):
        await engine.propose_undo(session, CUSTOMER, p["change_id"])


async def test_mcp_tools_end_to_end(env, monkeypatch):
    """Same flow as test 11, through the MCP tool layer."""
    engine, _, fake, _ = env
    token = AccessToken(token="google-token", client_id="c", scopes=[], claims={"email": ARDHO, "email_verified": True})
    monkeypatch.setattr(server, "get_access_token", lambda: token)
    seen = []
    mcp = server.build_server(engine, make_gateway=lambda cid, tok: seen.append(tok) or fake)
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
        assert {"execute_change", "propose_shift_ad_schedule", "emergency_pause_campaign"} <= names
        assert not any("create_campaign" in n or "broad" in n for n in names)
        p = (await c.call_tool("propose_add_campaign_negatives", {"customer_id": CUSTOMER, "campaign_id": CAMPAIGN, "keywords": ["free template download"], "match_type": "PHRASE"})).data
        pending = (await c.call_tool("list_pending_changes", {"customer_id": CUSTOMER})).structured_content
        assert pending["result"][0]["change_id"] == p["change_id"]
        await c.call_tool("execute_change", {"customer_id": CUSTOMER, "change_id": p["change_id"]})
        with pytest.raises(ToolError, match="executed"):
            await c.call_tool("execute_change", {"customer_id": CUSTOMER, "change_id": p["change_id"]})
        with pytest.raises(ToolError, match="Broad"):
            await c.call_tool("propose_add_keywords", {"customer_id": CUSTOMER, "ad_group_id": "201460749979", "keywords": [{"text": "x", "match_type": "BROAD"}]})
    assert set(seen) == {"google-token"}


async def test_1_403_through_full_http_app(env):
    """The real Starlette app, with auth in front and the allowlist after it."""
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
    from starlette.middleware import Middleware

    engine, _, fake, _ = env
    verifier = StaticTokenVerifier(
        tokens={
            "ardho": {"client_id": "c", "scopes": [], "email": ARDHO, "email_verified": True},
            "other": {"client_id": "c", "scopes": [], "email": "someone@example.com", "email_verified": True},
        }
    )
    mcp = server.build_server(engine, auth=verifier, make_gateway=lambda cid, tok: fake)
    app = mcp.http_app(middleware=[Middleware(server.AllowlistMiddleware, guardrails=engine.g)])
    body = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}'

    async def status(token):
        sent = []
        headers = [(b"content-type", b"application/json"), (b"accept", b"application/json, text/event-stream"), (b"host", b"localhost")]
        if token:
            headers.append((b"authorization", f"Bearer {token}".encode()))
        scope = {"type": "http", "method": "POST", "path": "/mcp", "raw_path": b"/mcp", "headers": headers, "query_string": b"", "scheme": "http", "server": ("localhost", 80), "client": ("127.0.0.1", 1), "http_version": "1.1", "root_path": ""}
        msgs = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            return msgs.pop(0) if msgs else {"type": "http.disconnect"}

        async def send(m):
            sent.append(m)

        async with app.router.lifespan_context(app):
            await app(scope, receive, send)
        return next(m["status"] for m in sent if m["type"] == "http.response.start")

    assert await status(None) == 401
    assert await status("other") == 403
    assert await status("ardho") == 200


# Follow-ups from the live handover report ----------------------------------


async def test_A_mixed_valid_and_blocked_negatives_still_proposes_valid_one(env):
    r = await propose(env, "add_campaign_negatives", campaign_id=CAMPAIGN, keywords=["new harmless term", "ai integration"], match_type="PHRASE")
    assert r["status"] == "proposed"
    assert '"new harmless term"' in r["before_after"] and '"ai integration"' not in r["before_after"]
    assert any("ai integration services" in w for w in r["warnings"])


async def test_B_same_text_other_match_type_counts_as_drift(env):
    engine, session, fake, _ = env
    r = await propose(env, "add_campaign_negatives", campaign_id=CAMPAIGN, keywords=["drift check one", "drift check two"], match_type="PHRASE")
    fake.state["negatives"]["555"] = {"text": "drift check one", "match_type": "BROAD"}  # added by hand, different match type
    with pytest.raises(Refused, match="no longer match"):
        await engine.execute(session, CUSTOMER, r["change_id"])
    assert not any(n["text"] == "drift check two" for n in fake.state["negatives"].values())


async def test_B_same_text_other_match_type_warns_at_propose(env):
    env[2].state["negatives"]["556"] = {"text": "cheap ai", "match_type": "BROAD"}
    r = await propose(env, "add_campaign_negatives", campaign_id=CAMPAIGN, keywords=["cheap ai"], match_type="PHRASE")
    assert any("already exists as a BROAD negative" in w for w in r["warnings"])


async def test_C_refused_execution_is_logged_with_reason_and_live_values(env):
    engine, session, fake, clock = env
    r = await propose(env, "set_daily_budget", campaign_id=CAMPAIGN, amount_idr=1_000_000)
    fake.state["campaign"]["budget_amount_micros"] = 950_000 * 1_000_000
    clock.now += dt.timedelta(minutes=1)
    with pytest.raises(Refused, match="no longer match"):
        await engine.execute(session, CUSTOMER, r["change_id"])

    doc = await engine.get_change(session, CUSTOMER, r["change_id"])
    assert doc["status"] == "failed" and doc["failure_kind"] == "drift"
    assert doc["live_before"]["amount_micros"] == 950_000 * 1_000_000
    assert doc["failed_at"].endswith("Z") and doc["failed_by"] == ARDHO

    assert await engine.change_log(session, CUSTOMER) == []
    (entry,) = await engine.change_log(session, CUSTOMER, include_failed=True)
    assert entry["change_id"] == r["change_id"] and entry["failure_kind"] == "drift"
    assert entry["live_before"]["amount_micros"] == 950_000 * 1_000_000
    assert (await engine.list_pending(session, CUSTOMER)) == []
