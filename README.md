# AutoTradeBot

A **broker-agnostic, human-in-the-loop** trading assistant for a small account
(TD Ameritrade / thinkorswim &rarr; Schwab, with Interactive Brokers and crypto
adapters stubbed in).

It scans the Nasdaq every few minutes, ranks a short list of names, and shows
**long / short "plays"** with a plain-English explanation on hover and the
stock's **sector** next to the ticker. By default nothing is routed to a broker
until you click **Execute ✓ Yes**; an optional **Autopilot** toggle lets the
bot take the entry too, inside hard caps (paper-only until you deliberately
allow live). Every open trade — clicked or auto — then runs an **automatic exit
strategy** (stop / target / break-even / trailing / end-of-day flatten) with no
further input, and carries an **expected time-to-exit** so a position that
overstays its welcome gets flagged for a manual look. Every idea and every
executed trade (with its realised P/L) is written to MySQL or a local SQLite
file.

The play explanations and the day-trade / swing recognition are modelled on
four books: **Aziz, *How to Day Trade for a Living*** (the intraday setups and
"stocks in play" filters), **Douglas, *Trading in the Zone*** (every play is
framed as *an edge with a probability*, not a prediction), **Murphy, *Technical
Analysis of the Financial Markets*** (horizontal S/R, oscillator divergence,
volume confirmation) and **Pignataro, *Financial Modeling and Valuation*** (the
DCF / comps overlay).

- **Paper ↔ Live toggle:** flip the whole app between **paper** and **live**
  from a switch in the dashboard header — no restart, no `.env` edit. With
  **Interactive Brokers** as the venue, paper mode routes to your real IBKR
  **paper account** (port 4002) and live to your real account (4001); the
  built-in $100k simulator is the offline fallback.
