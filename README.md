# AutoTradeBot

A **human-in-the-loop** trading assistant for Interactive Brokers.

> **Disclaimer.** This is a personal project for education and research, run on an IBKR **paper** account.
> Nothing in it is financial advice or a recommendation to buy or sell any security. Trading carries a real
> risk of loss, and automated trading can lose money quickly - through a bug, bad data, a broker outage or
> a strategy that simply has no edge. Use it at your own risk; the software comes with no warranty (see
> [LICENSE](LICENSE)).

Built by **Omar Abdeen** ([omarabdeen123@gmail.com](mailto:omarabdeen123@gmail.com)) with
**[Claude Code](https://claude.com/claude-code)**, Anthropic's AI coding assistant, as a pair programmer - see
[How it was built](#how-it-was-built).

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
A third shelf (2026-09-18) is about **not fooling yourself**: **Aronson,
*Evidence-Based Technical Analysis*** (is a record luck?), **López de Prado,
*Advances in Financial Machine Learning*** and **Jansen, *Machine Learning for
Algorithmic Trading*** (the learned model), **Tharp, *Trade Your Way to Financial
Freedom*** (the quality of an R-multiple distribution), **Carver, *Systematic
Trading*** (costs), **Harris, *Trading and Exchanges*** (what fills cost), and two
more books of setups, **Grimes, *The Art and Science of Technical Analysis*** and
**Bulkowski, *Encyclopedia of Chart Patterns***.

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
  about it, with a chart of the trade: the candles from the session before the
  entry, where the bot got in and out, the stop and the target, and how it
  stands, kept live while it's open; a crosshair reads the price under the
  mouse, and a drag across candles measures the move as this position's gain or
  loss. An open-trade record whose position no longer exists at the broker is
  deleted automatically.
- **Trading capital** — tell the bot to use only part of the account.
- **A report on every session** — after the close: the market's biggest movers,
  why each one moved (earnings, filings, analyst actions, news, its sector) and
  whether the bot traded it, sent an entry that never filled, offered it, watched it
  or missed it, with charts; then each of the bot's trades with what it was taken on,
  the mistakes, how the plays it didn't take would have done, and each strategy's real
  record against its replay.
- **Proof before Autopilot trades** — every strategy is replayed on past candles
  with costs, and has to make money on the held-out latest third of them too.
- **Pairs trading** — cointegrated stocks from one industry, one long and one short,
  entered and closed together (see [Pairs trading](#pairs-trading)).
- **Light / dark theme** — follows your OS setting; one click to switch.
- **Strategies:** 19 technical day-trade / swing setups (2 statistical ones from Chan, 3 from Grimes and Bulkowski),
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
fill, orders meant for an IBKR account are refused until it's back, and the data
pill reads `data: none`. Nothing is ever made up.

---

## How scanning works

`autotradebot/scanner/` — the schedule is in `schedule.py`, the ranking in `heat.py`,
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
waiting. It takes two to three minutes of Gateway time and the 5-minute cycle
waits for it. (The Gateway takes about half a second over a candle request
whether it asks for an hour or a week, so the rate is set by how many are in
flight: twelve at once read about 17 stocks a second, none refused in a test of
700; thirty-two timed out.) The plays it finds stay on the board and are
re-checked every 15 seconds like any other. **Settings → Scan every stock now**
runs it on demand.

**The movers hold slots of their own.** After each wide scan, today's biggest movers
(by % change since yesterday's close, on at least 1.5× their usual volume; default
10) take hot-list slots outright, whatever their sector — Aziz's stocks in play trump
sector diversity — each replacing the coolest hot-list name that isn't a mover
itself. The full scan does the same for the last session's biggest movers (default
10), in case they move for a second day. The Watchlist tab marks them and says why.

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
| Today's movers on the hot list | 10 | 0–20 | `movers` (`movers_min_rvol` 1.5) |
| Yesterday's movers on the hot list | 10 | 0–20 | `yesterday_movers` |

Also in `config.yaml`: `gapper_symbols` (400), `gapper_min_gap_pct` (2), `gapper_min_volume`
(50,000), `buffer_picks_per_sector` (2), `kept_per_sector` (2),
`fast_cycle_seconds` (60), `close_check` (true) and `mover_atr` (1.0) (see
**Candle-close check**), `live_scan` (10; see **Live scans**),
`fundamentals_leaders` (8), the liquidity `prefilter`,
and `max_universe` (0 = every listing; `SCANNER_MAX_UNIVERSE` caps it without
editing the file).

The **Watchlist** tab (bottom panel) shows the hot list with each stock's daily
and intraday heat, each sector's buffer (kept names, how many looked at, how
many still queued) and the latest adopt / keep / drop decisions.

---

## Where orders go

Two switches, both in the dashboard and remembered in `data/runtime.json`
(`.env` only supplies the starting value). The routing lives in
`autotradebot/brokers/venues.py`:

| Paper / Live | Paper platform | IB Gateway connection | Orders go to |
|---|---|---|---|
| **Live** | (either) | live account, trading | your **real** IBKR account |
| Paper | **IBKR paper account** | paper account, trading | your IBKR paper account |
| Paper | **Built-in simulator** | paper account, read-only (prices only) | the built-in simulator |

- If the Gateway isn't reachable, orders stay pointed at the IBKR account the
  switches choose and are refused until it answers — never sent to the simulator
  in its place — and the header pill turns red with the fix; the app connects by
  itself once it answers. **Live** is never faked: a switch to Live that can't
  connect is refused and tells you why, and an app already on Live stays on it,
  its orders refused, until the live account is back.
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
on its own (a live account it was already on is connected again the same way),
and it never moves orders away from open positions — the connection pill says why.

**Accounts in another currency.** An IBKR Canada account is kept in CAD. The
header shows equity, cash and buying power in the account's own currency (hover
for the USD figure); trades are sized in US dollars — at IBKR's own rate when it
sends one, otherwise the European Central Bank's daily reference rate. With no
rate at all nothing is sized, and the order card says so.

**No market-data subscription** (the default for a paper account). The Gateway
still serves historical candles, so scans run on IBKR's candles, and prices for
stops, targets and simulator fills come from the latest one-minute candle. The
data pill shows `data: IBKR (delayed)`; hover it for the detail.

**Real-time streams.** Once the connection has proven real-time data, the app
holds IBKR streams for the stocks that matter most: the open positions and
working entries first, then the plays still on offer - the ones you opened in
the detail panel in the last 5 minutes, then the ones Autopilot would take, then
the best of the rest (a play keeps its stream at least a minute) - then the
watch tier: the day's hot list, the movers from IBKR's live scans (see **Live
scans**), the kept buffer names and the buffer names the next cycle looks at,
up to `execution.stream_watch` (50; 0 = none) - all within
`execution.stream_lines` (80 of the account's ~100 market-data lines; 0 = none).
If IBKR says every line is in use (error 101), the watch names give theirs back
first, then the plays - never the positions.
A stock whose stream ticked in the last 2 seconds is priced off it; a quieter
one, or one past the budget, gets a one-off snapshot as before - an entry never
waits for a stream. Delayed data never streams. Each streamed stock also gets
live 1- and 5-minute candles built from its ticks and closed on the clock, for
speed and show only: the setups, confirmations and the replay still run on
IBKR's own 5-minute candles.

**Candle-close check.** At every 5-minute close in regular hours (09:35 to
15:55) the watch tier's setups are checked on IBKR's just-closed bars about 2
seconds after the close - one request per stock for the last hour of bars - so
a setup is typically published 5-8 seconds after its candle closes instead of
up to a couple of minutes later. The check stands in for the fast cycle due
then. Between closes, a watch stock whose streamed 1-minute candle spans at
least `scanner.mover_atr` (1.0; 0 = off) of its 5-minute ATRs, or makes a new
high or low of the day on 3x its average minute volume, gets an early-mover
check at once (at most once per 5 minutes). An early-mover check can't add a
candle confirmation - its 5-minute candle hasn't closed - so it catches
price-level setups and refreshes the stock's plays. `scanner.close_check:
false` turns both off (the fast cycle as before). The log says `close check
10:05 ET: ... published 5.4 s after the candle closed` or `early-mover check
10:07 ET (...)` for each one, and once a session `live candles vs IBKR
5-minute bars` with the volume ratio that tells whether the stream counts
shares or lots of 100.

**Live scans.** Every minute in regular hours the app runs three of IBKR's
live market scans - the biggest % gainers, the biggest % losers and the
stocks hottest by volume (US stocks and ADRs at $3-600), one open at a time
and each cancelled once it answers. The first `scanner.live_scan` (10; 0 =
off) of their names that are ordinary shares in the sectors the filters
allow, with daily candles and not on the hot list already, take watch-tier
slots right after the hot list - so a stock that was quiet until today is
streamed and checked at each close like the rest. Liquidity is judged on
today's volume: after a candle-close check, a live name whose dollar volume
so far is under the prefilter's `min_dollar_volume` pro rata for the time of
day (at least 30 of the session's 390 minutes) is left out for the rest of
the session, and sizing's `risk.max_adv_pct` cap still limits any order on
these stocks. The log says `live scan: +A +B -C` when the names change.

> Every position's stop, and its target as a one-cancels-the-other partner, rests at IBKR as a native
> order, so a position stays protected while the app is closed. The app's own exit manager moves the stop
> (break-even, trailing) and handles the time exits - those need the app running.

### Keeping your IBKR account safe

IBKR's API has **no password of its own**: any program that can reach the Gateway's API port can read the
account and place orders, with no login. Two locks keep that port to this computer:

1. **In IB Gateway** (*Configure → Settings → API → Settings*): tick *Allow connections from localhost only*,
   keep `127.0.0.1` as the **only** entry under *Trusted IPs* (remove anything else), and use the standard
   ports (4002 paper, 4001 live). If you only want to watch, leave *Read-Only API* ticked - the app then can't
   trade. Leave *Master API client ID* empty.
2. **In the operating system's firewall**, block those ports from the network, so even a Gateway setting
   changed by mistake can't expose them. On Windows, in PowerShell run as administrator:

   ```powershell
   New-NetFirewallRule -DisplayName "IBKR API - block from network" -Direction Inbound -Protocol TCP -LocalPort 4001,4002,7496,7497 -Action Block
   ```

   Windows Firewall doesn't filter traffic on the computer itself (loopback), so the app still connects;
   everything from another device is dropped, and a block rule wins over any *allow* rule Java or the
   Gateway's installer added. Check it with `Get-NetFirewallRule -DisplayName "IBKR API - block from network"`.
   On Linux: `sudo ufw deny 4001:4002/tcp` and `sudo ufw deny 7496:7497/tcp`.

And around it:

- Keep `IBKR_HOST=127.0.0.1`. The API connection isn't encrypted, so never point the app at a Gateway on
  another machine across a network you don't control.
- The app never stores your IBKR username or password - the Gateway holds the login. If you use IBC for the
  daily login, its `config.ini` holds your password in plain text: keep it in IBC's own folder (not in this
  repository), readable only by your Windows user, and never commit it.
- Turn on two-factor login for IBKR (*IBKR Mobile* → *Secure Login System*) and for GitHub.
- Your keys live in `.env`, which is git-ignored - never commit it. On a public GitHub repository, turn on
  *secret scanning* and *push protection* (Settings → Code security) so a key pushed by mistake is blocked.
- Keep `WEB_HOST=127.0.0.1` so the dashboard is only served to this computer (see below).

### Security of settings

- The app only answers **this computer**. Every request needs a loopback
  client, a `localhost`/`127.0.0.1` Host header and no foreign `Origin`; an
  `/api/` request made by another website's page is refused, and anything that
  changes something needs the dashboard's own request header (the Connections,
  share-count and quit endpoints want it on reads too). The live feed's
  WebSocket gets the same check before it opens. So another website open in
  your browser can't approve a play, close a position or read your account and
  positions, and a device on your network can't open the dashboard. No other
  website can show the dashboard inside a frame either, so a hidden page can't
  line your clicks up with its buttons. And the page only runs the dashboard's
  own script files (a Content-Security-Policy), so a headline or filing that
  ever reached it unescaped still couldn't run as script.
- Only the IB Gateway settings can be written. Values are validated (no line
  breaks or hidden characters, no `$`, ports in range), `.env` is read without
  expanding `${...}` so one setting can't show another's value, it is replaced
  atomically with your comments and other lines preserved, and the account id
  is **never sent back** — the panel
  and the live feed only show that it's set and its last four characters.
  `.env` is git-ignored.
- A stock's candle file is only named after it when the name looks like a
  stock symbol (capital letters and digits, a share class after a space), so a
  symbol from outside data, such as an insider filing, can't point the app at
  a file somewhere else.

### MySQL (optional — SQLite is the default)

```sql
CREATE DATABASE autotradebot CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'autotradebot'@'%' IDENTIFIED BY 'your-password';
GRANT ALL PRIVILEGES ON autotradebot.* TO 'autotradebot'@'%';
FLUSH PRIVILEGES;
```

```dotenv
DB_HOST=127.0.0.1
DB_PORT=3306
DB_NAME=autotradebot
DB_USER=autotradebot
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
- **Every change made while the app runs reaches Autopilot at once** - the day / swing slider, the
  trading capital, the filter boxes, the strategies, Autopilot's own settings, a switch of account,
  a finished replay. Its gates always read the settings as they are; on a change the plays it had
  refused for the day are handed back to it, the plays are sized again, the dashboard gets
  Autopilot's state and its verdict on every play, and the quick re-check of the board is pulled
  forward so the next pass is seconds away. A change never places an order by itself: entries stay
  in a scan's own pass. (Settings that live only in `config.yaml` still need a restart.)

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
  so nothing is deleted because an answer came back half-loaded. An order that finished while the
  connection was down comes back from IBKR without its order id; the app finds it by IBKR's permId or
  its executions and books it as if it had heard it live. An entry, stop or target IBKR no longer knows
  at all is looked for in IBKR's executions before it is given up, so what it filled is booked.
* **IBKR's nightly maintenance** (about 11:45 PM-12:45 AM ET). When the Gateway stays up but loses
  IBKR's servers, the app waits on the same connection for IBKR's all-clear; if that hasn't come after
  10 minutes it starts the connection afresh.
* **The weekly login.** About once a week IBKR wants a full login (with two-factor). If the Gateway
  has been gone for 10 minutes, the dashboard says so - log in and the app picks up by itself. That
  notice comes at night when the 9 PM restart doesn't come back, so on a trading day a Gateway still
  gone at 07:45 ET (`scanner.gateway_alert_time`, `""` = off) is said again, once, before the full scan
  needs it.
* **A failed connect never moves the orders.** While the IBKR account the switches choose can't be
  connected - at the start, after **Reconnect**, or on Live - orders stay pointed at it and are refused
  until it answers; none go to the simulator (or to paper) in its place, so its positions get their
  exits back the moment it reconnects. The connection pill says so.
* **A replay running at 9 PM** waits up to 15 minutes for the Gateway instead of failing.
* **A slow answer never reads as an empty account.** An account or positions read IBKR doesn't
  answer in time (the Gateway busy with a big download, say) keeps the last snapshot, which then
  counts as stale: no record is deleted or closed on its say-so, no exit is refused for a position
  that "isn't there", and the shares-without-a-record list doesn't blink. The same goes for the working
  orders: one request for them is out at a time (whoever asks meanwhile shares its answer), and one
  IBKR hasn't answered within 10 s is called off and counts as *unknown* - no stop is placed beside
  one that may be resting, an exit still goes out capped by the shares held, and the orders an earlier
  run left working are taken over by the next order sync that can list them.
* **An order IBKR doesn't answer in time is never taken as not sent.** One the Gateway's connection
  hadn't got to when the wait ran out is called off unsent. One it had started may have reached IBKR, so
  the next order syncs look for it there by its tag: it is followed if it is working, booked if IBKR's
  executions show it filled, and taken as never sent only once IBKR shows it neither, half a minute on.
  Nothing goes out in its place meanwhile: an entry's play stays sent and Autopilot keeps its slot
  (handed back if the order never went out), a position's exit waits, and a stop or target found resting
  is taken over rather than placed twice. The dashboard hears of an unanswered exit at once - its stop at
  the broker may already be cancelled for it - and of any order still not found a minute and a half on,
  again every five minutes, even while IBKR's orders can't be read or the Gateway is down.
* **A fill the trade log can't save is saved on the next pass, never lost.** When the database refuses
  to book a fill (busy with another writer, say) - an entry, an exit, or a stop or target IBKR filled - the
  app logs it, the dashboard says so (again every five minutes while it keeps failing), and the order stays
  followed until the next order sync books it. Meanwhile it still counts as working: an entry keeps
  Autopilot's slot, and nothing else is done for the position - no second exit, and its stop is neither
  moved nor placed afresh. An entry's shares are no order in flight, though: until the booking takes they
  have no record and no stop, so Shares without a record lists them, with their own Exit, and the warning
  on shares the records don't explain counts them. That Exit settles them: the entry is no longer booked,
  so no record, stop or second exit appears later for shares already sold (and if the booking took just
  before the click, the Exit sells only what the records still don't cover) - and while any closing order
  works for a stock (that one, or one placed by hand), an exit counts the shares it still has to sell and a
  stop isn't placed beside it (an exit that has filled, its booking waiting, counts for nothing: those
  shares are gone already). A quit doesn't cancel such an entry: it waits for its record and closes it like
  any other before it shuts down, and a pair leg whose pair is called off meanwhile stays followed too -
  once booked, the pairs desk closes it.
* **A restart picks the day up where it left off.** The plays on the board, the setups already traded
  or dismissed this session, the pre-market levels the gap check read and the time of the last wide
  scan are saved as the day goes (`data/day_state.bin`: after a scan at most every two minutes, at once
  when you or Autopilot decide on a play, and when the app stops). So after a restart the morning's
  swing and valuation setups and the last wide scan's finds are on the board straight away instead of
  waiting for the next morning or the next wide scan, a setup acted on isn't offered a second time, and
  the wide scan comes a spacing after the *last* one, not after the start. Only the current session's
  state returns, plays that have expired or that the filters and strategies no longer allow stay off,
  and no price is restored: the first cycle reads fresh candles within seconds, and an entry still
  needs a current quote. With today's watchlist on disk the full scan isn't repeated either; open
  positions and their resting stop and target orders are found again from the trade records and the
  order tags. An entry sent before the restart that filled while the app was off (its play still
  *sent*, with no trade and no order working) is booked from IBKR's executions tagged with the play (a
  read of them that fails is tried again), and gets its stop. A replay the restart cut short resumes
  from its last finished job (see *The replay*).
* **An open dashboard tab updates itself.** After the app is updated and restarted, a tab that was already
  open reconnects - and would go on running the scripts it loaded before. The server stamps the dashboard's
  files in its first message; a tab that started on another stamp reloads (or, with a dialog open, asks you to).
  While the app is away the tab says so (the **live** pill, then a banner - see *Dashboard controls*) and picks
  up again by itself.
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
| **IBKR (paper or live), swing positions with a stop at the broker** | the dialog also offers **Keep them open & quit**: those positions stay open, protected by their stop orders resting at IBKR, and the app picks them up again when it starts; day trades, pair legs and any position without a resting stop are closed first. Targets and trailing are not worked while the app is off |

**After hours** an exit can't fill, so the app never sends one into a closed exchange - and never takes a
position's stop off the broker for an exit that can't go out. An automatic exit turned away for that waits for
the open (the dashboard says so once) without counting as a failed try, and the first pass of the regular session
starts every exit's retries afresh, so each goes at the open rather than minutes into it. **Close all & quit**
is refused while the market is closed; **Keep them open & quit** then keeps every position. A quit that can't
finish can be stopped from the banner (**Stop quitting**): the positions still open stay open and managed, and
the app unlocks.

The Active orders tab has **Cancel working orders**: every working entry, exit and stray order is cancelled;
the stops protecting open positions stay (close the position and its stop goes with it).

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
button while it's open. The record opens as a panel across the window, and a
chart of the trade fills the rest of it (`GET /api/trades/{id}/chart`): 5-minute
candles from the session before the entry (daily candles once it has been held
longer than ten sessions), the entry, stop and target lines with the first stop
lighter once the stop has moved, the entry, the parts taken off, the exit and the
best point marked on the candles, and a strip saying where it stands — open R,
unrealized, best and worst, held, what the stop and the target would make of it
(a closed trade's R, P/L and exit reason). The strip follows the streamed price
and the chart is fetched again every minute while the panel shows the trade.
The chart answers the mouse: a crosshair reads the price at the cursor and the
candle under it (its time, open, high, low and close, and how far its close is
from the entry in R and in money for this position), and a click-and-drag from
one candle to another shades the span green or red for what the move would have
made or lost this position, with the two closes and the change; a click clears
it.

The orders in a record are the **broker order audit** (`order_audit`), which
keeps what happened to each order: every order placed, with the broker's order
id, status and message; every cancel the app asked for, and why; every move of
a stop resting at the broker; and every error the broker sent about one of the
app's orders — a rejection, a cancel it refused (IBKR's 10148, with the state it
names: the stop may be filling), a cancel it made without being asked (a DAY
order at the close, say) — marked failed. A call the broker refused or didn't
answer in time is marked failed too, with the reason. The account number is
never written into it.

An open-trade record whose position no longer exists where it was opened —
closed in the broker's own app, or by an exit that filled while the app was down
— is **closed from the broker's fills**: the exit-side fills of the stock since
the entry give the exit price and time, so the trade's outcome reaches the
history, the journal and the strategy records (exit reason `closed-outside`).
When the broker reports no such fill (IBKR keeps only the current session's) the
record is **deleted** instead, as after **Reset paper** — but only once the broker
has answered: when its fills can't be read (the connection dropped, no answer in
time) the record is kept and looked at again on a later check, never deleted for
a fill the app couldn't see. A wrong deletion would orphan a real position, so
the check is strict: it only acts on a connected
broker's fresh account snapshot, after the connection has been up a minute, for
trades older than 90 s whose close isn't in flight, and after two misses in a
row. Closed trades are never deleted, and the broker order audit log is always
kept.

**Fees** are booked with each fill — the entry's, every part taken off and the
exit's — and a trade's realised P/L, % and R are after all of them (a part taken
off banks what it made after its own fee). IBKR sends its commission report a
moment after each execution, so a fill is mostly booked before its fee is known:
about once a minute the order sync reads IBKR's executions for the day's recent
fills and adds what IBKR has reported since to the fill, the trade's fees and a
closed trade's P/L and R. Fifteen minutes after its booking a fill's fee is
settled and no longer looked up. IBKR keeps only the current day's executions, so
records booked before fees were recorded stay before commissions; the session
review says so ("Fees not recorded before …") rather than making numbers up.

The reverse case is shown too: **shares without a record** — held at the broker
beyond what the open-trade records cover, because they were bought or sold
outside the app or a fill couldn't be booked — are listed under **Open
positions** with their own **Exit** button. The app doesn't manage their exits.
A stock held with no open record at all, or held the other way round from its
record, that stays so for two minutes of the regular session is also logged as
a warning and said on the dashboard once a session (again if the count changes).
An account **short** with nothing recorded, or short where the record is long
(and the reverse), is **urgent** — a position nothing manages — and its notice
stays up longer. Nothing is traded because of it: the **Exit** above is yours.

---

## Learning from what happened - the training set

Every play the app acts on is kept with **what it looked like at the decision**, so a model can
later learn which plays pay. `autotradebot/research/features.py` turns a play into one flat row of
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
| **shadow** - the plays shown and not taken | `shadow_trades` | by the 16:15 review, which follows each day-trade play on the session's candles as if it had been taken; a rebuild replaces the day's rows with the ones it followed, and keeps them when it followed none (IB Gateway away) |
| **replay** - the simulated trades | `sim_trades` (one set of rows per run) | when a replay finishes; `held_out` marks its out-of-sample sessions |

Export them as one CSV, with a `source` column, while the app keeps running:

```
python scripts/export_training_set.py            # -> data/research/training_set.csv
```

The data collection is passive: the app trades as usual and the rows accumulate. See
`docs/AutoTradeBot-learning.pdf` for how they are meant to be used.

**Judging a model** (`autotradebot/research/validate.py`, `scripts/validate_model.py`): before any model
touches a decision it is judged the way the learning guide says - **purged walk-forward folds**
(each fold trains on rows that finished before its test window, less an embargo; never a random
split), against the **baselines** it must beat (the confidence the setup states and the calibrated
probability the app shows), against a **shuffled-label baseline** (the same model fitted on random
labels, many times: the real one must beat the best of those), and on a metric table - log loss
and Brier score, calibration by bins, the win rate and expectancy in R of the top decile and above
a floor. The first learned model is a regularised logistic regression in NumPy, fitted per fold.

```
python scripts/validate_model.py                      # every row in the database
python scripts/validate_model.py --source replay      # the replay's rows only
python scripts/validate_model.py --csv data/research/training_set.csv --folds 6 --shuffles 50
```

It prints the table with the pass marks (`usable: YES` or `no - stays in shadow`) and writes
`data/research/validation.json`. A model that doesn't pass is not used.

**The model** (`autotradebot/research/model.py`, `scripts/train_model.py`; needs `pip install scikit-learn`,
the `[learning]` extra). López de Prado's meta-labelling: the setups keep choosing the side, entry,
stop and target, and gradient-boosted trees learn the odds that a play pays. Rows alive together on
one stock share their weight and old rows count for less (AFML ch. 4); the verdict comes from the
harness above, walking forward, against the stated odds and against shuffled labels; probabilities
are calibrated on the model's own out-of-fold predictions; the card keeps each feature's
out-of-sample importance and information coefficient.

* The engine **retrains after each day's review** and scores every fresh play; the odds are logged
  in the play's evidence (`model: {p, id, usable}`). The dashboard says when a new model is in and
  whether it is usable.
* Autopilot's **learned model** setting (⚙): `shadow` (default) only logs; `gate` refuses plays under
  `model_min_p` (55%); `size` also scales the risk by the bet size of AFML ch. 10. Gate and size act
  **only while the model's own walk-forward verdict calls it usable** - today it does not.

```bash
python scripts/train_model.py                 # train, judge and keep a model now
python scripts/export_training_set.py
Rscript scripts/r/audit_records.R data/research/training_set.csv    # an independent check in base R
```

The R script recomputes the record statistics (expectancy, quality number, bootstrap and reality-check
p-values, calibration) with nothing but base R. It is an audit, run by hand: nothing live waits on R.

---

## Trading capital

The header shows the account's **equity, cash and buying power as the broker
reports them**. Next to them, **Trading capital** is how much of that the bot may
use — click it to choose:

- **Whole account (margin)** - the default. New positions may use the account's
  **buying power**, as IBKR reports it: what's left after the open positions, with
  the margin the broker allows already in it.
- **Cash only** - the bot's positions, long and short together, never hold more
  than the account is worth, so nothing is borrowed.
- **A set amount** - only that much of the account's money, no margin.

Either way:

- Risk per trade, the open-risk ceiling and the per-position size limit are
  measured against the **account's value** (or the set amount), never against
  margin: margin only lets more positions be open at once. New positions only use
  what's left of the trading capital after the bot's open positions. Cash only or
  with a set amount, that counts everything the broker holds on the account, entries
  still working included - shares with no trade record (bought by hand, or left
  untracked) use the same money. The day / swing split counts the bot's own trades.
- The open-risk ceiling (`risk.max_open_risk_pct`, 4%) counts the risk already at
  work on the account: each open trade from its entry to the stop it opened with,
  times its shares, plus the entries still working (pair legs aside) at the price
  each was sized at when it was sent - one whose fill is being saved counts as
  working until its record is there. A new position is sized with only what's
  left under it - and is refused when that isn't enough for one share (Autopilot
  asks again on its next pass: room frees as trades close). While the open trades
  can't be read, nothing is sized.
