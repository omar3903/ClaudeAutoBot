# AutoTradeBot

A **human-in-the-loop** trading assistant for a small account.

It scans the Nasdaq every few minutes, ranks a short list of names, and shows
**long / short "plays"** with a plain-English explanation on hover and the
stock's **sector** next to the ticker. Nothing is routed to a broker until you
click **Execute ✓ Yes** — or, if you switch it on, **Autopilot** takes the entry
inside hard caps (paper-only until you deliberately allow live). Every open
trade then runs an **automatic exit strategy** (stop / target / break-even /
trailing / end-of-day flatten) with no further input, and carries an **expected
time-to-exit** so a position that overstays gets flagged. Every idea and every
executed trade (with its realised P/L) is written to SQLite or MySQL.

The play explanations and the day-trade / swing recognition are modelled on
four books: **Aziz, *How to Day Trade for a Living*** (the intraday setups and
"stocks in play" filters), **Douglas, *Trading in the Zone*** (every play is
framed as *an edge with a probability*, not a prediction), **Murphy, *Technical
Analysis of the Financial Markets*** (horizontal S/R, oscillator divergence,
volume confirmation) and **Pignataro, *Financial Modeling and Valuation*** (the
DCF / comps overlay).

- **Choose where trades go** — in the dashboard's **Connections** panel. Paper
  trades on the **built-in $100k simulator**, your **IBKR paper account**, or
  **thinkorswim / Schwab** real-time data with simulated fills. Live trades on
  **Interactive Brokers** or **Charles Schwab**. No restart, no file editing.
- **Keys and sign-in inside the app** — paste broker settings into Connections
  (saved to your local `.env`; secrets are never shown again) and click **Sign
  in with Schwab**: a local callback server catches Schwab's OAuth redirect and
  stores the token. IBKR has no token — its login is the running IB Gateway —
  so its panel tests the connection instead.
- **Sector filter** — pick the sectors to scan *and* trade. The scanner skips the
  rest, and plays outside them can't be executed by you or by Autopilot.
- **Light / dark theme** — follows your OS setting; one click to switch.
- **Strategies:** 13 technical day-trade / swing setups (Aziz's ABCD, bull/bear
  flag, VWAP, opening-range, red-to-green, top/bottom reversal, horizontal S/R;
  Murphy/Connors divergence & mean-reversion) + 3 valuation setups from
  *Pignataro* (comps, a UFCF DCF with exit-multiple **and** perpetuity terminal
  value, a blended "football-field" band). Intraday setups are **weighted by
  time of day**.
- **Autopilot (hands-off entry):** the bot places the entry itself for plays
  that clear a strict gate (trade types, confidence floor, ≥ 2:1 reward:risk,
  per-day, concurrent, per-strategy and open-risk caps). **Paper-only** until
  you set `autopilot.allow_live: true` in `config.yaml`.
- **Safe switching:** every trade remembers the platform it was opened on. Exits
  are only ever sent there, and the app refuses to change platform while
  positions are open on the current one.
- **Guard rails (live mode):** a $2,000 equity floor and a rolling 5-session
  Pattern-Day-Trader counter (3-day-trade cap under $25k) that block or warn
  *before* you confirm.
- **UI:** a local web dashboard (FastAPI + WebSocket) at `http://127.0.0.1:8787`.

> ⚠️ **Not investment advice. Not audited. Trade paper first.** Markets can and
> will lose you money faster than any backtest suggests.

---

## Quick start (paper, no credentials)

```bash
python -m venv .venv && . .venv/Scripts/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
cp config/config.example.yaml config/config.yaml
python run.py
```

The dashboard opens at `http://127.0.0.1:8787` in **Paper** mode on the built-in
**$100,000** simulator. Click **Scan now**, hover a row to read the play, click
it to see the order preview (*Routes to: SIMULATED (built-in)*), then **Execute
✓ Yes** to fill it against the current quote. Close positions from **Open
positions**; realised P/L lands in **Trade history** and **P/L summary**.
**Reset paper** wipes the simulator back to a balance you choose (trade history
is kept). The simulator persists to `data/paper_state.json`.

With no broker connected, market data comes from `yfinance` (delayed ~15 min).
The header shows the source (`data: yfinance` / `broker:ibkr` / `broker:schwab`)
and whether it's real-time or delayed. With no MySQL configured, the database is
a local SQLite file.

