# tos-trader

A **broker-agnostic, human-in-the-loop** trading assistant for a small account.

It scans the Nasdaq every few minutes, ranks a short list of names, and shows
**long / short "plays"** with a plain-English explanation on hover. Nothing is
ever routed to a broker until you click **Execute ✓ Yes**. Every idea and every
executed trade (with its realised P/L) is written to MySQL.

- **Paper ↔ Live toggle:** flip the whole app between a **paper** account
  (simulated fills against *real* market data, starts at **$100,000**, no
  equity floor) and **live** Schwab from a switch in the dashboard header —
  no restart, no `.env` edit. Schwab has no sandbox of its own, so this is
  how you paper-trade a Schwab workflow.
- **Data + brokerage:** a `BrokerAdapter` abstraction with a live **Schwab**
  adapter (`schwab-py`), a legacy `tda-api` reference adapter, **Interactive
  Brokers** and **crypto** stubs, and the **paper** adapter above.
- **Strategies:** 8 technical day-trade / swing setups + 3 valuation setups
  derived from *Pignataro, Financial Modeling and Valuation* (2nd ed.): comps
  (EV/EBITDA vs peers), a UFCF DCF (CAPM/WACC, exit-multiple **and** perpetuity
  terminal value), and a blended "football-field" fair-value band.
- **Guard rails (live mode):** a $2,000 equity floor and a rolling 5-session
  Pattern-Day-Trader counter (3-day-trade cap under $25k) that block or warn
  *before* you confirm. In paper mode nothing is blocked — the counters are
  still shown so you learn where the live rules would bite.
- **Auth:** a token watchdog that keeps the access token fresh and, ~5 days
  before the refresh token's TTL (default **60 days**), backs up + deletes the
  old token and triggers a new grant — either a one-click prompt or an
  unattended headless flow.
- **UI:** a local web dashboard (FastAPI + WebSocket) at `http://127.0.0.1:8787`.

> ⚠️ **Not investment advice. Not audited. Trade paper first.** Markets can and
> will lose you money faster than any backtest suggests.

---

## ⚠️ Read this first: `tda-api` is end-of-life

