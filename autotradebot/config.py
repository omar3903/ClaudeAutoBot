"""Configuration loading.

Two layers:

* ``.env``               -> machine-specific values and secrets (:class:`Secrets`).
                            The dashboard's Connections panel edits the IB Gateway
                            settings in it (see :mod:`autotradebot.secrets_store`).
* ``config/config.yaml`` -> tunable behaviour (:class:`AppConfig`).

``get_settings()`` returns a cached :class:`Settings` bundle with both.
Everything else imports from here, never from ``os.environ``.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any, Dict

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
# overridable so tests never read or write yours
DATA_DIR = Path(os.getenv("ATB_DATA_DIR") or (PROJECT_ROOT / "data"))
ENV_PATH = Path(os.getenv("ATB_ENV_PATH") or (PROJECT_ROOT / ".env"))
#: the dashboard's remembered choices
RUNTIME_PATH = Path(os.getenv("ATB_RUNTIME_PATH") or (DATA_DIR / "runtime.json"))

load_dotenv(ENV_PATH)


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ENV_PATH), env_file_encoding="utf-8",
                                      extra="ignore", case_sensitive=False)

    # what Paper trades on until the dashboard says otherwise: ibkr | simulator
    paper_platform: str = "ibkr"
    # keep the simulator's balance and positions in data/paper_state.json between runs
    paper_persist: bool = True

    # Interactive Brokers (ib_async -> IB Gateway / TWS; no token)
    ibkr_host: str = "127.0.0.1"
    ibkr_paper_port: int = 4002       # IB Gateway paper  (TWS paper = 7497)
    ibkr_live_port: int = 4001        # IB Gateway live   (TWS live  = 7496)
    ibkr_port: int = 0               # non-zero = force this port for both accounts
    ibkr_client_id: int = 11         # any int unique to this app on the Gateway
    ibkr_account_id: str = ""        # DUxxxxxxx (paper) / Uxxxxxxx (live); blank = first
    ibkr_market_data: str = "auto"   # auto | live | delayed | delayed-frozen
    ibkr_readonly: bool = False      # true = connect for data only, never send orders

    # Finnhub company news (free key from finnhub.io; optional)
    finnhub_api_key: str = ""

    # database
    database_url: str = ""
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_name: str = "autotradebot"
    db_user: str = "autotradebot"
    db_password: str = ""
    db_allow_sqlite_fallback: bool = True

    # web
    web_host: str = "127.0.0.1"
    web_port: int = 8787
    open_browser_on_start: bool = True

    def ibkr_port_for(self, account: str) -> int:
        return int(self.ibkr_port or (self.ibkr_live_port if account == "live" else self.ibkr_paper_port))

    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        pw = f":{self.db_password}" if self.db_password else ""
        return (f"mysql+pymysql://{self.db_user}{pw}@{self.db_host}:{self.db_port}/{self.db_name}"
                "?charset=utf8mb4")

    def sqlite_fallback_url(self) -> str:
        return f"sqlite:///{(DATA_DIR / 'autotradebot.sqlite').as_posix()}"


class _Model(BaseModel):
    model_config = {"extra": "allow"}


class AccountCfg(_Model):
    min_start_equity: float = 2000.0          # LIVE only - paper ignores this
    paper_start_cash: float = 100000.0        # opening balance for the built-in simulator
    pdt_equity_threshold: float = 25000.0
    max_day_trades_under_threshold: int = 3
    day_trade_warn_at: int = 2
    cash_account: bool = False
    day_trade_pct: float = 75.0               # of the trading capital day trades may hold at once; swing trades get the rest


class RiskCfg(_Model):
    max_risk_per_trade_pct: float = 1.0
    max_open_risk_pct: float = 4.0
    max_position_pct_of_equity: float = 12.0   # one trade's notional (or the dashboard's "Max % per position")
    max_symbol_pct_of_equity: float = 15.0     # everything in one stock: shares held + entries working + this trade
    max_adv_pct: float = 1.0                   # one order's shares, as % of the stock's median daily volume over its
                                               # last 20 completed sessions, so thin stocks fill; 0 = off
    min_reward_risk: float = 1.5
    round_lot: int = 1
    midday_size_pct: float = 60.0              # a day trade sized at Mid-day (12-3 pm ET) risks this % of the usual
                                               # (Aziz: lower your size mid-day); 100 = off


class ScannerCfg(_Model):
    """Defaults for the Settings panel - the dashboard's choices are remembered
    in data/runtime.json and win over these."""

    premarket_time: str = "08:30"         # ET - the daily full scan (04:00 to 09:00)
    cycle_minutes: int = 5                # intraday rescan of the hot list + buffer (3-5)
    fast_cycle_seconds: int = 60          # hot list only, while Autopilot is day-trading
    plays_refresh_seconds: int = 15       # re-check the stocks with plays on the board (0 = off)
    close_check: bool = True              # at each 5-minute candle close in regular hours, +2 s, check the watch
                                          # tier's setups on IBKR's just-closed bars at once - it stands in for the
                                          # fast cycle due then; false = off, the fast cycle as before
    mover_atr: float = 1.0                # a watch stock whose streamed 1-minute candle spans at least this many of
                                          # its 5-minute ATRs, or makes a new high/low of the day on 3x its average
                                          # minute volume, is checked at once, at most once per 5 minutes (0-5; 0 = off)
    hot_list_size: int = 20
    sector_queue_size: int = 25           # buffer candidates lined up per sector
    wide_minutes: int = 30                # the wide scan - every liquid stock's 5-minute candles - this often
                                          # in the session (15-120; 0 = off); one request per stock
    wide_stocks: int = 0                  # the hottest N of the full scan's liquid stocks; 0 = all of them
    movers: int = 10                      # today's biggest movers (by % change, on volume) hold hot-list slots
                                          # after each wide scan (0-20; 0 = off) - Aziz's stocks in play
    yesterday_movers: int = 10            # the last session's biggest movers hold slots from the full scan, in
                                          # case they move for a second day (0-20; 0 = off)
    live_scan: int = 10                   # watch-tier slots, right after the hot list, for the stocks topping IBKR's
                                          # live scans - % gainers, % losers, hot by volume; US stocks and ADRs at
                                          # $3-600 - so a stock too quiet for the morning's ranking that explodes
                                          # intraday is streamed and checked (0-20; 0 = off)
    movers_min_rvol: float = 1.5          # a mover counts only on at least this much relative volume
    buffer_picks_per_sector: int = 2      # new buffer names scanned per sector per cycle
    kept_per_sector: int = 2              # buffer names kept waiting for a hot-list slot
    fundamentals_leaders: int = 8         # valuation setups run on this many top names
    gapper_time: str = "09:15"            # ET, 08:00-09:25 - the pre-open gap check (Aziz's gappers)
    gapper_symbols: int = 400             # at most this many hot-list and buffer names get a pre-market request
    gapper_min_gap_pct: float = 2.0       # a pre-market move this big, either way, counts as a gap
    gapper_min_volume: float = 50_000     # ...on at least this many pre-market shares
    max_universe: int = 0                 # 0 = every listing (smoke tests cap it)
    sectors: list = Field(default_factory=list)
    prefilter: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("mover_atr", mode="after")
    @classmethod
    def _within_the_atrs(cls, value: float) -> float:
        """Past 5 ATRs a minute's candle almost never qualifies; below 0 is off."""
        return max(0.0, min(5.0, value))

    @field_validator("live_scan", mode="after")
    @classmethod
    def _within_the_live_slots(cls, value: int) -> int:
        """The live-scan names take watch-tier slots, which the hot list and the buffers need too."""
        return max(0, min(20, value))