- Two sliders over the plays tune position size for you and Autopilot alike:
  the **Position Size factor** (0-5, every new position's risk times this) and
  **Max % per position** (1-100, the most one position may hold, as a share of
  the account's value - `risk.max_position_pct_of_equity`). A day trade's stop
  is usually tight, so its risk budget would buy far more than this cap allows:
  for most day trades the cap, not the factor, decides the size.
- Every entry's record says how it was sized (`sizing` in the play's
  `evidence.at_entry` and in `trades.entry_context`): its risk, the size factor,
  which limit decided the share count - the risk budget, the open-risk ceiling,
  the per-position %, the per-stock cap, the volume cap, buying power or the
  trading capital's room (or `preview`: an entry re-sized at the last look held
  to the order card's count) - and what each limit allowed, in dollars and shares.
- Autopilot and the pair desk fill up to **Max % of trading capital in positions**
  (`autopilot.max_gross_exposure_pct`, 10-100) of it. With margin, IBKR closes
  positions itself if the account's excess liquidity runs out, so a maximum under
  100% leaves a buffer.
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
- **Autopilot's positions follow the same split.** At 70% / 30% with ten positions allowed, day
  trades may hold seven of them and swing trades three, and the day's entries divide the same way
  (a kind with a share keeps at least one slot). Without this the kind that fires first - swing
  setups, before the open and after 15:30 - took every slot and left the other kind's capital
  idle. The Autopilot button says "day 0/7 - swing 3/3", and the dashboard warns when a share is
  kept for a kind Autopilot isn't taking (unticked in its settings, or its filter box off) and when
  a kind holds more than its share: nothing is sold for that, it just takes no new entries until it
  is back under. Entry orders still working count in their kind's share, so entries sent close
  together can't each see the same room and overrun it.

---

## $2,000 floor & the Pattern-Day-Trader rule  (live mode only)

`autotradebot/risk/pdt_guard.py` runs on every **Assess** before you can confirm.
**In paper mode nothing below is enforced** — the day-trade tally is still
shown so you can see where live would stop you.

- **Equity floor** — if account equity `< min_start_equity` ($2,000), *no new
  entries*. The dashboard shows a red banner with the reason; the engine checks
  again on a start, a switch of account or **↻ Refresh**.
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

`autotradebot/util/clock.py` knows the four sessions and the NYSE calendar
(full-day holidays **and** 1:00 pm half-days) through 2028.

* **Regular hours** → the order type from `execution.default_order_type`
  (`LIMIT` / `MARKET` / `STOP_LIMIT`). A `STOP_LIMIT` entry rests at IBKR as a
  real stop-limit: it waits for the price to reach the entry, then pays no more
  than its limit. An order type the IBKR adapter can't send is refused, never
  sent as some other order.
* **Pre-market / after-hours** → **limit only**, and only for setups flagged
  extended-hours-eligible (all swing/valuation setups, plus `gap_and_go`).
* **Closed** (overnight / weekend / holiday) → execution is blocked; the
  dashboard shows the reason and when the market next opens.

Every play's order card shows the **concrete order** — type, limit price, stop
trigger, TIF and protection mode — and which venue it **routes to**.

**Never chase (Aziz).** At the click — yours or Autopilot's — the live quote is
checked against the play: once the price has run past the entry by more than
`execution.max_chase_r` (0.25) of the distance to the stop, the reward:risk the
play was judged on is gone and the entry is refused. Within that, a limit entry
is priced off the quote so it fills now instead of waiting for the price to come
back through the entry — which is the move failing — and is judged again at that
price: it is re-sized there (the risk budget over the wider distance to the stop,
the per-position cap at the price paid), so the shares that go out can be fewer
than the order card's - never more - and it is refused when that comes to nothing
or when its reward:risk to the first target falls under the floor —
`risk.min_reward_risk` for a click, the higher of that and Autopilot's
`min_reward_risk` for its entries. A price
already at or through the play's stop refuses the entry outright: the setup is
void. A day-trade entry still
working after `execution.entry_timeout_min` (10) minutes is cancelled for the
same reason; swing entries keep their DAY life. The log says which quote each
check read (a stream's, a snapshot or a candle) and how old it was.

**Part fills get their stop.** Until an entry order is done, the shares it has
bought have no trade record, so no stop at the broker. An entry (day or swing)
that filled in part `execution.partial_entry_wait_s` (30) seconds ago and is
still working has the rest cancelled: the broker's answer books what was bought
and the stop goes on in the same pass. Pair legs are left to the pairs desk. A
cancel that doesn't take is sent again every 30 seconds, and if IBKR loses track
of the order, the shares it was seen to buy are booked all the same.

While an entry works, its play's ⏳ shows the shares filled of the order and the
time left: to the time-out (amber in its last two minutes) or, after a part
fill, to the cut (`cut in 18 s`), then `cancelling` until the broker confirms.
The Active orders tab has the same countdown in its **Time left** column.

**What became of each sent play** is saved to the play log with the reason -
`CANCELED` (timed out, cancelled, or refused by IBKR, which reports refusals as
cancellations) or `ERROR` (the broker lost it) - so the daily review follows an
entry that bought nothing as a play not taken instead of counting it as taken.
Who decided and when are kept. The log keeps only the plays the board holds: a
setup already acted on this session isn't offered again, so it isn't logged
again, and a scan never overwrites a play that has been sent, filled or
dismissed. A play dismissed before any scan logged it is logged when dismissed,
and a play whose order has gone out can't be dismissed. Rows from before this
change: `python scripts/repair_play_log.py` says which sent plays never filled
(`--apply` marks them CANCELED; an entry that filled while the app was off would
match too, so check the untracked shares first).

**What fills cost (Harris).** The same last look keeps the quote it saw, and the
trade record stores the fill against it: `decision_price`, `spread_bps`,
`entry_slippage_bps`, and for exits `exit_decision_price`, `exit_slippage_bps` -
the implementation shortfall. On live quotes an entry is refused when the spread
is more than `execution.max_spread_r` (0.10) of the distance to the stop. The
daily review averages the measured slippage and says when the account pays more
than the replay charges.

---

## Automatic exit strategy

**A stop that outlives the app.** On IBKR every open position also has a
good-till-cancelled **stop order resting at the broker** (`execution.native_stop`,
on by default; `autotradebot/execution/protective_stops.py`), at the trade's working
stop and for exactly the shares held. It protects the position while the app,
the computer or the connection is down, and on delayed quotes it reacts to the
real price instead of one fifteen minutes old. The trade record is the source of
truth: every few seconds the order is made to match it - the break-even and
trailing ratchets move it (at most once every 15 s), the scale-out resizes it.
Two rules keep it safe:

* **Never two exits on one position.** When the price crosses the stop while it rests at
  IBKR, the app first gives that stop `broker_stop_grace_s` (10 s from the first cross) to
  fill: IBKR fills it on real prices and the order sync books it, where cancelling it for a
  market order of the app's own costs a round trip and a worse fill, and may find it already
  filling. Back inside the stop, the next cross gets its 10 s afresh; past them, with no
  stop resting or with IBKR disconnected, the app's exit goes as below (`0` = at once). So
  it does when the price hasn't reached the stop where it rests at IBKR (a ratchet not sent
  yet) - and the grace counts only in the regular session, where IBKR's stop can fill.
  Before the app sends an exit of its own it
  cancels the stop and waits for IBKR to confirm; if the stop filled first, that
  fill is booked and nothing else is sent; if IBKR hasn't confirmed, the exit waits
  for the next pass. A cancel IBKR refuses (the stop is already filling) is no
  confirmation - ib_async would report that stop as cancelled, the app keeps it as
  working until IBKR says it filled or is cancelled - and a stop that read cancelled is
  read once more, for a fill landing just behind the cancel, before the exit goes out.
  A cancel IBKR answers with neither its confirmation nor a fill holds the exit back
  for half a minute, and the order sync leaves that stop alone meanwhile. A stop move IBKR
  refuses leaves the stop as it was, and it is replaced; one refused a few seconds after the
  move seemed taken reads where IBKR still holds it and rests on there - never cancelled for
  the next move - which goes once IBKR's list of its orders shows the stop still working.
  A close that waited while another (a second click, a quit, the order sync booking the
  stop's fill) had the position's orders looks again: if that one sent its exit or closed
  the record, nothing more goes out. The order sync, the dashboard's buttons, a quit's
  closes and Autopilot's entries take turns at the broker, one at a time: a second close
  for a position whose exit is being sent comes back at once, a Refresh while the sync is
  mid-pass doesn't start a second pass beside it, and a stop's fill is booked once, by
  whichever of them takes it off the books first - one the database couldn't save goes back on
  them, and the exit waits until the next pass has booked it. A stop IBKR no longer knows at all is
  looked for in its executions before an exit stands it down; while they can't be read the
  exit waits.
  An exit right after a start stands down the stop and target an earlier run left
  too, before the app has taken them over; while IBKR's order list can't be read (the
  connection down too: no list is no empty list), or is still reloading after a connect,
  the exit waits rather than go out beside one - and goes within seconds once it may.
  Once a full list has shown nothing of an earlier run's for a position (or the app
  opened it itself), a list that can't be read no longer holds its exit back. An exit still
  waiting after a minute and a half - however far apart its tries - is reported on the
  dashboard like a failed one. A partial exit first shrinks the stop to the
  shares that remain, and is never sent while a target rests at IBKR.
* **Never a stop without a position.** A stop is placed only while the account shows
  the shares and the broker's orders could be read; a stop whose trade is no longer
  open is cancelled; a stop an earlier run left is followed, never doubled. If it (or
  its target) filled while the app was off, what it filled is booked first, from
  IBKR's executions of that order and never more than the record holds beyond the
  account, and the pair is placed afresh for the shares the record then holds.

**And a target, in one group with it.** Beside the stop rests a good-till-cancelled
**limit order at the target** (`execution.native_target`, on by default): for the part
that comes off at the first target when the position scales out, for all of it
otherwise. The two share a **one-cancels-all group** at IBKR (type 3: when one fills,
the other is reduced by the shares filled) - the target taking half off shrinks the
stop to the other half, the stop filling cancels the target, and the two can never
both fill for the whole position. Moving the stop's price leaves its size as IBKR holds
it, so a target that has filled in part keeps the stop cut to what is left, and a resize
never asks for more than the record holds less what the target has filled. IBKR works
them on real prices, so profit is taken at the target even on delayed quotes, and while
the app is off. When the first
target fills, the app books the scale-out (stop to break-even, second target) and
rests a fresh pair for what is left; while a target rests the exit manager leaves
the target to the broker. A plain stop left by an earlier version is stood down and
replaced by the pair. A pair is only stood down to be placed afresh once IBKR's orders
have been read, so a stop is never cancelled that can't be replaced. If IBKR refuses the
pair, the trade gets a stop alone and the app works its target itself, as before.

**A position without a stop at the broker is never silent.** A stop is only ever rested for shares
the broker shows - triggered, one for shares it doesn't hold would open a position the other way - so
during a fill landing in pieces it waits a few seconds, and if the broker's count and the record
disagree (part sold by hand in TWS, say) it waits for them to agree. Meanwhile the app's exit
manager still watches the price and sends the exit itself - unless IBKR's orders can't be read for a
position never looked for in a full list, when its exit waits too (and the report says so); what
nothing covers is the app being off.
So a position with no stop at the broker is marked **no stop at the broker yet** in red in the Open
positions tab's **Protection** column, and once that has lasted 90 seconds the log and the dashboard say
so, with the reason, and again every five minutes until it's fixed.

**An exit called off after filling in part is booked.** Quitting and then pressing *Stop quitting*, or
cancelling an exit, can call off an exit order that has already sold part of the position. The app books
that part when the broker reports the cancelled order - unless it stops first. So the position check also
books it from the broker's executions: when a record holds more shares than the broker, the same way round,
the fills tagged with that trade's own exit orders (`exit:<trade>`) are booked at their prices - never more
than the record is over by, never while an exit for it is still working, only for a symbol with one record.
The counts then agree, and the stop and target go back at the broker for what's left. A difference its own
fills don't explain (shares sold by hand in TWS) is left alone and reported, as before.

**A share count that disagrees can be fixed from its warning.** Each yellow share-count warning has a
**Fix…** button. It reads the account again and shows what the record and the account hold, the broker's
executions of the stock since the entry that no record has booked (and which order sent each: the record's
stop or target, an exit the app sent, or something outside the app), and the price and reason the missing
shares would be booked at - from those executions, or estimated at the last price when the broker no longer
reports them (IBKR keeps only today's), which it says. When the account holds fewer shares than the record,
the same way round, there are two choices: **Match the record to the account** takes the missing shares off
the record (`stop` / `trailing-stop` when its stop order sold them, `target-1` for its target,
`closed-outside` otherwise), and the next pass sizes the stop at the broker from the corrected record; **Sell
what's left and close the record** does the same, then sends a market exit for the rest. Nothing happens
without the click, and the counts shown go with it: if they changed since, nothing is done. It is refused, with
the reason, while not connected, while quitting, while an order for the stock is working or its resting stop
or target has part filled (those shares are booked when it finishes), for a stock with more than one open
record or a pair leg, and - the exit only - while the market is closed. More shares than the record are
exited from **Shares without a record**, as before.

The Open orders panel lists them as **stop** and **target** with their trade. The
simulator keeps its own bracket and gets no such orders.

`autotradebot/execution/exit_manager.py` runs every few seconds on every open trade
held on the active platform — **entries need your click, exits never do**:

| rule | default | config key |
|---|---|---|
| cut losses at the working stop - one resting at IBKR gets a moment to fill first | on, 10 s grace | `broker_stop_grace_s` |
| take profit at the target | on | — |
| **take half off at the first target**, stop to break-even, the rest runs to the second target (Aziz) | 50 % | `scale_out_pct`, `scale_out_lock_r` (plays with one target exit whole) |
| tighten the stop to **lock a small profit** once green | at +1.3 R, lock +0.3 R | `breakeven_at_r`, `breakeven_lock_r` |
| **trail** the stop, keeping a fraction of the open R | from +2.0 R, lock 50% | `trail_start_r`, `trail_lock_ratio` |
| **close a day trade that isn't working** once its setup's window has passed | on | `intraday_time_stop` |
| **flatten day trades** before the (holiday-aware) close - one still open from an earlier session at the next regular-session pass | 10 min before | `flatten_intraday_before_close_min` |
| force-close **stale swings** at the flatten on their last day | 10 trading days, the entry's counted | `max_swing_hold_days` |

The stop only ever ratchets in your favour and never through the last price, and
only in the regular session: a pre-market or after-hours print is thin, so it still
counts in the trade's excursions and trips its stop or target, but moves no stop. R
is measured against the **original** stop. Each open position has an **Auto
exit** toggle in the blotter if you want to hand-manage it.

In regular hours a streamed tick on a stock held (see **Real-time streams**)
runs its stop and target checks, and moves its stop, within about a second. The
note on the record and the message about a moved stop wait for the next full
pass a few seconds later (or the exit, if one goes out first), and so do the time
exits.

The time exits read no price, so they go out even when a pass has no quote for
the stock (or only one from before the entry). A day trade still open from an
earlier session - the app was down at the flatten, say - is closed at the first
pass of the next regular session, with the reason `eod-flatten`: the flatten it
missed. A swing trade's limit counts trading days the way the replay counts its
daily candles, the entry's day included, and it goes at the flatten on the last
one (at the next open with the flatten off); the Open positions tab shows that
day.

Every strategy also declares how long its trade *should* take. The blotter shows
an **Age / Expected** bar per position (green → amber **aging** → red **⏰
overdue**). For a swing trade this is informational. **A day trade that reaches
overdue - its setup's longest expected hold, 35 minutes to six hours depending on
the setup - and isn't working is closed then** (`intraday_time_stop`, exit reason
`time-stop`): Aziz's point that a day trade which hasn't moved in its time is
wrong, and a stalled one otherwise sits all afternoon holding a slot and capital a
fresh setup could use. "Working" means its stop has reached break-even or better
(after +1.3R, or once the first target has taken half off): it can no longer lose,
and it keeps its trail until the flatten. The replay applies the same rule, so the
records the proof rule reads are for the exits actually used.

---

## Autopilot — hands-off entry

`autotradebot/execution/autopilot.py`. After each scan it walks the fresh plays and,
for any that clear the gate, calls the same approve/execute path as the
**Execute ✓ Yes** button. Toggle and tune it from the header (**Autopilot: off /
day / day+swing**, plus ⚙). Defaults live in `config/config.yaml → autopilot`:

| gate | default | key |
|---|---|---|
| master switch | off | `enabled` (UI toggle) |
| **route real orders** | **off** | `allow_live` — *config-file only*; with it off, Autopilot is armed for **paper only** even in Live mode, and says so |
| which trade types it may take | the **Intraday** / **Swing** boxes over the plays say what is scanned and shown; Autopilot's own **day / swing / pairs** boxes (⚙) say what it may take of that - untick day trades to keep day plays on the board for the review without trading them | `trade_types` |
| which setups it may take | **Only these setups** (⚙): tick the ones Autopilot may take - none ticked means every setup; the others stay on the board for you to click (the Strategies panel switches a setup off everywhere) | `strategies` |
| minimum strategy confidence, day trades | 0.5 | `min_confidence` (the replay found higher stated confidence went with worse trades) |
| minimum strategy confidence, swing trades | 0.5 | `min_swing_confidence` (the swing setups state flat 0.55–0.58 confidences; the replay's proof is their real gate) |
| minimum reward : risk | 2.0 | `min_reward_risk` (Aziz Rule 5) |
| concurrent open auto positions | 2 | `max_auto_positions` - divided between day and swing trades by the day / swing split of the trading capital; Autopilot counts the positions it opened before a restart too |
| auto trades per session | 3 | `max_auto_trades_per_day` - an entry that ends with nothing bought (timed out, cancelled, refused) hands its slot back, once; one the broker lost keeps it, as it may have filled, and so does one IBKR didn't answer in time until IBKR shows it never went out. The setup itself isn't offered again that day. No more than twice this many entry orders go out in a day, whatever happened to them (the play's bar turns amber and says so; the Autopilot button's tooltip shows the orders sent) |
| aggregate open auto $-risk | 4 % of equity | `max_open_risk_pct` |
| concurrent auto trades from **one** strategy | 2 | `max_per_strategy` |
| new auto entries **per scan cycle** | 1 | `max_new_per_cycle` - a cycle is a scan of the market (5 minutes for swing trades, the 60-second fast cycle for day trades), not the 15-second re-check of the board |
| **no entries while prices can't be read** | always | IBKR refusing candles (the login active somewhere else) stops every entry, and an order whose last-look quote fails is refused - never sent blind |
| **no entries while its own records can't be read** | always | a database read that fails refuses rather than guesses: a pass that can't read Autopilot's open trades takes nothing and the log warns (once, until they read again); a play whose already-held or cool-off check can't be read is refused; today's realized P/L keeps the last figure read, so a stop for the day holds |
| **cool off** a ticker after it stops out today | on | `cooldown_after_loss` (goes by the day the trade closed, so a position held overnight and stopped out today counts) |
| **proof is not luck**: the replayed edge, net of the stocks' own drift, survives a reality check across every setup tried, and costs take no more than a third of it | p ≤ 0.10 | `proof_p_value` (Aronson; Carver's speed limit; 0 = the luck test off) |
| **the learned model** | shadow | `model_mode`: `shadow` logs its odds, `gate` refuses plays under `model_min_p`, `size` also scales the risk - only while the model is usable |
| **stop for the day** once today's closed trades have lost this % of equity | 2 % | `max_daily_loss_pct` (Aziz's daily maximum loss; every trade closed today counts, swing trades too; 0 = off) |
| **stop for the day** once the day trades' realized gain has given back this % of its best | 30 % | `max_giveback_pct` (Aziz: never lose more than 30 % of what the morning made; only day trades count, so a swing closed at a profit doesn't make a peak one day-trade loss gives back; `giveback_floor_pct` 0.25 % of equity is the smallest gain that counts; 0 = off). Either stop holds for the rest of the session, even if a later trade brings the P/L back, and through a restart; only changing one of the two limits lifts it |
| **no new day trades** in the last minutes of the session | 30 | `min_minutes_to_close` (Aziz keeps the last half hour for closing; the exit manager flattens day trades 10 minutes before the bell; 0 = off) |
| require a catalyst / dry-run | off | `require_catalyst`, `dry_run` |

