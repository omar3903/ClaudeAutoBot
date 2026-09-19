"""The strategy replay and what the evidence says: each strategy's record, the evidence weight in the
ranking, the calibrated odds and the half-Kelly risk per trade.

A mixin of TradingEngine (engine.py): it works on the engine's own attributes and is kept apart
only so each concern reads on its own. Nothing here is instantiated by itself.
"""

from __future__ import annotations

import logging
import threading
import datetime as dt
import time
from typing import Any, Dict, List, Optional, Sequence

from ..core.models import Play
from ..research.history import replay_symbols
from ..research.in_play import in_play
from ..research.replay import ReplaySettings
from ..research.journal import live_records
from ..research.weights import evidence_multiplier, pooled_odds
from ..quant.sizing import MIN_TRADES as KELLY_MIN_TRADES, half_kelly_risk_pct
from ..pairs.model import KEY as PAIRS_KEY
from .market_regime import BENCHMARK
from ..scanner import schedule
from ..scanner.noise import NoiseSettings
from ..util import clock

log = logging.getLogger(__name__)


#: the share of the usual risk a strategy is traded with until the replay has proven it
PRACTICE_RISK = 0.25

class ResearchOps:
    # ------------------------------------------------------------------ #
    #  Strategy replay                                                   #
    # ------------------------------------------------------------------ #
    def start_replay(self, sessions: Optional[int] = None, swing_sessions: Optional[int] = None) -> Dict[str, Any]:
        """Replay the strategies over recent candles in the background (see research/)."""
        if not self.md.attached:
            return {"ok": False, "reason": "IB Gateway isn't connected, so there are no candles to replay."}
        symbols = replay_symbols(self.scanner.watchlist, int(self.settings.config.replay.swing_stocks or 0))
        if not symbols["swing"]:
            return {"ok": False, "reason": "Run the full scan first - the replay uses the stocks on its watchlist."}
        cfg = self.settings.config
        sessions = cfg.replay.sessions if sessions is None else sessions
        swing_sessions = cfg.replay.swing_sessions if swing_sessions is None else swing_sessions
        deep = list(dict.fromkeys(list(symbols["swing"]) + list(symbols["intraday"]) + [BENCHMARK]))
        through = schedule.last_completed_session(clock.now_ny())
        sessions = max(5, min(120, int(sessions)))
        wl, day_stocks = self.scanner.watchlist, int(cfg.replay.day_stocks or 0)
        universe = list(wl.leaders(0) or symbols["swing"]) if day_stocks > 0 else []

        def stocks_in_play(progress):
            """Each stock's sessions in play, as the morning scan would have picked them (research/in_play.py)."""
            last = clock.prev_trading_day(clock.session_date())
            return in_play(self.md.daily_frame, universe, clock.last_n_sessions(last, sessions), hot=day_stocks,
                           gappers=int(cfg.replay.day_gappers or 0), prefilter=cfg.scanner.prefilter,
                           min_gap_pct=float(cfg.scanner.gapper_min_gap_pct), watch=int(cfg.scanner.gapper_symbols),
                           progress=progress)

        return self.replay.start(
            strategies=self.scanner.strategies, source=self.md.source, daily_frame=self.md.deep_frame,
            prepare=lambda progress: self.md.deepen_daily(deep, through, self.scanner.con_ids(deep), progress),
            intraday_symbols=symbols["intraday"], swing_symbols=symbols["swing"],
            sessions=sessions, swing_sessions=max(20, min(1250, int(swing_sessions))),
            in_play=stocks_in_play if universe else None,
            settings=ReplaySettings.from_exit_rules(cfg.exit_manager, cfg.replay, cfg.risk.min_reward_risk),
            noise=NoiseSettings.from_config(cfg.noise),
            con_ids=self.scanner.con_ids(list(symbols["intraday"]) + universe),
            market=self._regime_history, earnings=self.earnings.times if cfg.signals.enabled else None,
            pairs=self._pair_replay_inputs() if cfg.pairs.enabled else None,
            held_out_fraction=float(cfg.replay.held_out_fraction),
            news=self._news_history if cfg.signals.enabled else None, benchmark=BENCHMARK,
            records=self.strategy_odds())

    def _keep_sim_trades(self, data: Dict[str, Any], trades: Sequence[Any]) -> None:
        """The replay's sink: its simulated trades go to the database, next to the real ones and
        the shadows, so a model can learn from all three (research/dataset.py)."""
        run_id = self.repo.save_sim_trades(str(data.get("ran_at") or ""), trades, data.get("held_out_from"))
        log.info("replay run %s: %d simulated trades kept", run_id, len(trades))

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
        state = self.replay.state(*self._record_terms())
        proof = {s.key: self.autopilot.proof_missing(s.key) or "" for s in self.scanner.strategies}
        return {**state, "evidence": self.evidence_state(), "proof": proof, "look_ahead_regime": self.regime.look_ahead,
                "defaults": {"sessions": cfg.sessions, "swing_sessions": cfg.swing_sessions,
                             "held_out_fraction": cfg.held_out_fraction}}

    def train_model(self) -> Dict[str, Any]:
        """Retrain the meta-label model on every row the database holds - live, shadow and the
        latest replay (research/model.py) - and keep it with its card. One at a time; the scorer
        picks the new model up by itself. It takes a minute or two, so callers use a thread."""
        from ..research import model as meta
        from ..research.dataset import training_rows

        if not meta.available():
            return {"ok": False, "reason": "scikit-learn isn't installed (pip install scikit-learn)"}
        if not self._training.acquire(blocking=False):
            return {"ok": False, "reason": "a model is being trained already"}
        try:
            out = meta.train(training_rows(self.repo), self.model.directory)
            if not out.get("saved"):
                return {"ok": False, "reason": out.get("why", "not trained")}
            card = out["card"]
            log.info("meta-label model %s trained on %s rows - usable: %s", card["id"], card["rows"], card["usable"])
            self._publish("model.trained", id=card["id"], rows=card["rows"], usable=card["usable"])
            return {"ok": True, "card": card}
        except Exception as e:  # noqa: BLE001
            log.exception("training the model failed")
            return {"ok": False, "reason": str(e)}
        finally:
            self._training.release()

    def train_model_soon(self) -> None:
        """Train in the background - after the day's review has added its rows."""
        threading.Thread(target=self.train_model, name="train-model", daemon=True).start()

    def replay_history(self, limit: int = 30) -> List[Dict[str, Any]]:
        return self.replay.runs(limit)

    def strategy_record(self, key: str) -> Optional[Dict[str, Any]]:
        """A strategy's replayed record over the trades Autopilot would have taken."""
        return self.replay.records(*self._record_terms()).get(key)

    def _record_terms(self) -> tuple:
        """How Autopilot judges plays, for the replay's records: its skipped flags, confirmations,
        reward:risk floor and confidence floors - so a strategy's record is over the trades it
        would actually take."""
        ap = self.autopilot
        return (ap.skipped_noise(), ap.min_confirmations, float(ap.min_reward_risk),
                {"INTRADAY": ap.confidence_floor("INTRADAY"), "SWING": ap.confidence_floor("SWING")})

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
        records = self.replay.records(*self._record_terms())
        live = self.live_stats()
        return {s.key: evidence_multiplier(records.get(s.key), live.get(s.key)).as_dict() for s in self.scanner.strategies}

    def evidence_weights(self) -> Dict[str, float]:
        return {key: row["multiplier"] for key, row in self.evidence_state().items()}

    def strategy_odds(self) -> Dict[str, Dict[str, Any]]:
        """Each active strategy's pooled win rate and trade count (research/weights.py pooled_odds),
        which calibrate the odds its plays state (strategies/base.py calibrated_probability)."""
        records = self.replay.records(*self._record_terms())
        live = self.live_stats()
        odds = ((s.key, pooled_odds(records.get(s.key), live.get(s.key))) for s in self.scanner.strategies)
        return {key: row for key, row in odds if row is not None}

    def strategy_risk_pct(self, key: str) -> Optional[float]:
        """Half-Kelly risk per trade from the strategy's record (quant/sizing.py): its real trades
        once there are enough - paper trading is the true out-of-sample test - otherwise the
        replayed trades Autopilot would have taken. A strategy the replay hasn't proven
        (autopilot.proof_missing) is traded at practice size, a quarter of the usual risk, whatever
        its record hints at: an edge that can't be told from luck is no reason to size up (Tharp,
        Aronson), and a setup never replayed is no reason to risk the full amount. The same
        quarter is left for a record with no edge, for a trade taken by hand."""
        stats, ap = self.live_stats(), self.autopilot
        skipped = ap.skipped_noise()
        records_for = (self.replay.ran_at, tuple(skipped), ap.min_confirmations, self._live_stats_at,
                       ap.min_replay_trades, ap.min_replay_expectancy_r, ap.proof_p_value)
        if records_for != self._risk_pct_for:                  # sized once per strategy until a record changes
            self._risk_pct, self._risk_pct_for = {}, records_for
        if key not in self._risk_pct:
            cap = float(self.settings.config.risk.max_risk_per_trade_pct)
            live = stats.get(key, {}).get("r", [])
            rs = live if len(live) >= KELLY_MIN_TRADES else self.replay.r_multiples(key, *self._record_terms())
            pct = half_kelly_risk_pct(rs, cap)
            if ap.proof_missing(key):
                self._risk_pct[key] = PRACTICE_RISK * cap
            else:
                self._risk_pct[key] = None if pct is None else max(pct, PRACTICE_RISK * cap)
        return self._risk_pct[key]

    def _entry_context(self, p: Play, operator: str) -> Dict[str, Any]:
        """What a play was taken on, kept in its evidence for the journal."""
        return {"at": clock.now_ny().isoformat(), "by": operator, "noise": list(p.noise),
                "confirmations": p.confirmations, "score": round(p.score, 4), "confidence": round(p.confidence, 3),
                "probability": round(p.probability, 3), "reward_risk": round(p.reward_risk, 2),
                "market_regime": self.regime.context(), "skipped_noise": self.autopilot.skipped_noise(),
                "unproven": self.autopilot.proof_missing(p.strategy), "replay_record": self.strategy_record(p.strategy),
                "evidence_weight": self.evidence_weights().get(p.strategy, 1.0),
                "risk_pct": self.strategy_risk_pct(p.strategy),
                # the gates in force when it was taken, so a trade taken on looser rules is never mistaken for
                # one the strict rules would have taken
                "settings": {"proof_required": self.autopilot.proof_required, **{k: getattr(self.autopilot, k) for k in (
                    "require_proven", "min_confidence", "min_swing_confidence", "min_reward_risk",
                    "min_confirmations", "model_mode", "max_auto_positions", "max_auto_trades_per_day")}},
                "data_delayed": bool(self.md.delayed)}
