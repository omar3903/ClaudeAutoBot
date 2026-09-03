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

    broker: str = "paper"            # "paper" | "schwab" | "tda" | "ibkr" | "crypto"
    live_broker: str = "schwab"      # which live broker the paper<->live toggle targets

    # Schwab
    schwab_api_key: str = ""
    schwab_app_secret: str = ""
    schwab_callback_url: str = "https://127.0.0.1:8182"
    schwab_account_id: str = ""

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
    db_name: str = "tos_trader"
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
        return f"sqlite:///{(PROJECT_ROOT / 'data' / 'tos_trader.sqlite').as_posix()}"


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
    breakeven_at_r: float = 1.0           # move stop to entry once +1R  (0 = off)
    breakeven_buffer_bps: float = 5.0     # nudge it just past entry
    trail_start_r: float = 1.5            # begin trailing past this R  (0 = off)
    trail_lock_ratio: float = 0.5        # keep the stop at this fraction of open R
    flatten_intraday_before_close_min: int = 10   # close day trades before the bell
    max_swing_hold_days: int = 10        # force-close stale swings (0 = off)


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
