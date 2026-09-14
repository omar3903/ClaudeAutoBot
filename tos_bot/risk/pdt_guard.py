"""Pattern-Day-Trader guard rails.

FINRA rule (margin accounts): 4+ day trades in 5 rolling business days makes
you a "pattern day trader" and you must then keep >= $25,000 equity. Under
that line you are capped at 3 day trades per 5 business days.

The user's brief: start each day with at least $2,000 all-in and never break
day-trading rules. So this guard:

* refuses any new entry if equity < ``min_start_equity``
* counts day trades (same-symbol open+close in one session) over the last
  5 sessions - from the trade log if available, else the broker's own count
* while equity < ``pdt_equity_threshold`` blocks a 4th day trade and warns
  from the 2nd/3rd
* treats every INTRADAY play as a *potential* day trade (conservative)
* for cash accounts, skips PDT but flags unsettled-funds risk (T+1)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from ..core.models import Account, Play


@dataclass
class PdtDecision:
    allowed: bool
    reason: str = ""
    day_trades_used: int = 0
    day_trades_remaining: int = 99
    warnings: List[str] = field(default_factory=list)
    is_potential_day_trade: bool = False

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class PdtGuard:
    def __init__(self, cfg, trade_repo=None, paper: bool = False) -> None:
        self.min_start_equity = float(cfg.min_start_equity)
        self.pdt_threshold = float(cfg.pdt_equity_threshold)
        self.max_dt = int(cfg.max_day_trades_under_threshold)
        self.warn_at = int(cfg.day_trade_warn_at)
        self.cash_account = bool(cfg.cash_account)
        self.repo = trade_repo
        #: in paper mode nothing is blocked - the counters/warnings are kept
        #: purely so the operator can see what live trading *would* do.
        self.paper = bool(paper)

    # ------------------------------------------------------------------ #
    def day_trades_last_5_sessions(self, account: Account) -> int:
        if self.repo is not None:
            try:
                return int(self.repo.count_day_trades(lookback_sessions=5))
            except Exception:  # noqa: BLE001
                pass
        return int(getattr(account, "round_trips", 0) or 0)

    # ------------------------------------------------------------------ #
    def assess(self, account: Account, play: Play) -> PdtDecision:
        warnings: List[str] = []
        potential_dt = play.is_day_trade
        used = self.day_trades_last_5_sessions(account)
        cap = 999 if (self.cash_account or account.equity >= self.pdt_threshold) else self.max_dt
        remaining = max(0, cap - used)

        # Paper mode: never block. Still show the day-trade tally + a note so
        # the operator learns where the live rules would bite.
        if self.paper:
            if potential_dt and used >= self.warn_at:
                warnings.append(
                    f"(paper) {used} day trades this rolling week - live, a "
                    f"sub-$25k account would be capped at {self.max_dt}."
                )
            return PdtDecision(
                allowed=True, reason="paper - no restrictions",
                day_trades_used=used, day_trades_remaining=remaining,
                warnings=warnings, is_potential_day_trade=potential_dt,
            )

        # 1) hard equity floor
        if account.equity < self.min_start_equity:
            return PdtDecision(
                allowed=False,
                reason=(f"Account equity ${account.equity:,.0f} is below the "
                        f"${self.min_start_equity:,.0f} floor - no new entries."),
                day_trades_used=used, day_trades_remaining=remaining,
                is_potential_day_trade=potential_dt,
            )

        # 2) PDT counting (margin, under threshold)
        if not self.cash_account and account.equity < self.pdt_threshold:
            if potential_dt and used >= self.max_dt:
                return PdtDecision(
                    allowed=False,
                    reason=(f"{used} day trades used in the last 5 sessions - a "
                            f"4th would flag PDT on a sub-$25k account. Take this "
                            f"as a swing (hold overnight) or wait."),
                    day_trades_used=used, day_trades_remaining=0,
                    is_potential_day_trade=True,
                )
            if potential_dt and used >= self.warn_at:
                warnings.append(
                    f"{used} of {self.max_dt} day trades used this rolling week - "
                    f"{remaining} left before PDT."
                )
        elif not self.cash_account and account.equity >= self.pdt_threshold:
            warnings.append("Equity >= $25k: PDT limits do not restrict day trades.")

        # 3) cash-account settlement note
        if self.cash_account and potential_dt:
            warnings.append(
                "Cash account: proceeds settle T+1. Re-using unsettled cash the "
                "same day is a good-faith violation - size with settled funds only."
            )

        # 4) does the risk fit under the floor?
        if play.dollar_risk and account.equity - play.dollar_risk < self.min_start_equity:
            warnings.append(
                f"Planned risk ${play.dollar_risk:,.0f} would pull equity near the "
                f"${self.min_start_equity:,.0f} floor if it stops out."
            )

        return PdtDecision(
            allowed=True, reason="ok",
            day_trades_used=used, day_trades_remaining=remaining,
            warnings=warnings, is_potential_day_trade=potential_dt,
        )