The filters and the Strategies panel apply to Autopilot too, and it takes no
entries while the app is quitting. It still passes every other check — session
validity, position sizing, the PDT guard, the $2,000 live floor. Valuation plays
are never auto-traded. Eligible plays get a **🤖** marker and a coloured bar at
the left of the row: **green** — Autopilot would take it on its next pass (it
passes every check and a cap has room); **amber** — it passes the checks but a
cap is full (the day's entries, the open positions, the day / swing slots, or
the setup's own), and hovering the robot says which. A dim robot means it has
already acted on the play; on one still on offer hovering it says why. While
Autopilot is on, a play it won't take gets a faded grey robot: hovering it gives
the first check the play fails, in the words the entry gate itself uses (the
badge and the gate share one set of checks). The play's detail panel says the
same in one line. A play Autopilot tried and was refused - by the engine's
assessment (say as too thin to trade), the last look at the quote (the spread, a
chase) or the broker - gets the faded robot with the refusal, and a note in
**Autopilot notes**; it isn't tried again that day unless a setting changes (one
refused only for want of room under the open-risk ceiling is tried on each pass
until room frees). The
refusal is logged too, and kept on the play's row (`evidence.autopilot_refused`)
with the play's confidence, reward:risk, confirmations and noise flags then.

**The strip under the header** says what Autopilot is doing now and why, in one
line: off, paper-only, no prices, stopped for the day, done (the day's entries
used, and how many more bought nothing), the orders-sent ceiling, every position
taken, then per kind - day trades waiting for the open or in the last minutes
before the close, a kind holding its share of the day / swing split - and while
a kind has room, pacing (with a countdown to the next entry) or taking, with how
many plays pass every check. It adds when unproven setups trade at practice
size and which replay losers are skipped, and ends with today's closed trades by
setup - a chip per setup with won / closed and the R they made (pair legs aren't
counted). Green is taking, amber waiting, red stopped or blind, grey off. When
the daily loss limit or the give-back rule stops it, a red banner says so as
well, with the reason, until it's dismissed or the stop is lifted.

