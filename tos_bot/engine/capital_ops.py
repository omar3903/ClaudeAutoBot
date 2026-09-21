"""Trading capital: the part of the account the bot may use, its day / swing split, and the account
sizing sees.

A mixin of TradingEngine (engine.py): it works on the engine's own attributes and is kept apart
only so each concern reads on its own. Nothing here is instantiated by itself.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Optional

from ..brokers.venues import venue_label
from ..core.models import Account, Play
from ..risk.position_sizing import size_play
from . import capital
from .support import _ACTED_ON


log = logging.getLogger(__name__)

class CapitalOps:
    # ------------------------------------------------------------------ #
    #  Trading capital                                                   #
    # ------------------------------------------------------------------ #
    def _invested_usd(self) -> float:
        return capital.invested_usd(self._account, self._positions_here())

    def _held_by_kind(self) -> Dict[str, float]:
        """Dollars each kind of trade holds on this account: its open positions at the broker's marks and
        its entry orders still working. On IBKR every entry works for a moment before it is booked, and a
        swing limit can rest all day - leave those out and entries sent close together each see the same
        room and together overrun the kind's share."""
        held = capital.invested_by_kind(self._account, self._positions_here())
        for w in self.working_entries():
            held[capital.kind_of(w.get("timeframe"))] += float(w.get("notional") or 0.0)
        return held

    def sizing_account(self, timeframe: Any = None) -> Optional[Account]:
        """The account as position sizing sees it - see capital.py. With a ``timeframe`` (a day trade or a
        swing trade), only that kind's share of the trading capital is open to it."""
        acc, limit = self._account, self.capital.get(self._venue)
        share = 1.0 if timeframe is None else capital.share_of(capital.kind_of(timeframe), self.effective_day_pct())
        if acc is None or (not limit and share >= 1.0):
            return acc
        held = self._held_by_kind()
        return capital.sizing_account(acc, limit, sum(held.values()), share=share,
                                      invested_in_kind=held[capital.kind_of(timeframe)] if timeframe is not None else 0.0)

    def capital_state(self) -> Optional[Dict[str, Any]]:
        if self._account is None:
            return None
        held = self._held_by_kind()
        state = capital.state(self._account, self._venue, venue_label(self._venue), self.capital.get(self._venue),
                              sum(held.values()), self.effective_day_pct(), held)
        state["split"].update(on=self._both_kinds(), set_pct=self.day_trade_pct)
        return state

    def _both_kinds(self) -> bool:
        return {"INTRADAY", "SWING"} <= set(self.filters.timeframes)

    def effective_day_pct(self) -> float:
        """The day-trade share in force: the split while the filters allow day trades and swing trades
        both, otherwise all of the trading capital for the one kind that's switched on."""
        if self._both_kinds():
            return self.day_trade_pct
        return 100.0 if "INTRADAY" in self.filters.timeframes else 0.0

    def set_capital_split(self, day_pct: Any) -> Dict[str, Any]:
        """The part of the trading capital day trades may hold at once, in percent; swing trades get the rest."""
        locked = self._locked()
        if locked:
            return {"ok": False, "reason": locked}
        try:
            value = capital.parse_day_pct(day_pct)
        except ValueError as e:
            return {"ok": False, "reason": str(e)}
        self.day_trade_pct = value
        self._save_runtime()
        log.info("day/swing split set to %g / %g", value, 100 - value)
        self._settings_changed()
        state = self.capital_state()
        self._publish("capital.updated", capital=state)
        note = f"Day trades may now hold up to {value:g}% of the trading capital at once, swing trades {100 - value:g}%."
        if not self._both_kinds():
            note += " It applies while Intraday and Swing are both switched on."
        note += self._over_share_note(state)
        return {"ok": True, "capital": state, "note": note}

    def _over_share_note(self, state: Optional[Dict[str, Any]]) -> str:
        """Said once, when a change leaves a kind of trade holding more than its share."""
        split = (state or {}).get("split") or {}
        if not split.get("on"):
            return ""
        over = [(name, split[key]) for key, name in (("day", "Day"), ("swing", "Swing")) if (split.get(key) or {}).get("over")]
        return "".join(f" {name} trades hold {capital.money(part['over'], state['currency'])} more than that share - "
                       "nothing is sold for it; they take no new entries until they are back under it."
                       for name, part in over)

    def set_capital(self, amount: Any = None) -> Dict[str, Any]:
        """How much of the account on the current platform the bot may use, in the
        account's own currency. Empty = the whole account; never more than it holds."""
        locked = self._locked()
        if locked:
            return {"ok": False, "reason": locked}
        venue, label = self._venue, venue_label(self._venue)
        if amount is None or amount == "":
            self.capital.pop(venue, None)
            note = f"The bot can use all of {label} again."
        else:
            try:
                value = capital.parse_amount(amount)
            except ValueError as e:
                return {"ok": False, "reason": str(e)}
            if not self._refresh_account() or self._account is None:
                return {"ok": False, "reason": f"Couldn't read your account on {label}, so the amount can't be "
                                               "checked. Try again once it's connected."}
            try:
                worth = capital.check_fits(value, self._account, label)
            except ValueError as e:
                return {"ok": False, "reason": str(e)}
            self.capital[venue] = value
            currency = worth["currency"]
            note = (f"The bot will use {capital.money(value, currency)} of the "
                    f"{capital.money(worth['equity'], currency)} in {label}. Position sizes now use this amount.")
        self._save_runtime()
        log.info("trading capital on %s: %s", venue, self.capital.get(venue) or "the whole account")
        self._settings_changed()
        state = self.capital_state()
        self._publish("capital.updated", capital=state)
        return {"ok": True, "capital": state, "note": note + self._over_share_note(state)}

    def _resize_plays(self) -> None:
        self._size_plays([p for p in self.board.plays.values() if p.status not in _ACTED_ON])
        self._publish_plays()

    def _size_plays(self, plays: Iterable[Play]) -> None:
        """Suggested sizes for plays on the board, each against its own share of the trading capital."""
        accounts = {kind: self.sizing_account(kind) for kind in (capital.DAY, capital.SWING)}
        if accounts[capital.DAY] is None:
            return
        exposure = self.exposure_by_symbol()
        for p in plays:
            size_play(p, accounts[capital.kind_of(p.timeframe)], self.settings.config.risk,
                      symbol_notional=exposure.get(p.symbol, 0.0), risk_pct=self._play_risk_pct(p))