---

## Where orders go

Three switches, all in the dashboard and remembered in `data/runtime.json`
(`.env` only supplies the starting values). The routing lives in
`tos_bot/brokers/venues.py`:

| Paper / Live | Paper platform | Connection the app holds | Orders go to |
|---|---|---|---|
| **Live** | (any) | your live broker | your **real** IBKR or Schwab account |
| Paper | **IBKR paper account** | IB Gateway, paper port | your IBKR paper account |
| Paper | **thinkorswim / Schwab** | Schwab (data only) | the built-in simulator |
| Paper | **Built-in simulator** | the live broker's feed, read-only | the built-in simulator |

- **thinkorswim's paperMoney has no API**, so "thinkorswim / Schwab" paper
  trading simulates fills on Schwab's real-time quotes. Those positions won't
  appear in the thinkorswim app.
- If the chosen platform isn't reachable (Gateway not running, not signed in),
  paper falls back to the simulator and the header pill turns red with the fix.
  **Live** is never faked: if the live broker isn't reachable, the switch is
  refused and tells you why.
- Every trade is stamped with its venue (`paper`, `ibkr-paper`, `ibkr-live`,
  `schwab`). The exit manager only manages trades on the active venue, a manual
  **Close** is refused for a position held elsewhere, and a platform or
  Paper/Live switch is refused while positions are open on the current venue —
  so an exit can never be sent to an account that doesn't hold the shares.
  Positions parked on another platform show an **on IBKR paper**-style badge.

---

## Connecting a broker

Open **Connections** (header button, or click the connection pill). Everything
below can also be done by editing `.env` by hand — see `.env.example`.

### Interactive Brokers

IBKR's API is a socket into a running **IB Gateway** (or TWS). There is **no
token and no expiry** — the Gateway login *is* the authentication. IBKR
restarts the Gateway once a day; **[IBC](https://github.com/IbcAlpha/IBC)**
re-enters your login automatically, and the app reconnects on its own.

1. In **Client Portal → Settings**, enable your **paper account** (username
   `DU…`) and tick *Share real-time market data subscriptions with paper
   account*.
2. Optional: **Settings → Market Data Subscriptions** (e.g. *US Securities
   Snapshot Bundle*, ~$10/mo). Without it data is 15-minute delayed and labelled
   `delayed`.
3. **IB Gateway → Configure → Settings → API → Settings**: enable *ActiveX and
   Socket Clients*, socket port **4002** (paper) / **4001** (live), Trusted IP
   `127.0.0.1`, untick *Read-Only API*. Then *Lock and Exit → Auto restart*.
4. For a hands-off daily login, set `IbLoginId` / `IbPassword` /
   `TradingMode=paper` in IBC's `config.ini` and start `StartGateway.bat` (add a
   Windows "at log on" task).
5. In **Connections → Interactive Brokers**, check the host/ports, **Save**, then
   **Test paper** / **Test live** — a read-only connection that reports the
   account and whether data is real-time or delayed. From a terminal:
   `python scripts/ibkr_setup.py` (`--guide` prints the full walkthrough).

> The app manages exits itself (it doesn't attach a native OCO bracket at IBKR,
> so two exit managers never fight over one position). That means **no stop is
> resting at IBKR if the app isn't running** — keep it running while positions
> are open.

### Charles Schwab / thinkorswim

1. Create an app at <https://developer.schwab.com> (*Trader API — Individual*),
   set its callback URL to exactly **`https://127.0.0.1:8182`**, and wait for
   Schwab to approve it (usually a few days).
2. In **Connections → thinkorswim / Schwab**, paste the **App key** and **App
   secret** and **Save**.
3. Click **Sign in with Schwab**. Your browser opens schwab.com — log in and
   allow access. The browser then warns about a certificate for
   `127.0.0.1:8182`: that page is served by *this computer* to catch Schwab's
   redirect, so continue. The token is saved and the app connects.
4. **Schwab's sign-in lasts 7 days.** A day before it expires the header pill
   shows `Schwab · 1d` with a **Sign in with Schwab** banner; one click renews it.

From a terminal: `python scripts/authenticate.py` (`--check` prints the token
status, `--reset` removes the token first).

### Security of keys and sign-in