class ValuationCfg(_Model):
    risk_free_rate: float = 0.042
    market_risk_premium: float = 0.055
    tax_rate: float = 0.21
    midyear_convention: bool = True
    perpetuity_growth: float = 0.025
    projection_years: int = 5


class ExecutionCfg(_Model):
    default_order_type: str = "LIMIT"     # LIMIT | MARKET | STOP_LIMIT (regular hours)
    limit_offset_bps: float = 5.0
    time_in_force: str = "DAY"
    bracket_orders: bool = True
    native_stop: bool = True              # on a venue that can hold one (IBKR), keep a good-till-cancelled stop order
                                          # at the broker for every open position, at the trade's working stop - it
                                          # protects the position while the app, the computer or the connection is
                                          # down (execution/protective_stops.py)
    native_target: bool = True            # ...and, in a one-cancels-all group with that stop, a limit order at the
                                          # target: the part that comes off at the first target when the position
                                          # scales out, all of it otherwise. The broker works it on real prices, which
                                          # the app, on delayed quotes, sees fifteen minutes late
    entry_timeout_min: int = 10           # a day-trade entry order not filled in this many minutes is cancelled - a
                                          # fill later, when the price comes back through it, is the move failing
                                          # (Aziz: never chase); 0 = leave it working for the day like a swing entry
    partial_entry_wait_s: float = 30.0    # an entry (day or swing) that filled in part this many seconds ago and is
                                          # still working has the rest cancelled: until the order is done the shares
                                          # bought have no record, so no stop at the broker; the cancel books them and
                                          # the stop goes on in the same pass (0 = wait for the order to finish)
    max_spread_r: float = 0.10            # on live quotes, an entry is refused when the bid-ask spread is more than this
                                          # share of the distance to the stop: the spread is the price of immediacy
                                          # (Harris), paid on the way in and again on the way out (0 = off)
    max_chase_r: float = 0.25             # an entry is refused once the price has run past the play's entry by more
                                          # than this share of the distance to the stop - the reward:risk the play was
                                          # judged on is gone; within it a limit entry is priced off the live quote so
                                          # it fills now (0 = off)
    stream_lines: int = 80                # on proven real-time data, hold IBKR streams for this many stocks - the
                                          # positions and working entries first, then the best plays, then the watch
                                          # tier - and price them off a stream that ticked in the last 2 s instead of
                                          # a snapshot each time. Of the account's ~100 market-data lines, ~20 stay
                                          # free for snapshots (0-90; 0 = off)
    stream_watch: int = 50                # the watch tier: the day's hot list, the kept buffer names and the buffer
                                          # names the cycles sample next, streamed after the plays within
                                          # stream_lines, and checked at each candle close (0-80; 0 = off, as before)

    @field_validator("stream_lines", mode="after")
    @classmethod
    def _within_the_lines(cls, value: int) -> int:
        """IBKR refuses streams past the account's lines, and the snapshots need some of them too."""
        return max(0, min(90, value))

    @field_validator("stream_watch", mode="after")
    @classmethod
    def _within_the_watch(cls, value: int) -> int:
        """The watch tier streams inside stream_lines, behind the positions and the plays."""
        return max(0, min(80, value))