- **Data + brokerage:** a `BrokerAdapter` abstraction with a full **Interactive
  Brokers** adapter (`ib_async` → IB Gateway / TWS, live *and* paper, real
  exchange quotes/volume/history, native bracket orders, auto-reconnect on the
  daily Gateway restart), a **Schwab** adapter (`schwab-py`), a legacy
  `tda-api` reference adapter, a **crypto** stub, and the **paper** simulator.
  IBKR has **no OAuth token / no 60-day expiry** — "auth" is the Gateway
  session; hands-off login is [IBC](https://github.com/IbcAlpha/IBC).
- **Strategies:** 13 technical day-trade / swing setups (Aziz's ABCD, bull/bear
  flag, VWAP, opening-range, red-to-green, top/bottom reversal, horizontal S/R;
  Murphy/Connors divergence & mean-reversion) + 3 valuation setups from
  *Pignataro* (comps, a UFCF DCF with exit-multiple **and** perpetuity terminal
  value, a blended "football-field" band). Intraday setups are **weighted by
  time of day** — momentum fades at midday, reversals hold up, trends improve
  into the close.
- **Autopilot (hands-off entry):** a header toggle that lets the bot place the
  entry itself for plays that clear a strict gate (your chosen trade types, a
  confidence floor, ≥ 2:1 reward:risk, per-day and concurrent-position caps,
  aggregate open-risk cap). **Paper-only** until you set
  `autopilot.allow_live: true` in `config.yaml`. Exits are automatic either way.
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

**Data:** with a running IB Gateway (or the `SCHWAB_*` keys) the app uses that
venue's real quotes/candles **in both modes**. Otherwise it uses `yfinance`
(delayed ~15 min) or, if that isn't installed, a deterministic synthetic feed.
The header shows which (`data: broker:ibkr` / `schwab` / `yfinance` /
`synthetic`), and whether the IBKR feed is real-time or `delayed` (no
market-data subscription).

### Paper ↔ Live switch

The header has a **Paper / Live** toggle. With `LIVE_BROKER=ibkr` it maps to
the Gateway port (**paper 4002 ↔ live 4001**); paper is your real IBKR paper
account, live is your real account. **Live** is greyed until that venue is
reachable; switching to it pops a confirmation and, from then on, approved
orders are real and the $2,000 floor + PDT cap apply. The choice is remembered
in `data/runtime.json`.

With no MySQL configured, everything above still works — the database falls
back to a local SQLite file (`data/tos_trader.sqlite`).

---

## Going live

### Option A — Interactive Brokers (recommended)

IBKR gives you real exchange data and a real paper account through the same
API. There is **no token and no 60-day expiry** — the API is a socket into a
running **IB Gateway** (or TWS); IBKR force-restarts that once a day, and
**[IBC](https://github.com/IbcAlpha/IBC)** makes the re-login hands-off. The
adapter watches the socket and **auto-reconnects**, so an IBC restart is
invisible; if the Gateway is genuinely down the header shows it.

```bash
pip install ib_async
python scripts/ibkr_setup.py --guide     # the full walkthrough
python scripts/ibkr_setup.py             # probe ports, connect read-only, report
```

1. **Client Portal → Settings** → enable your **paper account** (`DU…`). Tick
   *"Share real-time market data subscriptions with paper account"*.
2. **Market data** (optional): *Settings → Market Data Subscriptions* → e.g.
   *US Securities Snapshot Bundle* (~$10/mo). Without it the bot runs on
   15-minute delayed data and labels the feed `delayed`.
3. **IB Gateway** → *Configure → Settings → API → Settings*: enable
   *ActiveX and Socket Clients*, socket port **4002** (paper) / **4001**
   (live), Trusted IP `127.0.0.1`, untick *Read-Only API*. Then
   *Configure → Lock and Exit → Auto restart*.
4. **IBC**: set `IbLoginId` / `IbPassword` / `TradingMode=paper` in its
   `config.ini`, launch `StartGateway.bat`, and add a Windows "run at logon"
   task so it survives reboots.
5. `.env`:

   ```dotenv
   BROKER=paper
   LIVE_BROKER=ibkr
   IBKR_HOST=127.0.0.1
   IBKR_PAPER_PORT=4002
   IBKR_LIVE_PORT=4001
   IBKR_CLIENT_ID=11
   # IBKR_ACCOUNT_ID=DU1234567   # only if the login has several accounts
   # IBKR_MARKET_DATA=auto       # auto | live | delayed | delayed-frozen
   # IBKR_READONLY=0             # 1 = data only, never send orders
   ```

The Paper/Live toggle now switches ports for you. The header's session pill
shows `connected` / `reconnecting` / `Gateway not reachable` instead of a
token status.

### Option B — Schwab

#### 1. Schwab developer app

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

### MySQL (optional — SQLite is the default)

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

### Run

```bash
python run.py --host 127.0.0.1 --port 8787
```

---

## The 60-day token rotation (Schwab / TDA only)

> **Interactive Brokers has no token and no 60-day clock** — its "auth" is the
> IB Gateway login, which IBKR restarts daily. IBC handles the re-login; the
> adapter auto-reconnects the socket. None of this section applies to
> `LIVE_BROKER=ibkr`.

The brief: *"delete and get the authentication token every 60 days
automatically with little to no manual handling."* (Schwab's real refresh
token is ~7 days, so the watchdog rotates well inside that.)

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
schtasks /Create /TN "AutoTradeBot token" /SC HOURLY /MO 6 ^
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

## Market sessions, order types & holidays

`tos_bot/util/clock.py` knows the four sessions and the NYSE calendar
(full-day holidays **and** 1:00 pm half-days) through 2028.

* **Session-aware execution.** You can only send an order when a session can
  accept it:
  * **Regular hours** → the order type from `execution.default_order_type`
    (`LIMIT` / `MARKET` / `STOP_LIMIT`), with a native broker OCO bracket.
  * **Pre-market / after-hours** → **limit only**, and only for setups flagged
    extended-hours-eligible (all swing/valuation setups, plus `gap_and_go`).
    No native stop is possible, so the auto exit manager holds it instead.
  * **Closed** (overnight / weekend / holiday) → execution is blocked; the
    dashboard shows the exact reason and when the market next opens.
* Every play's order card shows the **concrete order** — "LIMIT (regular
  hours)", "pre-market limit", "after-hours limit", "MARKET", "STOP_LIMIT" —
  with the limit price, stop trigger, TIF and protection mode.
