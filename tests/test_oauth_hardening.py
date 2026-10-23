"""Refresh-token reuse window, tested against FastMCP's real GoogleProvider.

Only Google's token endpoint is faked. Each test patches its own subclass, so
the unpatched behaviour can be checked in the same run.
"""

import contextlib
import time
from types import SimpleNamespace

import pytest
from fastmcp.server.auth.oauth_proxy.models import JTIMapping, RefreshTokenMetadata, UpstreamTokenSet, _hash_token
from fastmcp.server.auth.providers.google import GoogleProvider
from key_value.aio.stores.memory import MemoryStore
from key_value.aio.wrappers.prefix_collections import PrefixCollectionsWrapper

from ads_actions import oauth_hardening, server

CLIENT = SimpleNamespace(client_id="https://claude.ai/oauth/mcp-oauth-client-metadata")
SCOPES = server.SCOPES


class FakeGoogle:
    calls = 0

    async def refresh_token(self, url, refresh_token, scope=None, **kw):
        assert refresh_token == "google-refresh"  # Google's own refresh token never changes
        FakeGoogle.calls += 1
        return {"access_token": f"google-access-{FakeGoogle.calls}", "expires_in": 3599, "scope": " ".join(SCOPES), "token_type": "Bearer"}


def make_provider(patched: bool):
    cls = type("Provider", (GoogleProvider,), {})
    if patched:
        assert oauth_hardening.install(cls)
    p = cls(
        client_id="cid.apps.googleusercontent.com",
        client_secret="not-a-real-secret",
        base_url="https://example.test",
        required_scopes=SCOPES,
        jwt_signing_key="test-signing-key-test-signing-key",
        client_storage=MemoryStore(),
    )

    @contextlib.asynccontextmanager
    async def fake_client():
        yield FakeGoogle()

    async def no_claims(raw):
        return None

    p._upstream_oauth_client = fake_client
    p._extract_upstream_claims = no_claims
    p.get_routes("/mcp")  # creates the JWT issuer, as at server start
    return p


async def connect(p, lifetime=86400) -> str:
    """Stores what a fresh connect leaves behind and returns the refresh token."""
    now = time.time()
    upstream = UpstreamTokenSet(
        upstream_token_id="up-1", access_token="google-access-0", refresh_token="google-refresh",
        refresh_token_expires_at=None, expires_at=now + 60, token_type="Bearer", scope=" ".join(SCOPES),
        client_id=CLIENT.client_id, created_at=now, raw_token_data={},
    )
    await p._upstream_token_store.put(key="up-1", value=upstream, ttl=lifetime)
    token = p.jwt_issuer.issue_refresh_token(client_id=CLIENT.client_id, scopes=SCOPES, jti="r-1", expires_in=lifetime)
    await p._jti_mapping_store.put(key="r-1", value=JTIMapping(jti="r-1", upstream_token_id="up-1", created_at=now), ttl=lifetime)
    await p._refresh_token_store.put(
        key=_hash_token(token),
        value=RefreshTokenMetadata(client_id=CLIENT.client_id, scopes=SCOPES, expires_at=int(now) + lifetime, created_at=now),
        ttl=lifetime,
    )
    return token


async def refresh(p, token):
    """What the MCP SDK's /token handler does for grant_type=refresh_token."""
    loaded = await p.load_refresh_token(CLIENT, token)
    if loaded is None:
        raise LookupError("invalid_grant: refresh token not found")
    return await p.exchange_refresh_token(CLIENT, loaded, SCOPES)


async def test_unpatched_fastmcp_logs_out_the_second_session():
    p = make_provider(patched=False)
    shared = await connect(p)
    await refresh(p, shared)  # session A refreshes
    with pytest.raises(LookupError):
        await refresh(p, shared)  # session B, same copy: needs-auth


async def test_patched_both_sessions_keep_working():
    p = make_provider(patched=True)
    shared = await connect(p)
    a = await refresh(p, shared)  # session A
    b = await refresh(p, shared)  # session B with the older copy
    assert a.access_token != b.access_token and a.refresh_token != b.refresh_token
    # Each session's new chain keeps working, and so does the old copy.
    await refresh(p, a.refresh_token)
    await refresh(p, b.refresh_token)
    await refresh(p, shared)
    # New access tokens validate against the stored upstream token.
    mapping = await p._jti_mapping_store.get(key=p.jwt_issuer.verify_token(b.access_token)["jti"])
    assert mapping.upstream_token_id == "up-1"