class ExitManagerCfg(_Model):
    enabled: bool = True
    broker_stop_grace_s: float = 10.0     # a stop cross while the trade's stop rests at the broker: that stop gets this
                                          # many seconds to fill before the app stands it down and sends its own exit
                                          # - the broker fills it on real prices, a cancel and a market order cost a
                                          # round trip and a worse fill (0 = the app's exit at once)
    breakeven_at_r: float = 1.3           # tighten the stop once the trade is +this R  (0 = off)
    breakeven_lock_r: float = 0.3         # ...to lock +this R of profit, not a pure scratch
    breakeven_buffer_bps: float = 5.0
    trail_start_r: float = 2.0            # begin trailing past this R  (0 = off)
    trail_lock_ratio: float = 0.5
    flatten_intraday_before_close_min: int = 10
    intraday_time_stop: bool = True       # a day trade past its setup's own window (the play's longest expected
                                          # hold) that isn't working - its stop not yet at break-even - is closed
                                          # then, not left to the close: its reason to be held has run out, and it
                                          # holds a slot and capital a fresh setup could use. One that is working
                                          # keeps its trailing stop until the flatten. The replay does the same
    max_swing_hold_days: int = 10
    scale_out_pct: float = 50.0           # at the first target of a play with two, take this % off (0 = exit all there)
    scale_out_lock_r: float = 0.0         # ...and move the stop to the entry plus this R (Aziz: break-even)


