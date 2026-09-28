"""Five-minute candles for the strategy replay, kept on disk.

The replay needs several weeks of 5-minute candles for the stocks it replays.
They're downloaded from IBKR a few sessions at a time and saved per stock, so a
session already on disk is never asked for again - a replay run a week later
only fetches the week since.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import pickle
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional

import pandas as pd

from ..data.bars import symbol_path
from ..util import clock

log = logging.getLogger(__name__)

NY = "America/New_York"
BAR_SIZE = "5 mins"
SESSIONS_PER_REQUEST = 5          # IBKR serves 5-minute history up to a week per request
#: how long a download waits for IB Gateway to come back (its nightly restart takes a minute or two)
GATEWAY_WAIT_S = 900.0
GATEWAY_POLL_S = 15.0
#: the sessions already asked for per stock (load_days), so one IBKR has nothing for isn't asked for again
ASKED_FILE = "_asked.json"
#: stocks a request batch, so the download budget is looked at often and the progress line moves
BATCH = 24


class IntradayHistory:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        #: candle requests the last load_days left for the next run (its download budget ran out)
        self.pending = 0

    def load(self, source, symbols: Iterable[str], sessions: int, con_ids: Optional[Mapping[str, int]] = None,
             today: Optional[dt.date] = None,
             progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, pd.DataFrame]:
        """The last ``sessions`` completed sessions of 5-minute candles per symbol,
        downloading only the sessions that aren't on disk yet."""
        today = today or clock.session_date()
        wanted = sorted(clock.last_n_sessions(clock.prev_trading_day(today), sessions))
        symbols = list(dict.fromkeys(symbols))
        frames = {s: self._read(s) for s in symbols}
        chunks = [wanted[max(0, end - SESSIONS_PER_REQUEST):end]
                  for end in range(len(wanted), 0, -SESSIONS_PER_REQUEST)]
        for n, chunk in enumerate(chunks, 1):
            missing = [s for s in symbols if not set(chunk) <= _sessions_in(frames[s])]
            if missing:
                close = dt.datetime.combine(chunk[-1], clock.regular_close_time(chunk[-1]))
                got = _through_gateway_drops(source, lambda: source.history_many(
                    {s: (BAR_SIZE, f"{len(chunk)} D") for s in missing}, con_ids,
                    end=pd.Timestamp(close, tz=NY).to_pydatetime()))
                for symbol, frame in got.items():
                    frames[symbol] = _merge(frames[symbol], frame)
                    self._write(symbol, frames[symbol])
            if progress:
                progress(n, len(chunks))
        first = pd.Timestamp(wanted[0], tz=NY)
        return {s: f[f.index >= first] for s, f in frames.items() if f is not None and len(f)}

    def load_days(self, source, wanted: Mapping[str, Iterable[dt.date]],
                  con_ids: Optional[Mapping[str, int]] = None, lookback: int = SESSIONS_PER_REQUEST - 1,
                  progress: Optional[Callable[[int, int], None]] = None,
                  budget_s: Optional[float] = None) -> Dict[str, pd.DataFrame]:
        """Each stock's 5-minute candles for its own ``wanted`` sessions and the ``lookback``
        sessions before each (what a live scan on that day would have had in hand), downloading
        only what isn't on disk. One request brings a session and the four before it, so a stock
        in play on scattered days costs about a request a day, and a run of days far fewer.
        A session IBKR had nothing for is asked for once and remembered, not on every replay -
        but a request that timed out is asked again next time.

        IBKR answers a request for past candles far more slowly than one for the latest (measured:
        seconds each, and some time out), so ``budget_s`` caps how long one call downloads, the
        latest sessions first; what is left comes with the next call. ``self.pending`` says how
        many requests that is."""
        frames = {s: self._read(s) for s in wanted}
        asked = self._asked()
        plan: Dict[dt.date, List[str]] = {}
        for symbol, days in wanted.items():
            need: set = set()
            for day in days:
                need.update(clock.last_n_sessions(day, lookback + 1))
            missing = need - _sessions_in(frames[symbol]) - {dt.date.fromisoformat(d) for d in asked.get(symbol, ())}
            while missing:
                end = max(missing)                      # a request ending here brings this session and four before
                plan.setdefault(end, []).append(symbol)
                missing -= set(clock.last_n_sessions(end, SESSIONS_PER_REQUEST))
        total, done, started = sum(len(v) for v in plan.values()), 0, time.monotonic()
        self.pending = 0
        if progress and total:
            progress(0, total)
        batches = [(end, plan[end][i:i + BATCH]) for end in sorted(plan, reverse=True)
                   for i in range(0, len(plan[end]), BATCH)]
        for n, (end, symbols) in enumerate(batches):
            if budget_s is not None and time.monotonic() - started >= budget_s:
                self.pending = total - done
                log.info("replay: %d of %d candle requests made in the %d minutes a run may download for - "
                         "the rest come with the next replay", done, total, budget_s // 60)
                break
            close = dt.datetime.combine(end, clock.regular_close_time(end))
            got = _through_gateway_drops(source, lambda: source.history_many(
                {s: (BAR_SIZE, f"{SESSIONS_PER_REQUEST} D") for s in symbols}, con_ids,
                end=pd.Timestamp(close, tz=NY).to_pydatetime()))
            failed = set(getattr(got, "failed", ()))
            covered = [d.isoformat() for d in clock.last_n_sessions(end, SESSIONS_PER_REQUEST)]
            for symbol in symbols:
                if symbol in got:
                    frames[symbol] = _merge(frames[symbol], got[symbol])
                    self._write(symbol, frames[symbol])
                if symbol not in failed:                 # answered - with candles or with nothing: don't ask again
                    asked[symbol] = sorted(set(asked.get(symbol, ())) | set(covered))
            self._save_asked(asked)
            done += len(symbols)
            if progress:
                progress(done, total)
        return {s: f for s, f in frames.items() if f is not None and len(f)}

    def _asked(self) -> Dict[str, List[str]]:
        try:
            data = json.loads((self.directory / ASKED_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_asked(self, asked: Mapping[str, List[str]]) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            tmp = (self.directory / ASKED_FILE).with_suffix(".tmp")
            tmp.write_text(json.dumps(asked), encoding="utf-8")
            tmp.replace(self.directory / ASKED_FILE)
        except OSError:
            log.debug("could not save %s", ASKED_FILE, exc_info=True)

    def stored(self, symbol: str) -> Optional[pd.DataFrame]:
        """The candles already on disk, without asking IBKR."""
        return self._read(symbol)

    def _path(self, symbol: str) -> Path:
        return symbol_path(self.directory, symbol, ".pkl")

    def _read(self, symbol: str) -> Optional[pd.DataFrame]:
        try:
            return pd.read_pickle(self._path(symbol))
        except (OSError, ValueError, EOFError, pickle.UnpicklingError):
            return None

    def _write(self, symbol: str, frame: pd.DataFrame) -> None:
        try:
            path = self._path(symbol)
        except ValueError:
            return                                # not a stock symbol: nothing saved, the download goes on
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        frame.to_pickle(tmp)
        tmp.replace(path)


def _through_gateway_drops(source, request: Callable[[], Dict[str, pd.DataFrame]]) -> Dict[str, pd.DataFrame]:
    """Run a download. If IB Gateway drops part way - its nightly restart - wait for it to come back and
    ask again; an error while it's connected is a real one."""
    waited = 0.0
    while True:
        try:
            return request()
        except Exception:
            if getattr(source, "is_connected", True) or waited >= GATEWAY_WAIT_S:
                raise
            if not waited:
                log.warning("replay: IB Gateway dropped - waiting up to %d minutes for it", GATEWAY_WAIT_S // 60)
            time.sleep(GATEWAY_POLL_S)
            waited += GATEWAY_POLL_S


def _sessions_in(frame: Optional[pd.DataFrame]) -> set:
    return set(frame.index.date) if frame is not None and len(frame) else set()


def _merge(old: Optional[pd.DataFrame], new: pd.DataFrame) -> pd.DataFrame:
    combined = new if old is None else pd.concat([old, new])
    return combined[~combined.index.duplicated(keep="last")].sort_index()


def replay_symbols(watchlist, swing_stocks: int = 0) -> Dict[str, List[str]]:
    """Which stocks the replay uses: day-trade setups on the hot list and the kept buffer names
    (their 5-minute candles cost IBKR requests); swing setups on every watchlist stock and, with
    ``swing_stocks``, on that many of the full scan's leaders too - their daily candles are
    already on disk, so a wide swing replay costs no requests. A strategy's record then rests on
    hundreds of stocks rather than the day's forty."""
    if watchlist is None:
        return {"intraday": [], "swing": []}
    intraday = list(dict.fromkeys(watchlist.hot_symbols() + watchlist.kept_symbols()))
    queued = [c.symbol for queue in watchlist.queues.values() for c in queue]
    leaders = watchlist.leaders(swing_stocks) if swing_stocks else []
    swing = list(dict.fromkeys(intraday + queued + leaders))
    return {"intraday": intraday, "swing": swing}
