"""Keeps claude.ai connectors signed in across sessions.

FastMCP's OAuth proxy makes every refresh token single-use: a refresh issues a
new refresh token and deletes the old one at once. claude.ai can hold the same
connector token in more than one session (a chat and a scheduled task, say).
The first session to refresh then invalidates the copy the others hold, and
their next refresh fails with ``invalid_grant``, which claude.ai shows as
"needs-auth". A slow refresh during a cold start has the same effect if the
client gives up and retries with the old token.

``install()`` does two things:

1. Keeps a rotated-out refresh token valid for a reuse window
   (``OAUTH_REFRESH_REUSE_SECONDS``, default 30 days, never past the token's
   own expiry). Each use still returns a fresh token pair. Revocation is
   unchanged.
2. Makes the access token given to claude.ai last longer than Google's 1-hour
   token (``OAUTH_ACCESS_TOKEN_SECONDS``, default 8 hours). The server already
   renews the Google token by itself when a call comes in after it expired, so
   clients only need to refresh a few times a day instead of every hour.

This module only depends on FastMCP, so it can also be loaded into the
read-only google-ads-mcp image through ``sitecustomize``.
"""

import inspect
import logging
import os
import time

logger = logging.getLogger("oauth_hardening")

REUSE_ENV = "OAUTH_REFRESH_REUSE_SECONDS"
DEFAULT_REUSE_SECONDS = 30 * 24 * 3600
ACCESS_ENV = "OAUTH_ACCESS_TOKEN_SECONDS"
DEFAULT_ACCESS_SECONDS = 8 * 3600
_MARK = "_oauth_hardening_installed"


def _env_seconds(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


def reuse_seconds() -> int:
    return _env_seconds(REUSE_ENV, DEFAULT_REUSE_SECONDS)


def access_seconds() -> int:
    return _env_seconds(ACCESS_ENV, DEFAULT_ACCESS_SECONDS)


def install(provider_cls=None) -> bool:
    """Applies both changes. ``provider_cls`` limits them to one class (tests).

    Returns False if FastMCP doesn't have what the refresh patch needs.
    """
    _install_access_lifetime(provider_cls)
    return _install_refresh_reuse(provider_cls)


def _install_access_lifetime(provider_cls=None) -> bool:
    try:
        from fastmcp.server.auth.providers.google import GoogleProvider
    except Exception as e:  # pragma: no cover
        logger.error("oauth_hardening access lifetime not installed: %s", e)
        return False
    cls = provider_cls or GoogleProvider
    init = cls.__init__
    if getattr(init, _MARK, False):
        return True
    if "fastmcp_access_token_expiry_seconds" not in inspect.signature(init).parameters:
        logger.error("oauth_hardening access lifetime not installed: unsupported FastMCP")
        return False

    def __init__(self, *args, **kwargs):
        if kwargs.get("fastmcp_access_token_expiry_seconds") is None and access_seconds():
            kwargs["fastmcp_access_token_expiry_seconds"] = access_seconds()
        init(self, *args, **kwargs)

    setattr(__init__, _MARK, True)
    cls.__init__ = __init__
    return True


def _install_refresh_reuse(proxy_cls=None) -> bool:
    try:
        from fastmcp.server.auth.oauth_proxy import proxy as proxy_mod
    except Exception as e:  # pragma: no cover - only on an incompatible FastMCP
        logger.error("oauth_hardening not installed: %s", e)
        return False
    cls = proxy_cls or proxy_mod.OAuthProxy
    original = cls.exchange_refresh_token
    if getattr(original, _MARK, False):
        return True
    hash_token = getattr(proxy_mod, "_hash_token", None)
    if hash_token is None:
        logger.error("oauth_hardening not installed: FastMCP has no _hash_token")
        return False

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        window = reuse_seconds()
        jti = mapping = metadata = None
        token_hash = hash_token(refresh_token.token)
        if window:
            try:
                payload = self.jwt_issuer.verify_token(refresh_token.token, expected_token_use="refresh")
                jti = payload["jti"]
                mapping = await self._jti_mapping_store.get(key=jti)
                metadata = await self._refresh_token_store.get(key=token_hash)
            except Exception:
                mapping = metadata = None  # let FastMCP produce its usual error

        result = await original(self, client, refresh_token, scopes)

        if mapping is not None and metadata is not None:
            remaining = int(metadata.expires_at - time.time()) if metadata.expires_at else window
            ttl = min(window, remaining)
            if ttl > 0:
                await self._jti_mapping_store.put(key=jti, value=mapping, ttl=ttl)
                await self._refresh_token_store.put(key=token_hash, value=metadata, ttl=ttl)
                logger.info(
                    "Refresh for client=%s: old refresh token stays valid for %ds (token_hash=%s)",
                    getattr(client, "client_id", "?"),
                    ttl,
                    token_hash[:8],
                )
        return result

    setattr(exchange_refresh_token, _MARK, True)
    exchange_refresh_token.__doc__ = original.__doc__
    cls.exchange_refresh_token = exchange_refresh_token
    logger.info(
        "oauth_hardening installed: refresh tokens reusable for %ds after rotation, access tokens last %ds",
        reuse_seconds(),
        access_seconds(),
    )
    return True