**Proof, noise and size.** A day-trade setup must show up `min_confirmations`
(2) times in a row, and plays carrying a flag in `skip_noise` are skipped. With
`confirm_on_new_candle` (on) each of those is a newer 5-minute candle, whichever
scan reads it - the scans read one candle several times over, and the replay
enters once a setup has shown on two candles running; a sighting more than two
candles after the last one counted starts the count again. A setup that fires on
one candle by its nature (a reclaim, a flag) then never reaches two, and the
Strategies panel says so: set 1 confirmation, or turn the setting off, to
practise those again. The replay models two at most, so 3 or more asks live for
more than it tested.
With `require_proven` on, a strategy is auto-traded only once the replay (**Strategies
→ Run replay**) has at least `min_replay_trades` (30) trades Autopilot would have
taken, averaging at least `min_replay_expectancy_r` (+0.05R) — **and** at least 10
of them in the held-out latest third of the sessions, averaging more than 0R
there. The statistical noise checks (`not_trending`, `not_mean_reverting`,
`turbulent_market`) and the two news checks (`news_driven_move`,
`move_without_news`) are skipped by themselves once the replay shows that the
trades they remove did worse on every session and on the held-out ones. Each
entry risks no more than `risk.max_risk_per_trade_pct`, lowered to **half-Kelly**
when the strategy's record calls for less.

