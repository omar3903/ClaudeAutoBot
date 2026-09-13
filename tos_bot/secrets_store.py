"""Read and update the machine-local ``.env`` from the dashboard.

Only a fixed allow-list of broker settings can be written. Values are validated
(no line breaks, so nothing can smuggle extra lines into the file), the file is
replaced atomically with comments and unrelated lines preserved, and secret
values are never sent back to the browser - only whether they're set and their
last four characters.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

from dotenv import dotenv_values

from .config import ENV_PATH, Secrets


@dataclass(frozen=True)
class EnvField:
    key: str
    label: str
    group: str                      # "schwab" | "ibkr"
    secret: bool = False
    kind: str = "text"              # text | int | bool | choice | url
    choices: Tuple[str, ...] = ()
    help: str = ""
    min: int = 0
    max: int = 0


FIELDS: Tuple[EnvField, ...] = (
    EnvField("SCHWAB_API_KEY", "App key", "schwab", secret=True,
             help="developer.schwab.com → Dashboard → your app → App Key"),
    EnvField("SCHWAB_APP_SECRET", "App secret", "schwab", secret=True,
             help="shown next to the App Key once Schwab approves the app"),
    EnvField("SCHWAB_CALLBACK_URL", "Callback URL", "schwab", kind="url",
             help="must match your Schwab app exactly, e.g. https://127.0.0.1:8182"),
    EnvField("SCHWAB_ACCOUNT_ID", "Account number", "schwab", secret=True,
             help="optional - only if you have more than one Schwab account"),
    EnvField("IBKR_HOST", "Gateway host", "ibkr",
             help="127.0.0.1 unless IB Gateway runs on another computer"),
    EnvField("IBKR_PAPER_PORT", "Paper port", "ibkr", kind="int", min=1, max=65535,
             help="IB Gateway 4002 · TWS 7497"),
    EnvField("IBKR_LIVE_PORT", "Live port", "ibkr", kind="int", min=1, max=65535,
             help="IB Gateway 4001 · TWS 7496"),
    EnvField("IBKR_CLIENT_ID", "Client ID", "ibkr", kind="int", min=0, max=999999,
             help="any number not used by another app on the same Gateway"),
    EnvField("IBKR_ACCOUNT_ID", "Account ID", "ibkr", secret=True,
             help="optional - DU… (paper) / U… (live) if the login has several accounts"),
    EnvField("IBKR_MARKET_DATA", "Market data", "ibkr", kind="choice",
             choices=("auto", "live", "delayed", "delayed-frozen"),
             help="auto = real-time if you're subscribed, otherwise 15-min delayed"),
    EnvField("IBKR_READONLY", "Read-only (data only, never send orders)", "ibkr", kind="bool"),
)
_BY_KEY = {f.key: f for f in FIELDS}

_MAX_LEN = 512
_SAFE_UNQUOTED = re.compile(r"[A-Za-z0-9_\-.:/@+=,]*")
_LINE_KEY = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_MARKER = "# ---- saved from the AutoTradeBot dashboard ----"


def read_env(path: Path = ENV_PATH) -> Dict[str, str]:
    if not path.exists():
        return {}
    return {k: (v or "") for k, v in dotenv_values(path).items()}


def describe(path: Path = ENV_PATH) -> List[Dict[str, Any]]:
    """The editable fields with their current state - never a secret's value."""
    env = read_env(path)
    defaults = Secrets.model_fields
    out = []
    for f in FIELDS:
        value = env.get(f.key, "")
        default = defaults.get(f.key.lower())
        row: Dict[str, Any] = {
            "key": f.key, "label": f.label, "group": f.group, "secret": f.secret,
            "kind": f.kind, "choices": list(f.choices), "help": f.help, "set": bool(value),
            "default": "" if default is None or default.default in (None, "") else str(default.default),
        }
        if f.secret:
            row["hint"] = ("…" + value[-4:]) if len(value) >= 8 else ""
        else:
            row["value"] = value
        out.append(row)
    return out