class AutopilotCfg(_Model):
    """Hands-off entry. Exits are already automatic; this lets the bot take the
    entry without a click. Off by default, and paper-only until ``allow_live``."""

    enabled: bool = False
    allow_live: bool = False              # HARD gate: never auto-route real orders unless true
    trade_types: list = Field(default_factory=lambda: ["INTRADAY"])
    strategies: list = Field(default_factory=list)   # the setups Autopilot may take, by key - empty = every setup.
                                          # Unlike the Strategies panel's switch, the others stay on the board to click
    min_confidence: float = 0.5           # day trades: the setup's own conviction. The replay found higher stated
                                          # confidence went with worse trades, so the gate only keeps out the weakest
    min_swing_confidence: float = 0.5     # swing setups state flat, modest confidences (0.55-0.58); their real
                                          # gate is the replay's proof, so this only keeps out the weakest
    min_reward_risk: float = 2.0
    max_auto_positions: int = 2
    max_auto_trades_per_day: int = 3
    max_open_risk_pct: float = 4.0
    max_per_strategy: int = 2
    max_new_per_cycle: int = 1
    max_gross_exposure_pct: float = 100.0  # all positions together, as % of the trading capital (10-100): with
                                          # the whole account and margin that's the broker's buying power, cash only
                                          # the account's value - so 100 = all of it, below 100 keeps a buffer
    min_confirmations: int = 2            # a day-trade setup must show up this many times in a row
    confirm_on_new_candle: bool = True    # ...each time on a newer 5-minute candle, not just another scan: the scans read
                                          # one candle several times over, and the replay's proof enters a day setup once
                                          # it has shown on two candles running. A setup that fires on one candle by its
                                          # nature is then never taken - min_confirmations 1, or this off, brings those
                                          # back. The replay models two at most, so 3 or more asks more than it tested
    min_minutes_to_close: int = 30        # no new day trades with fewer minutes than this to the close: Aziz keeps the
                                          # last half hour for closing, and the exit manager flattens day trades 10
                                          # minutes before the bell, so a late entry has no time to work (0 = off)
    # the flags Autopilot refuses. Only against_gap removed worse trades on both halves of a year's replay;
    # the others removed trades that did as well or better, so they stay visible on the board but aren't
    # skipped. The replay adds the learnable checks that prove themselves (learned skips).
    skip_noise: list = Field(default_factory=lambda: ["against_gap"])
    require_proven: bool = True           # only strategies whose replayed record is good enough
    min_replay_trades: int = 30
    min_replay_expectancy_r: float = 0.05
    skip_replay_losers: str = "day"       # with require_proven off (paper), still skip a setup with evidence it loses:
                                          # its replay averages -replay_loser_r (-0.05R) or worse over min_replay_trades
                                          # and over 10 held-out trades, or its own trades -0.30R or worse over 10.
                                          # off / day / all - day only by default: skipping the swing losers didn't help
                                          # in the replay
    replay_loser_r: float = 0.05          # ...the loss per trade, overall and held out, that counts as losing
    model_mode: str = "shadow"            # the learned model (research/model.py): shadow = its odds are logged with every
                                          # play and never acted on; gate = plays under model_min_p are refused; size =
                                          # gate, and the risk follows the odds (AFML ch. 10). gate and size act only
                                          # while the model's own walk-forward judgement calls it usable
    model_min_p: float = 0.55
    proof_p_value: float = 0.10           # ...and the chance its edge is luck, once every setup tried is allowed for
                                          # (Aronson's reality check on the replayed trades), is this or less; 0 = off
    cooldown_after_loss: bool = True
    max_daily_loss_pct: float = 2.0       # no new entries once today's closed trades have lost this % of equity (0 = off)
    max_giveback_pct: float = 30.0        # ...or once the day's realized gain has given back this % of its peak (0 = off)
    giveback_floor_pct: float = 0.25      # the give-back rule only counts a peak gain of at least this % of equity
    require_catalyst: bool = False
    dry_run: bool = False

    @field_validator("skip_replay_losers", mode="before")
    @classmethod
    def _bare_off(cls, value: Any) -> Any:
        """YAML reads a bare ``off`` (or ``on``) as a boolean, which would stop the app starting."""
        if isinstance(value, bool):
            return "day" if value else "off"
        return value


class NoiseCfg(_Model):
    """What the noise checks count as a bad moment for a setup - see scanner/noise.py."""

    gap_pct: float = 2.0
    volume_ratio: float = 1.5
    volume_bars: int = 6
    min_expected_r: float = 0.15
    trending_hurst: float = 0.55          # a Hurst exponent from here up reads as trending...
    reverting_hurst: float = 0.45         # ...and from here down as mean reverting
    turbulent_probability: float = 0.7    # the market counts as turbulent from this regime probability
    abnormal_z: float = 2.0               # a move this many usual moves beyond what the market explains is the stock's own
    market_model_sessions: int = 60       # the sessions the market model is fitted on
    news_fresh_minutes: float = 60.0      # a stock's news read longer ago than this doesn't count as "no news"
    earnings_ahead_days: int = 5          # a swing play with a report due this many sessions ahead is flagged


