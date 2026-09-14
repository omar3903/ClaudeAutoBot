"""Guard for endpoints that read or change secrets or start a broker sign-in.

Only a same-machine request from the dashboard itself gets through. That
refuses a remote client (if the server was started with ``--host 0.0.0.0``), a
foreign ``Host`` header (DNS rebinding), a foreign ``Origin`` (another website
open in your browser), and any request without the dashboard's
``X-ATB-Request`` header - a cross-site request can't add a custom header
without a CORS preflight, which this app never grants.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import HTTPException, Request

LOCAL_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})
ALLOWED_CLIENTS = set(LOCAL_NAMES)     # tests add their fake client here
ALLOWED_HOSTS = set(LOCAL_NAMES)
HEADER = "x-atb-request"


def _host_of(value: str) -> str:
    return (urlsplit("//" + value).hostname or "").lower()


def require_local(request: Request) -> None:
    client = request.client.host if request.client else ""
    if client not in ALLOWED_CLIENTS:
        raise HTTPException(403, "Connection settings can only be changed on this computer.")
    if _host_of(request.headers.get("host", "")) not in ALLOWED_HOSTS:
        raise HTTPException(403, "Unexpected Host header.")
    origin = request.headers.get("origin")
    if origin is not None and (urlsplit(origin).hostname or "").lower() not in ALLOWED_HOSTS:
        raise HTTPException(403, "Cross-site request refused.")
    if request.headers.get(HEADER) != "1":
        raise HTTPException(403, "Missing dashboard request header.")