**Proof is always asked for with real money.** In **Live** the proven-only rule is
in force whatever the setting says - the box in the Autopilot dialog is locked on.
On **paper** it is your choice: untick it and Autopilot practises the unproven
setups too, which is how the records and the learned model get their real
trades. Either way a strategy the replay hasn't proven is sized at **practice
size - a quarter of the usual risk** - however good its record looks: an edge
that can't be told from luck is no reason to size up (Tharp, Aronson), and a
setup never replayed is no reason to risk the full amount. Every trade keeps the
settings it was taken on (`proof_required`, the floors, the caps) and whether
its strategy was unproven, so practice trades are never mistaken for proven ones.

**Losers are skipped even in practice.** With proof not asked for, Autopilot still
skips a setup with evidence that it loses (`skip_replay_losers`, day trades by
default; `off` / `day` / `all`): its replay the way Autopilot takes it averages
-0.05R a trade or worse - a loss of `replay_loser_r` (0.05) per trade - over
`min_replay_trades` (30) and over 10 held-out trades, or its own closed trades -
the in-app simulator's count too - average -0.30R or worse over 10. The records
are read afresh on every pass, so a replay that recovers lifts it. The threshold is fragile - a little lower and a
setup flips - so set `off` to practise every setup again. The Autopilot dialog
lists what is skipped now, and each trade keeps that list.

