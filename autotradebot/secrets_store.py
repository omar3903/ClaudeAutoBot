"""Read and update the machine-local ``.env`` from the dashboard.

Only a fixed allow-list - the IB Gateway settings and the signals' news key - can be written. Values are
validated (no line breaks or other hidden characters, so nothing can smuggle
extra lines into the file, and no ``$``), the file is read without ``${VAR}``
expansion, so one setting can't show another's value,
the file is replaced atomically with comments and unrelated lines preserved,
and secret values are never sent back to the browser - only whether they're
set and their last four characters.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from dotenv import dotenv_values

from .config import ENV_PATH, Secrets


@dataclass(frozen=True)
class EnvField:
    key: str
    label: str
    secret: bool = False
    kind: str = "text"              # text | int | bool | choice
    choices: Tuple[str, ...] = ()
    help: str = ""
    min: int = 0
    max: int = 0


FIELDS: Tuple[EnvField, ...] = (
    EnvField("IBKR_HOST", "Gateway host", help="127.0.0.1 unless IB Gateway runs on another computer"),
    EnvField("IBKR_PAPER_PORT", "Paper port", kind="int", min=1, max=65535, help="IB Gateway 4002 · TWS 7497"),
    EnvField("IBKR_LIVE_PORT", "Live port", kind="int", min=1, max=65535, help="IB Gateway 4001 · TWS 7496"),
    EnvField("IBKR_CLIENT_ID", "Client ID", kind="int", min=0, max=999999,
             help="any number not used by another app on the same Gateway"),
    EnvField("IBKR_ACCOUNT_ID", "Account ID", secret=True,
             help="optional - DU… (paper) / U… (live) if the login has several accounts"),
    EnvField("IBKR_MARKET_DATA", "Market data", kind="choice",
             choices=("auto", "live", "delayed", "delayed-frozen"),
             help="auto = real-time if you're subscribed, otherwise delayed"),
    EnvField("IBKR_READONLY", "Read-only (data only, never send orders)", kind="bool"),
)
#: keys for the signals' news feeds - shown in their own part of the Connections panel
SIGNAL_FIELDS: Tuple[EnvField, ...] = (
    EnvField("FINNHUB_API_KEY", "Finnhub API key", secret=True,
             help="Optional: a free key from finnhub.io adds company news to the signals"),
)
_BY_KEY = {f.key: f for f in FIELDS + SIGNAL_FIELDS}

_MAX_LEN = 512
_SAFE_UNQUOTED = re.compile(r"[A-Za-z0-9_\-.:/@+=,]*")
_LINE_KEY = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_MARKER = "# ---- saved from the AutoTradeBot dashboard ----"


def read_env(path: Path = ENV_PATH) -> Dict[str, str]:
    if not path.exists():
        return {}
    # no ${VAR} expansion: a value written as "${FINNHUB_API_KEY}" in a shown
    # field would otherwise come back to the browser as the secret itself
    return {k: (v or "") for k, v in dotenv_values(path, interpolate=False).items()}


def describe(path: Path = ENV_PATH, fields: Tuple[EnvField, ...] = FIELDS) -> List[Dict[str, Any]]:
    """The editable ``fields`` with their current state - never a secret's value."""
    env = read_env(path)
    defaults = Secrets.model_fields
    out = []
    for f in fields:
        value = env.get(f.key, "")
        default = defaults.get(f.key.lower())
        row: Dict[str, Any] = {
            "key": f.key, "label": f.label, "secret": f.secret, "kind": f.kind,
            "choices": list(f.choices), "help": f.help, "set": bool(value),
            "default": "" if default is None or default.default in (None, "") else str(default.default),
        }
        if f.secret:
            row["hint"] = mask(value) if len(value) >= 8 else ""
        else:
            row["value"] = value
        out.append(row)
    return out