* The header pill shows the live status ("Pre-market — closes into regular at
  09:30 ET", "Closed — Thanksgiving Day", "Regular hours (half day)"), and
  `GET /api/market` returns the full breakdown incl. the next holiday.

---

## Automatic exit strategy

`tos_bot/execution/exit_manager.py` runs every few seconds on every open
trade — **entries need your click, exits never do**:

| rule | default | config key |
|---|---|---|
| cut losses at the working stop | on | — |
| take profit at the target | on | — |
| tighten the stop to **lock a small profit** once green | at +1.3 R, lock +0.3 R | `breakeven_at_r`, `breakeven_lock_r` |
| **trail** the stop, keeping a fraction of the open R | from +2.0 R, lock 50% | `trail_start_r`, `trail_lock_ratio` |
| **flatten day trades** before the (holiday-aware) close | 10 min before | `flatten_intraday_before_close_min` |
| force-close **stale swings** | 10 days | `max_swing_hold_days` |

> The break-even used to snap to entry at +1 R; a live run showed that killing
> a trade that had run +1.2 R and then wobbled. It now waits for +1.3 R and
> tightens to *+0.3 R locked* rather than a pure scratch, and trailing starts
> later (+2 R).

The stop only ever ratchets in your favour and never through the last price.
R is always measured against the **original** stop (`trades.initial_stop_price`),
not a trailed one. Each open position has an **Auto exit** toggle in the
blotter (`POST /api/trades/{id}/managed`) if you want to hand-manage one.
Tune everything in `config/config.yaml → exit_manager` (`enabled: false`
turns it off entirely).

### Expected time-to-exit (overwatch)

Every strategy declares how long its trade *should* take — minutes for
intraday setups, trading days for swing/valuation ones (`Strategy.expected_hold`,
overridable per strategy with `params.hold_typical` / `hold_max`). At fill,
that's turned into `expected_exit_at` and `overwatch_at` on the trade (via the
trading-day calendar, capped at the close for day trades).

The blotter shows an **Age / Expected** bar per position — green while on
track, amber once past the typical hold (**aging**), red once past the review
threshold (**⏰ overdue**) — and you get a one-time toast when a trade goes
overdue, noting its current R so you can tell "let it run" from "get out". This
is **purely informational**: it never moves the stop or closes anything.

---

## Autopilot — hands-off entry

`tos_bot/execution/autopilot.py`. The exit side is *already* automatic (above);
Autopilot is the switch that also lets the bot take the **entry** with no click.
After each scan it walks the fresh plays and, for any that clear the gate, calls
the exact same approve/execute path as the **Execute ✓ Yes** button.

Toggle and tune it from the header (**Autopilot: off / day / day+swing**, plus a
⚙ settings pop-up). Defaults live in `config/config.yaml → autopilot`:

| gate | default | key |
|---|---|---|
| master switch | off | `enabled` (UI toggle) |
| **route real orders** | **off** | `allow_live` — *config-file only*; with it off, the UI toggle arms Autopilot for **paper only** even in Live mode, and says so |
| which trade types it may take | `["INTRADAY"]` | `trade_types` (Day / Swing checkboxes) |
| minimum strategy confidence | 0.62 | `min_confidence` |
| minimum reward : risk | 2.0 | `min_reward_risk` (Aziz Rule 5) |
| concurrent open auto positions | 2 | `max_auto_positions` |
| auto trades per session | 3 | `max_auto_trades_per_day` (≈ the sub-$25k PDT cap) |
| aggregate open auto $-risk | 4 % of equity | `max_open_risk_pct` |
| concurrent auto trades from **one** strategy | 2 | `max_per_strategy` |
| new auto entries **per scan cycle** | 1 | `max_new_per_cycle` |
| **cool off** a ticker after it stops out today | on | `cooldown_after_loss` |
| sit out a sector / require a catalyst / dry-run | off | `block_sectors`, `require_catalyst`, `dry_run` |

It still passes through every existing check — session validity, position
sizing, the PDT guard, the $2,000 live floor. Fundamental (valuation) plays are
never auto-traded. Every decision is on the event bus
(`autopilot.entered` / `.skipped` / `.blocked`) and eligible plays get a
**🤖** marker in the table. `GET`/`POST /api/autopilot`.

> `max_per_strategy`, `max_new_per_cycle` and `cooldown_after_loss` were added
> after a 7-hour run went **1-for-5**: every auto trade was the *same* strategy
> (`sr_bounce`), five positions opened inside a 10-minute mid-day chop, and two
> tickers were entered long *then* short minutes apart. They stop the pile-on.

**Faster loop while day-trading.** When Autopilot is armed with `INTRADAY` in
`trade_types` *and* the regular session is open, the engine drops from the
normal `scanner.interval_seconds` (300) to `scanner.autopilot_interval_seconds`
(45) — self-throttled so a new scan never starts before the previous cycle
finished (+3 s) and never below `scanner.min_interval_seconds` (20). The account
also re-syncs every ~10 s instead of ~30 s in this mode. The header button shows
the live cadence (`Autopilot: day ⚡45s`). Outside those conditions everything
returns to the normal cadence.

