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
    def _invested_usd(self, held: Dict[str, float]) -> float:
        """What the positions on this account hold together, in US dollars - the room left for new ones is measured
        from it. ``held`` is _held_by_kind's: the trade records plus the entries still working, all the day / swing
        split counts. Cash only or with a set amount, the broker's own positions count as well (exposure_by_symbol,
        the entries still working in it): shares the account holds with no trade record - bought by hand, or left
        untracked - use the same money, and leaving them out would let new positions take the total past the
        account's value or the amount. Per stock, whichever is more - a fill booked a moment ago may not be in the
        last account read yet. With margin, the records' figure stays: the broker's buying power is already what's
        left after everything the account holds."""
        recorded = sum(held.values())
        if not self.capital.get(self._venue) and self._capital_mode() == capital.MODE_MARGIN:
            return recorded
        mine = capital.invested_by_symbol(self._account, self._positions_here())
        for w in self.working_entries():
            mine[w["symbol"]] = mine.get(w["symbol"], 0.0) + float(w.get("notional") or 0.0)
        broker = self.exposure_by_symbol()
        return max(recorded, sum(max(broker.get(s, 0.0), mine.get(s, 0.0)) for s in {*broker, *mine}))

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
        acc, limit, mode = self._account, self.capital.get(self._venue), self._capital_mode()
        share = 1.0 if timeframe is None else capital.share_of(capital.kind_of(timeframe), self.effective_day_pct())
        if acc is None or (not limit and share >= 1.0 and mode == capital.MODE_MARGIN):
            return acc                      # the whole account with margin: the broker's buying power is the limit
        held = self._held_by_kind()
        return capital.sizing_account(acc, limit, self._invested_usd(held), share=share, mode=mode,
                                      invested_in_kind=held[capital.kind_of(timeframe)] if timeframe is not None else 0.0)

    def _capital_mode(self) -> str:
        """With no amount set, what the whole account means on this venue: margin (the default) or cash only."""
        return self.capital_mode.get(self._venue, capital.MODE_MARGIN)

    def exposure_ceiling(self) -> float:
        """The most the positions may hold together, in US dollars (capital.capacity_usd) - what Autopilot's and
        the pair desk's max_gross_exposure_pct is a share of. 0 with no account read yet."""
        if self._account is None:
            return 0.0
        return capital.capacity_usd(self._account, self.capital.get(self._venue), sum(self._held_by_kind().values()),
                                    self._capital_mode())

    def capital_state(self) -> Optional[Dict[str, Any]]:
        if self._account is None:
            return None
        held = self._held_by_kind()
        state = capital.state(self._account, self._venue, venue_label(self._venue), self.capital.get(self._venue),
                              self._invested_usd(held), self.effective_day_pct(), held, mode=self._capital_mode())
        state["split"].update(on=self._both_kinds(), set_pct=self.day_trade_pct)
        state["size_factor"] = self.size_factor
        state["max_position_pct"] = float(self.settings.config.risk.max_position_pct_of_equity)
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

    def set_size_factor(self, factor: Any) -> Dict[str, Any]:
        """Every position's size times this, 0-5: 1 is the usual size, 0 sizes every new position at nothing.
        It scales the risk each trade takes; the caps (open risk, per stock, liquidity, buying power) still hold."""
        locked = self._locked()
        if locked:
            return {"ok": False, "reason": locked}
        try:
            value = capital.parse_size_factor(factor)
        except ValueError as e:
            return {"ok": False, "reason": str(e)}
        self.size_factor = value
        self._save_runtime()
        log.info("position size factor set to %g", value)
        self._settings_changed()
        state = self.capital_state()
        self._publish("capital.updated", capital=state)
        note = (f"New positions are now {value:g} times the usual size" if value > 0 else
                "New positions are now sized at nothing - no entries until the factor is above 0")
        return {"ok": True, "capital": state,
                "note": note + ". The caps on open risk, per stock, liquidity and buying power still hold."}

    def set_max_position_pct(self, pct: Any) -> Dict[str, Any]:
        """The most one position may hold, 1-100% of the account's value (risk.max_position_pct_of_equity). Day
        trades' stops are tight, so this cap - not the risk budget - usually decides their size."""
        locked = self._locked()
        if locked:
            return {"ok": False, "reason": locked}
        try:
            value = capital.parse_position_pct(pct)
        except ValueError as e:
            return {"ok": False, "reason": str(e)}
        self.position_pct = value
        self.settings.config.risk.max_position_pct_of_equity = value
        self._save_runtime()
        log.info("the most one position may hold set to %g%% of the account", value)
        self._settings_changed()
        state = self.capital_state()
        self._publish("capital.updated", capital=state)
        return {"ok": True, "capital": state,
                "note": f"One position may now hold up to {value:g}% of the account's value. A day trade's tight stop "
                        "usually makes this the cap that decides its size; the open-risk ceiling, 1% of the stock's "
                        "daily volume and the buying power still hold."}

    def _over_share_note(self, state: Optional[Dict[str, Any]]) -> str:
        """Said once, when a change leaves a kind of trade holding more than its share."""
        split = (state or {}).get("split") or {}
        if not split.get("on"):
            return ""
        over = [(name, split[key]) for key, name in (("day", "Day"), ("swing", "Swing")) if (split.get(key) or {}).get("over")]
        return "".join(f" {name} trades hold {capital.money(part['over'], state['currency'])} more than that share - "
                       "nothing is sold for it; they take no new entries until they are back under it."
                       for name, part in over)

    def set_capital(self, amount: Any = None, mode: Any = None) -> Dict[str, Any]:
        """How much of the account on the current platform the bot may use, in the account's own currency - never
        more than it holds. Empty = the whole account: with margin (``mode`` "margin", the default) new positions may
        use the broker's buying power; cash only ("cash"), the positions never hold more than the account's value."""
        locked = self._locked()
        if locked:
            return {"ok": False, "reason": locked}
        venue, label = self._venue, venue_label(self._venue)
        if amount is None or amount == "":
            if mode is not None:
                try:
                    self.capital_mode[venue] = capital.parse_mode(mode)
                except ValueError as e:
                    return {"ok": False, "reason": str(e)}
            self.capital.pop(venue, None)
            note = (f"The bot can use all of {label}, cash only: its positions, long and short together, never hold "
                    "more than the account is worth, so nothing is borrowed."
                    if self._capital_mode() == capital.MODE_CASH else
                    f"The bot can use all of {label} with margin: new positions may use its buying power, as IBKR "
                    "reports it. Risk per trade is still measured against the account's value, not the margin.")
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
        log.info("trading capital on %s: %s", venue, self.capital.get(venue)
                 or f"the whole account, {'cash only' if self._capital_mode() == capital.MODE_CASH else 'with margin'}")
        self._settings_changed()
        state = self.capital_state()
        self._publish("capital.updated", capital=state)
        return {"ok": True, "capital": state, "note": note + self._over_share_note(state)}

    def _resize_plays(self) -> None:
        self._size_plays([p for p in self.board.plays.values() if p.status not in _ACTED_ON])
        self._publish_plays()

    def _size_plays(self, plays: Iterable[Play]) -> None:
        """Suggested sizes for plays on the board, each against its own share of the trading capital and what's left
        under the open-risk ceiling after the trades and entries already at work - none, while the open trades
        can't be read (_risk_used)."""
        accounts = {kind: self.sizing_account(kind) for kind in (capital.DAY, capital.SWING)}
        if accounts[capital.DAY] is None:
            return
        exposure, open_risk = self.exposure_by_symbol(), self.open_risk_usd()
        for p in plays:
            account = accounts[capital.kind_of(p.timeframe)]
            size_play(p, account, self.settings.config.risk, open_risk_used=self._risk_used(open_risk, account),
                      symbol_notional=exposure.get(p.symbol, 0.0), risk_pct=self._play_risk_pct(p),
                      risk_why=self.strategy_risk_why(p.strategy), size_factor=self.size_factor)