class ReplayCfg(_Model):
    """How the strategy replay tests the setups - see research/replay.py."""

    sessions: int = 60                    # day-trade sessions of 5-minute candles (5-120)
    day_stocks: int = 40                  # day-trade setups are replayed, each session, on the stocks in play that
                                          # morning: this many of the hottest by the scan's daily heat as of the
                                          # session before (research/in_play.py). 0 = the old way: today's hot
                                          # list on every session, which hands a momentum setup its own hindsight
    day_gappers: int = 10                 # ...and this many of that morning's biggest gaps among the watchlist
    daily: bool = True                    # run the replay by itself once a day, as soon as the morning's full
                                          # scan has built the watchlist it runs on (about 08:30 ET, an hour
                                          # before the open): the records, the proof rule and the learned model
                                          # are then current for the session without anyone being awake for it
    day_download_minutes: int = 20        # IBKR answers requests for past candles slowly (seconds each), so one
                                          # replay downloads for at most this long, the latest sessions first, and
                                          # replays the stock-days it has; the next replay goes on from there
    swing_sessions: int = 700             # swing sessions of daily candles (20-1250; as far as daily_years reaches)
    daily_years: int = 3                  # years of daily candles kept for the stocks the replay runs on, in
                                          # data/research/daily (1-5; 1 = the live store's year only). One request
                                          # per stock the first time, none after: a year is one market - three
                                          # give the proof rule's statistics something to work with
    swing_stocks: int = 400               # swing setups replayed on this many of the full scan's leaders as well
                                          # as the day's watchlist (0 = the watchlist only); costs no requests
    slippage_bps: float = 5.0             # on every market fill, each way
    commission_bps: float = 1.0           # on every fill
    held_out_fraction: float = 0.3334     # the latest sessions kept out of sample
    workers: int = 0                      # processes replaying stocks side by side; 0 = every core but two
    sessions_per_job: int = 10            # a day-trade job replays this many sessions of one stock, so the work spreads


class PairsCfg(_Model):
    """Pairs trading - see autotradebot/pairs/."""

    enabled: bool = True
    max_pairs: int = 12                   # pairs on the watch list
    per_group: int = 2                    # at most this many from one industry
    min_correlation: float = 0.6
    min_price: float = 5.0
    min_dollar_volume: float = 10_000_000.0
    fit_days: int = 200                   # sessions the hedge ratio and band are fitted on
    test_days: int = 100                  # the latest sessions each pair is tested on, fitted on the ones before
    require_stable: bool = True           # only watch pairs that were already pairs on the sessions before
    half_life_days: list = Field(default_factory=lambda: [2.0, 30.0])
    min_crossings: int = 6
    entry_z: list = Field(default_factory=lambda: [1.0, 2.5])   # the band's bounds, in standard deviations
    exit_z: float = 0.0
    stop_beyond_entry: float = 2.0
    time_stop_half_lives: float = 2.0
    cost_bps: float = 6.0                 # each leg, each fill - for the band and the replay
    window_minutes: list = Field(default_factory=lambda: [30, 5])   # entries and exits are decided in this window before the close
    max_open_pairs: int = 3
    max_new_per_day: int = 2              # Autopilot
    emergency_loss_r: float = 2.0         # close at once when a pair has lost this many times its planned risk
    etf_pairs: list = Field(default_factory=lambda: [["EWA", "EWC"], ["GLD", "GDX"], ["XLE", "XOP"], ["KBE", "KRE"]])


class JournalCfg(_Model):
    """The daily review - see research/journal.py."""

    enabled: bool = True
    review_at: str = "16:15"              # ET, after the close
    keep_days: int = 0                    # 0 = keep every review
    movers: int = 10                      # the session's biggest gainers and losers in the report, each; 0 = none