---

## Dashboard controls

* **↻ Refresh** — re-pull account, positions and fills right now
  (`POST /api/account/refresh`).
* **Autopilot: off / day / …** + **⚙** — turn hands-off entry on/off and pick
  trade types, confidence floor, and caps (see the Autopilot section above).
* **Reconcile** (paper only) — rebuild the paper broker's positions from the
  open trades in the database, e.g. after a mis-click
  (`POST /api/paper/reconcile`).
* A play you've executed shows a **✓ executed** badge and can't be fired
  again — re-opening it shows the linked trade, not the button (the engine
  also refuses a second submit server-side).

---

## Strategy catalogue

Toggle each one and tune its params/weight in `config/config.yaml → strategies`.
Hover any play in the UI for its thesis; the **Strategies** button lists them all.

**How the explanation is framed (Douglas).** Every play's hover text reads:
the *edge* (what tends to happen at this setup) → what's true *right now* →
**the plan** (entry, stop with the $-per-share you risk "to find out", target,
reward:risk, and an *estimated ~P%* — a probability over many trades, not a call
on this one) → the **invalidation** price → a short reminder that wins and
losses land randomly around an edge, so decide the loss is acceptable *before*
you click and then leave the stop alone. `Play.probability` and
`Play.invalidation` are persisted with every play.

### Technical (`tos_bot/strategies/technical.py`)

Intraday setups carry a `tod_profile` (`momentum` / `reversal` / `trend`) that
scales confidence by Aziz's session clock — e.g. a momentum breakout is
discounted ~25 % at midday, a reversal is not.

**Every play's stop is floored.** A protective stop closer than ~0.6 % of price
(or ~0.9 intraday ATRs) is noise, not a level — `_mk_play` widens it to that
floor and *then* re-checks reward:risk, so a stop can't be tightened to fake a
good ratio (Aziz p.66). A live run had `sr_bounce` stops 0.2 % wide producing
−3 R fills; those plays now either carry an honest stop or don't exist.

