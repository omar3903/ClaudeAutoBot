"""The same-machine guard: only the dashboard, on this computer, drives the app.

:func:`refusal` runs on every HTTP request (the app's middleware asks it before
routing) and on the live feed's WebSocket before it is accepted. It refuses a
remote client, a foreign ``Host`` header (DNS rebinding), a foreign ``Origin``
(another website open in your browser), an ``/api/`` request another site's
page made (an image or script load needs no CORS), and any request that
changes something without the dashboard's ``X-ATB-Request`` header - a
cross-site request can't add a custom header without a CORS preflight, which
this app never grants.

:func:`require_local` makes the same checks, header included even on a read,
for the endpoints that touch secrets, exit every position, fix a share count or
quit the app.
"""

from __future__ import annotations

from typing import Mapping, Optional
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

LOCAL_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})
ALLOWED_CLIENTS = set(LOCAL_NAMES)     # tests add their fake client here
ALLOWED_HOSTS = set(LOCAL_NAMES)
HEADER = "x-atb-request"
#: methods that only read - the dashboard's plain GETs don't send the header
READS = frozenset({"GET", "HEAD"})


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


def refusal(client: str, headers: Mapping[str, str], method: str, path: str) -> Optional[str]:
    """Why this request is refused, or None to let it through. ``headers`` is the request's own
    (names are looked up in lower case). The allow-lists are read when it's called, so a test that
    adds its fake client and host is honoured."""
    if client not in ALLOWED_CLIENTS:
        return "The dashboard only answers this computer."
    if _host_of(headers.get("host", "")) not in ALLOWED_HOSTS:
        return "Unexpected Host header."
    origin = headers.get("origin")
    if origin is not None and (urlsplit(origin).hostname or "").lower() not in ALLOWED_HOSTS:
        return "Cross-site request refused."
    # another site's <img> or <script> can still load an API answer (and make a broker request);
    # a link from another site to the dashboard itself still opens it, so only /api/ is refused
    if path.startswith("/api/") and headers.get("sec-fetch-site") == "cross-site":
        return "Cross-site request refused."
    # a web page can send a plain form POST here without asking; it can't add this header
    if method not in READS and headers.get(HEADER) != "1":
        return "Missing dashboard request header."
    return None