async def test_reuse_window_respects_env_and_token_expiry(monkeypatch):
    monkeypatch.setenv(oauth_hardening.REUSE_ENV, "0")
    p = make_provider(patched=True)
    shared = await connect(p)
    await refresh(p, shared)
    with pytest.raises(LookupError):
        await refresh(p, shared)  # window disabled: FastMCP's default behaviour

    monkeypatch.setenv(oauth_hardening.REUSE_ENV, str(30 * 86400))
    p = make_provider(patched=True)
    shared = await connect(p, lifetime=120)
    await refresh(p, shared)
    _, ttl = await p._client_storage.ttl(key=_hash_token(shared), collection="mcp-refresh-tokens")
    assert ttl is not None and ttl <= 120  # never outlives the token's own expiry


async def test_revoke_still_ends_a_rotated_token():
    p = make_provider(patched=True)
    shared = await connect(p)
    await refresh(p, shared)
    await p.revoke_token(await p.load_refresh_token(CLIENT, shared))
    with pytest.raises(LookupError):
        await refresh(p, shared)


def test_install_is_idempotent():
    cls = type("Provider", (GoogleProvider,), {})
    assert oauth_hardening.install(cls) and oauth_hardening.install(cls)
    assert getattr(cls.exchange_refresh_token, oauth_hardening._MARK)


def test_build_auth_uses_own_collections_and_installs_patch(monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_MCP_OAUTH_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET", "not-a-real-secret")
    monkeypatch.setenv("GOOGLE_ADS_MCP_JWT_SIGNING_KEY", "test-signing-key-test-signing-key")
    monkeypatch.setenv("GOOGLE_ADS_MCP_STORAGE_TYPE", "memory")
    installed = []
    monkeypatch.setattr(oauth_hardening, "install", lambda *a: installed.append(True) or True)
    auth = server.build_auth()
    assert isinstance(auth._client_storage, PrefixCollectionsWrapper)
    assert auth._client_storage.prefix == "gads-actions"
    assert installed


async def test_access_token_outlives_google_token_and_server_renews_google_token():
    p = make_provider(patched=True)
    shared = await connect(p)
    tokens = await refresh(p, shared)
    assert tokens.expires_in == oauth_hardening.DEFAULT_ACCESS_SECONDS  # not Google's 3599

    class Validator:
        async def verify_token(self, token):  # stands in for Google's tokeninfo
            from fastmcp.server.auth.auth import AccessToken

            if token.startswith("google-access-") and token != "google-access-expired":
                return AccessToken(token=token, client_id="cid", scopes=SCOPES, claims={"email": "ardhofortana@gmail.com"})
            return None

    p._token_validator = Validator()
    # Two hours later: the Google token has expired, the claude.ai token has not.
    up = await p._upstream_token_store.get(key="up-1")
    up.access_token, up.expires_at = "google-access-expired", time.time() - 3600
    await p._upstream_token_store.put(key="up-1", value=up, ttl=3600)
    before = FakeGoogle.calls
    validated = await p.load_access_token(tokens.access_token)
    assert validated is not None and FakeGoogle.calls == before + 1
    assert (await p._upstream_token_store.get(key="up-1")).access_token != "google-access-expired"


def test_access_lifetime_env_and_explicit_value(monkeypatch):
    monkeypatch.setenv(oauth_hardening.ACCESS_ENV, "7200")
    p = make_provider(patched=True)
    assert p._fastmcp_access_token_expiry_seconds == 7200
    cls = type("Provider", (GoogleProvider,), {})
    oauth_hardening.install(cls)
    explicit = cls(client_id="c.apps.googleusercontent.com", client_secret="x", base_url="https://e.test",
                   required_scopes=SCOPES, jwt_signing_key="k" * 32, client_storage=MemoryStore(),
                   fastmcp_access_token_expiry_seconds=600)
    assert explicit._fastmcp_access_token_expiry_seconds == 600
    assert make_provider(patched=False)._fastmcp_access_token_expiry_seconds is None
