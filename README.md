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

A second shelf decides **whether a setup can be trusted right now, how much to
risk on it and how its record is judged**: **Chan, *Quantitative Trading*** and
***Algorithmic Trading*** (mean-reversion and momentum tests, backtests with costs
and held-out data, half-Kelly, the gap and post-earnings setups), **Tsay,
*Analysis of Financial Time Series*** and **Enders, *Applied Econometric Time
Series*** (volatility forecasts), **Hamilton, *Time Series Analysis*** (calm and
turbulent market regimes), and **Vidyamurthy, *Pairs Trading***, **Johansen** and
**Juselius** (cointegration) - see
[What the books taught it](#what-the-books-taught-it--the-quantitative-layer).

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
- **A report on every session** — after the close: the market's biggest movers,
  why each one moved (earnings, filings, analyst actions, news, its sector) and
  whether the bot traded it, offered it, watched it or missed it, with charts; then
  each of the bot's trades with what it was taken on, the mistakes, how the plays it
  didn't take would have done, and each strategy's real record against its replay.
- **Proof before Autopilot trades** — every strategy is replayed on past candles
  with costs, and has to make money on the held-out latest third of them too.
- **Pairs trading** — cointegrated stocks from one industry, one long and one short,
  entered and closed together (see [Pairs trading](#pairs-trading)).
- **Light / dark theme** — follows your OS setting; one click to switch.
- **Strategies:** 16 technical day-trade / swing setups (2 statistical ones from Chan),
  insider buying from SEC Form 4 filings, + 3 valuation setups from
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

**The gap check — once, just before the open** (default 09:15 ET, 08:00–09:25 in
Settings): the hot list and every buffer name (at most 400) get one request for
today's pre-market 5-minute candles. A stock that has moved 2 % or more from
yesterday's close on at least 50,000 pre-market shares is a **gapper** — Aziz's
stocks in play — and takes the hot-list slot of the coolest name that isn't
gapping itself (the cap of a third per sector still holds). The Watchlist tab
shows each hot-list name's pre-market gap and the decisions with what was seen.
Every stock's pre-market high and low are kept as the day's first support and
resistance levels for the setups. **Settings → Check gappers now** runs it on
demand.

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

**The wide scan — every 30 minutes** (15–120 in Settings, 0 = off): every
liquid stock the full scan ranked (about 2,400; `wide_stocks` caps it to the
hottest N) gets one request for its 5-minute candles, in chunks of 250, and the
day-trade and swing setups the filters allow run on all of them with today's
partial candle — so a stock that heats up mid-session is seen even if the
morning's ranking had it cold, and a swing setup that forms during the day is on
the board within the half hour. Its intraday heat refreshes the hot list: a
stock hotter than the coolest hot-list name by 10 % takes its slot (within the
cap of a third per sector), and the best of the rest per sector are kept
waiting. It takes a few minutes of Gateway time (about 11 stocks a second) and
the 5-minute cycle waits for it; plays it finds stay on the board and are
re-checked every 15 seconds like any other. **Settings → Scan every stock now**
runs it on demand.

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
| Gap check at | 09:15 ET | 08:00–09:25 | `gapper_time` |
| Wide scan every | 30 min | 15–120, 0 = off | `wide_minutes` |
| Wide scan covers | all liquid stocks | 0 = all, else the hottest N | `wide_stocks` |

Also in `config.yaml`: `gapper_symbols` (400), `gapper_min_gap_pct` (2), `gapper_min_volume`
(50,000), `buffer_picks_per_sector` (2), `kept_per_sector` (2),
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
(0.1–3) per setup, grouped into day-trade, swing and valuation. Switching a setup
off removes its plays from the board; switching one on or changing a weight
rescans. **Reset to config.yaml** drops your changes.

**What a weight does, in plain words.** Every play gets a *score*: what it
should make per dollar risked, from the setup's odds of paying and its reward
against its risk. The weight multiplies that score, so it only moves a setup's
plays up or down the list against other setups' plays. It doesn't change whether
a setup fires, its odds, its stop or its target, and Autopilot's gates
(confidence, reward:risk, noise flags, proof) never look at it - though Autopilot
takes the highest-scored play first, so a weight decides which of two qualifying
plays it takes. Leave every weight at 1 unless you have a reason of your own: the
bot already tilts each setup by its record (the *evidence weight*, ×0.5 to ×1.5,
from the replay and your real trades), and each card shows the weight the
ranking actually uses. A setup you don't want traded should be switched off, not
weighted 0.1.

Filters, strategy switches, scan settings, Paper/Live and the paper platform are
saved in `data/runtime.json`, so they survive a restart (`config.yaml` supplies
the defaults), and are pushed over the WebSocket so every open tab updates.

---

## A day in the bot's life

Times are New York time, with the default settings.

| when | what the bot does |
|---|---|
| overnight – 8:30 AM | Holds its connection to IB Gateway and watches open positions. Reads SEC's live feed of insider filings every 3 minutes and the news on the stocks it holds and on yesterday's hot list every 15. If the last session has no report yet, it writes one. No scanning — the market is closed. |
| **8:30 AM** (`premarket_time`, 4:00–9:00) | **The full scan.** Every listed stock's daily candles, through yesterday's close, are brought up to date (after a report the evening before, there's almost nothing left to download). Every liquid stock is ranked by *yesterday's* heat: relative volume, the move in average daily ranges, closeness to a 20-day high or low, daily range and dollar volume. The hottest become today's **hot list** (at most a third per sector) and the next ones each sector's **buffer**. Swing and valuation setups are looked for, and pairs are fitted. |
| 8:30 – 9:15 AM | News follows the new hot list. |
| **9:15 AM** (`gapper_time`, 8:00–9:25) | **The gap check.** One read of the hot list and buffer names' pre-market candles. The stocks gapping 2 % or more on 50,000+ pre-market shares take hot-list slots, and every name's pre-market high and low become levels for the day's setups. |
| **9:30 AM – 4:00 PM** | **Cycles** every 15 minutes rescan the hot list on 5-minute candles and try the next names from each sector's buffer — adopting a hotter one into the hot list, keeping or dropping the rest. With Autopilot day-trading, a fast cycle over the hot list every minute. Plays are offered, taken by you or Autopilot, and exits are managed. Pairs are decided in the last 30 minutes. |
| **4:15 PM** (`journal.review_at`) | **The report**: the session's daily candles for every stock, the market's biggest movers and what the bot made of them, then the bot's own trades, mistakes and missed plays. |
| 9:00 PM | IB Gateway's daily restart — the app rides through it (see below). |

So yesterday's top movers *are* what the morning works from: the heat ranking is mostly
yesterday's relative volume and move. A stock that starts moving overnight is caught by
the gap check, as long as it was in the hot list or a buffer; the report counts the
ones that weren't, every day.

## Running 24/7

The app is built to be left running:

* **IB Gateway's daily restart** (set it to *Auto restart* at 9:00 PM ET - see *Connecting IB Gateway*).
  The dashboard says the Gateway disconnected; the app keeps trying (5 s, 10 s, 15 s, 30 s, then every
  minute or two) and reconnects once the Gateway has logged back in. A Gateway that takes connections
  before it has loaded the account isn't trusted - the app tries again - and for a minute after any
  reconnect, positions aren't compared with the trade records and working orders aren't given up on,
  so nothing is deleted because an answer came back half-loaded.
* **IBKR's nightly maintenance** (about 11:45 PM-12:45 AM ET). When the Gateway stays up but loses
  IBKR's servers, the app waits on the same connection for IBKR's all-clear; if that hasn't come after
  10 minutes it starts the connection afresh.
* **The weekly login.** About once a week IBKR wants a full login (with two-factor). If the Gateway
  has been gone for 10 minutes, the dashboard says so - log in and the app picks up by itself.
* **A replay running at 9 PM** waits up to 15 minutes for the Gateway instead of failing.
* **A slow answer never reads as an empty account.** An account or positions read IBKR doesn't
  answer in time (the Gateway busy with a big download, say) keeps the last snapshot, which then
  counts as stale: no record is deleted or closed on its say-so, no exit is refused for a position
  that "isn't there", and the shares-without-a-record list doesn't blink.
* **Days on end** - candles and quotes nothing has asked for in half an hour are let go, the log
  rotates at 5 MB, and the scans, the reports, Autopilot's daily counts and the pairs roll over by date.
* **Sleep.** While the app runs it asks Windows not to go to sleep (the screen can still turn off);
  set `app.keep_awake: false` to stop that.
* **Crashes.** `scripts\run_24_7.bat` starts the app and starts it again 30 seconds after it stops
  unexpectedly; quitting from the dashboard ends it. A shortcut to it in the Startup folder
  (`Win+R`, `shell:startup`) brings the app back after a reboot - Windows Update's included.

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

An open-trade record whose position no longer exists where it was opened —
closed in the broker's own app, or by an exit that filled while the app was down
— is **closed from the broker's fills**: the exit-side fills of the stock since
the entry give the exit price and time, so the trade's outcome reaches the
history, the journal and the strategy records (exit reason `closed-outside`).
When the broker reports no such fill (IBKR keeps only the current session's) the
record is **deleted** instead, as after **Reset paper**. A wrong deletion would
orphan a real position, so the check is strict: it only acts on a connected
broker's fresh account snapshot, after the connection has been up a minute, for
trades older than 90 s whose close isn't in flight, and after two misses in a
row. Closed trades are never deleted, and the broker order audit log is always
kept.

The reverse case is shown too: **shares without a record** — held at the broker
beyond what the open-trade records cover, because they were bought or sold
outside the app or a fill couldn't be booked — are listed under **Open
positions** with their own **Exit** button. The app doesn't manage their exits.

---

## Learning from what happened - the training set

Every play the app acts on is kept with **what it looked like at the decision**, so a model can
later learn which plays pay. `tos_bot/research/features.py` turns a play into one flat row of
features - the setup's confidence, odds, reward:risk and geometry, its noise flags and
confirmations, the time of day, the market regime, the stock's relative volume, gap and range,
the volatility forecast, the price character, the abnormal-move reading, the sessions to
earnings - always the same keys, in the same order, with `None` where a reading wasn't
available. `FEATURE_SCHEMA` goes up when a key is added or changes meaning, so rows written
by different versions can be told apart; changing the app elsewhere never disturbs them.

Three populations are kept, because training on the trades taken alone would learn the gates'
choices rather than the market:

| rows | where | written when |
|---|---|---|
| **live** - the trades the app took | `trades.entry_context` (+ `submitted_at`, `mfe_at`) | at the fill, whether Autopilot or you approved the play |
| **shadow** - the plays shown and not taken | `shadow_trades` | by the 16:15 review, which follows each day-trade play on the session's candles as if it had been taken |
| **replay** - the simulated trades | `sim_trades` (one set of rows per run) | when a replay finishes; `held_out` marks its out-of-sample sessions |

Export them as one CSV, with a `source` column, while the app keeps running:

```
python scripts/export_training_set.py            # -> data/research/training_set.csv
```

The data collection is passive: the app trades as usual and the rows accumulate. See
`docs/AutoTradeBot-learning.pdf` for how they are meant to be used.

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
- **Day trades and swing trades share it** while the **Intraday** and **Swing** filters are both
  on - 75% / 25% by default (`account.day_trade_pct`, or the slider that appears next to those
  filters). Each kind may hold up to its share at once (pair trades count as swing trades); a
  trade that doesn't fit what's left of its share is made smaller. With only one of the two
  filters on, that kind gets all of it. Risk per trade is still measured against the whole
  trading capital, and with no trading capital set the whole account is split the same way.

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
| **take half off at the first target**, stop to break-even, the rest runs to the second target (Aziz) | 50 % | `scale_out_pct`, `scale_out_lock_r` (plays with one target exit whole) |
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
| which trade types it may take | the **Intraday** / **Swing** / **Pairs** boxes over the plays, switched live | `trade_types` (`PAIRS`) |
| minimum strategy confidence, day trades | 0.62 | `min_confidence` |
| minimum strategy confidence, swing trades | 0.5 | `min_swing_confidence` (the swing setups state flat 0.55–0.58 confidences; the replay's proof is their real gate) |
| minimum reward : risk | 2.0 | `min_reward_risk` (Aziz Rule 5) |
| concurrent open auto positions | 2 | `max_auto_positions` |
| auto trades per session | 3 | `max_auto_trades_per_day` |
| aggregate open auto $-risk | 4 % of equity | `max_open_risk_pct` |
| concurrent auto trades from **one** strategy | 2 | `max_per_strategy` |
| new auto entries **per scan** | 1 | `max_new_per_cycle` |
| **cool off** a ticker after it stops out today | on | `cooldown_after_loss` |
| **stop for the day** once today's closed trades have lost this % of equity | 2 % | `max_daily_loss_pct` (Aziz's daily maximum loss; 0 = off) |
| **stop for the day** once the day's realized gain has given back this % of its best | 30 % | `max_giveback_pct` (Aziz: never lose more than 30 % of what the morning made; `giveback_floor_pct` 0.25 % of equity is the smallest gain that counts; 0 = off) |
| require a catalyst / dry-run | off | `require_catalyst`, `dry_run` |

The filters and the Strategies panel apply to Autopilot too, and it takes no
entries while the app is quitting. It still passes every other check — session
validity, position sizing, the PDT guard, the $2,000 live floor. Valuation plays
are never auto-traded. Eligible plays get a **🤖** marker.

**Proof, noise and size.** A day-trade setup must show up in `min_confirmations`
(2) scans in a row, and plays carrying a flag in `skip_noise` are skipped. With
`require_proven` on, a strategy is auto-traded only once the replay (**Strategies
→ Run replay**) has at least `min_replay_trades` (30) trades Autopilot would have
taken, averaging at least `min_replay_expectancy_r` (+0.05R) — **and** at least 10
of them in the held-out latest third of the sessions, averaging more than 0R
there. The statistical noise checks (`not_trending`, `not_mean_reverting`,
`turbulent_market`) and the two news checks (`news_driven_move`,
`move_without_news`) are skipped by themselves once the replay shows that the
trades they remove did worse on every session and on the held-out ones. Each
entry risks no more than `risk.max_risk_per_trade_pct`, lowered to **half-Kelly**
when the strategy's record calls for less.

**Faster loop while day-trading.** When Autopilot is armed with the **Intraday** filter on and
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
- Headlines are scored from -1 (negative) to +1 (positive) by FinBERT on this computer when it is installed:
  `pip install transformers torch` (about 140 MB of packages); the model itself (ProsusAI/finbert, 438 MB) is
  downloaded from Hugging Face the first time a headline is scored and cached under `~/.cache/huggingface`.
  Without it headlines carry no sentiment and the news nudge does nothing.

**What the signals do**
- **Insider buying** (`insider_buying`, a swing setup): unusual buying in the last 10 days, while the stock is no
  more than 15% above what the insiders paid. The stop goes under the recent swing low, one to two daily ranges
  away, and the target is three times the risk. It is on in `config.example.yaml`; if your own `config.yaml`
  doesn't list it, switch it on in the Strategies panel. The strategy replay has no insider history to test it
  on, so Autopilot won't take it while `autopilot.require_proven` is on.
- **Score nudges** on every other play: unusual insider buying adds up to 0.10 to a long (and takes it from a
  short), unusual selling takes up to 0.08 from a long (and adds half that to a short), and the news moves a play
  by up to 0.06 once two or more headlines are scored. The play's evidence shows each nudge and why.
- **The earnings calendar** (Finnhub, with the key; read every 6 hours, one request for every company):
  - *Post-earnings drift* counts a report from the calendar as well as from SEC's 8-K - the 8-K often comes
    later than the press release. When the calendar has the reported EPS, a gap the other way from the
    surprise, `(actual - estimate) / |estimate|`, isn't taken (Chan, *Quantitative Trading*: buy the beats,
    short the misses).
  - A momentum or reversal **swing** play with a report due within its hold is flagged `earnings_ahead` - a
    report can gap the price through the stop. A swing position already held into a report due by the next
    session gets a warning on the dashboard, once.
- **Moves with and without news** (`quant/market_model.py`): the market model from Tsay (ch. 9),
  `r_stock = alpha + beta * r_SPY + e`, fitted on the last 60 sessions, says how far today's move goes beyond
  what the market explains: `z = (r_stock - alpha - beta * r_SPY) / sd(e)`. From `|z| >= 2` the news decides:
  Chan finds moves on news keep going and moves without news tend to be taken back. A reversal setup fading a
  big move that came with news is flagged `news_driven_move`; a momentum setup chasing a big move with no news
  `move_without_news`. Neither is claimed for a stock whose news isn't being read. The play's Statistics show
  the move, the market's part, z and the stories found.
- These three flags start as information: Autopilot doesn't skip them unless you add them to
  `autopilot.skip_noise`, and each day's journal compares how the flagged plays not taken would have done.
  The replay measures the two news flags from the headlines the app has stored (each story counts from
  the moment it was published, and the S&P 500 ETF's candles feed the market model), so as the store
  grows, Autopilot learns to skip them the way it learns the statistical checks. The earnings flag
  needs a calendar history the app doesn't keep.
- They are kept in the `insider_trades`, `filings_read` and `news_items` tables, and the calendar in
  `data/signals/earnings_calendar.json`.

**The Signals page** (header → **Signals**)
- **Sources**: when SEC's filings and the news were last read, whether IBKR's news feeds are connected, whether a
  Finnhub key is saved, FinBERT's state and how many headlines it has scored, and any check that failed.
  **Check now** reads the filings and the news straight away (after adding a Finnhub key, say).
- **Stocks with signals**: insider buying and selling (score, dollars, insiders, who), the news tone, recent
  material 8-Ks, and exactly what each does to a long and a short play's score. Click a stock for its insiders'
  filings over the past year, why the wave counts as unusual, and its news of the last 30 days.
- **Insider filings**: every Form 4 trade of the last 30 days with the day traded, the day filed and how many
  business days late - with the median delay and the share that met SEC's two-business-day deadline. The app
  reads SEC's feed every 3 minutes, so the delay shown is the insiders'.
- **Headlines**: the latest stories with their source and FinBERT's tone.
- Plays whose score a signal moved carry a **signal** badge; click it for that stock on the page.
- API: `GET /api/signals`, `GET /api/signals/stock/{symbol}`, `POST /api/signals/check`.

## Dashboard controls

* **Paper / Live** — which side you're trading. Going Live asks for confirmation.
* **Data pill** — `data: IBKR`, `data: IBKR (delayed)` or `data: none`.
* **Market pill** — `market: calm` or `market: turbulent`, from Hamilton's regime model on SPY; hover for the numbers.
* **Connection pill** — what orders go to and whether it's healthy: `Simulator`,
  `IBKR paper ●`, `IBKR live ✕`. Click it to open **Connections**. A banner
  appears when the Gateway needs you.
* **Connections** — paper platform, IB Gateway settings, **Test paper / Test
  live**, **Reconnect**.
* **Settings** — the scan schedule and list sizes, the scan status, **Run full
  scan now** and **Rescan hot list now**.
* **Signals** — insider trades and company news, their sources and what they do to plays (see [Signals](#signals--insider-trades-and-company-news)).
* **Reports** — each session's report (see [Reports](#reports--every-session-the-market-and-the-bot));
  a dot marks one you haven't opened.
* **Scan now** — rescan the hot list and the next buffer names. The plays header
  shows the last scan, a progress bar while one runs, and when the next full
  scan is due.
* **Long / Short / Intraday / Swing** and **Sectors** — what the bot scans for
  and may trade.
* **Strategies** — switch setups on or off and weight them.
* **◐** — light / dark theme.  **↻ Refresh** — re-pull account, positions and fills; when IB Gateway
  came up after the app, it connects at once instead of waiting for the background retry. The button spins
  until the answer is in and then says what happened, so there is no need to press it again.
* **Trading capital** — how much of the account the bot may use.
* **Autopilot** + **⚙** — hands-off entry and its caps.
* **Reset paper** (simulator only) — reset the balance.
* **Exit** / **Exit all** — close one position, or all of them, at the market.
* **Quit** — close out, then shut down.
* Bottom tabs: **Open positions**, **Active orders**, **Trade history**, **P/L summary**, **Watchlist**, **Pairs**.

---

## What the books taught it — the quantitative layer

The setups come from Aziz, Murphy and Pignataro. A second shelf of books on
algorithmic trading and time-series econometrics decides whether a setup can be
trusted *right now*, how much to risk on it, and how its record is judged. The
models are plain numpy in `tos_bot/quant/` (no statistics packages), and each one
is tested on simulated series whose answer is known.

| book | what it adds | where |
|---|---|---|
| Chan, *Algorithmic Trading* | Hurst exponent, variance ratio, ADF test and half-life (ch. 2, 6); buy-on-gap (ch. 4); post-earnings drift (ch. 7); regimes and risk (ch. 8) | `quant/stationarity.py`, `quant/readings.py`, the noise checks, `strategies/statistical.py` |
| Chan, *Quantitative Trading* | backtests with transaction costs and an out-of-sample test (ch. 3); Kelly sizing (ch. 6) | the replay, `quant/sizing.py`, `research/weights.py` |
| Tsay, *Analysis of Financial Time Series*; Enders, *Applied Econometric Time Series* | GARCH(1,1) and RiskMetrics volatility forecasts | `quant/volatility.py`, the stop floor |
| Hamilton, *Time Series Analysis* | the Markov switching model of calm and turbulent markets (ch. 22) | `quant/regime.py`, `engine/market_regime.py` |
| Vidyamurthy, *Pairs Trading*; Johansen; Juselius | cointegration (Engle–Granger, Johansen), zero crossings, the entry-band design | `quant/cointegration.py`, `quant/bands.py`, `pairs/` — see [Pairs trading](#pairs-trading) |

**On every play** (open a play → *Statistics*):

* **Price character** — the Hurst exponent and variance ratio of the candles the
  setup trades on (three sessions of 5-minute closes for a day trade, 120 daily
  closes for a swing): *trending*, *mean reverting* or *random walk*. A momentum
  setup on a price that keeps snapping back is flagged `not_trending`; a reversal
  setup on a trending price, `not_mean_reverting`. A reversal setup's expected
  hold is its price's half-life.
* **Tomorrow's volatility** — the GARCH(1,1) forecast from the completed daily
  candles. While it is above the last 60 days' volatility, stops are floored at
  1.4 forecast standard deviations for a swing trade and 0.35 for a day trade:
  volatility clusters, so a normal-looking stop would sit inside tomorrow's noise.
* **The market's regime** — Hamilton's two-regime model on SPY's daily returns,
  refitted once a session and shown on the header's **market** pill. Momentum
  setups are flagged `turbulent_market` while the chance of the turbulent regime
  is 70 % or more.
* **Evidence weight** — each strategy's weight in the ranking is multiplied by
  what its record says (0.5× to 1.5×): its replayed trades and its real ones,
  each real trade counting twice, shrunk toward "no edge" as if 50 trades at 0R
  came first. A strategy losing money on the held-out sessions or in real
  trading is never raised.
* **Half-Kelly risk** — half of Kelly's mean ÷ variance of the strategy's R
  multiples (its real trades once there are 30, otherwise the replayed ones),
  capped at `risk.max_risk_per_trade_pct`. It can only lower the risk.
* **Odds from the record** — the "estimated odds the edge pays" in a play's
  explanation start as the setup's own read and are blended with the win rate
  of its replayed and real trades (each real trade counting twice), 30 trades
  of record weighing as much as the setup's read. Douglas: an edge is a
  probability over a series of trades; Chan: measure it. Those odds drive the
  play's expected value, and so its score and the `low_expected_value` flag.
  The replay never uses them, so it can't flatter itself.
* **Mid-day size** — a day trade sized between 12 and 3 pm ET risks
  `risk.midday_size_pct` (60 %) of the usual: Aziz's thin, choppy hours, when
  he lowers his share size. Swing trades are untouched.

**Two statistical day trades** (`tos_bot/strategies/statistical.py`; on even when
an older `config.yaml` doesn't list them):

| key | idea |
|---|---|
| `gap_reversion` | an open more than one standard deviation of daily returns below yesterday's low while still above the 20-day average (the mirror for shorts): the gap tends to be partly won back during the day. Targets yesterday's low, then its close. |
| `earnings_drift` | an earnings release (SEC 8-K item 2.02) accepted after the previous close or before the open, and a gap of more than half a standard deviation of the stock's usual overnight moves: the price tends to keep drifting that way through the day. |

### The replay — judged the way Chan judges a backtest

**Strategies → Run replay** walks recorded candles the way the scans see them and
follows every play the way the automatic exits would. Its settings are in
`config/config.yaml → replay`:

* **It uses every core but two** (`replay.workers`, 0 = automatic): each stock is replayed in its own
  worker process, and a day-trade replay is cut into jobs of `sessions_per_job` (10) sessions so the
  long ones don't leave workers idle at the end. Series that are the same for every bar of a session
  (the session VWAP, the opening range) are computed once per session.
* **Which stocks.** Day-trade setups replay on the hot list and the kept buffer names (their 5-minute
  candles cost IBKR requests). Swing setups replay on every watchlist stock **and on the full scan's
  400 leaders** (`replay.swing_stocks`; 0 = the watchlist only) - their daily candles are already on
  disk, so a strategy's record rests on hundreds of stocks rather than the day's forty.
* **60 day-trade sessions and 250 swing sessions** by default. Sixty sessions give
  most setups the 30+ trades a record needs *with* a month held out; 250 is a
  year, as far back as the stored daily candles go. Up to 120 day-trade sessions
  can be asked for — 5-minute candles are downloaded once and kept.
* **Costs**: 5 bps slippage on every market fill and 1 bp commission on every fill.
* **Held out**: the latest third of the sessions. Every strategy record and every
  noise verdict is also given for those sessions alone, and Autopilot wants a
  strategy to have made money there too.
* **No look-ahead**: a day's regime comes from a model fitted on the days before
  the replay, the volatility forecast from completed candles, and an earnings
  report counts from the moment SEC accepted it.
* Every run is summarised in `data/research/replay_runs.jsonl`, so the records
  can be followed from one run to the next.

## Reports — every session, the market and the bot

At `journal.review_at` (16:15 ET) the bot writes a report on the session, kept in
the database (`daily_reviews`) and in `data/journal/`. **Reports** in the header opens
it — the newest first, every earlier one a click away.

### The market's movers

`research/movers.py`. The biggest gainers and losers of the session among every stock the
scanner could trade that day (`journal.movers`, 10 of each). To rank them the bot downloads
the session's daily candle for every stock after the close — the same download the next
morning's scan would make, made the evening before, so IBKR is asked nothing extra and the
morning scan is quicker. If IB Gateway isn't connected, the report is written without them
and they're added once it is. For each mover:

* **why it moved** — the session's stories from IBKR's news feeds, SEC 8-K filings and
  Finnhub (with a key), read from the previous close to this close, the likeliest cause
  first: earnings (an item 2.02 filing or an earnings headline), another material filing,
  an analyst action, a news story — or, with no story, its whole sector moving with it;
* **how it moved** — gapped at the open or built during the session, volume against its
  20-day average, the move in average daily ranges, a close at a 20-day high or low;
* **what the bot made of it** — *traded* (with the move or against it, R and P/L),
  *offered, not taken* (and what the setup would have made, followed on the candles),
  *watched, no setup* (on the hot list, adopted into it, or scanned from a sector buffer),
  or *not watched* — and why: the morning's ranking put it #412 of 2,950, it was too thin
  for the scanner's filters the day before, or its sector is switched off;
* **charts** (📈) — the session's 5-minute candles with the bot's entries and exits, the
  setups it offered and the news as it came out, and the daily candles around the day.

Above the table, the score: how many of the movers were traded, offered, on the morning
watchlist and moved at the open — and over the last 20 sessions, the share of the market's
biggest movers the watchlist held. That share is the scanner's report card: if most movers
make their move at the open on overnight news, a ranking of the previous day's candles
can't see them, and the report says so.

### The bot's own trading

* **the trades** that closed, each with what it was taken on — noise flags, scans
  in a row, price character, the market's regime, who took it;
* **mistakes** — a loss beyond the planned 1R, a winner of 1R or more closed at a
  loss, a trade taken through a noise flag or before it was confirmed, going
  straight back into a stock that had just lost, a strategy without a proven
  record;
* **the plays not taken**, each followed on the session's 5-minute candles as if
  it had been — grouped by noise flag and by whether Autopilot's checks passed
  it, so every check is tested on live plays every day;
* **each strategy's real record** over the last 20 sessions against its replay,
  flagged when it falls more than 0.3R a trade short;
* **lessons**, in plain sentences. **Rebuild the last session** writes it again on demand
  (the movers are kept, and rebuilt once the session's candles are in).

---

## Pairs trading

`tos_bot/pairs/`. Two stocks from one industry whose log prices are cointegrated drift
apart and come back together. When their spread strays past its band the bot buys one
and shorts the other, and both legs come off together. From Vidyamurthy's *Pairs
Trading* and Chan's *Algorithmic Trading* (ch. 2-4 and 8).

**Finding them** - after each full scan, among the watchlist's stocks grouped by IBKR
industry, plus the ETF pairs in `config/config.yaml → pairs.etf_pairs`:

* daily returns correlated 0.6 or more (only to narrow the search), $10M+ traded a day, $5+;
* Engle–Granger cointegration at 5% with a positive hedge ratio, and Johansen's trace test agreeing at 90%;
* a spread half-life of 2-30 sessions and at least 6 crossings of its mean;
* at most two pairs per industry, each stock in one pair, the 12 most strongly cointegrated kept;
* fitted on the last 200 sessions - and tested on the latest 100 after being fitted on the 200
  before them. A pair that wasn't a pair back then isn't watched: Chan's warning is that stock
  pairs often stop being pairs.

**The rules** - the spread's z-score over a lookback equal to its half-life; an entry band from
Vidyamurthy's design, net of costs (between 1 and 2.5 standard deviations); exit back at the mean;
a stop 2 standard deviations beyond the band; a time stop of two half-lives. Entries and those
exits are decided in the last 30 minutes of the session, the way the replay reads its closes;
a pair that has lost twice its planned risk is closed at once, any time of day.

**Size** - the first stock's dollars are the risk per trade (half-Kelly when the pairs' record
calls for less) over the z distance to the stop times the spread's standard deviation; the
second leg is the hedge ratio times that. Each leg stays within `max_position_pct_of_equity`,
both together within the buying power and the trading capital.

**Safety** - both legs are market orders in the regular session. If the second can't be sent,
or both haven't filled within two minutes, what did fill is closed again: a pair is never left
half on. The legs carry no stop or target of their own, so the regular exit manager leaves them
to the pair desk; if one leg is closed outside it (Exit, Exit all, quitting, the broker check),
the other is closed too. Live pairs need $25,000+ in a margin account.

**The Pairs tab** - the pair trades on (both legs, z at entry and now, open P/L and R, sessions
held, **Exit pair**); the pairs being watched (hedge ratio, half-life, band and stop, where the
spread is now, the signal, how the rules did out of sample, **Enter**, and a 📈 chart of the
spread with its band, stop and past trades); and the recent pair trades.

**Replay and Autopilot** - **Strategies → Run replay** also chooses pairs on the sessions *before*
the replayed ones and trades them only on those, paying costs on both legs; their record is
`pairs_reversion`. Autopilot takes pairs only with the **Pairs** box over the plays ticked, once that
record is proven (held-out sessions included), and at most `pairs.max_new_per_day` a day.

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

Also: `gap_reversion` and `earnings_drift` (see [What the books taught it](#what-the-books-taught-it--the-quantitative-layer)) and `insider_buying` (see [Signals](#signals--insider-trades-and-company-news)).

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
    engine.py                the loops, scans on schedule, orders and positions, operator actions, snapshot
    research_ops.py          the strategy replay and the evidence: records, weights, calibrated odds, half-Kelly
    journal_ops.py           the daily journal, the movers report and the Reports page
    pairs_ops.py             pairs trading from the engine's side
    capital_ops.py           trading capital and its day / swing split
    quit_ops.py              quitting without stranding a position
    support.py               small helpers the engine and its mixins share
    connections.py           the one IB Gateway connection + the simulator
    board.py                 the plays on the dashboard
    capital.py               trading capital
    reconcile.py             when an open-trade record counts as gone at the broker
    runtime.py               data/runtime.json
    views.py                 pieces of the dashboard snapshot
    market_regime.py         calm or turbulent, from SPY's daily returns (Hamilton ch. 22)
    chart.py                 a play's candles and its exit routes
  core/                    enums + framework-free dataclasses + event bus
  data/                    listings, symbols, daily bar store, market data, SEC EDGAR, fx, sectors
  indicators/ta.py         vectorised TA (no TA-Lib) incl. divergence, relative volume, beta
  analysis/                horizontal S/R clustering + candlestick reads (Aziz / Murphy)
  valuation/               EV, multiples, DCF, football field, projections   (Pignataro)
  strategies/              base (shared indicators, Douglas-framed explanations) + registry + technical + fundamental
                           + insider + statistical (Chan's gap and earnings setups)
  quant/                   the books' models in numpy: stationarity, cointegration, volatility, regime, sizing, bands, readings
  research/                the replay (replay, runner, history), the evidence weights, the daily journal and the movers report
  signals/                 SEC Form 4 insider trades, company news, 8-K earnings dates, the signal book
  pairs/                   pairs trading: the model, the finder, the backtest and the desk that trades both legs
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
trading capital and exchange rates. The quantitative models are tested on simulated
series whose answers are known (a mean-reverting series' half-life, a cointegrated
pair's hedge ratio, a GARCH process's parameters, a two-regime market), and the
journal on a scripted session. The real IB Gateway isn't exercised in tests.

## Roadmap

- a native stop resting at the broker as a crash-safety backup, kept in sync with the exit manager
- streaming IBKR ticks (`reqMktData` subscriptions) for the hot list instead of candle polls
- options plays
- equity-curve chart in the dashboard
