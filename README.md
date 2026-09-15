# AutoTradeBot

A **human-in-the-loop** trading assistant for Interactive Brokers.

Once a day before the open it ranks **every US-listed stock and ADR** by how in
play it is, keeps a **hot list** for the day with a small **buffer of candidates
per sector** behind it, and through the session rescans those on a cycle. It
shows **long / short "plays"** with a plain-English explanation on hover and the
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

- **One data source: IB Gateway** — prices, candles and each stock's sector come
  from IBKR. Company financials come from the SEC's own filings (EDGAR) and
  exchange rates from the European Central Bank: official, free, no keys.
- **Scans on a schedule you set** — the full scan's time (pre-market), the cycle
  length and the list sizes are in **Settings**. The **Watchlist** tab shows the
  hot list, each sector's buffer, and what every cycle adopted, kept or dropped.
- **Choose where trades go** — Paper trades on your **IBKR paper account** or the
  **built-in simulator** (filled on IBKR's prices); Live trades on your **live
  IBKR account**. Switch in the dashboard; no restart, no file editing.
- **Filters that drive the bot** — Long / Short, Intraday / Swing and Sectors
  decide what the scanner looks for *and* what can be executed, by you or by
  Autopilot. A change applies at once, updates every open tab and is remembered.
- **Strategies panel** — switch each setup on or off and set its weight, live.
- **Hover to learn** — every play type, column, strategy name and watchlist
  column explains itself on hover.
- **Quitting never strands a position** — paper closes everything, resets the
  simulator and shuts down; live asks whether to exit everything first or
  cancel. Until the last position is out, nothing else can change.
- **Trade records** — click a position (or a closed trade) for everything stored
  about it. An open-trade record whose position no longer exists at the broker
  is deleted automatically.
- **Trading capital** — tell the bot to use only part of the account.
- **Light / dark theme** — follows your OS setting; one click to switch.
- **Strategies:** 14 technical day-trade / swing setups + 3 valuation setups from
  *Pignataro* (comps, a UFCF DCF with exit-multiple **and** perpetuity terminal
  value, a blended "football-field" band).
- **Guard rails (live mode):** a $2,000 equity floor and a rolling 5-session
  Pattern-Day-Trader counter (3-day-trade cap under $25k).
- **UI:** a local web dashboard (FastAPI + WebSocket) at `http://127.0.0.1:8787`.

> ⚠️ **Not investment advice. Not audited. Trade paper first.** Markets can and
> will lose you money faster than any backtest suggests.

---

## Quick start

```bash
python -m venv .venv && . .venv/Scripts/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
cp config/config.example.yaml config/config.yaml
python run.py
```

Start **IB Gateway** logged in to your paper account (see
[Connecting IB Gateway](#connecting-ib-gateway)); the app connects as soon as it
answers, whichever starts first. The dashboard opens at `http://127.0.0.1:8787`
in **Paper** mode.

The first full scan starts straight away when there's no watchlist yet. The very
first one learns every symbol from IBKR and downloads a year of daily candles
(roughly a quarter of an hour); after that each morning's scan only tops up
yesterday's candles. Click a play to see the order preview, then **Execute ✓
Yes**. **Exit** positions from **Open positions**; realised P/L lands in **Trade
history** and **P/L summary**. Stop the app with **Quit** or Ctrl+C — see
[Quitting](#quitting).

Without IB Gateway there are no prices: nothing is scanned, the simulator can't
fill, and the data pill reads `data: none`. Nothing is ever made up.

---

## How scanning works

`tos_bot/scanner/` — the schedule is in `schedule.py`, the ranking in `heat.py`,
the lists in `watchlist.py` and the two scans in `scanner.py`.

**The full scan — once a day, pre-market** (default 08:30 ET; pick 04:00–09:00 in
Settings so it's done before the bell):

1. **Listings.** Every stock and ADR on Nasdaq, NYSE, NYSE American, NYSE Arca,
   Cboe BZX and IEX, from Nasdaq Trader's daily symbol directory — about 5,700
   names. ETFs, test issues, warrants, units, rights, preferreds and notes are
   left out. Cached per day; if the download fails, yesterday's copy is used.
2. **Contracts.** IBKR contract details for symbols not seen before: contract id,
   stock type (common stock, ADR, REIT…) and IBKR's industry, which maps onto the
   11 sectors. Kept in `data/symbols.json`, so only new listings cost a request.
3. **Daily candles.** Each stock's daily candles live on disk (`data/bars/`); a
   stock is downloaded in full once, then only the sessions it's missing.
4. **Daily heat** — no requests: relative volume in the last session, the size of
   its move against its ATR, a close near a 20-day high or low, volatility and
   dollar volume, each turned into a percentile across every **liquid** stock
   (price $3–600, ≥ $5M average daily dollar volume, ATR ≥ 1% of price).
5. **The day's lists.** The hottest become the **hot list** (default 20, no more
   than a third from one sector). The next best in each sector line up in its
   **buffer** (default 25 per sector).
6. **Setups.** Swing setups run on the 400 hottest liquid stocks; valuation
   setups on the top 8 that file US-GAAP 10-Ks, compared with same-industry peers.

**The cycle — during the session** (default every 15 minutes, 5–60 in Settings):
5-minute candles for the hot list, the buffer names being kept, and the next two
names from each sector's buffer. Day-trade and swing setups run on all of them,
and each gets an **intraday heat** (today's relative volume, the move so far
against the ATR, the day's range, plus a bump when a setup fires). Then each
buffer name looked at is:

- **adopted** — hotter than the coolest hot-list stock by 10%, which it replaces;
- **kept** — among the two best seen so far in its sector, waiting for a slot;
- **dropped** — of less use than what's already hot or kept.

So each sector's buffer is worked through over the day for a handful of requests
per cycle — about 60 every 15 minutes. While **Autopilot** is day-trading an
open session, the hot list alone is also rescanned every minute.

If the app starts after the full-scan time with no watchlist for the day (or on a
day off with none at all), the full scan runs as soon as prices are available.
**Settings → Run full scan now** rebuilds the lists at any time; candles already
downloaded are reused, so that's quick. **Scan now** rescans the hot list and the
next buffer names.

| setting (dashboard) | default | range | `config.yaml → scanner` |
|---|---|---|---|
| Full scan at | 08:30 ET | 04:00–09:00 | `premarket_time` |
| Rescan every | 15 min | 5–60 | `cycle_minutes` |
| Hot list size | 20 | 5–50 | `hot_list_size` |
| Buffer per sector | 25 | 10–50 | `sector_queue_size` |

Also in `config.yaml`: `buffer_picks_per_sector` (2), `kept_per_sector` (2),
`fast_cycle_seconds` (60), `fundamentals_leaders` (8), the liquidity `prefilter`,
and `max_universe` (0 = every listing; `SCANNER_MAX_UNIVERSE` caps it without
editing the file).

The **Watchlist** tab (bottom panel) shows the hot list with each stock's daily
and intraday heat, each sector's buffer (kept names, how many looked at, how
many still queued) and the latest adopt / keep / drop decisions.

---

## Where orders go

Two switches, both in the dashboard and remembered in `data/runtime.json`
(`.env` only supplies the starting value). The routing lives in
`tos_bot/brokers/venues.py`:

| Paper / Live | Paper platform | IB Gateway connection | Orders go to |
|---|---|---|---|
| **Live** | (either) | live account, trading | your **real** IBKR account |
| Paper | **IBKR paper account** | paper account, trading | your IBKR paper account |
| Paper | **Built-in simulator** | paper account, read-only (prices only) | the built-in simulator |

- If the Gateway isn't reachable, paper orders fall back to the simulator (which
  has no prices until the Gateway answers) and the header pill turns red with the
  fix. **Live** is never faked: if your live account isn't reachable, the switch
  is refused and tells you why.
- Every trade is stamped with its venue (`paper`, `ibkr-paper`, `ibkr-live`). The
  exit manager only manages trades on the active venue, a manual **Exit** is
  refused for a position held elsewhere, and a platform or Paper/Live switch is
  refused while positions are open on the current venue — so an exit can never
  be sent to an account that doesn't hold the shares.
- Before any exit the app asks the broker what it actually holds. If the
  position is gone or only the other side is there, nothing is sent: selling
  shares you don't have would open a new short. It never sells more than the
  broker holds. If the broker can't answer, the exit goes ahead, since a missed
  stop is the bigger risk.

---

## Connecting IB Gateway

Open **Connections** (header button, or click the connection pill). Everything
below can also be done by editing `.env` by hand — see `.env.example`.

IBKR's API is a socket into a running **IB Gateway** (or TWS). There is **no
token and no expiry** — the Gateway login *is* the authentication. IBKR
restarts the Gateway once a day; **[IBC](https://github.com/IbcAlpha/IBC)**
re-enters your login automatically, and the app reconnects on its own.

1. In **Client Portal → Settings**, enable your **paper account** (username
   `DU…`) and tick *Share real-time market data subscriptions with paper
   account*.
2. Optional: **Settings → Market Data Subscriptions** (e.g. *US Securities
   Snapshot Bundle*, ~$10/mo). Without it quotes are delayed and labelled
   `delayed`.
3. **IB Gateway → Configure → Settings → API → Settings**: socket port **4002**
   (paper) / **4001** (live), *Allow connections from localhost only* with
   Trusted IP `127.0.0.1`, and untick *Read-Only API*. (Older versions also have
   an *Enable ActiveX and Socket Clients* box to tick; newer ones have the API
   on already.) Then *Lock and Exit → Auto restart* (not *Auto logoff*) at
   **9:00 PM New York time**: after-hours trading has ended at 8:00 PM, IBKR's
   nightly maintenance (about 11:45 PM–12:45 AM ET) hasn't started, and
   pre-market (4:00 AM) and the pre-market scan are hours away. The app
   reconnects on its own; IBKR still asks for a full login about once a week.
4. For a hands-off daily login, set `IbLoginId` / `IbPassword` /
   `TradingMode=paper` in IBC's `config.ini` and start `StartGateway.bat` (add a
   Windows "at log on" task).
5. In **Connections → Interactive Brokers**, check the host/ports, **Save**, then
   **Test paper** / **Test live** — a read-only connection that reports the
   account and whether data is real-time or delayed. From a terminal:
   `python scripts/ibkr_setup.py` (`--guide` prints the full walkthrough).

The app keeps checking (every 15 s) and connects as soon as the Gateway answers;
a successful **Test paper** connects it straight away. It never switches to Live
on its own, and it won't move paper orders from the simulator to IBKR while
positions are still open on the simulator — the connection pill says why.

**Accounts in another currency.** An IBKR Canada account is kept in CAD. The
header shows equity, cash and buying power in the account's own currency (hover
for the USD figure); trades are sized in US dollars — at IBKR's own rate when it
sends one, otherwise the European Central Bank's daily reference rate. With no
rate at all nothing is sized, and the order card says so.

**No market-data subscription** (the default for a paper account). The Gateway
still serves historical candles, so scans run on IBKR's candles, and prices for
stops, targets and simulator fills come from the latest one-minute candle. The
data pill shows `data: IBKR (delayed)`; hover it for the detail.

> The app manages exits itself (it doesn't attach a native OCO bracket at IBKR,
> so two exit managers never fight over one position). That means **no stop is
> resting at IBKR if the app isn't running** — keep it running while positions
> are open.

### Security of settings

- The Connections endpoints only answer **this computer**: a loopback client, a
  `localhost`/`127.0.0.1` Host header, no foreign `Origin`, and the dashboard's
  own request header. Another website open in your browser, or a device on your
  network when the server runs with `--host 0.0.0.0`, is refused.
- Only the IB Gateway settings can be written. Values are validated (no line
  breaks, ports in range), `.env` is replaced atomically with your comments and
  other lines preserved, and the account id is **never sent back** — the panel
  only shows that it's set and its last four characters. `.env` is git-ignored.

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

## Filters, strategies and live changes

The **Long / Short** and **Intraday / Swing** checkboxes and **Sectors** (above
the plays table) are the bot's instructions, not just a view:

- **Scan:** only setups of the switched-on timeframes run, only plays on the
  switched-on sides are kept, and the hot list and buffers are built from the
  selected sectors only. Sectors come from IBKR's own classification (its
  industry, with a few categories — drug makers, REITs — overriding it).
- **Execute:** a play the filters exclude can't be executed, by you or by
  Autopilot — the order card says why.
- **Live:** narrowing a filter clears non-matching plays from the board at once;
  widening one rebuilds the lists (a quick full scan on the candles already
  stored). At least one side and one timeframe stay on. **Hide executed** is the
  one view-only checkbox (remembered per browser).

The **Strategies** button opens the playbook: an on/off switch and a **weight**
(0.1–3, multiplies the setup's score) per setup, grouped into day-trade, swing
and valuation. Switching a setup off removes its plays from the board; switching
one on or changing a weight rescans. **Reset to config.yaml** drops your changes.

Filters, strategy switches, scan settings, Paper/Live and the paper platform are
saved in `data/runtime.json`, so they survive a restart (`config.yaml` supplies
the defaults), and are pushed over the WebSocket so every open tab updates.

---

## Quitting

**Quit** (header) or **Ctrl+C** in the terminal never walks away from open
positions:

| trading on | what happens |
|---|---|
| **Paper** | working entry orders are cancelled, every open paper position is closed at the market, the simulator is reset to `account.paper_start_cash` (when trading on it), and the app shuts down |
| **Live, positions open** | a dialog lists them: **Exit all & quit** closes every one and shuts down once they're out; **Cancel** keeps them open and the app running, so their exits stay managed |
| **Live, nothing open** | the app shuts down |

While it closes out, a red banner shows what's left and the app is **locked**:
routing, filters, strategies, scan settings, Autopilot, scanning and new entries
are refused (the API says why) — only exits go through. Closes that haven't
filled are re-sent every 30 s. If the app is stopped mid-quit, the quit resumes
on the next start. Ctrl+C in live mode asks in the dashboard; press it again
within 10 s to force-quit (positions then stay open and **unmanaged**).
Positions parked on another platform are listed and left alone.

**Exit** (on each position) and **Exit all** (blotter header) close positions at
the market at any time, including while quitting.

## Trade records

Click an open position or a closed trade for its **record**: status, entry and
exit, stop moves, what the broker holds right now, why it was taken (the play's
explanation), every fill and every order sent to the broker — with an **Exit**
button while it's open.

An **open-trade record is deleted** once its position no longer exists where it
was opened — closed in the broker's own app, removed, or wiped by **Reset
paper**. A wrong deletion would orphan a real position, so the check is strict:
it only acts on a connected broker's fresh account snapshot, after the
connection has been up a minute, for trades older than 90 s whose close isn't in
flight, and after two misses in a row. A paper reset removes the simulator's
records straight away. Closed trades are never deleted, and the broker order
audit log is always kept.

---

## Trading capital

The header shows the account's **equity, cash and buying power as the broker
reports them**. Next to them, **Trading capital** is how much of that the bot may
use — click it to set an amount, or **Use the whole account** to clear it.

- Risk per trade, the open-risk ceiling and the per-position size limit are
  measured against the trading capital, and new positions only use what's left
  of it after the bot's open positions.
- It can't be more than the account is worth (net liquidation, in the account's
  own currency). If the account later falls below it, the bot uses the account's
  value and the button turns amber.
- It's remembered per platform, so a paper amount never carries over to a live
  account. The broker balance isn't touched, and the PDT rule and the live
  $2,000 floor still look at the real account.

---

## $2,000 floor & the Pattern-Day-Trader rule  (live mode only)

`tos_bot/risk/pdt_guard.py` runs on every **Assess** before you can confirm.
**In paper mode nothing below is enforced** — the day-trade tally is still
shown so you can see where live would stop you.

- **Equity floor** — if account equity `< min_start_equity` ($2,000), *no new
  entries*.
- **PDT** — FINRA flags a *pattern day trader* at **4 day trades in 5 business
  days** on a **margin** account; flagged accounts must hold **$25,000**. Below
  that line you get **3 day trades per rolling 5 sessions**. The guard counts
  closed same-session round-trips (plus still-open intraday trades opened today)
  and **blocks the 4th**, warning from the 2nd–3rd.
- Every **intraday** play is treated as a *potential* day trade.
- **Cash account** (`account.cash_account: true`) — PDT does not apply, but the
  guard warns about T+1 settlement / good-faith violations.

---

## Market sessions, order types & holidays

`tos_bot/util/clock.py` knows the four sessions and the NYSE calendar
(full-day holidays **and** 1:00 pm half-days) through 2028.

* **Regular hours** → the order type from `execution.default_order_type`
  (`LIMIT` / `MARKET` / `STOP_LIMIT`).
* **Pre-market / after-hours** → **limit only**, and only for setups flagged
  extended-hours-eligible (all swing/valuation setups, plus `gap_and_go`).
* **Closed** (overnight / weekend / holiday) → execution is blocked; the
  dashboard shows the reason and when the market next opens.

Every play's order card shows the **concrete order** — type, limit price, stop
trigger, TIF and protection mode — and which venue it **routes to**.

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
is measured against the **original** stop. Each open position has an **Auto
exit** toggle in the blotter if you want to hand-manage it.

Every strategy also declares how long its trade *should* take. The blotter shows
an **Age / Expected** bar per position (green → amber **aging** → red **⏰
overdue**). This is **purely informational**: it never moves the stop.

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
| which trade types it may take | `["INTRADAY"]` | `trade_types` |
| minimum strategy confidence | 0.62 | `min_confidence` |
| minimum reward : risk | 2.0 | `min_reward_risk` (Aziz Rule 5) |
| concurrent open auto positions | 2 | `max_auto_positions` |
| auto trades per session | 3 | `max_auto_trades_per_day` |
| aggregate open auto $-risk | 4 % of equity | `max_open_risk_pct` |
| concurrent auto trades from **one** strategy | 2 | `max_per_strategy` |
| new auto entries **per scan** | 1 | `max_new_per_cycle` |
| **cool off** a ticker after it stops out today | on | `cooldown_after_loss` |
| require a catalyst / dry-run | off | `require_catalyst`, `dry_run` |

The filters and the Strategies panel apply to Autopilot too, and it takes no
entries while the app is quitting. It still passes every other check — session
validity, position sizing, the PDT guard, the $2,000 live floor. Valuation plays
are never auto-traded. Eligible plays get a **🤖** marker.

**Faster loop while day-trading.** When Autopilot is armed with `INTRADAY` and
the regular session is open, the hot list is rescanned every
`scanner.fast_cycle_seconds` (60) between the regular cycles. The header button
shows it (`Autopilot: day ⚡60s`).

---

## Signals — insider trades and company news

A background service (`tos_bot/signals/`) watches what happens off the price chart. It is on by default
(`signals.enabled` in `config.yaml`; `SIGNALS_ENABLED=0` turns it off, and the tests do).

**Insider trades (SEC Form 4, no key needed)**
- SEC's live feed of Form 4 filings is read every 3 minutes. Trading days the app missed are read from SEC's
  daily indexes (the last 5 on the first start). Each filing is read once, within SEC's 10-requests-a-second limit.
- Only purchases (code P) and sales (code S) count. Trades under a 10b5-1 plan, and purchases the filing says
  were made in a private placement or offering, are left out.
- A wave of buying is scored 0–1 from who bought (a CEO or CFO counts most), how much in dollars, how much it
  grew their holding, how many insiders bought within 30 days, and whether the company's insiders rarely buy.
  From 0.55 it is unusual; selling needs 0.65. Several insiders paying exactly one price on one day look like a
  placement and never count as unusual. "Rarely buys" is only claimed once the company's past year of filings
  has been read.

**Company news**
- Every 15 minutes, for the stocks held, the hot list and those with unusual insider buying (up to 40): IBKR
  headlines (on a paper account mostly Briefing.com columns and analyst actions), SEC 8-K filings (material items
  such as earnings or an officer leaving), and Finnhub stories with a free key (Connections → Company news).
- Headlines are scored by FinBERT on this computer when it is installed - `pip install transformers torch`, a
  download of several hundred MB. Without it they simply carry no sentiment.

**What the signals do**
- **Insider buying** (`insider_buying`, a swing setup): unusual buying in the last 10 days, while the stock is no
  more than 15% above what the insiders paid. The stop goes under the recent swing low, one to two daily ranges
  away, and the target is three times the risk. It is on in `config.example.yaml`; if your own `config.yaml`
  doesn't list it, switch it on in the Strategies panel. The strategy replay has no insider history to test it
  on, so Autopilot won't take it while `autopilot.require_proven` is on.
- **Score nudges** on every other play: unusual insider buying adds up to 0.10 to a long (and takes it from a
  short), unusual selling takes up to 0.08 from a long (and adds half that to a short), and the news moves a play
  by up to 0.06 once two or more headlines are scored. The play's evidence shows each nudge and why.
- `GET /api/signals` lists the current signals; they are kept in the `insider_trades`, `filings_read` and
  `news_items` tables.

## Dashboard controls

* **Paper / Live** — which side you're trading. Going Live asks for confirmation.
* **Data pill** — `data: IBKR`, `data: IBKR (delayed)` or `data: none`.
* **Connection pill** — what orders go to and whether it's healthy: `Simulator`,
  `IBKR paper ●`, `IBKR live ✕`. Click it to open **Connections**. A banner
  appears when the Gateway needs you.
* **Connections** — paper platform, IB Gateway settings, **Test paper / Test
  live**, **Reconnect**.
* **Settings** — the scan schedule and list sizes, the scan status, **Run full
  scan now** and **Rescan hot list now**.
* **Scan now** — rescan the hot list and the next buffer names. The plays header
  shows the last scan, a progress bar while one runs, and when the next full
  scan is due.
* **Long / Short / Intraday / Swing** and **Sectors** — what the bot scans for
  and may trade.
* **Strategies** — switch setups on or off and weight them.
* **◐** — light / dark theme.  **↻ Refresh** — re-pull account, positions and fills.
* **Trading capital** — how much of the account the bot may use.
* **Autopilot** + **⚙** — hands-off entry and its caps.
* **Reset paper** (simulator only) — reset the balance.
* **Exit** / **Exit all** — close one position, or all of them, at the market.
* **Quit** — close out, then shut down.
* Bottom tabs: **Open positions**, **Trade history**, **P/L summary**, **Watchlist**.

---

## Strategy catalogue

Switch each one on or off and set its weight in the **Strategies** panel; the
defaults, params and weights live in `config/config.yaml → strategies`. Hover
any play or strategy name for its thesis.

**How the explanation is framed (Douglas).** Every play's hover text reads:
the *edge* (what tends to happen at this setup) → what's true *right now* →
**the plan** (entry, stop with the $-per-share you risk "to find out", target,
reward:risk, and an *estimated ~P%* — a probability over many trades, not a call
on this one) → the **invalidation** price → a reminder that wins and losses land
randomly around an edge.

### Technical (`tos_bot/strategies/technical.py`)

Intraday setups carry a `tod_profile` (`momentum` / `reversal` / `trend`) that
scales confidence by Aziz's session clock. **Every play's stop is floored**: a
stop closer than ~0.6 % of price (or ~0.9 intraday ATRs) is widened, *then*
reward:risk is re-checked. Indicators the setups share (ATRs, VWAP, relative
volume, the S/R map, the daily trend) are computed once per stock per scan.

| key | timeframe | idea |
|---|---|---|
| `abcd_pattern` | intraday | hard push A→B, pullback to a **higher low C**; enter near C, target the B retest and measured move |
| `bull_bear_flag` | intraday | near-vertical pole + tight sideways flag; enter on the flag break |
| `opening_range_breakout` | intraday | break of the first N-min range **only when that range < the daily ATR**, VWAP-side stop |
| `vwap_reclaim` | intraday | a **5-min close** back across session VWAP after ≥ 3 bars on the other side |
| `red_to_green` | intraday | gapped stock grinding back to the **prior-day close** on rising volume |
| `intraday_reversal` | intraday | 5+ candles one way **+** 5-min RSI extreme **+** at a daily level **+** an indecision candle |
| `sr_bounce` | intraday | price into a strong **horizontal** S/R level on ≥ 1.3× volume, stop a full buffer beyond it, only if the next level pays ≥ 2:1 |
| `ema_pullback_trend` | intraday | first pullback to the 20-EMA inside a 9/20/50 stacked trend |
| `gap_and_go` | intraday | ≥ 2 % gap holding the right side of the opening VWAP on ≥ 2× volume |
| `rsi2_mean_reversion` | swing | Connors RSI(2) < 10 above the 200-SMA (long) / > 90 below it (short) |
| `bollinger_fade` | swing | close outside the 2σ band while ADX < 20 |
| `atr_channel_breakout` | swing | close beyond a Keltner/ATR channel with ADX rising through 20 |
| `divergence_reversal` | swing | RSI / MACD-histogram divergence **at a horizontal level** (Murphy) |
| `week52_breakout` | swing | push to a new 52-week high/low on volume expansion |

### Valuation — from *Pignataro, Financial Modeling and Valuation* (2nd ed.)

`tos_bot/valuation/` + `tos_bot/strategies/fundamental.py`:

| key | book chapter | idea |
|---|---|---|
| `relative_value_comps` | Ch. 8 & 10 | EV/EBITDA, P/E, EV/Sales vs the **peer median** (same IBKR industry). Consistently cheaper ⇒ long; richer ⇒ short. |
| `dcf_fair_value_gap` | Ch. 9 | project UFCF (**NOPAT method**), discount at **WACC** (beta measured against SPY from the stored candles), terminal value by exit multiple **and** Gordon perpetuity. When the two disagree by > 2.5× the verdict is **`ambiguous`** and the setup sits out. |
| `valuation_football_field` | Ch. 12 | overlay 52-week range + comps range + both DCF ranges into one band. Below the band ⇒ long; above ⇒ short. |

Financial statements come from each company's **SEC EDGAR** XBRL filings — the
last five fiscal years of annual (10-K / 20-F / 40-F) figures, with restatements
taking precedence, cached for a week. Companies that don't file US-GAAP figures
(most foreign ADRs file IFRS) aren't covered, so their valuation setups sit out.

---

## Data sources and files

| what | from | kept in |
|---|---|---|
| prices, daily and 5-minute candles | IB Gateway | `data/bars/` (daily, one file per stock) |
| contract ids, stock types, sectors | IB Gateway contract details | `data/symbols.json` |
| the list of US stocks and ADRs | Nasdaq Trader symbol directory (daily) | `data/cache/listings/` |
| company financials | SEC EDGAR companyfacts | `data/cache/sec/` (7 days) |
| exchange rates | IB Gateway, else ECB reference rates | memory (6 hours) |
| the day's hot list, buffers, decisions | the full scan and cycles | `data/watchlists/` (last 5 days) |
| your dashboard choices | you | `data/runtime.json` |
| the simulator's balance and positions | the simulator | `data/paper_state.json` |

`data/` is git-ignored. Deleting any of it is safe; it's rebuilt on the next scan.

---

## Adding a broker

Implement `tos_bot/brokers/base.py::BrokerAdapter`, register it in
`tos_bot/brokers/__init__.py::get_broker`, and add it to the routing in
`tos_bot/brokers/venues.py` and `tos_bot/engine/connections.py`. To use it as a
price source too, give it the `PriceSource` methods from
`tos_bot/data/market_data.py` (`history_many`, `contract_details_many`,
`get_quote`, `quotes_from_bars`). Shipped: `paper_adapter.py` (simulator) and
`ibkr_adapter.py` (`ib_async`, own asyncio-loop thread, auto-reconnect, paper +
live by port).

---

## Project layout

```
run.py                     boot engine + dashboard (Ctrl+C follows the quit rules)
scripts/
  init_db.py               create the MySQL schema
  ibkr_setup.py            IB Gateway connectivity doctor + setup guide  (--guide, --live)
tos_bot/
  config.py                .env + config.yaml loader
  secrets_store.py         the Connections panel's validated, allow-listed .env writer
  engine/                  the conductor
    engine.py                loops, scans on schedule, operator actions, quitting, snapshot
    connections.py           the one IB Gateway connection + the simulator
    board.py                 the plays on the dashboard
    capital.py               trading capital
    reconcile.py             when an open-trade record counts as gone at the broker
    runtime.py               data/runtime.json
    views.py                 pieces of the dashboard snapshot
  core/                    enums + framework-free dataclasses + event bus
  data/                    listings, symbols, daily bar store, market data, SEC EDGAR, fx, sectors
  indicators/ta.py         vectorised TA (no TA-Lib) incl. divergence, relative volume, beta
  analysis/                horizontal S/R clustering + candlestick reads (Aziz / Murphy)
  valuation/               EV, multiples, DCF, football field, projections   (Pignataro)
  strategies/              base (shared indicators, Douglas-framed explanations) + registry + technical + fundamental
  scanner/                 schedule, heat, watchlist, evaluator, filters, the scans
  risk/                    position sizing + PDT guard
  execution/               order_builder + executor + exit_manager + autopilot
  util/                    clock (sessions + NYSE calendar to 2028), net, logging
  brokers/                 base, venues (routing), paper (simulator), ibkr
  persistence/             SQLAlchemy models, repository, schema.sql
  server/                  app.py (FastAPI REST + WebSocket) + security.py (same-machine guard)
  web/                     the dashboard: index.html, styles.css, js/ (ES modules, no build step)
tests/                     pytest  (fast by default; `-m slow` for the end-to-end run)
```

## Tests

```bash
pytest              # fast unit tests
pytest -m slow      # boots the engine on a synthetic Gateway: scan → approve → close
```

Tests run against a synthetic IB Gateway (`tests/fakes.py`: seeded random-walk
candles, contract details and an account), with a throwaway `.env`, database,
data folder and runtime file — never yours. Coverage includes the scans (full
scan ranking, hot list, cycle decisions), the schedule, the watchlist, heat,
the listings directory, SEC financials, the daily bar store, the strategies, the
Autopilot gate, the exit manager, the IBKR adapter against a fake `ib_async.IB`,
routing and the open-position switch guard, the `.env` writer, the same-machine
guard, live dashboard changes, quitting, the broker-vs-database record check,
trading capital and exchange rates. The real IB Gateway isn't exercised in tests.

## Roadmap

- a native stop resting at the broker as a crash-safety backup, kept in sync with the exit manager
- streaming IBKR ticks (`reqMktData` subscriptions) for the hot list instead of candle polls
- per-strategy backtester + walk-forward on the persisted play log
- options plays
- equity-curve chart in the dashboard