**Faster loop while day-trading.** When Autopilot is armed with the **Intraday** filter on and
the regular session is open, the hot list is rescanned every
`scanner.fast_cycle_seconds` (60) between the regular cycles. The header button
shows it (`Autopilot: day ⚡60s`). At each 5-minute close the candle-close check
(see **Candle-close check**) takes the place of the fast cycle due then.

---

## Signals — insider trades and company news

A background service (`autotradebot/signals/`) watches what happens off the price chart. It is on by default
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
* **Live pill** — whether this tab still hears the app: `live · 3 s` (the time since its last update; off
  hours one comes about every 30 s) or `reconnecting · last update 48 s ago`. Once the link has been down for
  10 s a red banner says since when nothing has come in, the tables dim, and the buttons that send orders or
  change settings are off until it's back - the tab reconnects by itself and the app's first message brings the
  plays and the Open positions, Active orders, Trade history and P/L summary tabs up to date. Those four tabs,
  when they can't be refreshed, keep what they showed, under *Couldn't refresh - showing data from 10:42* and
  the reason, and a request the app refuses shows its own reason.
* **Connections** — paper platform, IB Gateway settings, **Test paper / Test
  live**, **Reconnect**.
* **Settings** — the scan schedule and list sizes, the scan status, **Run full
  scan now** and **Rescan hot list now**.
