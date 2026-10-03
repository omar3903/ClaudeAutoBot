"""The same-machine guard: only the dashboard, on this computer, drives the app.

:func:`refusal` runs on every HTTP request (the app's middleware asks it before
routing) and on the live feed's WebSocket before it is accepted. It refuses a
remote client, a foreign ``Host`` header (DNS rebinding), an ``Origin`` that
isn't the dashboard's own - scheme, host and port: another website, or a page
another program serves on this computer (a dev server, a notebook) - an
``/api/`` or live-feed request another page made (an image or script load needs
no CORS), and any request that changes something without the dashboard's
``X-ATB-Request`` header - a cross-site request can't add a custom header
without a CORS preflight, which this app never grants.

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
#: what the browser says about who made a request: typed in or bookmarked ("none"), or the dashboard's
#: own page. "same-site" is refused too - a page on another port of this computer is the same site
FETCH_SITES = frozenset({"same-origin", "none"})


def _host_of(value: str) -> str:
    return (urlsplit("//" + value).hostname or "").lower()


def _foreign_origin(origin: str, host: str) -> bool:
    """Whether an ``Origin`` isn't this request's own: plain http, the ``Host`` header's port, and its
    host name or another name for this computer (a tab on localhost:8787 talking to 127.0.0.1:8787).
    A page on another port is another program's - it could read the live feed and the account. "null"
    (a file, a sandboxed frame) and every other scheme are foreign."""
    try:
        theirs, ours = urlsplit(origin), urlsplit("//" + host)
        their_port, our_port = theirs.port, ours.port
    except ValueError:                                                    # a port that isn't a number
        return True
    if theirs.scheme != "http" or "@" in theirs.netloc or theirs.path or theirs.query or theirs.fragment:
        return True
    if (80 if their_port is None else their_port) != (80 if our_port is None else our_port):
        return True
    name, own = (theirs.hostname or ""), (ours.hostname or "")
    return not (name == own or (name in LOCAL_NAMES and own in LOCAL_NAMES))


def require_local(request: Request) -> None:
    client = request.client.host if request.client else ""
    if client not in ALLOWED_CLIENTS:
        raise HTTPException(403, "Connection settings can only be changed on this computer.")
    if _host_of(request.headers.get("host", "")) not in ALLOWED_HOSTS:
        raise HTTPException(403, "Unexpected Host header.")
    origin = request.headers.get("origin")
    if origin is not None and _foreign_origin(origin, request.headers.get("host", "")):
        raise HTTPException(403, "Cross-site request refused.")
    if request.headers.get("sec-fetch-site", "none") not in FETCH_SITES:
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
    if origin is not None and _foreign_origin(origin, headers.get("host", "")):
        return "Cross-site request refused."
    # another page's <img> or <script> sends no Origin and can still load an API answer (and make a
    # broker request); a link from another site to the dashboard itself still opens it, so only the
    # API and the live feed are held to it. A script sends no such header, and is let through as before
    api = path.startswith("/api/") or path == "/ws"
    if api and headers.get("sec-fetch-site", "none") not in FETCH_SITES:
        return "Cross-site request refused."
    # a web page can send a plain form POST here without asking; it can't add this header
    if method not in READS and headers.get(HEADER) != "1":
        return "Missing dashboard request header."
    return None