Your PDF documents `tda-api`, which wraps the **TD Ameritrade Developer API**.
After Charles Schwab acquired TD Ameritrade, that platform was shut down —
`api.tdameritrade.com` no longer issues OAuth tokens. The maintained successor,
by the same author, is **[`schwab-py`](https://github.com/alexgolec/schwab-py)**,
and its call surface mirrors `tda-api` almost line for line
(`easy_client`, `get_price_history_every_five_minutes`, `place_order` + order
templates, …).

This project therefore:

- puts everything behind `tos_bot/brokers/base.py::BrokerAdapter`;
- ships **`schwab_adapter.py`** as the live path (`BROKER=schwab`);
- keeps **`tda_adapter.py`** as a reference that still matches your docs, in
  case you have confirmed legacy access (`BROKER=tda`);
- ships **`paper_adapter.py`** so you can run the whole thing today with no
  broker at all (`BROKER=paper`, the default).

---

## Quick start (paper, no credentials)

```bash
python -m venv .venv && . .venv/Scripts/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                    # BROKER=paper is the default
cp config/config.example.yaml config/config.yaml
python run.py
```

The dashboard opens at `http://127.0.0.1:8787` in **Paper** mode with a
**$100,000** simulated account. Click **Scan now**, hover a row to read the
play, click it to see the order preview (it says *Routes to: SIMULATED
(paper)*), then **Execute ✓ Yes** to fill it against the live quote. Close
positions from the **Open positions** tab; realised P/L lands in **Trade
history** and **P/L summary**. **Reset paper** in the header wipes the paper
cash/positions back to a balance you choose (trade history is kept).

The paper account **persists** to `data/paper_state.json`, so it keeps
tracking across restarts.

**Data:** if you add the `SCHWAB_*` keys and authenticate, the app uses
Schwab real-time quotes/candles **even in paper mode**. Otherwise it uses
`yfinance` (delayed ~15 min) or, if that isn't installed, a deterministic
synthetic feed. The header shows which (`data: schwab` / `yfinance` /
`synthetic`).

### Paper ↔ Live switch

The header has a **Paper / Live** toggle. **Live** is greyed until the
`SCHWAB_*` block is filled and `python scripts/authenticate.py` has run;
switching to it pops a confirmation and, from then on, approved orders go to
your **real Schwab account** and the $2,000 floor + PDT cap apply. The choice
is remembered in `data/runtime.json`.

With no MySQL configured, everything above still works — the database falls
back to a local SQLite file (`data/tos_trader.sqlite`).

---

## Going live

### 1. Schwab developer app

1. Create an app at <https://developer.schwab.com> (the *Trader API* product).
2. Add **`https://127.0.0.1:8182`** as an allowed callback URL.
3. Put the key/secret in `.env`:

   ```dotenv
   BROKER=schwab
   SCHWAB_API_KEY=...
   SCHWAB_APP_SECRET=...
   SCHWAB_CALLBACK_URL=https://127.0.0.1:8182
   SCHWAB_ACCOUNT_ID=12345678          # plain account number; the hash is resolved at runtime
   ```
4. `pip install schwab-py`
5. Mint the token once (opens a browser):

   ```bash
   python scripts/authenticate.py
   ```

### 2. MySQL

```sql
CREATE DATABASE tos_trader CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'tos'@'%' IDENTIFIED BY 'your-password';
GRANT ALL PRIVILEGES ON tos_trader.* TO 'tos'@'%';
FLUSH PRIVILEGES;
```

```dotenv
DB_HOST=127.0.0.1
DB_PORT=3306
DB_NAME=tos_trader
DB_USER=tos
DB_PASSWORD=your-password
DB_ALLOW_SQLITE_FALLBACK=0            # set 0 in production so a DB outage is loud
```

```bash
python scripts/init_db.py            # creates all tables + indexes
```

`tos_bot/persistence/schema.sql` has the raw DDL if you prefer to apply it by hand.

### 3. Run

```bash
python run.py --host 127.0.0.1 --port 8787
```

---

## The 60-day token rotation

The brief: *"delete and get the authentication token every 60 days
automatically with little to no manual handling."*

`tos_bot/auth/token_manager.py` + `AuthWatchdog`:

| what | how |
|---|---|
| access token (minutes-long) | refreshed automatically by the SDK; the watchdog pings on a timer and logs `REFRESH` |
| refresh token age | read from the token file / a sidecar `*.meta.json` written at each full auth |
| rotation | when age ≥ `refresh_token_ttl_days − rotate_before_days`: timestamped **backup → delete** the token file → new grant |
| new grant | `auth.auto_reauth: notify` → dashboard banner + `POST /api/auth/reauth` (one click); or `headless` → `scripts/reauth_headless.py` (Playwright + OS keyring, **opt-in**) |
| audit | every event goes to the `token_audit` MySQL table |

Set the TTL to your broker's real value in `config/config.yaml`:

```yaml
auth:
  refresh_token_ttl_days: 60     # Schwab's real refresh token is ~7 days; TDA was 90
  rotate_before_days: 5
  auto_reauth: notify            # or: headless
```

> **Security:** this project never asks for or stores your brokerage password.
> Plain-text credential entry is out of scope by design. The headless path is
> a script **you** own that reads **your** OS keyring; many brokers also force
> 2FA, in which case keep `auto_reauth: notify`.

**Unattended check on Windows** (Task Scheduler, every 6 h):

```bat
schtasks /Create /TN "tos-trader token" /SC HOURLY /MO 6 ^
  /TR "\"%CD%\.venv\Scripts\python.exe\" \"%CD%\scripts\authenticate.py\" --check" /F
```

(`--check` prints status and exits; the live dashboard's watchdog does the real
work while it is running.)

---

## $2,000 floor & the Pattern-Day-Trader rule  (live mode only)

`tos_bot/risk/pdt_guard.py` runs on every **Assess** before you can confirm.
**In paper mode nothing below is enforced** — the day-trade tally is still
shown, with a "(paper)" note, so you can see where live would stop you.

- **Equity floor** — if account equity `< min_start_equity` ($2,000), *no new
  entries*, full stop.
- **PDT** — FINRA flags a *pattern day trader* at **4 day trades in 5 business
  days** on a **margin** account; flagged accounts must hold **$25,000**. Below
  that line you get **3 day trades per rolling 5 sessions**. The guard counts
  closed same-session round-trips (plus still-open intraday trades opened today)
  from the `trades` table and **blocks the 4th**, warning from the 2nd–3rd.
- Every **intraday** play is treated as a *potential* day trade (conservative).
  Mark a setup `SWING` (hold overnight) and it no longer counts.
- **Cash account** (`account.cash_account: true`) — PDT does not apply, but the
  guard warns about T+1 settlement / good-faith violations.

Thresholds live in `config/config.yaml → account`.

---

## Strategy catalogue

Toggle each one and tune its params/weight in `config/config.yaml → strategies`.
Hover any play in the UI for its thesis; the **Strategies** button lists them all.

### Technical (`tos_bot/strategies/technical.py`)

| key | timeframe | idea |
|---|---|---|
| `opening_range_breakout` | intraday | break of the first N-min high/low, above/below VWAP, on relative volume |
| `vwap_reclaim` | intraday | reclaim / loss of session VWAP after time on the other side |
| `ema_pullback_trend` | intraday | first pullback to the 20-EMA inside a 9/20/50 stacked trend |
| `rsi2_mean_reversion` | swing | Connors RSI(2) < 10 above the 200-SMA (long) / > 90 below it (short) |
| `bollinger_fade` | swing | close outside the 2σ band while ADX < 20 (range regime) |
| `atr_channel_breakout` | swing | close beyond a Keltner/ATR channel with ADX rising through 20 |
| `gap_and_go` | intraday | ≥ 2 % gap holding the right side of the opening VWAP on ≥ 2× volume |
| `week52_breakout` | swing | push to a new 52-week high/low on volume expansion |

### Valuation — from *Pignataro, Financial Modeling and Valuation* (2nd ed.)

`tos_bot/valuation/` + `tos_bot/strategies/fundamental.py`:

| key | book chapter | idea |
|---|---|---|
| `relative_value_comps` | Ch. 8 & 10 | EV/EBITDA, P/E, EV/Sales vs the **peer median**. Consistently cheaper ⇒ long; richer ⇒ short. Target = re-rate to the peer multiple. |
| `dcf_fair_value_gap` | Ch. 9 | project UFCF (`NI + D&A + deferred tax + SBC + ΔWC − CapEx + A/T net interest`), discount at **WACC** (CAPM cost of equity `rf + β·MRP`), terminal value **both** ways — exit multiple **and** Gordon perpetuity `UFCF·(1+g)/(WACC−g)` — mid-year convention. Blended per-share vs price, beyond a margin of safety. |
| `valuation_football_field` | Ch. 12 | overlay 52-week range + comps range + both DCF ranges into one low/high band. Price below the band ⇒ long to the band edge / weighted fair value; above ⇒ short. |

The seven projection methods (Ch. 1) are in `tos_bot/valuation/projections.py`.

> The book is a valuation text, not a technical-trading book — so the technical
> setups above are standard market-structure practice, and the *fundamental
> overlay* is what comes from Pignataro. Fundamental data is from `yfinance`
> (free, occasionally patchy); every field is optional and the engine degrades
> gracefully. Drop in a paid provider behind `FundamentalsProvider` later.

---

## Scanning "all of the Nasdaq"

`tos_bot/scanner/scanner.py`:

1. loads the full symbol list from the Nasdaq Trader directory (cached daily;
   bundled Nasdaq-100 fallback offline);
2. each cycle takes a **rotating slice** of `scanner.max_symbols_scanned`
   symbols, so the whole universe is covered over several cycles without
   hammering the data provider;
3. cheap **pre-filter** (price band, 20-day $-volume, ATR %, spread);
4. survivors get intraday bars + the technical strategies;
5. the leaders additionally get fundamentals + peers + the valuation strategies;
6. every play is position-sized, ranked by a blended score, and the top
   `shortlist_size` names become the focus list.

Tune cadence and size in `config/config.yaml → scanner`, or override without
editing it: `SCANNER_UNIVERSE`, `SCANNER_MAX_SYMBOLS`, `SCANNER_INTERVAL_SECONDS`.

---

## Adding a broker later

Implement `tos_bot/brokers/base.py::BrokerAdapter` and register it in
`tos_bot/brokers/__init__.py::get_broker`. Stubs already map the interface:

- **`ibkr_adapter.py`** → `ib_async` against TWS / IB Gateway. No OAuth token,
  so the token watchdog is a no-op.
- **`crypto_adapter.py`** → `ccxt` (Coinbase / Kraken / …). 24/7 market, no PDT,
  no token; `AssetClass.CRYPTO` bypasses `PdtGuard`.

---

## Project layout

```
run.py                     boot engine + dashboard
scripts/
  init_db.py               create the MySQL schema
  authenticate.py          one-time / re-auth broker OAuth  (--check, --force-rotate)
  reauth_headless.py       unattended re-auth (Playwright + keyring, opt-in)
  run_scan_once.py         one scan cycle from the CLI
tos_bot/
  config.py                .env + config.yaml loader
  engine.py                the conductor (scan / sync / snapshot loops)
  core/                    enums + framework-free dataclasses + event bus
  indicators/ta.py         vectorised TA (no TA-Lib)
  data/                    universe loader, market data (broker→yfinance→synthetic), fundamentals
  valuation/               EV, multiples, DCF, football field, projections   (Pignataro)
  strategies/              base + registry + technical.py + fundamental.py
  scanner/                 pre-filter + cycle orchestration
  risk/                    position sizing + PDT guard
  execution/               order builder + executor (fills → trade log)
  auth/token_manager.py    access-token refresh + 60-day rotation + audit
  brokers/                 base + paper + schwab + tda + ibkr/crypto stubs
  persistence/             SQLAlchemy models, repository, schema.sql
  server/app.py            FastAPI REST + WebSocket
  web/                     the dashboard (index.html / styles.css / app.js)
tests/                     pytest  (fast by default; `-m slow` for the e2e)
```

## Tests

```bash
pytest              # fast unit tests
pytest -m slow      # boots the engine, runs scan → approve → close on paper
```

## Roadmap

- real-time streaming quotes (Schwab streamer / IBKR ticks) instead of REST polls
- per-strategy backtester + walk-forward on the persisted play log
- options plays (the Schwab adapter already exposes the option-chain endpoint)
- MAE/MFE capture on open trades; equity-curve chart in the dashboard
- IBKR and crypto adapters fleshed out
