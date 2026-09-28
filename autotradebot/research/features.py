"""What a play looked like when it was offered - the features a model can learn from.

One flat dict per play with the same keys whether the play came from a scan (a trade the app
took, or a play it showed and didn't take) or from the replay, so the three populations line up
in one table (research/dataset.py). Every key is always present; a reading the app didn't have
is None. ``FEATURE_SCHEMA`` goes up whenever a key is added or changes meaning, so rows written
by older versions can be told apart - the app can change elsewhere without disturbing this.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, Iterable, Mapping, Optional

from ..util import clock

FEATURE_SCHEMA = 1

#: every feature, in the order they are written
FEATURE_KEYS = (
    "schema",
    # the setup
    "strategy", "kind", "timeframe", "side", "sector",
    "confidence", "probability", "reward_risk", "score", "expected_r", "evidence_weight",
    "stop_pct", "target_pct", "n_targets",
    "noise", "n_noise", "confirmations", "tags", "has_catalyst",
    # when
    "minutes_since_open", "time_of_day", "weekday",
    # the market and the stock's activity
    "p_turbulent", "regime",
    "rvol", "gap_pct", "atr_pct", "change_pct", "range_atr", "heat", "dollar_volume", "move_atr", "extreme",
    # the quantitative readings (strategies/base.py, quant/)
    "vol", "recent_vol", "vol_ratio", "vol_model",
    "price_character", "hurst", "variance_ratio_z", "half_life_bars",
    "market_z", "market_beta", "market_move_pct", "news_since_move",
    "sessions_to_earnings", "signal_nudge",
    # only known for a trade the app took
    "replay_expectancy_r", "replay_trades", "held_out_r", "risk_pct", "unproven", "by",
)

#: the activity metrics kept on a play (scanner/heat.py DailyMetrics / IntradayMetrics)
ACTIVITY_KEYS = ("rvol", "gap_pct", "atr_pct", "change_pct", "range_atr", "heat", "dollar_volume", "move_atr",
                 "extreme")


def play_features(play: Any, *, now: Optional[dt.datetime] = None, market: Optional[Mapping[str, Any]] = None,
                  noise: Optional[Iterable[str]] = None, confirmations: Optional[int] = None,
                  activity: Any = None, at_entry: Optional[Mapping[str, Any]] = None,
                  by: str = "") -> Dict[str, Any]:
    """``play``: a Play, or a play row from the database (persistence play_to_dict). ``now``: when it
    was seen (a row's created_at when not given). ``market`` / ``activity``: the regime and the
    stock's activity at that moment; the play's evidence is read when they aren't given.
    ``noise`` / ``confirmations`` replace the play's own (the replay flags a play separately).
    ``at_entry``: the engine's entry context for a trade the app took."""
    ev = dict(_get(play, "evidence") or {})
    at = _when(now, _get(play, "created_at"))
    flags = list(noise if noise is not None else (_get(play, "noise") or []))
    tags = list(_get(play, "tags") or [])
    entry, stop = _num(_get(play, "entry")), _num(_get(play, "stop"))
    targets = [t for t in (_get(play, "targets") or []) if _num(t) is not None]
    act = _activity(activity, ev)
    regime = dict(market or ev.get("market_regime") or {})
    vol = dict(ev.get("vol_forecast") or {})
    character = dict(ev.get("price_character") or {})
    move = dict(ev.get("market_move") or {})
    earnings = dict(ev.get("next_earnings") or {})
    entry_ctx = dict(at_entry or ev.get("at_entry") or {})
    record = dict(entry_ctx.get("replay_record") or {})
    out: Dict[str, Any] = {
        "schema": FEATURE_SCHEMA,
        "strategy": _get(play, "strategy"), "kind": _enum(_get(play, "kind")),
        "timeframe": _enum(_get(play, "timeframe")), "side": _enum(_get(play, "side")),
        "sector": _get(play, "sector") or None,
        "confidence": _num(_get(play, "confidence")), "probability": _num(_get(play, "probability")),
        "reward_risk": _num(_get(play, "reward_risk")), "score": _num(_get(play, "score")),
        "expected_r": _num(ev.get("expected_r")), "evidence_weight": _num(ev.get("evidence_weight"), 1.0),
        "stop_pct": _pct(entry, stop), "target_pct": _pct(entry, _num(targets[0]) if targets else None),
        "n_targets": len(targets),
        "noise": flags, "n_noise": len(flags),
        "confirmations": int(confirmations if confirmations is not None else (_get(play, "confirmations") or 1)),
        "tags": tags, "has_catalyst": any(t in ("catalyst", "gap") for t in tags),
        "minutes_since_open": _minutes_since_open(at), "time_of_day": _time_of_day(at),
        "weekday": at.weekday() if at else None,
        "p_turbulent": _num(regime.get("p_turbulent")), "regime": regime.get("regime"),
        **{k: _num(act.get(k)) for k in ACTIVITY_KEYS},
        "vol": _num(vol.get("vol")), "recent_vol": _num(vol.get("recent_vol")), "vol_ratio": _num(vol.get("ratio")),
        "vol_model": vol.get("model"),
        "price_character": character.get("character"), "hurst": _num(character.get("hurst")),
        "variance_ratio_z": _num(character.get("variance_ratio_z")),
        "half_life_bars": _num(character.get("half_life_bars")),
        "market_z": _num(move.get("z")), "market_beta": _num(move.get("beta")),
        "market_move_pct": _num(move.get("move_pct")), "news_since_move": _num(move.get("news")),
        "sessions_to_earnings": _num(earnings.get("sessions")), "signal_nudge": _num(ev.get("signal_nudge")),
        "replay_expectancy_r": _num(record.get("expectancy_r")), "replay_trades": _num(record.get("trades")),
        "held_out_r": _num((record.get("out_of_sample") or {}).get("expectancy_r")),
        "risk_pct": _num(entry_ctx.get("risk_pct")),
        "unproven": bool(entry_ctx["unproven"]) if "unproven" in entry_ctx else None,
        "by": by or entry_ctx.get("by") or None,
    }
    return {k: out.get(k) for k in FEATURE_KEYS}


# ---------------------------------------------------------------- helpers
def _get(play: Any, name: str) -> Any:
    if isinstance(play, Mapping):
        return play.get(name)
    return getattr(play, name, None)


def _enum(value: Any) -> Optional[str]:
    if value is None:
        return None
    return getattr(value, "value", value)


def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return default if value is None else float(value)
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if f == f else default            # NaN reads as unknown


def _pct(entry: Optional[float], other: Optional[float]) -> Optional[float]:
    if not entry or other is None:
        return None
    return round(abs(other - entry) / entry * 100.0, 4)


def _activity(activity: Any, evidence: Mapping[str, Any]) -> Dict[str, Any]:
    if activity is None:
        activity = evidence.get("activity")
    if activity is None:
        return {}
    if is_dataclass(activity) and not isinstance(activity, type):
        return asdict(activity)
    if isinstance(activity, Mapping):
        return dict(activity)
    return {k: getattr(activity, k, None) for k in ACTIVITY_KEYS}


def _when(now: Optional[dt.datetime], created_at: Any) -> Optional[dt.datetime]:
    stamp = now if now is not None else created_at
    if stamp is None:
        return None
    if isinstance(stamp, str):
        try:
            stamp = dt.datetime.fromisoformat(stamp)
        except ValueError:
            return None
    elif hasattr(stamp, "to_pydatetime"):
        stamp = stamp.to_pydatetime()
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)     # the database keeps naive UTC
    return stamp.astimezone(clock.NY)


def _minutes_since_open(at: Optional[dt.datetime]) -> Optional[float]:
    if at is None:
        return None
    return round(clock.minutes_since_open(at), 1)


def _time_of_day(at: Optional[dt.datetime]) -> Optional[str]:
    if at is None:
        return None
    try:
        return clock.time_of_day(at)
    except Exception:  # noqa: BLE001
        return None


def activity_summary(activity: Any) -> Dict[str, Any]:
    """The activity metrics worth keeping on a play, for the features later on."""
    data = _activity(activity, {})
    return {k: data[k] for k in ACTIVITY_KEYS if data.get(k) is not None}