def validate(updates: Mapping[str, Any]) -> Dict[str, str]:
    """Normalise ``updates``; raise ``ValueError`` with a user-facing message.
    ``None`` means "leave unchanged", ``""`` clears the setting."""
    clean: Dict[str, str] = {}
    for key, raw in (updates or {}).items():
        f = _BY_KEY.get(key)
        if f is None:
            raise ValueError(f"{key} can't be changed from the dashboard")
        if raw is None:
            continue
        v = ("1" if raw else "0") if isinstance(raw, bool) else str(raw).strip()
        if any(c in v for c in "\r\n\x00"):
            raise ValueError(f"{f.label}: line breaks aren't allowed")
        if len(v) > _MAX_LEN:
            raise ValueError(f"{f.label}: too long")
        if v:
            v = _check(f, v)
        clean[key] = v
    return clean


def _check(f: EnvField, v: str) -> str:
    if f.kind == "int":
        try:
            n = int(v)
        except ValueError:
            raise ValueError(f"{f.label}: must be a whole number") from None
        if not f.min <= n <= f.max:
            raise ValueError(f"{f.label}: must be between {f.min} and {f.max}")
        return str(n)
    if f.kind == "bool":
        low = v.lower()
        if low not in ("1", "0", "true", "false", "yes", "no", "on", "off"):
            raise ValueError(f"{f.label}: must be on or off")
        return "1" if low in ("1", "true", "yes", "on") else "0"
    if f.kind == "choice" and v not in f.choices:
        raise ValueError(f"{f.label}: must be one of {', '.join(f.choices)}")
    if f.kind == "url":
        u = urlparse(v)
        if u.scheme != "https" or u.hostname != "127.0.0.1" or not _has_port(u):
            raise ValueError(f"{f.label}: must look like https://127.0.0.1:8182 "
                             "(the sign-in helper only accepts 127.0.0.1 with a port)")
    return v


def _has_port(u) -> bool:
    try:
        return u.port is not None
    except ValueError:
        return False


def write(updates: Mapping[str, Any], path: Path = ENV_PATH) -> List[str]:
    """Validate and apply ``updates``. Returns the keys whose value changed."""
    clean = validate(updates)
    if not clean:
        return []
    before = read_env(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    out: List[str] = []
    done = set()
    for line in lines:
        m = _LINE_KEY.match(line)
        key = m.group(1) if m else None
        if key not in clean:
            out.append(line)
            continue
        if key in done:                       # drop duplicate definitions
            continue
        out.append(f"{key}={_quote(clean[key])}{_trailing_comment(line[m.end():])}")
        done.add(key)

    missing = [k for k in clean if k not in done]
    if missing:
        if _MARKER not in lines:
            if out and out[-1].strip():
                out.append("")
            out.append(_MARKER)
        out += [f"{k}={_quote(clean[k])}" for k in missing]

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    # load_dotenv() already copied the old values into the process environment,
    # and pydantic-settings prefers the environment over the file - keep it in step
    for k, v in clean.items():
        os.environ[k] = v
    return sorted(k for k, v in clean.items() if before.get(k, "") != v)


def _quote(v: str) -> str:
    if _SAFE_UNQUOTED.fullmatch(v):
        return v
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _trailing_comment(rest: str) -> str:
    """Keep a `  # note` that followed the old value (dotenv: whitespace then #)."""
    s, stripped = rest, rest.lstrip()
    if stripped[:1] in ("'", '"'):
        end = stripped.find(stripped[0], 1)
        s = stripped[end + 1:] if end != -1 else ""
    m = re.search(r"\s+#.*$", s)
    return m.group(0) if m else ""


def mask(value: Optional[str]) -> str:
    v = value or ""
    return ("…" + v[-4:]) if len(v) >= 4 else ""