class SignalsCfg(_Model):
    """Insider trades, company news and headline sentiment - see autotradebot/signals/."""

    enabled: bool = True
    insider_poll_minutes: float = 3.0          # how often SEC's live feed of Form 4 filings is read
    backfill_days: int = 5                     # trading days of Form 4 filings read on the first start
    insider_window_days: int = 30              # insider trades this close together count as one wave
    insider_history_days: int = 365            # how far back "its insiders rarely buy" looks
    buy_floor: float = 25_000.0                # a wave of buying below this is a token
    buy_full: float = 1_000_000.0
    sell_floor: float = 250_000.0
    sell_full: float = 10_000_000.0
    unusual_buying: float = 0.55               # insider score from which buying is unusual
    unusual_selling: float = 0.65
    news_poll_minutes: float = 15.0            # news for the hot list and open positions
    news_lookback_days: int = 3
    sentiment: bool = True                     # score headlines with FinBERT when it is installed
    boost_insider_buying: float = 0.10         # added to a long play's score at an insider score of 1
    boost_insider_selling: float = 0.08        # taken off a long play's score at an insider score of 1
    boost_news: float = 0.06                   # at a news sentiment of +1 or -1
    calendar_hours: float = 6.0                # how often Finnhub's earnings calendar is read (with a key)
    calendar_days_ahead: int = 30              # how far ahead it's read


class DatabaseCfg(_Model):
    echo_sql: bool = False
    pool_size: int = 5
    record_rejected_plays: bool = True


class AppCfg(_Model):
    log_level: str = "INFO"
    keep_awake: bool = True               # keep Windows from sleeping while the app runs (24/7)


class AppConfig(_Model):
    app: AppCfg = Field(default_factory=AppCfg)
    account: AccountCfg = Field(default_factory=AccountCfg)
    risk: RiskCfg = Field(default_factory=RiskCfg)
    scanner: ScannerCfg = Field(default_factory=ScannerCfg)
    strategies: Dict[str, Any] = Field(default_factory=dict)
    valuation: ValuationCfg = Field(default_factory=ValuationCfg)
    execution: ExecutionCfg = Field(default_factory=ExecutionCfg)
    exit_manager: ExitManagerCfg = Field(default_factory=ExitManagerCfg)
    autopilot: AutopilotCfg = Field(default_factory=AutopilotCfg)
    noise: NoiseCfg = Field(default_factory=NoiseCfg)
    replay: ReplayCfg = Field(default_factory=ReplayCfg)
    journal: JournalCfg = Field(default_factory=JournalCfg)
    pairs: PairsCfg = Field(default_factory=PairsCfg)
    signals: SignalsCfg = Field(default_factory=SignalsCfg)
    database: DatabaseCfg = Field(default_factory=DatabaseCfg)


def _load_yaml() -> Dict[str, Any]:
    for name in ("config.yaml", "config.example.yaml"):
        path = CONFIG_DIR / name
        if path.exists():
            with open(path, "r", encoding="utf-8") as fh:
                return yaml.safe_load(fh) or {}
    return {}


class Settings(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    secrets: Secrets
    config: AppConfig

    def strategy_entries(self, family: str) -> Dict[str, Dict[str, Any]]:
        """`family` is 'technical' or 'fundamental'."""
        return dict(self.config.strategies.get(family, {}) or {})


def _apply_env_overrides(cfg: AppConfig) -> None:
    """A few knobs are handy to override without editing config.yaml."""
    if os.getenv("APP_LOG_LEVEL"):
        cfg.app.log_level = os.environ["APP_LOG_LEVEL"]
    if (cap := os.getenv("SCANNER_MAX_UNIVERSE", "")).isdigit():
        cfg.scanner.max_universe = int(cap)
    try:
        cfg.account.paper_start_cash = float(os.environ["PAPER_START_CASH"])
    except (KeyError, ValueError):
        pass
    if os.getenv("SIGNALS_ENABLED"):
        cfg.signals.enabled = os.environ["SIGNALS_ENABLED"].strip().lower() in ("1", "true", "yes", "on")


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    cfg = AppConfig(**_load_yaml())
    _apply_env_overrides(cfg)
    for d in (DATA_DIR, DATA_DIR / "cache", PROJECT_ROOT / "logs"):
        d.mkdir(parents=True, exist_ok=True)
    return Settings(secrets=Secrets(), config=cfg)