- The Connections endpoints only answer **this computer**: a loopback client, a
  `localhost`/`127.0.0.1` Host header, no foreign `Origin`, and the dashboard's
  own request header. Another website open in your browser, or a device on your
  network when the server runs with `--host 0.0.0.0`, is refused.
- Only a fixed list of broker settings can be written. Values are validated (no
  line breaks, ports in range, the callback URL shape), `.env` is replaced
  atomically with your comments and other lines preserved, and **secret values
  are never sent back** — the panel only shows that a key is set and its last
  four characters.
- The app never asks for your brokerage password: you log in on schwab.com.
  The Schwab token lives in `secrets/` and `.env` stays on your machine — both
  are git-ignored.

### Schwab token upkeep

`tos_bot/auth/token_manager.py` + `AuthWatchdog` (IBKR and the simulator need none):

| what | how |
|---|---|
| access token (30 min) | refreshed by `schwab-py`; the watchdog makes a cheap authenticated call on a timer |
| refresh token (7 days) | age read from a sidecar `*.meta.json` written at each sign-in |
| reminder | from `refresh_token_ttl_days − rotate_before_days` (day 6): one banner + toast per token, nothing deleted |
| expiry | on day 7: timestamped backup, the dead token is removed, and you're asked to sign in again |
| audit | sign-ins, refreshes and expiries go to the `token_audit` table |