| key | timeframe | idea |
|---|---|---|
| `abcd_pattern` | intraday | hard push A→B, pullback to a **higher low C**; enter near C (stop = loss of C), target the B retest and measured move |
| `bull_bear_flag` | intraday | near-vertical pole + tight sideways flag; enter on the flag break, target ≈ one more pole-length |
| `opening_range_breakout` | intraday | break of the first N-min range **only when that range < the daily ATR**, VWAP-side stop, target = next level |
| `vwap_reclaim` | intraday | a **5-min close** back across session VWAP after ≥ 3 bars on the other side; target = next level |
| `red_to_green` | intraday | gapped stock grinding back to the **prior-day close** on rising volume; target = that close, stop = nearest technical level |
| `intraday_reversal` | intraday | 5+ candles one way **+** 5-min RSI extreme **+** at a daily level **+** an indecision / opposite candle (Aziz's top/bottom reversal) |
| `sr_bounce` | intraday | price into a strong **horizontal** S/R level (≥ 0.58 strength) **on ≥ 1.3× volume**, confirming candle, not against the daily trend; stop a full buffer *beyond* the level (a 5-min close-through), and only if the next level still pays ≥ 2:1 |
| `ema_pullback_trend` | intraday | first pullback to the 20-EMA inside a 9/20/50 stacked trend |
| `gap_and_go` | intraday | ≥ 2 % gap holding the right side of the opening VWAP on ≥ 2× volume |
| `rsi2_mean_reversion` | swing | Connors RSI(2) < 10 above the 200-SMA (long) / > 90 below it (short) |
| `bollinger_fade` | swing | close outside the 2σ band while ADX < 20 (range regime) |
| `atr_channel_breakout` | swing | close beyond a Keltner/ATR channel with ADX rising through 20 |
| `divergence_reversal` | swing | RSI / MACD-histogram divergence **at a horizontal level** (Murphy) |
| `week52_breakout` | swing | push to a new 52-week high/low on volume expansion |

Shared chart-reading primitives live in `tos_bot/analysis/` — `levels.py`
(horizontal S/R clustering: swing pivots, prior-day close, round numbers,
pre-market H/L) and `candles.py` (doji / hammer / shooting-star / engulfing).

### Valuation — from *Pignataro, Financial Modeling and Valuation* (2nd ed.)

`tos_bot/valuation/` + `tos_bot/strategies/fundamental.py`:

| key | book chapter | idea |
|---|---|---|
| `relative_value_comps` | Ch. 8 & 10 | EV/EBITDA, P/E, EV/Sales vs the **peer median**. Consistently cheaper ⇒ long; richer ⇒ short. Target = re-rate to the peer multiple. |
| `dcf_fair_value_gap` | Ch. 9 | project UFCF — **NOPAT method** `EBIT·(1−t) + D&A + deferred tax + non-cash + ΔNWC − CapEx` (the book's Amazon model; falls back to the net-income form, then reported FCF) — discount at **WACC** (CAPM `rf + β·MRP`, debt weight includes leases), terminal value **both** ways: exit multiple **and** Gordon perpetuity `UFCF·(1+g)/(WACC−g)`. Blended per-share vs price beyond a margin of safety; a `sensitivity` grid (g ±1 %, exit ±20 %) rides along. When the two terminal values disagree by > 2.5× — the growth-stock case the book calls out — the verdict is **`ambiguous`** and this strategy sits out. |
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
The cadence is `interval_seconds` normally and `autopilot_interval_seconds` when
Autopilot is day-trading an open session (see the Autopilot section).

---

## Adding a broker later

Implement `tos_bot/brokers/base.py::BrokerAdapter` and register it in
`tos_bot/brokers/__init__.py::get_broker`. Live adapters shipped:
**`schwab_adapter.py`** (`schwab-py` + OAuth) and **`ibkr_adapter.py`**
(`ib_async` → IB Gateway/TWS, own asyncio-loop thread, auto-reconnect, native
brackets, live+paper by port). Still a stub:

- **`crypto_adapter.py`** → `ccxt` (Coinbase / Kraken / …). 24/7 market, no PDT,
  no token; `AssetClass.CRYPTO` bypasses `PdtGuard`.

---

## Project layout

```
run.py                     boot engine + dashboard
scripts/
  init_db.py               create the MySQL schema
  ibkr_setup.py            IBKR connectivity doctor + setup guide  (--guide, --live)
  authenticate.py          one-time / re-auth Schwab OAuth  (--check, --force-rotate)
  reauth_headless.py       unattended re-auth (Playwright + keyring, opt-in)
  run_scan_once.py         one scan cycle from the CLI
tos_bot/
  config.py                .env + config.yaml loader
  engine.py                the conductor (scan / sync / snapshot loops)
  core/                    enums + framework-free dataclasses + event bus
  indicators/ta.py         vectorised TA (no TA-Lib) incl. divergence / run-length
  analysis/                horizontal S/R clustering + candlestick reads (Aziz / Murphy)
  data/                    universe loader, market data (broker→yfinance→synthetic), fundamentals
  valuation/               EV, multiples, DCF, football field, projections   (Pignataro)
  strategies/              base (Douglas-framed explanations) + registry + technical.py + fundamental.py
  scanner/                 pre-filter + cycle orchestration
  risk/                    position sizing + PDT guard
  execution/               order_builder + executor + exit_manager + autopilot (hands-off entry)
  util/clock.py            sessions + NYSE holiday / half-day calendar to 2028
  auth/token_manager.py    Schwab access-token refresh + 60-day rotation + audit
  brokers/                 base + paper (sim) + ibkr + schwab + tda + crypto stub
  persistence/             SQLAlchemy models, repository, schema.sql
  server/app.py            FastAPI REST + WebSocket
  web/                     the dashboard (index.html / styles.css / app.js)
tests/                     pytest  (fast by default; `-m slow` for the e2e)
```

## Tests

```bash
pytest              # 98 fast unit tests
pytest -m slow      # boots the engine, runs scan → approve → close on paper
```

Coverage includes the analysis primitives (`test_analysis.py`), the Autopilot
gate — trade-type filter, confidence/RR floors, position & per-day caps, the
paper-only-in-live hard gate, dry-run (`test_autopilot.py`), the new Aziz
setups on purpose-built frames (`test_strategies.py`), and the IBKR adapter's
translation layer against a fake `ib_async.IB` (`test_ibkr_adapter.py`). The
real Gateway path needs a running IB Gateway and isn't in CI.

## Roadmap

- streaming IBKR ticks (`reqMktData` subscriptions) instead of snapshot polls
- per-strategy backtester + walk-forward on the persisted play log
- options plays (IBKR + the Schwab option-chain endpoint)
- ATR-based trailing stop option in the exit manager (currently R-based)
- equity-curve chart in the dashboard
- crypto adapter (`ccxt`) fleshed out