* **Signals** — insider trades and company news, their sources and what they do to plays (see [Signals](#signals--insider-trades-and-company-news)).
* **Reports** — each session's report (see [Reports](#reports--every-session-the-market-and-the-bot));
  a dot marks one you haven't opened.
* **Scan now** — rescan the hot list and the next buffer names. The plays header
  shows the last scan, a progress bar while one runs, when the next full
  scan is due, and "replay running" while a replay runs in the background.
* **Long / Short / Intraday / Swing** and **Sectors** — what the bot scans for
  and may trade.
* **Strategies** — switch setups on or off and weight them.
* **◐** — light / dark theme.  **↻ Refresh** — re-pull account, positions and fills, and fetch a fresh
  price for every play on the board and every position held (one batched request of one-minute candles,
  pre-market and after-hours included, the positions first). When IB Gateway came up after the app, it
  connects at once instead of waiting for the background retry. The button spins until the answer is in
  and then says what happened, so there is no need to press it again.
* **Price** (plays table) — the latest price the app holds for the stock and how far it is past the entry
  in R; amber beyond `execution.max_chase_r` (0.25R), where an entry is refused. The tooltip says when the
  price is from. The positions' **Mark** is the app's own price when it fetched one in the last two minutes
  (the exit manager's, Refresh's or an open panel's), otherwise the broker's mark, which IBKR updates only
  every few minutes. Prices fetched to be shown include pre-market and after-hours trades and are kept apart:
  the exits and the entry checks read regular-hours prices only. `GET /api/price/{symbol}` gives one stock's
  latest price with its time and session, asking IBKR at most every 15 s per stock. While a stock streams
  (see **Real-time streams**) its Price, Mark, Unrealized, R now, the header's Unrealized and its Market price
  in any panel follow each move in place, at most once a second (`prices.tick`).
* **Market price** (every panel about a stock) — a play's detail panel (with how far it is past the entry in
  R until the play is sent) and its chart, the stock on the Signals page, an open trade's record, an exit's
  confirmation ("Last trade"), a mover's chart (the price now) and a pair's chart (both legs) show the latest
  price with its time, and the session outside regular hours. It is fetched as the panel opens and every 30 s
  while it stays open and the tab is in view.
* **Replay record** (plays table, beside the setup) — the setup's replayed average R a trade and how many
  trades, over the ones Autopilot would take: green proven, amber not proven yet, red losing, grey no replayed
  trades. The tooltip adds the held-out sessions and why it isn't proven. The play's detail panel sets what the
  replayed wins average beside the play's expected R (which counts a win at the full target), and **Switch
  this setup off** is the Strategies switch: the setup stays off, through a restart, until it's switched back on.
* **R now** and **Protection** (Open positions) — where each trade stands at its Mark in R, against the risk it
  was opened with (entry to the original stop), as the exit manager measures it; and what stands ready to close
  it: green when its stop (and target) rest at the broker - the orders the app placed and follows - amber when
  the app watches the price itself (the simulator), red for **no stop at the broker yet**. Under it a day trade
  counts down to its time stop, "out in 31 min unless working", amber in the last 5 minutes.
* **Execute ✓ Yes** / **Dismiss** (a play's detail panel) — the click shows at once: the row says *sending…*,
  or leaves the table, and a refusal puts it back as it was, with the app's reason. The answer comes as soon
  as the order is out; the account is read and sent a moment later. The panel follows its play: once
  Autopilot (or another tab) has sent it, Execute is gone and the panel says who sent it and when.
* **Trading capital** — how much of the account the bot may use.
* **Autopilot** + **⚙** — hands-off entry and its caps.
* **Reset paper** (simulator only) — reset the balance.
* **Exit** / **Exit all** — close one position, or all of them, at the market. A position's Exit says
  *Sending…* and stays off until the app answers.
* **Quit** — close out, then shut down.
* Bottom tabs: **Open positions**, **Active orders**, **Trade history**, **P/L summary**, **Watchlist**, **Pairs**.

---

## What the books taught it — the quantitative layer

The setups come from Aziz, Murphy and Pignataro. A second shelf of books on
algorithmic trading and time-series econometrics decides whether a setup can be
trusted *right now*, how much to risk on it, and how its record is judged. The
models are plain numpy in `autotradebot/quant/` (no statistics packages), and each one
is tested on simulated series whose answer is known.

| book | what it adds | where |
|---|---|---|
| Chan, *Algorithmic Trading* | Hurst exponent, variance ratio, ADF test and half-life (ch. 2, 6); buy-on-gap (ch. 4); post-earnings drift (ch. 7); regimes and risk (ch. 8) | `quant/stationarity.py`, `quant/readings.py`, the noise checks, `strategies/statistical.py` |
| Chan, *Quantitative Trading* | backtests with transaction costs and an out-of-sample test (ch. 3); Kelly sizing (ch. 6) | the replay, `quant/sizing.py`, `research/weights.py` |
| Tsay, *Analysis of Financial Time Series*; Enders, *Applied Econometric Time Series* | GARCH(1,1) and RiskMetrics volatility forecasts | `quant/volatility.py`, the stop floor |
| Hamilton, *Time Series Analysis* | the Markov switching model of calm and turbulent markets (ch. 22) | `quant/regime.py`, `engine/market_regime.py` |
| Vidyamurthy, *Pairs Trading*; Johansen; Juselius | cointegration (Engle–Granger, Johansen), zero crossings, the entry-band design | `quant/cointegration.py`, `quant/bands.py`, `pairs/` — see [Pairs trading](#pairs-trading) |
| Aronson, *Evidence-Based Technical Analysis* | a record is a hypothesis test: the bootstrap against zero, White's reality check across every setup tried, detrending so being long in a rising market is no edge | `research/significance.py`, `SimTrade.drift_r`, the proof rule's `proof_p_value` |
| Tharp, *Trade Your Way to Financial Freedom* | R-multiples and expectancy; the quality of the R distribution (SQN); the marble-bag drawdown simulation | `research/significance.py` (`sqn`, `marble_bag`), the Strategies panel |
| Carver, *Systematic Trading* | the speed limit: costs may take no more than a third of a rule's pre-cost return | `SimTrade.cost_r`, `significance.cost_share`, the proof rule |
| Harris, *Trading and Exchanges* | implementation shortfall; the spread as the price of immediacy; a stale limit order is a free option for someone else | `Engine._chase_check` (spread gate, the quote kept), `Executor.expire_entries`, `trades.*_slippage_bps`, the review's execution block |
| López de Prado, *Advances in Financial Machine Learning*; Jansen, *Machine Learning for Algorithmic Trading* | meta-labelling, uniqueness weights, purged walk-forward, MDA importance, bet sizing; boosted trees, calibration, information coefficients | `research/model.py`, `research/validate.py`, `scripts/train_model.py` |
| Grimes, *The Art and Science of Technical Analysis*; Bulkowski, *Encyclopedia of Chart Patterns* | the failure test, the pullback after a thrust, the confirmed double bottom - with their measured statistics | `strategies/patterns.py` |

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
  capped at `risk.max_risk_per_trade_pct`. It can only lower the risk, and it
  only counts once the replay has proven the strategy: until then the risk is a
  quarter of the usual.
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
* **Liquidity cap** — one order never takes more than `risk.max_adv_pct` (1 %)
  of the stock's median daily volume over its last 20 completed sessions
  (today's candle counts only once the session has closed, and the median
  ignores a one-off spike day), so the entry and later the stop can fill
  without moving a thin stock.
  The order card lists it under *Size limited by*; a stock so thin that the cap
  is under one share is refused with that reason. A stock with no daily history
  isn't capped; `0` turns it off. Day and swing trades alike; pair legs are
  sized by the pairs desk and are untouched.

**Two statistical day trades** (`autotradebot/strategies/statistical.py`; on even when
an older `config.yaml` doesn't list them):

| key | idea |
|---|---|
| `gap_reversion` | an open more than one standard deviation of daily returns below yesterday's low while still above the 20-day average (the mirror for shorts): the gap tends to be partly won back during the day. Targets yesterday's low, then its close. |
| `earnings_drift` | an earnings release (SEC 8-K item 2.02) accepted after the previous close or before the open, and a gap of more than half a standard deviation of the stock's usual overnight moves: the price tends to keep drifting that way through the day. |

### The replay — judged the way Chan judges a backtest

**It runs itself every morning** (`replay.daily`, on by default). The moment the 08:30 full scan has
built the day's watchlist, the replay starts on it — an hour before the open, when the watchlist is
fresh and the Gateway is free for the candles it downloads. So the records, the proof rule, the
half-Kelly sizes and the learned model are current for the session without anyone being awake for
it; each morning also fetches another 20 minutes of past candles until the sixty sessions are
covered. Once a session, never while a replay is already running or the app is quitting, and the
session it ran for is remembered so a restart doesn't start a second one. **A restart part way
through picks the run up where it stopped**: each finished job is kept in
`data/research/replay_partial.jsonl`, and once the Gateway is back the app resumes the same
session's run, skipping those jobs and downloading no candles (the rest see the ones the finished
jobs saw). A file from an earlier session is deleted; one made with other settings or other replay
code is replayed afresh. A full scan *during*
the session — a cold start at lunchtime, a widened filter — doesn't trigger it: the replay's
downloads would be taking the Gateway from the cycles that need it, so it waits for the morning. **Run replay** in the
Strategies panel still works whenever you want it.

**Day trades are replayed on the stocks that were in play, entered the way Autopilot enters.**
A day-trade setup only ever sees the morning's hot list, so each past session is replayed on
the stocks the scan would have picked *that morning* - the `replay.day_stocks` (40) hottest by
daily heat as of the session before, plus the `replay.day_gappers` (10) biggest opening gaps
among the watchlist (`research/in_play.py`; on a real morning it picks the scanner's own top 40,
name for name). Replaying sixty sessions on *today's* hot list, as it did before, tests
something else: today's names are hot because of what they have just done, which hands a
momentum setup its own hindsight, and on most of those sessions no scan would have shown them.
Their 5-minute candles are downloaded for those sessions only (each request brings a session and
the four before it, what a live scan has in hand) and kept, so a later replay fetches only the
new sessions; `day_stocks: 0` brings the old way back. IBKR answers a request for *past* candles
far more slowly than one for the latest - measured at seconds each, with some timing out, against
a dozen a second - so one replay downloads for at most `replay.day_download_minutes` (20), the
latest sessions first, replays the stock-days it has, and says how many requests are left; the
next replays go on from there until the sixty sessions are covered. A request that timed out is
asked again; a session IBKR really has nothing for is remembered and isn't.

Every day setup is followed twice: **entered on sight** (the record of all trades) and **entered
after it has shown two bars in a row**, one bar later and once a session per setup - which is how
Autopilot enters when it asks a day trade to be seen on two candles running. Autopilot's record,
the one the proof rule reads, is built from the way in it really uses. Before, nearly every
replayed day trade was entered on sight, so 27 of 1,466 counted and no day setup could ever
reach the 30 trades proof asks for.

What the day-trade replay still can't know: it reads 5-minute candles, so a stop and a target
inside one candle count as the stop, fills are the next candle's open with a flat slippage, the
spread and size of a thin stock aren't modelled, and the names the wide scan adopts mid-session
aren't reproduced. It is good at throwing out a setup that loses; a setup it likes still has to
show it on paper with real-time data - which is why real trades count double in the evidence
weights and replace the replayed ones in the sizing once there are 30.

**Strategies → Run replay** walks recorded candles the way the scans see them and
follows every play the way the automatic exits would. Its settings are in
`config/config.yaml → replay`:

* **It uses every core but two** (`replay.workers`, 0 = automatic): each stock is replayed in its own
  worker process, and a day-trade replay is cut into jobs of `sessions_per_job` (10) sessions so the
  long ones don't leave workers idle at the end. Series that are the same for every bar of a session
  (the session VWAP, the opening range) are computed once per session.
* **Only the plays the app would show and take.** A play under the scanner's reward:risk floor
  (`risk.min_reward_risk`) is never on the board, so the replay never trades it; and a strategy's
  Autopilot record counts only the trades Autopilot would take - its skipped flags, its confirmations,
  its own reward:risk floor and its confidence floors, read off the features kept on each replayed
  trade. The replayed plays also state the same calibrated odds as live, from the strategy records
  the replay is handed.
* **Which stocks.** Day-trade setups replay on the hot list and the kept buffer names (their 5-minute
  candles cost IBKR requests). Swing setups replay on every watchlist stock **and on the full scan's
  400 leaders** (`replay.swing_stocks`; 0 = the watchlist only) - their daily candles are already on
  disk, so a strategy's record rests on hundreds of stocks rather than the day's forty.
* **60 day-trade sessions and 700 swing sessions** by default. Sixty sessions give
  most setups the 30+ trades a record needs *with* a month held out. The swing
  replay runs on **three years of daily candles** (`replay.daily_years`, 1-5): one
  year is one market, and a proof rule that asks whether an edge is luck needs more
  than one. The long history is kept apart from the live store, in
  `data/research/daily/`, only for the stocks the replay runs on: the first replay
  asks IBKR once per stock ("years of daily candles" in the progress line), and
  after that the live store's candles keep it current for nothing. However long the
  history, a replayed setup sees the 300 sessions it would see live. Up to 120
  day-trade sessions can be asked for — 5-minute candles are downloaded once and kept.
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
  *sent, not filled* (an entry went out — Autopilot's or yours — and no trade came of it;
  the row it was sent from is listed beside the setup's first sighting, with what it would
  have made followed on the candles),
  *offered, not taken* (and what the setup would have made, followed on the candles),
  *watched, no setup* (on the hot list, adopted into it, or scanned from a sector buffer),
  or *not watched* — and why: the morning's ranking put it #412 of 2,950, it was too thin
  for the scanner's filters the day before, or its sector is switched off;
* **charts** (📈) — the session's 5-minute candles with the bot's entries and exits, the
  setups it offered and the news as it came out, and the daily candles around the day.

Above the table, the score: how many of the movers were traded, sent and not filled (when
any were), offered, on the morning watchlist and moved at the open — and over the last 20
sessions, the share of the market's biggest movers the watchlist held. That share is the
scanner's report card: if most movers make their move at the open on overnight news, a
ranking of the previous day's candles can't see them, and the report says so.

### The bot's own trading

* **the positions opened** that session, closed or not — entry, stop, target, the
  dollars at risk, what each was taken on (and whether its strategy was a practice
  one, not yet proven), and for the ones still open where they stood at the
  review, in R and in money, on the session's close. A session whose entries are
  all still open is not a session without trades: the day list says "9 opened ·
  0 closed", and what an entry was taken on is judged the day it is taken;
* **the trades** that closed, each with what it was taken on — noise flags, scans
  in a row, price character, the market's regime, who took it. A position taken off
  in parts is shown whole: the shares it was entered with, at the size-weighted
  average of every exit, its exit marked "(last of 2 exits)";
* **mistakes** — a loss more than 0.2R past the planned 1R (the lessons count every
  loss over 1R, and how many went that far past the stop), a winner of 1R or more
  closed at a loss (break-even isn't one), a trade taken through a noise flag or
  before it was confirmed, going straight back into a stock that had just lost, a
  strategy without a proven record;
* **the plays not taken**, each followed on the session's 5-minute candles as if
  it had been, from the next bar after it was on the board with the values it was
  recorded with (a play's row holds the last scan that wrote it, and a scan's plays
  reach the board when it finishes); an entry sent that never filled (a sent row
  no trade was booked from, even one still marked submitted) is followed
  from the row it was sent from, from the moment it went out, however it scored —
  grouped by noise flag and by whether Autopilot's checks passed
  it, so every check is tested on live plays every day. The checks are the ones in
  force that session, as the last Autopilot entry recorded them (a rebuild keeps
  them; with no entry, the settings at the rebuild, and the review says so), so a
  flag the evening replay learns later never re-judges the day. A check is said to
  have helped or cost only when the two sides are at least 0.10R a play apart and
  at least one standard error apart (Welch's t); anything less reads "no clear
  difference", and a flag with no clear difference gets no lesson. The counts are
  told in order: every day setup not taken, the ones followed (past 150, only the
  highest-scoring and the entries sent, and the review says what share of them
  that was), and the ones that would have filled at the next bar's open. Entries
  sent that never filled are counted apart, and the **Setups offered** card
  counts a setup once however often it came back to the board (the play-log rows
  are in its tooltip);
* **each strategy's real record** over the last 20 sessions against its replay,
  flagged when it falls more than 0.3R a trade short;
* **how the orders filled** — the seconds from an order going out to the fill coming back, typically
  (the median) and at worst, going in and coming out, with how many fills and since which day. Every
  trade keeps its own (`entry_latency_s`, `exit_latency_s`), so the training set can learn from how
  long a fill took, and a broker or a venue that gets slower shows up as a number rather than a
  feeling. The review measures entries on fills within 60 s; a limit entry that rested longer was
  waiting for its price, and is counted apart. Every exit counts, however slow - the app sends
  exits at market - and the ones over 60 s are counted. A stop or target resting at the broker has
  no such time, nor an entry taken back after a restart (when it really went out isn't known);
* **lessons**, in plain sentences. **Rebuild the last session** writes it again on demand,
  the movers too: after a restart it first downloads the session's daily candles again - only
  while the market is closed, since the download shares IB Gateway with the orders. When it
  can't (the market open, IB Gateway away), it keeps the movers built before and says when
  they were built and why they weren't refreshed.

---

## Pairs trading

`autotradebot/pairs/`. Two stocks from one industry whose log prices are cointegrated drift
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
randomly around an edge. The plays pushed to the dashboard carry only what the table
shows, so this text is fetched once the pointer rests on a play; the one-line
rationale shows until it arrives.

### Technical (`autotradebot/strategies/technical.py`)

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
| `failure_test` | swing | a probe through a swing level at least 5 sessions old that **closes back inside** - Wyckoff's spring / upthrust; the stop goes just beyond the test (Grimes) |
| `trend_pullback` | swing | the first shallow, quiet pullback to the 20-EMA after a close outside the **2.25-ATR Keltner channel**, entered when a candle closes back beyond the one before (Grimes) |
| `double_bottom` | swing | twin lows within 4%, 2–7 weeks apart, a 10% rally between - taken **only on the confirming close** above that rally's peak; the stop sits inside the pattern (Bulkowski, Murphy). Tops are the mirror |
| `week52_breakout` | swing | push to a new 52-week high/low on volume expansion |

Also: `gap_reversion` and `earnings_drift` (see [What the books taught it](#what-the-books-taught-it--the-quantitative-layer)) and `insider_buying` (see [Signals](#signals--insider-trades-and-company-news)).

### Valuation — from *Pignataro, Financial Modeling and Valuation* (2nd ed.)

`autotradebot/valuation/` + `autotradebot/strategies/fundamental.py`:

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
| the day so far: the board's plays, settled setups, the gap check's levels, the last scans | the scans | `data/day_state.bin` (the current session) |
| your dashboard choices | you | `data/runtime.json` |
| the simulator's balance and positions | the simulator | `data/paper_state.json` |

`data/` is git-ignored. Deleting any of it is safe; it's rebuilt on the next scan.

---

## Adding a broker

Implement `autotradebot/brokers/base.py::BrokerAdapter`, register it in
`autotradebot/brokers/__init__.py::get_broker`, and add it to the routing in
`autotradebot/brokers/venues.py` and `autotradebot/engine/connections.py`. To use it as a
price source too, give it the `PriceSource` methods from
`autotradebot/data/market_data.py` (`history_many`, `contract_details_many`,
`get_quote`, `quotes_from_bars`). A venue that reports order errors after the
call (a rejection, a refused cancel) hands them over through `order_errors()`,
which the executor writes into the order audit. Shipped: `paper_adapter.py`
(simulator) and `ibkr_adapter.py` (`ib_async`, own asyncio-loop thread,
auto-reconnect, paper + live by port).

---

## Project layout

```
run.py                     boot engine + dashboard (Ctrl+C follows the quit rules)
scripts/
  init_db.py               create the MySQL schema
  ibkr_setup.py            IB Gateway connectivity doctor + setup guide  (--guide, --live)
autotradebot/
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
    day_state.py             the board and the scans' state, kept across a restart
    capital.py               trading capital
    reconcile.py             when an open-trade record counts as gone at the broker
    runtime.py               data/runtime.json
    views.py                 pieces of the dashboard snapshot
    market_regime.py         calm or turbulent, from SPY's daily returns (Hamilton ch. 22)
    chart.py                 a play's or a trade's candles, marks and exit routes
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

- options plays
- equity-curve chart in the dashboard

## How it was built

AutoTradeBot is built by Omar Abdeen with **Claude Code**, Anthropic's AI coding assistant, as a pair
programmer - and says so openly. Omar Abdeen decided what to build and what the app must never do, set the
trading rules and risk limits, ran it day after day against an IBKR paper account, and approved every change
before it was merged. Claude Code wrote much of the code, the tests and the documentation, and ran the reviews
and replays behind the decisions. That's why most commits carry a `Co-Authored-By: Claude` line and many pull
requests say they were generated with Claude Code; the pull-request history keeps the reasoning, the test
results and the reviews behind each change. Claude is listed as a co-author in [AUTHORS.md](AUTHORS.md) and in
the package metadata (`pyproject.toml`).

## License

[MIT](LICENSE) - use it, change it, share it; keep the copyright notice. No warranty: see the disclaimer at
the top.