```yaml
auth:
  refresh_token_ttl_days: 7
  rotate_before_days: 1
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

---

## Sector filter

**Sectors** (above the plays table) picks which of the 11 GICS sectors to scan
and trade; "all" means no filter. The choice is remembered in
`data/runtime.json` (default: `scanner.sectors` in `config.yaml`).

- **Scan:** each cycle drops symbols outside the selection before any price data
  is fetched. Sectors come from a bundled map, then a disk cache, then a
  one-off `yfinance` lookup (at most 20 new tickers per cycle, so a filter never
  stalls a scan); a ticker whose sector isn't known yet is skipped until it is.
- **Execute:** a play outside the selection can't be executed, by you or by
  Autopilot — the order card says why. Changing the selection clears
  non-matching plays from the board and starts a rescan.
- yfinance spellings are normalised (`Consumer Cyclical` → *Consumer
  Discretionary*, `Financial Services` → *Financials*, …).

---

## $2,000 floor & the Pattern-Day-Trader rule  (live mode only)

`tos_bot/risk/pdt_guard.py` runs on every **Assess** before you can confirm.
**In paper mode nothing below is enforced** — the day-trade tally is still
shown so you can see where live would stop you.

- **Equity floor** — if account equity `< min_start_equity` ($2,000), *no new
  entries*, full stop.
- **PDT** — FINRA flags a *pattern day trader* at **4 day trades in 5 business
  days** on a **margin** account; flagged accounts must hold **$25,000**. Below
  that line you get **3 day trades per rolling 5 sessions**. The guard counts
  closed same-session round-trips (plus still-open intraday trades opened today)
  from the `trades` table and **blocks the 4th**, warning from the 2nd–3rd.
- Every **intraday** play is treated as a *potential* day trade (conservative).
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
    (`LIMIT` / `MARKET` / `STOP_LIMIT`).
  * **Pre-market / after-hours** → **limit only**, and only for setups flagged
    extended-hours-eligible (all swing/valuation setups, plus `gap_and_go`).
  * **Closed** (overnight / weekend / holiday) → execution is blocked; the
    dashboard shows the reason and when the market next opens.
* Every play's order card shows the **concrete order** — type, limit price, stop
  trigger, TIF and protection mode — and which venue it **routes to**.
* The header pill shows the live session status, and `GET /api/market` returns
  the full breakdown incl. the next holiday.

---

## Automatic exit strategy

`tos_bot/execution/exit_manager.py` runs every few seconds on every open trade
held on the active platform — **entries need your click, exits never do**:

| rule | default | config key |
|---|---|---|
| cut losses at the working stop | on | — |
| take profit at the target | on | — |
| tighten the stop to **lock a small profit** once green | at +1.3 R, lock +0.3 R | `breakeven_at_r`, `breakeven_lock_r` |
| **trail** the stop, keeping a fraction of the open R | from +2.0 R, lock 50% | `trail_start_r`, `trail_lock_ratio` |
| **flatten day trades** before the (holiday-aware) close | 10 min before | `flatten_intraday_before_close_min` |
| force-close **stale swings** | 10 days | `max_swing_hold_days` |

The stop only ever ratchets in your favour and never through the last price. R
is measured against the **original** stop (`trades.initial_stop_price`). Each
open position has an **Auto exit** toggle in the blotter if you want to
hand-manage it. Tune everything in `config/config.yaml → exit_manager`.

### Expected time-to-exit (overwatch)

Every strategy declares how long its trade *should* take — minutes for intraday
setups, trading days for swing/valuation ones. The blotter shows an **Age /
Expected** bar per position (green → amber **aging** → red **⏰ overdue**) and
you get a one-time toast when a trade goes overdue. This is **purely
informational**: it never moves the stop or closes anything.

---

## Autopilot — hands-off entry

`tos_bot/execution/autopilot.py`. After each scan it walks the fresh plays and,
for any that clear the gate, calls the same approve/execute path as the
**Execute ✓ Yes** button. Toggle and tune it from the header (**Autopilot: off /
day / day+swing**, plus ⚙). Defaults live in `config/config.yaml → autopilot`:

| gate | default | key |
|---|---|---|
| master switch | off | `enabled` (UI toggle) |
| **route real orders** | **off** | `allow_live` — *config-file only*; with it off, Autopilot is armed for **paper only** even in Live mode, and says so |
| which trade types it may take | `["INTRADAY"]` | `trade_types` (Day / Swing checkboxes) |
| minimum strategy confidence | 0.62 | `min_confidence` |
| minimum reward : risk | 2.0 | `min_reward_risk` (Aziz Rule 5) |
| concurrent open auto positions | 2 | `max_auto_positions` |
| auto trades per session | 3 | `max_auto_trades_per_day` (≈ the sub-$25k PDT cap) |
| aggregate open auto $-risk | 4 % of equity | `max_open_risk_pct` |
| concurrent auto trades from **one** strategy | 2 | `max_per_strategy` |
| new auto entries **per scan cycle** | 1 | `max_new_per_cycle` |
| **cool off** a ticker after it stops out today | on | `cooldown_after_loss` |
| require a catalyst / dry-run | off | `require_catalyst`, `dry_run` |

Sectors are set with the **Sectors** button and apply to Autopilot too. It
still passes through every other check — session validity, position sizing, the
PDT guard, the $2,000 live floor. Fundamental (valuation) plays are never
auto-traded. Eligible plays get a **🤖** marker.

> `max_per_strategy`, `max_new_per_cycle` and `cooldown_after_loss` were added
> after a 7-hour run went **1-for-5**: every auto trade was the *same* strategy
> (`sr_bounce`), five positions opened inside a 10-minute mid-day chop, and two
> tickers were entered long *then* short minutes apart.

**Faster loop while day-trading.** When Autopilot is armed with `INTRADAY` *and*
the regular session is open, scans run every `scanner.autopilot_interval_seconds`
(45) instead of `scanner.interval_seconds` (300), self-throttled so a cycle never
overlaps the last and never below `scanner.min_interval_seconds` (20). The
header button shows the cadence (`Autopilot: day ⚡45s`).

---

## Dashboard controls

* **Paper / Live** — which side you're trading; the platforms behind each are
  chosen in Connections. Going Live asks for confirmation.
* **Connection pill** (next to the data pill) — what orders go to and whether
  it's healthy: `Simulator`, `IBKR paper ●`, `Schwab live · 1d`, `IBKR live ✕`.
  Click it to open **Connections**. A banner appears when something needs you
  (Gateway down, Schwab sign-in expiring).
* **Connections** — paper platform, live broker, broker settings and keys,
  **Test paper / Test live** for IBKR, **Sign in with Schwab**, **Reconnect**.
* **Sectors** — which sectors to scan and trade.
* **◐** — light / dark theme (remembered per browser).
* **↻ Refresh** — re-pull account, positions and fills now.
* **Autopilot** + **⚙** — hands-off entry and its caps.
* **Reset paper** / **Reconcile** (simulator only) — reset the balance, or
  rebuild simulator positions from the open trades in the database.
* A play you've executed shows **✓ executed** and can't be fired again.

---

## Strategy catalogue

Toggle each one and tune its params/weight in `config/config.yaml → strategies`
(params there override the defaults in code). Hover any play in the UI for its
thesis; the **Strategies** button lists them all.

**How the explanation is framed (Douglas).** Every play's hover text reads:
the *edge* (what tends to happen at this setup) → what's true *right now* →
**the plan** (entry, stop with the $-per-share you risk "to find out", target,
reward:risk, and an *estimated ~P%* — a probability over many trades, not a call
on this one) → the **invalidation** price → a reminder that wins and losses land
randomly around an edge, so decide the loss is acceptable *before* you click.

### Technical (`tos_bot/strategies/technical.py`)

Intraday setups carry a `tod_profile` (`momentum` / `reversal` / `trend`) that
scales confidence by Aziz's session clock — e.g. a momentum breakout is
discounted ~25 % at midday, a reversal is not.

**Every play's stop is floored.** A protective stop closer than ~0.6 % of price
(or ~0.9 intraday ATRs) is noise, not a level — `_mk_play` widens it to that
floor and *then* re-checks reward:risk, so a stop can't be tightened to fake a
good ratio (Aziz p.66).

| key | timeframe | idea |
|---|---|---|
| `abcd_pattern` | intraday | hard push A→B, pullback to a **higher low C**; enter near C (stop = loss of C), target the B retest and measured move |
| `bull_bear_flag` | intraday | near-vertical pole + tight sideways flag; enter on the flag break, target ≈ one more pole-length |
| `opening_range_breakout` | intraday | break of the first N-min range **only when that range < the daily ATR**, VWAP-side stop, target = next level |
| `vwap_reclaim` | intraday | a **5-min close** back across session VWAP after ≥ 3 bars on the other side; target = next level |
| `red_to_green` | intraday | gapped stock grinding back to the **prior-day close** on rising volume; target = that close |
| `intraday_reversal` | intraday | 5+ candles one way **+** 5-min RSI extreme **+** at a daily level **+** an indecision / opposite candle |
| `sr_bounce` | intraday | price into a strong **horizontal** S/R level (≥ 0.58 strength) **on ≥ 1.3× volume**, confirming candle, not against the daily trend; stop a full buffer *beyond* the level, and only if the next level pays ≥ 2:1 |
| `ema_pullback_trend` | intraday | first pullback to the 20-EMA inside a 9/20/50 stacked trend |
| `gap_and_go` | intraday | ≥ 2 % gap holding the right side of the opening VWAP on ≥ 2× volume |
| `rsi2_mean_reversion` | swing | Connors RSI(2) < 10 above the 200-SMA (long) / > 90 below it (short) |
| `bollinger_fade` | swing | close outside the 2σ band while ADX < 20 (range regime) |
| `atr_channel_breakout` | swing | close beyond a Keltner/ATR channel with ADX rising through 20 |
| `divergence_reversal` | swing | RSI / MACD-histogram divergence **at a horizontal level** (Murphy) |
| `week52_breakout` | swing | push to a new 52-week high/low on volume expansion |

Shared chart-reading primitives live in `tos_bot/analysis/` — `levels.py`
(horizontal S/R clustering) and `candles.py` (doji / hammer / shooting-star /
engulfing).

### Valuation — from *Pignataro, Financial Modeling and Valuation* (2nd ed.)

`tos_bot/valuation/` + `tos_bot/strategies/fundamental.py`:

| key | book chapter | idea |
|---|---|---|
| `relative_value_comps` | Ch. 8 & 10 | EV/EBITDA, P/E, EV/Sales vs the **peer median**. Consistently cheaper ⇒ long; richer ⇒ short. Target = re-rate to the peer multiple. |
| `dcf_fair_value_gap` | Ch. 9 | project UFCF (**NOPAT method**), discount at **WACC**, terminal value **both** ways (exit multiple and Gordon perpetuity). Blended per-share vs price beyond a margin of safety, with a sensitivity grid. When the two terminal values disagree by > 2.5× the verdict is **`ambiguous`** and this strategy sits out. |
| `valuation_football_field` | Ch. 12 | overlay 52-week range + comps range + both DCF ranges into one band. Price below the band ⇒ long; above ⇒ short. |

The seven projection methods (Ch. 1) are in `tos_bot/valuation/projections.py`.
Fundamental data is from `yfinance` (free, occasionally patchy); every field is
optional and the engine degrades gracefully.

---

## Scanning "all of the Nasdaq"

`tos_bot/scanner/scanner.py`:

1. loads the symbol list (cached daily; bundled Nasdaq-100 fallback offline);
2. each cycle takes a **rotating slice** of `scanner.max_symbols_scanned`
   symbols, then drops anything outside the **sector filter**;
3. cheap **pre-filter** (price band, 20-day $-volume, ATR %, spread);
4. survivors get intraday bars + the technical strategies;
5. the leaders additionally get fundamentals + peers + the valuation strategies;
6. every play is position-sized, ranked by a blended score, and the top
   `shortlist_size` names become the focus list.

Override without editing the config: `SCANNER_UNIVERSE`, `SCANNER_MAX_SYMBOLS`,
`SCANNER_INTERVAL_SECONDS`.

---

## Adding a broker

Implement `tos_bot/brokers/base.py::BrokerAdapter`, register it in
`tos_bot/brokers/__init__.py::get_broker`, and add it to the tables in
`tos_bot/brokers/venues.py` (plus any `.env` fields to `tos_bot/secrets_store.py`
so Connections can edit them). Shipped: `paper_adapter.py` (simulator),
`ibkr_adapter.py` (`ib_async`, own asyncio-loop thread, auto-reconnect,
paper + live by port) and `schwab_adapter.py` (`schwab-py`).

---

## Project layout

```
run.py                     boot engine + dashboard
scripts/
  init_db.py               create the MySQL schema
  ibkr_setup.py            IBKR connectivity doctor + setup guide  (--guide, --live)
  authenticate.py          Schwab sign-in from the terminal  (--check, --reset)
  run_scan_once.py         one scan cycle from the CLI
