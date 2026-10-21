"""Exceptions raised by the change engine.

Every refusal is a ``Refused`` so the MCP layer can return its message to the
caller unchanged. ``Forbidden`` is the 403 for users outside the allowlist.
"""


class Refused(Exception):
    """The request breaks a guardrail or cannot be carried out safely."""


class Forbidden(Refused):
    """The signed-in user is not allowed to use this server."""

    def __init__(self, message: str = "403 Forbidden: this user may not use this server."):
        super().__init__(message)


class AdsApiError(Refused):
    """Google Ads rejected a request (validate_only or real)."""

    def __init__(self, messages: list[str], request_id: str | None = None):
        self.messages = messages
        self.request_id = request_id
        detail = "; ".join(messages) or "unknown error"
        suffix = f" (request id {request_id})" if request_id else ""
        super().__init__(f"Google Ads rejected the request: {detail}{suffix}")
