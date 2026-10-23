"""Runs at interpreter start (via PYTHONPATH) in the patched google-ads-mcp image.

Adds the refresh-token reuse window from oauth_hardening.py to FastMCP. It never
stops the server from starting: on any problem it reports and does nothing.
"""

import sys

try:
    import oauth_hardening

    if not oauth_hardening.install():
        print("oauth_hardening: not installed (incompatible FastMCP)", file=sys.stderr)
except Exception as e:  # pragma: no cover
    print(f"oauth_hardening: not installed: {e}", file=sys.stderr)
