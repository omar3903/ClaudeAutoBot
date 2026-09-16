"""The strategy replay and what the evidence says: each strategy's record, the evidence weight in the
ranking, the calibrated odds and the half-Kelly risk per trade.

A mixin of TradingEngine (engine.py): it works on the engine's own attributes and is kept apart
only so each concern reads on its own. Nothing here is instantiated by itself.
"""

from __future__ import annotations

import logging
import datetime as dt
import time
from typing import Any, Dict, List, Optional, Sequence

from ..core.models import Play
from ..research.history import replay_symbols
from ..research.replay import ReplaySettings
from ..research.journal import live_records
from ..research.weights import evidence_multiplier, pooled_odds
from ..quant.sizing import MIN_TRADES as KELLY_MIN_TRADES, half_kelly_risk_pct
from ..pairs.model import KEY as PAIRS_KEY
from .market_regime import BENCHMARK
from ..scanner.noise import NoiseSettings
from ..util import clock

log = logging.getLogger(__name__)


class ResearchOps:
    # ------------------------------------------------------------------ #
    #  Strategy replay                                                   #
    # ------------------------------------------------------------------ #
    def start_replay(self, sessions: Optional[int] = None, swing_sessions: Optional[int] = None) -> Dict[str, Any]:
        """Replay the strategies over recent candles in the background (see research/)."""
        if not self.md.attached:
            return {"ok": False, "reason": "IB Gateway isn't connected, so there are no candles to replay."}
        symbols = replay_symbols(self.scanner.watchlist)
        if not symbols["swing"]:
            return {"ok": False, "reason": "Run the full scan first - the replay uses the stocks on its watchlist."}
        cfg = self.settings.config
        sessions = cfg.replay.sessions if sessions is None else sessions
        swing_sessions = cfg.replay.swing_sessions if swing_sessions is None else swing_sessions
        return self.replay.start(
            strategies=self.scanner.strategies, source=self.md.source, daily_frame=self.md.daily_frame,
            intraday_symbols=symbols["intraday"], swing_symbols=symbols["swing"],
            sessions=max(5, min(120, int(sessions))), swing_sessions=max(20, min(250, int(swing_sessions))),
            settings=ReplaySettings.from_exit_rules(cfg.exit_manager, cfg.replay),
            noise=NoiseSettings.from_config(cfg.noise), con_ids=self.scanner.con_ids(symbols["intraday"]),
            market=self._regime_history, earnings=self.earnings.times if cfg.signals.enabled else None,
            pairs=self._pair_replay_inputs() if cfg.pairs.enabled else None,
            held_out_fraction=float(cfg.replay.held_out_fraction),
            news=self._news_history if cfg.signals.enabled else None, benchmark=BENCHMARK)

    def _news_history(self, symbols: Sequence[str], first_day: dt.date) -> Dict[str, List[Dict[str, Any]]]:
        """Each stock's stored stories since ``first_day`` - as far back as the app has been reading
        the news - shaped like the signal book's, for the replay's news checks."""
        since = dt.datetime.combine(first_day, dt.time(0, 0), tzinfo=dt.timezone.utc) - dt.timedelta(days=5)
        out: Dict[str, List[Dict[str, Any]]] = {}
        for row in self.signals.store.news(list(symbols), since):
            out.setdefault(row["symbol"], []).append({"at": row["published_at"], "headline": row.get("headline"),
                                                      "kind": row.get("kind"), "source": row.get("source")})
        return out

    def _regime_history(self, first_day: dt.date) -> Dict[dt.date, float]:
        self._refresh_regime()
        return self.regime.history(first_day)

    def replay_state(self) -> Dict[str, Any]:
        cfg = self.settings.config.replay
        state = self.replay.state(self.autopilot.skipped_noise(), self.autopilot.min_confirmations)
        return {**state, "evidence": self.evidence_state(), "look_ahead_regime": self.regime.look_ahead,
                "defaults": {"sessions": cfg.sessions, "swing_sessions": cfg.swing_sessions,
                             "held_out_fraction": cfg.held_out_fraction}}

    def replay_history(self, limit: int = 30) -> List[Dict[str, Any]]:
        return self.replay.runs(limit)

    def strategy_record(self, key: str) -> Optional[Dict[str, Any]]:
        """A strategy's replayed record over the trades Autopilot would have taken."""
        return self.replay.records(self.autopilot.skipped_noise(), self.autopilot.min_confirmations).get(key)

    def learned_skips(self) -> List[str]:
        """Noise checks the replay has shown are worth skipping (see replay.learned_skips)."""
        return self.replay.learned_skips()

    # ------------------------------------------------------------------ #
    #  What the evidence says: the market, the weights, the risk         #
    # ------------------------------------------------------------------ #
    #: how long the closed real trades are kept before they're read again
    LIVE_STATS_S = 300.0

    #: how far back real trades count toward the weights and the half-Kelly risk
    LIVE_SESSIONS = 60

    def _refresh_regime(self) -> None:
        try:
            source = self.md.source if self.md.attached else None
            self.regime.refresh(source, self.scanner.con_ids([BENCHMARK]) if source is not None else None)
        except Exception:  # noqa: BLE001
            log.debug("market regime refresh failed", exc_info=True)

    def live_stats(self) -> Dict[str, Dict[str, Any]]:
        """Each strategy's closed real trades over the last sessions, in R."""
        mono = time.monotonic()
        if mono - self._live_stats_at >= self.LIVE_STATS_S:
            today = clock.session_date()
            try:
                first = min(clock.last_n_sessions(today, self.LIVE_SESSIONS))
                closed = self.repo.closed_trades_between(first, today)
                closed += [{"strategy": PAIRS_KEY, "r_multiple": r["r_multiple"]}
                           for r in self.repo.pair_trades_closed_between(first, today)]
                self._live_stats = live_records(closed)
            except Exception:  # noqa: BLE001
                log.debug("could not read the closed trades", exc_info=True)
            self._live_stats_at = mono
        return self._live_stats

    def evidence_state(self) -> Dict[str, Dict[str, Any]]:
        """Each active strategy's evidence multiplier and what it rests on (research/weights.py)."""
        records = self.replay.records(self.autopilot.skipped_noise(), self.autopilot.min_confirmations)
        live = self.live_stats()
        return {s.key: evidence_multiplier(records.get(s.key), live.get(s.key)).as_dict() for s in self.scanner.strategies}

    def evidence_weights(self) -> Dict[str, float]:
        return {key: row["multiplier"] for key, row in self.evidence_state().items()}

    def strategy_odds(self) -> Dict[str, Dict[str, Any]]:
        """Each active strategy's pooled win rate and trade count (research/weights.py pooled_odds),
        which calibrate the odds its plays state (strategies/base.py calibrated_probability)."""
        records = self.replay.records(self.autopilot.skipped_noise(), self.autopilot.min_confirmations)
        live = self.live_stats()
        odds = ((s.key, pooled_odds(records.get(s.key), live.get(s.key))) for s in self.scanner.strategies)
        return {key: row for key, row in odds if row is not None}

    def strategy_risk_pct(self, key: str) -> Optional[float]:
        """Half-Kelly risk per trade from the strategy's record (quant/sizing.py): its real trades
        once there are enough - paper trading is the true out-of-sample test - otherwise the
        replayed trades Autopilot would have taken. A record with no edge still leaves a quarter
        of the usual risk for a trade taken by hand; Autopilot doesn't take those at all."""
        stats, ap = self.live_stats(), self.autopilot
        skipped = ap.skipped_noise()
        records_for = (self.replay.ran_at, tuple(skipped), ap.min_confirmations, self._live_stats_at)
        if records_for != self._risk_pct_for:                  # sized once per strategy until a record changes
            self._risk_pct, self._risk_pct_for = {}, records_for
        if key not in self._risk_pct:
            cap = float(self.settings.config.risk.max_risk_per_trade_pct)
            live = stats.get(key, {}).get("r", [])
            rs = live if len(live) >= KELLY_MIN_TRADES else self.replay.r_multiples(key, skipped, ap.min_confirmations)
            pct = half_kelly_risk_pct(rs, cap)
            self._risk_pct[key] = None if pct is None else max(pct, 0.25 * cap)
        return self._risk_pct[key]

    def _entry_context(self, p: Play, operator: str) -> Dict[str, Any]:
        """What a play was taken on, kept in its evidence for the journal."""
        return {"at": clock.now_ny().isoformat(), "by": operator, "noise": list(p.noise),
                "confirmations": p.confirmations, "score": round(p.score, 4), "confidence": round(p.confidence, 3),
                "probability": round(p.probability, 3), "reward_risk": round(p.reward_risk, 2),
                "market_regime": self.regime.context(), "skipped_noise": self.autopilot.skipped_noise(),
                "unproven": self.autopilot.proof_missing(p.strategy), "replay_record": self.strategy_record(p.strategy),
                "evidence_weight": self.evidence_weights().get(p.strategy, 1.0),
                "risk_pct": self.strategy_risk_pct(p.strategy)}
