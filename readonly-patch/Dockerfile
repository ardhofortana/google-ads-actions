# The read-only google-ads-mcp image, unchanged, plus the refresh-token reuse
# window. BASE_IMAGE is replaced with the current image digest by
# scripts/deploy-google-ads-mcp-auth-patch.sh.
FROM BASE_IMAGE

COPY oauth_hardening.py sitecustomize.py /opt/oauth-hardening/
ENV PYTHONPATH=/opt/oauth-hardening

# Fail the build unless the patch is active in this image's FastMCP.
RUN python -c "import fastmcp, fastmcp.server.auth.oauth_proxy.proxy as p, fastmcp.server.auth.providers.google as g; m='_oauth_hardening_installed'; assert getattr(p.OAuthProxy.exchange_refresh_token, m, False), 'refresh patch not active'; assert getattr(g.GoogleProvider.__init__, m, False), 'access-lifetime patch not active'; print('fastmcp', fastmcp.__version__, 'patched')"