def validate(updates: Mapping[str, Any], path: Path = ENV_PATH) -> Dict[str, str]:
    """Normalise ``updates``; raise ``ValueError`` with a user-facing message.
    ``None`` means "leave unchanged", ``""`` clears the setting. ``path`` is the
    file they'd be saved to, for the checks that read the other settings too."""
    clean: Dict[str, str] = {}
    for key, raw in (updates or {}).items():
        f = _BY_KEY.get(key)
        if f is None:
            raise ValueError(f"{key} can't be changed from the dashboard")
        if raw is None:
            continue
        v = ("1" if raw else "0") if isinstance(raw, bool) else str(raw).strip()
        # any space but a plain one, and any control or invisible format character:
        # besides \r and \n, Python splits lines on \x0b, \x85, U+2028 and others,
        # and a pasted key can carry a zero-width character nobody can see
        if any(c != " " and (c.isspace() or unicodedata.category(c).startswith("C")) for c in v):
            raise ValueError(f"{f.label}: line breaks and hidden characters aren't allowed")
        # python-dotenv expands ${VAR}, and none of these settings ever needs a $
        if "$" in v:
            raise ValueError(f"{f.label}: $ isn't allowed")
        if len(v) > _MAX_LEN:
            raise ValueError(f"{f.label}: too long")
        clean[key] = _check(f, v) if v else v
    _check_ports(clean, path)
    return clean


#: IBKR's live ports - IB Gateway's and TWS's
LIVE_PORTS = (4001, 7496)


def _check_ports(clean: Mapping[str, str], path: Path) -> None:
    """The app tells the paper account from the live one only by the port it dials: the two ports must differ,
    a live port is never the paper one, and IBKR_PORT (one port for both) is only allowed read-only. Checked
    against the values the settings would have after the save, when it changes a port or Read-only."""
    ports, readonly_saved = {"IBKR_PAPER_PORT", "IBKR_LIVE_PORT"} & set(clean), "IBKR_READONLY" in clean
    if not ports and not readonly_saved:
        return
    env = read_env(path)

    def after(key: str) -> str:
        # the value being saved, else the environment (which wins over the file, as in Secrets), else the
        # file; a cleared or unset one is the default
        value = clean[key] if key in clean else (os.environ.get(key) or env.get(key) or "").strip()
        if value:
            return value
        default = Secrets.model_fields[key.lower()].default
        return "" if default is None else str(default)

    def port(key: str) -> Optional[int]:
        try:
            return int(after(key))
        except ValueError:
            return None                        # a hand-written value Secrets refuses by itself

    if ports:
        paper, live = port("IBKR_PAPER_PORT"), port("IBKR_LIVE_PORT")
        if paper in LIVE_PORTS:
            raise ValueError(f"Paper port: {paper} is IBKR's live port (IB Gateway live 4001, TWS live 7496) - "
                             "the paper Gateway listens on 4002 (TWS 7497)")
        if paper is not None and paper == live:
            raise ValueError("Paper port and Live port can't be the same - the app tells your paper and live "
                             "accounts apart by the port it dials")
    readonly = after("IBKR_READONLY").lower() in ("1", "true", "yes", "on", "y", "t")
    if readonly_saved and port("IBKR_PORT") and not readonly:
        raise ValueError("Read-only can't be turned off while IBKR_PORT in .env forces one port for both "
                         "accounts - remove IBKR_PORT and use the Paper and Live ports first")


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
    return v


def write(updates: Mapping[str, Any], path: Path = ENV_PATH) -> List[str]:
    """Validate and apply ``updates``. Returns the keys whose value changed."""
    clean = validate(updates, path)
    if not clean:
        return []
    before = read_env(path)
    # split on \n only, as python-dotenv does (read_text already turns \r\n into \n):
    # splitlines() would also break a quoted value at \x0b, \x85 or U+2028 and
    # write the pieces back as lines of their own
    lines = path.read_text(encoding="utf-8").split("\n") if path.exists() else [""]
    if lines[-1] == "":                       # the final newline, not a blank line
        lines.pop()

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
