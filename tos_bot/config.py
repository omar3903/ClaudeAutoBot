"""Configuration loading.

Two layers:

* ``.env``            -> secrets and machine-specific values (:class:`Secrets`)
* ``config/config.yaml`` -> tunable behaviour (:class:`AppConfig`, plain dict-ish)

``get_settings()`` returns a cached :class:`Settings` bundle with both.
Everything else in the codebase imports from here, never from ``os.environ``.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

load_dotenv(PROJECT_ROOT / ".env")


# --------------------------------------------------------------------------- #
#  Secrets (.env)                                                             #
# --------------------------------------------------------------------------- #
class Secrets(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    broker: str = "paper"            # "paper" | "schwab" | "ibkr" | "tda" | "crypto"
    live_broker: str = "schwab"      # which live broker the paper<->live toggle targets

    # Schwab
    schwab_api_key: str = ""
    schwab_app_secret: str = ""
    schwab_callback_url: str = "https://127.0.0.1:8182"
    schwab_account_id: str = ""

    # Interactive Brokers (via ib_async -> IB Gateway / TWS; no OAuth token).
    # The Paper/Live toggle picks the port: paper 4002, live 4001 (Gateway).
    ibkr_host: str = "127.0.0.1"
    ibkr_paper_port: int = 4002       # IB Gateway paper  (TWS paper = 7497)
    ibkr_live_port: int = 4001        # IB Gateway live   (TWS live  = 7496)
    ibkr_port: int = 0               # non-zero = force this port for BOTH modes
    ibkr_client_id: int = 11         # any int unique to this app on the Gateway
    ibkr_account_id: str = ""        # DUxxxxxxx (paper) / Uxxxxxxx (live); blank = first
    ibkr_market_data: str = "auto"   # auto | live | delayed | delayed-frozen
    ibkr_readonly: bool = False      # true = connect for data only, never send orders

    # Legacy TDA (reference only)
    tda_api_key: str = ""
    tda_redirect_uri: str = "https://localhost:8182"

    # Token storage
    token_dir: str = "./secrets"
    token_encryption_key: str = ""

    # Database
    database_url: str = ""
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_name: str = "autotradebot"
    db_user: str = "tos"
    db_password: str = ""
    db_allow_sqlite_fallback: bool = True

    # Fundamentals
    fmp_api_key: str = ""
    alphavantage_api_key: str = ""

    # Web
    web_host: str = "127.0.0.1"
    web_port: int = 8787
    open_browser_on_start: bool = True

    # ---- derived helpers -------------------------------------------------- #
    @property
    def effective_live_broker(self) -> str:
        """The live venue the paper<->live toggle targets. ``BROKER`` in .env
        may name it directly; otherwise fall back to ``LIVE_BROKER``."""
        return self.broker if self.broker != "paper" else self.live_broker

    @property
    def token_dir_path(self) -> Path:
        p = Path(self.token_dir)
        return p if p.is_absolute() else PROJECT_ROOT / p

    def token_path_for(self, broker: str) -> Path:
        return self.token_dir_path / f"{broker}.token.json"

    @property
    def token_path(self) -> Path:
        # tokens always belong to the live broker, never to "paper"
        return self.token_path_for(self.effective_live_broker)

    @property
    def token_key_path(self) -> Path:
        return self.token_path.parent / "token_key.bin"

    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        pw = f":{self.db_password}" if self.db_password else ""
        return (
            f"mysql+pymysql://{self.db_user}{pw}@{self.db_host}:{self.db_port}/{self.db_name}"
            "?charset=utf8mb4"
        )

    def sqlite_fallback_url(self) -> str:
        return f"sqlite:///{(PROJECT_ROOT / 'data' / 'autotradebot.sqlite').as_posix()}"


# --------------------------------------------------------------------------- #
#  App config (YAML) -- typed just enough to be safe, permissive elsewhere    #
# --------------------------------------------------------------------------- #
class _Model(BaseModel):
    model_config = {"extra": "allow"}


class AccountCfg(_Model):
    min_start_equity: float = 2000.0          # LIVE only - paper ignores this
    paper_start_cash: float = 100000.0        # opening balance for the paper account
    pdt_equity_threshold: float = 25000.0
    max_day_trades_under_threshold: int = 3
    day_trade_warn_at: int = 2
    cash_account: bool = False


class RiskCfg(_Model):
    max_risk_per_trade_pct: float = 1.0
    max_open_risk_pct: float = 4.0
    max_positions: int = 5
    max_position_pct_of_equity: float = 35.0
    default_stop_atr_mult: float = 1.5
    min_reward_risk: float = 1.5
    round_lot: int = 1


class ScannerCfg(_Model):
    interval_seconds: int = 300
    # When Autopilot is armed for day trades and the regular session is open,
    # the scanner runs on this faster cadence instead (self-throttled so a new
    # cycle never starts before the previous one finishes + a small gap).
    autopilot_interval_seconds: int = 45
    min_interval_seconds: int = 20        # absolute floor, never scan faster than this
    universe: str = "nasdaq100"
    universe_file: str = "config/watchlist.txt"
    shortlist_size: int = 8
    max_symbols_scanned: int = 110
    fundamentals_leaders: int = 8
    prefilter: Dict[str, Any] = Field(default_factory=dict)
    bars: Dict[str, Any] = Field(default_factory=dict)


class ValuationCfg(_Model):
    risk_free_rate: float = 0.042
    market_risk_premium: float = 0.055
    tax_rate: float = 0.21
    midyear_convention: bool = True
    terminal_method: str = "both"
    projection_method: str = "last_year"


class AuthCfg(_Model):
    refresh_token_ttl_days: int = 60
    rotate_before_days: int = 5
    access_refresh_margin_seconds: int = 120
    check_interval_seconds: int = 1800
    auto_reauth: str = "notify"
    backup_old_tokens: bool = True


class ExecutionCfg(_Model):
    default_order_type: str = "LIMIT"     # LIMIT | MARKET | STOP_LIMIT (regular hours)
    limit_offset_bps: float = 5.0
    time_in_force: str = "DAY"
    bracket_orders: bool = True
    confirm_required: bool = True
    allow_extended_hours: bool = True     # let pre/post-market entries through at all


class ExitManagerCfg(_Model):
    enabled: bool = True
    breakeven_at_r: float = 1.3           # tighten the stop once the trade is +this R  (0 = off)
    breakeven_lock_r: float = 0.3         # ...to lock +this R of profit, not a pure scratch
    breakeven_buffer_bps: float = 5.0     # plus this nudge past the lock point
    trail_start_r: float = 2.0            # begin trailing past this R  (0 = off)
    trail_lock_ratio: float = 0.5        # keep the stop at this fraction of open R
    flatten_intraday_before_close_min: int = 10   # close day trades before the bell
    max_swing_hold_days: int = 10        # force-close stale swings (0 = off)


class AutopilotCfg(_Model):
    """Hands-off entry. Exits are ALREADY automatic (see ExitManagerCfg); this
    is the switch that also lets the bot take the *entry* without your click.
    Off by default, and paper-only until ``allow_live`` is deliberately set."""

    enabled: bool = False                 # master switch (also toggled from the UI)
    allow_live: bool = False              # HARD gate: never auto-route real orders unless true
    trade_types: list = Field(default_factory=lambda: ["INTRADAY"])  # INTRADAY and/or SWING
    min_confidence: float = 0.62          # skip plays the strategy isn't sure about
    min_reward_risk: float = 2.0          # Aziz Rule 5 - never auto-take worse than 2:1
    max_auto_positions: int = 2           # concurrent open autopilot trades
    max_auto_trades_per_day: int = 3      # matches the sub-$25k PDT day-trade cap
    max_open_risk_pct: float = 4.0        # sum of open autopilot $-risk vs equity
    # anti-concentration / anti-chop - no pile-on in one strategy, no burst of
    # entries, no name flipped long<->short minutes apart
    max_per_strategy: int = 2            # concurrent open auto trades from ONE strategy key
    max_new_per_cycle: int = 1          # new auto entries per scan cycle (no bursts)
    cooldown_after_loss: bool = True    # don't re-enter a name that stopped out earlier today
    block_sectors: list = Field(default_factory=list)  # e.g. ["Energy"] to sit out a sector
    require_catalyst: bool = False        # only auto-take plays tagged "catalyst"/"gap"
    dry_run: bool = False                 # log what it WOULD do, place nothing


class DatabaseCfg(_Model):
    echo_sql: bool = False
    pool_size: int = 5
    record_rejected_plays: bool = True


class AppCfg(_Model):
    timezone: str = "America/New_York"
    mode: str = "suggest"
    log_level: str = "INFO"


class AppConfig(_Model):
    app: AppCfg = Field(default_factory=AppCfg)
    account: AccountCfg = Field(default_factory=AccountCfg)
    risk: RiskCfg = Field(default_factory=RiskCfg)
    scanner: ScannerCfg = Field(default_factory=ScannerCfg)
    strategies: Dict[str, Any] = Field(default_factory=dict)
    valuation: ValuationCfg = Field(default_factory=ValuationCfg)
    auth: AuthCfg = Field(default_factory=AuthCfg)
    execution: ExecutionCfg = Field(default_factory=ExecutionCfg)
    exit_manager: ExitManagerCfg = Field(default_factory=ExitManagerCfg)
    autopilot: AutopilotCfg = Field(default_factory=AutopilotCfg)
    database: DatabaseCfg = Field(default_factory=DatabaseCfg)
    web: Dict[str, Any] = Field(default_factory=dict)


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
    project_root: Path = PROJECT_ROOT

    def strategy_entries(self, family: str) -> Dict[str, Dict[str, Any]]:
        """`family` is 'technical' or 'fundamental'."""
        return dict(self.config.strategies.get(family, {}) or {})


def _apply_env_overrides(cfg: AppConfig) -> None:
    """A few knobs are handy to override without editing config.yaml."""
    uni = os.getenv("SCANNER_UNIVERSE")
    if uni:
        cfg.scanner.universe = uni
    mx = os.getenv("SCANNER_MAX_SYMBOLS")
    if mx and mx.isdigit():
        cfg.scanner.max_symbols_scanned = int(mx)
    iv = os.getenv("SCANNER_INTERVAL_SECONDS")
    if iv and iv.isdigit():
        cfg.scanner.interval_seconds = int(iv)
    if os.getenv("APP_LOG_LEVEL"):
        cfg.app.log_level = os.environ["APP_LOG_LEVEL"]
    pc = os.getenv("PAPER_START_CASH")
    if pc:
        try:
            cfg.account.paper_start_cash = float(pc)
        except ValueError:
            pass


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    cfg = AppConfig(**_load_yaml())
    _apply_env_overrides(cfg)
    s = Settings(secrets=Secrets(), config=cfg)
    # make sure state dirs exist
    for d in ("data", "data/cache", "logs", "secrets"):
        (PROJECT_ROOT / d).mkdir(parents=True, exist_ok=True)
    return s


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