tos_bot/
  config.py                .env + config.yaml loader
  secrets_store.py         the Connections panel's validated, allow-listed .env writer
  engine.py                the conductor (routing, scan / sync / snapshot loops)
  core/                    enums + framework-free dataclasses + event bus
  indicators/ta.py         vectorised TA (no TA-Lib) incl. divergence / run-length
  analysis/                horizontal S/R clustering + candlestick reads (Aziz / Murphy)
  data/                    universe, market data (broker→yfinance→synthetic), fundamentals, sectors
  valuation/               EV, multiples, DCF, football field, projections   (Pignataro)
  strategies/              base (Douglas-framed explanations) + registry + technical.py + fundamental.py
  scanner/                 pre-filter, sector filter + cycle orchestration
  risk/                    position sizing + PDT guard
  execution/               order_builder + executor + exit_manager + autopilot
  util/                    clock (sessions + NYSE calendar to 2028), net, logging
  auth/                    schwab_login (one-click OAuth) + token_manager (7-day upkeep)
  brokers/                 base, venues (routing), paper (simulator), ibkr, schwab
  persistence/             SQLAlchemy models, repository, schema.sql
  server/                  app.py (FastAPI REST + WebSocket) + security.py (same-machine guard)
  web/                     the dashboard (index.html / styles.css / app.js)
tests/                     pytest  (fast by default; `-m slow` for the e2e)
```

## Tests

```bash
pytest              # 148 fast unit tests
pytest -m slow      # boots the engine, runs scan → approve → close on paper
```

Coverage includes the analysis primitives, the strategies on purpose-built
frames, the Autopilot gate, the exit manager, the IBKR adapter against a fake
`ib_async.IB`, venue routing and the open-position switch guard
(`test_venues.py`, `test_venue_safety.py`, `test_engine_routing.py`), the `.env`
writer (`test_secrets_store.py`), the same-machine guard (`test_security.py`),
Schwab sign-in with a faked browser flow (`test_schwab_login.py`), token upkeep
(`test_token_manager.py`) and the sector filter (`test_sectors.py`). Tests use a
throwaway `.env`, database and runtime file, never yours. The real IB Gateway and
Schwab login paths need your accounts and aren't exercised in tests.

## Roadmap

- a native stop resting at the broker as a crash-safety backup, kept in sync with the exit manager
- streaming IBKR ticks (`reqMktData` subscriptions) instead of snapshot polls
- per-strategy backtester + walk-forward on the persisted play log
- options plays (IBKR + the Schwab option-chain endpoint)
- equity-curve chart in the dashboard
