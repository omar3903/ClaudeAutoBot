"""Build the AutoTradeBot architecture guide: an HTML file with inline-SVG UML diagrams,
rendered to PDF by headless Edge (see build.sh)."""
from __future__ import annotations

import html
import pathlib
import re

OUT = pathlib.Path(__file__).with_name("AutoTradeBot-architecture.html")
ROOT = pathlib.Path(__file__).resolve().parents[1]


def esc(s: str) -> str:
    return html.escape(str(s), quote=True)


# ----------------------------------------------------------------------------------------------
#  A tiny SVG toolkit for UML-style figures
# ----------------------------------------------------------------------------------------------
class Svg:
    FONT = "Segoe UI, Arial, sans-serif"
    MONO = "Consolas, Menlo, monospace"

    def __init__(self, w: int, h: int) -> None:
        self.w, self.h = w, h
        self.parts: list[str] = []

    def rect(self, x, y, w, h, fill="#fff", stroke="#333", rx=3, sw=1, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" '
                          f'stroke="{stroke}" stroke-width="{sw}"{d}/>')

    def text(self, x, y, s, size=10, anchor="start", weight="normal", fill="#111", mono=False, italic=False):
        fam = self.MONO if mono else self.FONT
        st = ' font-style="italic"' if italic else ""
        self.parts.append(f'<text x="{x}" y="{y}" font-family="{fam}" font-size="{size}" text-anchor="{anchor}" '
                          f'font-weight="{weight}" fill="{fill}"{st}>{esc(s)}</text>')

    def line(self, x1, y1, x2, y2, stroke="#333", sw=1, dash=None, end=None, start=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        me = f' marker-end="url(#{end})"' if end else ""
        ms = f' marker-start="url(#{start})"' if start else ""
        self.parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" '
                          f'stroke-width="{sw}"{d}{me}{ms}/>')

    def path(self, d, stroke="#333", sw=1, dash=None, end=None, start=None, fill="none"):
        dd = f' stroke-dasharray="{dash}"' if dash else ""
        me = f' marker-end="url(#{end})"' if end else ""
        ms = f' marker-start="url(#{start})"' if start else ""
        self.parts.append(f'<path d="{d}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{dd}{me}{ms}/>')

    # -- a labelled box: bold title, then lines -----------------------------------------------
    def box(self, x, y, w, h, title, lines=(), fill="#fff", stroke="#333", tsize=10.5, lsize=9, mono=False,
            title_fill=None, dash=None, rx=4):
        self.rect(x, y, w, h, fill, stroke, rx=rx, dash=dash)
        if title_fill:
            self.rect(x, y, w, 18, title_fill, stroke, rx=rx)
            self.rect(x, y + 12, w, 6, title_fill, "none", rx=0)
        self.text(x + w / 2, y + 13, title, tsize, "middle", "bold")
        for i, ln in enumerate(lines):
            self.text(x + 7, y + 30 + i * (lsize + 3.5), ln, lsize, mono=mono)
        return (x, y, w, h)

    # -- a UML class box with compartments -----------------------------------------------------
    def uml_class(self, x, y, w, name, attrs=(), methods=(), stereo=None, fill="#fdfdfd", abstract=False):
        ls = 9
        head = 20 if stereo is None else 30
        ha = len(attrs) * (ls + 3) + 6 if attrs else 4
        hm = len(methods) * (ls + 3) + 6 if methods else 4
        h = head + ha + hm
        self.rect(x, y, w, h, fill, "#333", rx=0)
        self.rect(x, y, w, head, "#e8eef8", "#333", rx=0)
        if stereo:
            self.text(x + w / 2, y + 11, f"«{stereo}»", 8.5, "middle", fill="#444")
            self.text(x + w / 2, y + 23, name, 10, "middle", "bold", italic=abstract)
        else:
            self.text(x + w / 2, y + 14, name, 10, "middle", "bold", italic=abstract)
        yy = y + head
        self.line(x, yy, x + w, yy)
        for i, a in enumerate(attrs):
            self.text(x + 5, yy + 12 + i * (ls + 3), a, ls, mono=True)
        yy += ha
        self.line(x, yy, x + w, yy)
        for i, m in enumerate(methods):
            self.text(x + 5, yy + 12 + i * (ls + 3), m, ls, mono=True)
        return (x, y, w, h)

    def label(self, x, y, s, size=8.5, fill="#333", anchor="middle", bg=True):
        if bg:
            tw = len(s) * size * 0.55 + 6
            self.rect(x - tw / 2 if anchor == "middle" else x - 3, y - size, tw, size + 5, "#fff", "none", rx=2)
        self.text(x, y, s, size, anchor, fill=fill)

    def render(self, caption=None, width_pct=100) -> str:
        defs = (
            '<defs>'
            '<marker id="arr" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto" markerUnits="strokeWidth">'
            '<path d="M0,0 L10,4 L0,8 z" fill="#333"/></marker>'
            '<marker id="open" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">'
            '<path d="M0,0 L10,4 L0,8" fill="none" stroke="#333"/></marker>'
            '<marker id="tri" markerWidth="12" markerHeight="10" refX="11" refY="5" orient="auto">'
            '<path d="M0,0 L12,5 L0,10 z" fill="#fff" stroke="#333"/></marker>'
            '<marker id="dia" markerWidth="12" markerHeight="8" refX="1" refY="4" orient="auto">'
            '<path d="M1,4 L6,0 L11,4 L6,8 z" fill="#333" stroke="#333"/></marker>'
            '<marker id="odia" markerWidth="12" markerHeight="8" refX="1" refY="4" orient="auto">'
            '<path d="M1,4 L6,0 L11,4 L6,8 z" fill="#fff" stroke="#333"/></marker>'
            '<marker id="crow" markerWidth="12" markerHeight="10" refX="11" refY="5" orient="auto">'
            '<path d="M0,5 L11,0 M0,5 L11,5 M0,5 L11,10" fill="none" stroke="#333"/></marker>'
            '</defs>'
        )
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" '
               f'width="{width_pct}%" style="max-width:{self.w}px;display:block;margin:0 auto">{defs}'
               + "".join(self.parts) + "</svg>")
        cap = f'<div class="cap">{caption}</div>' if caption else ""
        return f'<div class="fig">{svg}{cap}</div>'


def edge(a, b):
    """Connect two boxes (x, y, w, h) at the closest edge midpoints."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    acx, acy, bcx, bcy = ax + aw / 2, ay + ah / 2, bx + bw / 2, by + bh / 2
    dx, dy = bcx - acx, bcy - acy
    if abs(dx) * ah > abs(dy) * aw:      # mostly horizontal
        if dx > 0:
            return (ax + aw, acy, bx, bcy)
        return (ax, acy, bx + bw, bcy)
    if dy > 0:
        return (acx, ay + ah, bcx, by)
    return (acx, ay, bcx, by + bh)


def connect(s: Svg, a, b, label=None, dash=None, end="arr", start=None, stroke="#333", offset=0):
    x1, y1, x2, y2 = edge(a, b)
    if abs(x1 - x2) < abs(y1 - y2):
        x1 += offset
        x2 += offset
    else:
        y1 += offset
        y2 += offset
    s.line(x1, y1, x2, y2, stroke, dash=dash, end=end, start=start)
    if label:
        s.label((x1 + x2) / 2, (y1 + y2) / 2 - 3, label)


def elbow(s: Svg, a, b, label=None, dash=None, end="arr", start=None, via="h", stroke="#333", offset=0):
    """A right-angled connector: leave a horizontally (via='h') or vertically (via='v')."""
    x1, y1, x2, y2 = edge(a, b)
    if via == "h":
        y1 += offset
        y2 += offset
        mx = (x1 + x2) / 2
        d = f"M{x1},{y1} L{mx},{y1} L{mx},{y2} L{x2},{y2}"
        lx, ly = mx, (y1 + y2) / 2 - 3
    else:
        x1 += offset
        x2 += offset
        my = (y1 + y2) / 2
        d = f"M{x1},{y1} L{x1},{my} L{x2},{my} L{x2},{y2}"
        lx, ly = (x1 + x2) / 2, my - 3
    s.path(d, stroke, dash=dash, end=end, start=start)
    if label:
        s.label(lx, ly, label)


# ----------------------------------------------------------------------------------------------
#  Figure 1 - the component view
# ----------------------------------------------------------------------------------------------
def fig_components() -> str:
    s = Svg(720, 700)
    # browser
    ui = s.box(20, 15, 680, 58, "Dashboard  (tos_bot/web/  -  index.html, styles.css, 24 ES modules, no build step)",
               ["opens http://127.0.0.1:8787  -  REST calls for actions, a WebSocket for live events (every panel"
                " re-renders from the engine snapshot)"], fill="#f5f9ff", title_fill="#dbe7fb")
    # server
    srv = s.box(20, 100, 680, 58, "FastAPI app  (tos_bot/server/app.py  +  security.py)",
                ["GET/POST /api/*  ->  engine methods           /ws  ->  EventBus queue  ->  JSON events        "
                 "/static  ->  the dashboard files",
                 "same-machine guard on the secrets, quit and setup routes (client, Host, Origin, X-ATB-Request)"],
                fill="#f5f9ff", title_fill="#dbe7fb")
    connect(s, ui, srv, "HTTP + WebSocket", start=None, end="arr", offset=-120)
    s.line(360 + 120, 158, 360 + 120, 73, end="arr")
    s.label(480, 90, "events, snapshots")

    # engine
    s.rect(20, 185, 680, 300, "#fbfbf6", "#333", rx=6)
    s.text(360, 203, "TradingEngine  (tos_bot/engine/engine.py + mixins)  -  one object, 7 background threads",
           11, "middle", "bold")
    s.text(360, 217, "ResearchOps · JournalOps · PairsOps · CapitalOps · QuitOps  (mixins)      "
                     "EventBus (core/eventbus.py) carries every change to the dashboard", 8.5, "middle", fill="#444")
    cw, ch = 150, 62
    cols = [40, 210, 380, 550]
    r1, r2 = 228, 305
    sc = s.box(cols[0], r1, cw, ch, "Scanner", ["scanner/  full scan 08:30,", "gap check 09:15, cycles 5 min,",
                                              "DayWatchlist, evaluate()"], lsize=8.5)
    bd = s.box(cols[1], r1, cw, ch, "PlayBoard", ["engine/board.py", "the plays on the dashboard,",
                                                "ranked, kept across a restart"], lsize=8.5)
    ap = s.box(cols[2], r1, cw, ch, "AutoPilot", ["execution/autopilot.py", "gates a play, caps, daily",
                                                "loss stop, proof rule"], lsize=8.5)
    ex = s.box(cols[3], r1, cw, ch, "Executor + ExitManager", ["execution/  orders in,",
                                                              "stops / targets / trailing,", "scale-out, EOD flatten"],
               lsize=8.5)
    pr = s.box(cols[0], r2, cw, ch, "PairDesk", ["pairs/  cointegrated pairs,", "z-score bands, both legs"],
               lsize=8.5)
    sg = s.box(cols[1], r2, cw, ch, "SignalService", ["signals/  SEC Form 4, 8-K,", "IBKR / Finnhub news,",
                                                    "FinBERT sentiment"], lsize=8.5)
    rp = s.box(cols[2], r2, cw, ch, "ReplayRunner", ["research/  backtest with", "costs + held-out third,",
                                                   "worker processes"], lsize=8.5)
    jn = s.box(cols[3], r2, cw, ch, "Journal + MarketRegime", ["research/journal.py 16:15 review,",
                                                              "movers report; SPY Markov", "switching (calm/turbulent)"],
               lsize=8.5)
    s.text(360, 400, "shared services inside the engine", 8.5, "middle", italic=True, fill="#555")
    md = s.box(40, 408, 200, 62, "MarketData + DailyBarStore", ["data/market_data.py, data/bars.py",
                                                               "daily candles cached as data/bars/*.pkl",
                                                               "5-min candles, quotes, pre-market"], lsize=8.5)
    rep = s.box(260, 408, 200, 62, "Repository  (persistence/)", ["SQLAlchemy ORM: trades, fills, plays,",
                                                                  "reviews, news, pair trades ...",
                                                                  "auto ADD COLUMN migration"], lsize=8.5)
    cn = s.box(480, 408, 200, 62, "Connections + brokers", ["engine/connections.py  ->  brokers/",
                                                            "IbkrBroker (ib_async, own asyncio thread)",
                                                            "PaperBroker (simulator on IBKR prices)"], lsize=8.5)
    connect(s, srv, (20, 185, 680, 300), "method calls / snapshot()", end="arr", offset=-200)

    # below: outside world
    gw = s.box(480, 510, 200, 50, "IB Gateway  (127.0.0.1:4002 / 4001)", ["Interactive Brokers: quotes, candles,",
                                                                          "news, account, orders"], lsize=8.5,
               fill="#fff7e6")
    db = s.box(260, 510, 200, 50, "SQLite  data/autotradebot.sqlite", ["(MySQL if DATABASE_URL is set)",
                                                                       "13 tables - see figure 6"], lsize=8.5,
               fill="#f0fff0")
    fs = s.box(40, 510, 200, 50, "Files under data/", ["bars/, cache/, watchlists/, journal/,",
                                                       "research/, signals/, runtime.json"], lsize=8.5,
               fill="#f0fff0")
    connect(s, cn, gw, "TCP socket (ib_async)")
    connect(s, rep, db, "SQL")
    connect(s, md, fs, "pickle / JSON")
    ext = s.box(40, 590, 640, 70, "External HTTP sources (requests)", [
        "SEC EDGAR: company financials (companyfacts), Form 4 insider trades, 8-K earnings dates      "
        "Nasdaq Trader: the list of US stocks and ADRs",
        "ECB: reference exchange rates (fallback for FX)      Finnhub (optional, with a key): company news, "
        "earnings calendar",
        "Hugging Face (optional): the FinBERT model for headline sentiment, downloaded once"], lsize=8.5,
        fill="#fff")
    s.line(140, 590, 140, 470, end="arr", dash="4,3")
    s.line(310, 590, 310, 367, end="arr", dash="4,3")
    s.label(225, 585, "listings, financials, FX", 8)
    s.label(395, 585, "filings, news, calendar", 8)
    return s.render("Figure 1 - Components: the browser, the FastAPI server, the engine and its services, "
                    "and where data comes from and goes to.")


# ----------------------------------------------------------------------------------------------
#  Figure 2 - the class diagram
# ----------------------------------------------------------------------------------------------
def fig_classes() -> str:
    s = Svg(720, 1010)

    def vtext(x, y, t, size=8):
        s.parts.append(f'<text x="{x}" y="{y}" font-family="{Svg.FONT}" font-size="{size}" fill="#333" '
                       f'text-anchor="middle" transform="rotate(-90 {x} {y})">{esc(t)}</text>')

    eng = s.uml_class(20, 20, 230, "TradingEngine",
                      ["repo: Repository", "md: MarketData", "connections: Connections", "scanner: Scanner",
                       "board: PlayBoard", "autopilot: AutoPilot", "executor: Executor", "exit_manager: ExitManager",
                       "pairs: PairDesk", "signals: SignalService", "replay: ReplayRunner", "journal: Journal",
                       "model: Scorer", "regime: MarketRegime", "mode: paper | live",
                       "filters, strategy_overrides, capital"],
                      ["start() / stop()", "snapshot() -> dict", "request_scan(kind)", "assess_play(id) -> dict",
                       "approve_play(id) / reject_play(id)", "close_position(trade_id)", "set_autopilot(**kw)",
                       "set_filters(...) / set_strategy(key, ...)", "untracked_positions() / close_untracked()"])
    mix = s.box(20, 400, 230, 62, "«mixins»  ResearchOps  JournalOps",
                ["PairsOps  CapitalOps  QuitOps  (engine/*_ops.py)",
                 "start_replay(), strategy_record(), review_session(),",
                 "enter_pair(), set_capital(), begin_quit() ..."], lsize=8.5, fill="#f7f7f7")
    s.line(135, 400, 135, eng[1] + eng[3], end="tri")

    scn = s.uml_class(280, 20, 200, "Scanner",
                      ["md: MarketData", "symbols: SymbolMaster", "listings: UsListings",
                       "fundamentals: SecEdgarFundamentals", "strategies: list[Strategy]", "watchlist: DayWatchlist"],
                      ["run_full(scan) -> ScanResult", "run_cycle(fast) -> ScanResult", "run_gappers()",
                       "run_plays(symbols)"])
    wl = s.uml_class(280, 205, 200, "DayWatchlist",
                     ["session, hot: list[Candidate]", "queues: sector -> [Candidate]", "decisions"],
                     ["build(...)", "next_picks(n, sectors)", "apply_cycle(heat, picks, kept)",
                      "apply_gappers(...)", "save() / load_latest()"])
    strat = s.uml_class(510, 20, 195, "Strategy", ["key, title, thesis", "kind: TECHNICAL | FUNDAMENTAL",
                                                   "timeframe: INTRADAY | SWING", "style, tod_profile",
                                                   "params, weight"],
                        ["generate(ctx) -> list[Play]", "_mk_play(...)  (geometry floors)"], stereo="abstract",
                        abstract=True)
    sub = s.box(510, 175, 195, 50, "22 concrete setups",
                ["technical.py (13)  patterns.py (3)  insider.py (1)", "statistical.py (2)  fundamental.py (3)"], lsize=8.5,
                fill="#f7f7f7")
    s.line(607, 175, 607, strat[1] + strat[3], end="tri")
    ctx = s.uml_class(510, 245, 195, "StrategyContext",
                      ["symbol, intraday, daily: DataFrame", "quote, fundamentals, peers",
                       "activity, signals, market, news", "records, premarket, shared"],
                      ["daily_atr, vwap_series (memo)", "levels(), opening_range(m)",
                       "calibrated_probability(...)"])
    play = s.uml_class(510, 400, 195, "Play", ["symbol, side, strategy, kind", "timeframe, entry, stop, targets",
                                               "confidence, probability, score", "noise: list, confirmations",
                                               "suggested_qty, dollar_risk", "status: PlayStatus, trade_id"],
                       [], stereo="dataclass")
    # the scanner calls the strategies; a strategy reads its context and creates plays (gutter routes)
    s.line(480, 60, 510, 60, end="arr", dash="4,3")
    s.label(495, 52, "generate()", 7.5)
    s.path("M510,100 L490,100 L490,300 L510,300", end="arr", dash="4,3")
    vtext(485, 200, "reads")
    s.path("M510,115 L500,115 L500,440 L510,440", end="arr", dash="4,3")
    vtext(497, 370, "creates")
    # ownership: the diamond sits at the owner
    s.line(280, 90, 250, 90, end="dia")
    s.line(380, 205, 380, scn[1] + scn[3], end="dia")

    ap = s.uml_class(280, 380, 200, "AutoPilot",
                     ["enabled, dry_run, trade_types", "min_confidence, min_reward_risk",
                      "require_proven, min_confirmations", "max_auto_positions / per day",
                      "max_daily_loss_pct, max_giveback_pct"],
                     ["consider(plays) -> actions", "_pre_gate(play) -> reason | None",
                      "proof_missing(strategy)", "daily_loss_reason(equity)", "verdict(play) -> str"])
    exe = s.uml_class(280, 590, 200, "Executor",
                      ["broker: BrokerAdapter", "repo: Repository", "_pending: order -> _Pending"],
                      ["execute_play(play, account)", "close_trade(id, reason, qty)",
                       "sync_open_orders()", "adopt_working_orders()", "close_untracked(symbol)"])
    xm = s.uml_class(280, 760, 200, "ExitManager",
                     ["repo, executor, quote_fn, cfg", "RETRY_DELAYS_S"],
                     ["run_once() -> exits", "_manage(trade) -> exit | None", "_scale_out(...)"])
    s.line(380, ap[1] + ap[3], 380, 590, end="arr", dash="4,3")
    s.text(386, ap[1] + ap[3] + 15, "approve -> execute", 7.5)
    s.line(380, 760, 380, exe[1] + exe[3], end="arr", dash="4,3")
    s.text(386, 752, "close_trade()", 7.5)
    s.line(280, 420, 250, 420, end="dia")
    s.line(250, 400, 250, 420 - 1, stroke="none")

    ba = s.uml_class(510, 590, 195, "BrokerAdapter",
                     ["name, paper: bool", "supports_bracket_native"],
                     ["connect() / close()", "get_account() -> Account", "get_quote(symbol) -> Quote",
                      "place_order(req) -> OrderResult", "place_bracket(...)", "cancel_order(id)",
                      "get_fills(symbol)"], stereo="abstract", abstract=True)
    ib = s.box(510, 790, 92, 62, "IbkrBroker", ["ib_async on its", "own asyncio", "loop thread"], lsize=8.5)
    pb = s.box(613, 790, 92, 62, "PaperBroker", ["simulator,", "fills on IBKR", "prices"], lsize=8.5)
    s.line(556, 790, 556, 770)
    s.line(659, 790, 659, 770)
    s.line(556, 770, 659, 770)
    s.line(607, 770, 607, ba[1] + ba[3], end="tri")
    s.line(480, 650, 510, 650, end="arr", dash="4,3")
    s.label(495, 664, "orders", 7.5)

    repo = s.uml_class(20, 590, 230, "Repository",
                       ["(SQLAlchemy session per call)"],
                       ["record_scan() / record_play()", "open_trade(play, fill, qty, ...)",
                        "reduce_trade(id, qty, price, ...)", "close_trade(id, price, reason, ...)",
                        "open_trades() / trades_on(day)", "save_review() / pnl_summary()",
                        "create_pair_trade() / update_pair_trade()"])
    s.line(280, 640, 250, 640, end="arr", dash="4,3")
    s.label(265, 654, "books", 7.5)
    md = s.uml_class(20, 800, 230, "MarketData",
                     ["source: PriceSource (the broker)", "bars: DailyBarStore"],
                     ["update_daily(symbols, through)", "daily_frame(symbol)", "intraday(symbols) (5-min)",
                      "premarket(symbols)", "quote(symbol)"])
    s.path("M280,120 L262,120 L262,840 L250,840", end="arr", dash="4,3")
    vtext(258, 560, "Scanner reads MarketData")

    bus = s.uml_class(510, 880, 195, "EventBus", ["queues: asyncio.Queue[]"],
                      ["publish(topic, **payload)", "add_queue(q) / remove_queue(q)"])
    sc = s.uml_class(280, 880, 200, "Scorer  (research/model.py)",
                     ["bundle: model + calibrator + columns"],
                     ["score(features) -> {p, id, usable}", "card: the walk-forward verdict"])
    s.text(20, 990, "Notation: a filled diamond = composition (the owner's end); a hollow triangle = inheritance; "
                    "a dashed arrow = a call or dependency.", 8.5, fill="#555")
    return s.render("Figure 2 - Class diagram of the core: the engine and what it owns, the scanner and the "
                    "strategies, the trade path, the broker interface and the persistence layer.")


# ----------------------------------------------------------------------------------------------
#  Figure 3 - the entity-relationship diagram
# ----------------------------------------------------------------------------------------------
def fig_er() -> str:
    s = Svg(720, 790)

    def ent(x, y, w, name, cols, fill="#fff"):
        ls = 8.5
        h = 20 + len(cols) * (ls + 3) + 6
        s.rect(x, y, w, h, fill, "#333", rx=0)
        s.rect(x, y, w, 20, "#e8f5e9", "#333", rx=0)
        s.text(x + w / 2, y + 14, name, 10, "middle", "bold")
        for i, c in enumerate(cols):
            s.text(x + 5, y + 32 + i * (ls + 3), c, ls, mono=True)
        return (x, y, w, h)

    sr = ent(20, 20, 200, "scan_runs", ["id  PK", "kind (full/cycle/fast/gappers)", "started_at, finished_at",
                                        "universe_size, scanned, n_plays", "hot (JSON), elapsed_s"])
    pl = ent(260, 20, 200, "play_logs", ["id  PK", "scan_run_id  FK -> scan_runs", "symbol, side, strategy, kind",
                                         "timeframe, entry, stop, targets", "reward_risk, confidence, score",
                                         "probability, evidence (JSON)", "noise (JSON), confirmations",
                                         "status, decided_at, decided_by"])
    tr = ent(500, 20, 200, "trades", ["id  PK", "play_id  FK -> play_logs (0..1)", "symbol, side, strategy, kind",
                                      "timeframe, broker (venue), status", "quantity, initial_quantity",
                                      "entry_price, entry_time", "stop_price, target_price, target2",
                                      "initial_stop/target, hwm_price", "managed_exit, expected_exit_at",
                                      "exit_price, exit_time, exit_reason", "fees, realized_pl, r_multiple",
                                      "mae, mfe, mfe_at, banked_pl", "submitted_at, entry_context (JSON)",
                                      "is_day_trade, session_date", "pair_id  (-> pair_trades)"])
    fl = ent(500, 312, 200, "fills", ["id  PK (autoincrement)", "trade_id  FK -> trades", "broker_order_id, ts",
                                      "side, leg (ENTRY/EXIT)", "quantity, price, commission"])
    pt = ent(260, 312, 200, "pair_trades", ["id  PK", "pair (FIRST/SECOND), side", "status, venue, by",
                                            "hedge, half_life, entry_z, band_z", "stop_z, exit_z, time_stop_days",
                                            "qty_first, qty_second", "trade_first_id, trade_second_id",
                                            "realized_pl, r_multiple, model"])
    s.line(220, 60, 260, 60, end="crow")
    s.label(240, 52, "1 : n", 8)
    s.line(460, 60, 500, 60, end="arr")
    s.label(480, 52, "1 : 0..1", 8)
    s.line(600, tr[1] + tr[3], 600, 312, end="crow")
    s.label(620, 302, "1 : n", 8)
    s.line(460, 382, 500, 382, end="arr", dash="4,3")
    s.line(460, 397, 500, 397, end="arr", dash="4,3")
    s.label(480, 374, "two legs", 8)

    ac = ent(20, 200, 200, "account_snapshots", ["id  PK", "ts, broker", "equity, cash, buying_power",
                                                  "day_trades_5d, open_positions", "unrealized_pl, realized_pl_day"])
    oa = ent(20, 330, 200, "order_audit", ["id  PK", "ts, play_id, trade_id, broker", "action (PLACE/CANCEL/...)",
                                            "request, response (JSON)", "ok, message"])
    dr = ent(20, 445, 200, "daily_reviews", ["session_date  PK", "trades, total_r, realized_pl",
                                              "mistakes, review (JSON)"])
    nw = ent(260, 482, 200, "news_items", ["key  PK", "symbol, source, provider, kind", "headline, url, ref, items",
                                            "published_at", "sentiment, sentiment_conf"])
    it = ent(500, 432, 200, "insider_trades", ["accession + line  PK", "symbol, issuer_*, owner_*",
                                                "role, title, code (P/S)", "trade_date, shares, price",
                                                "planned, direct, offering, filed"])
    fr = ent(500, 567, 200, "filings_read", ["accession  PK", "form, filed, trades, read_at"])
    st = ent(20, 560, 200, "sim_trades", ["id  PK, run_id, ran_at", "strategy, symbol, side, timeframe",
                                           "entered_at, exited_at, entry, exit", "r, exit_reason, mfe_r, held_out",
                                           "features (JSON), feature_schema"], fill="#f3f0ff")
    sh = ent(260, 612, 200, "shadow_trades", ["play_id  PK (-> play_logs)", "session_date, symbol, strategy",
                                               "seen_at, passed_checks, filled", "entered_at, exited_at, r, mfe_r",
                                               "exit_reason, features (JSON)"], fill="#f3f0ff")
    s.path(f"M260,640 L245,640 L245,60 L{pl[0]},60", dash="4,3", end="arr")
    s.parts.append(f'<text x="241" y="400" font-family="{Svg.FONT}" font-size="7.5" fill="#333" text-anchor="middle" '
                   'transform="rotate(-90 241 400)">one per play not taken</text>')
    s.text(20, 750, "Money columns use a NUMERIC(20,6) type; JSON columns hold dicts/lists. Missing columns are added "
                    "at start-up (persistence/db.py _add_missing_columns).", 8.5, fill="#555")
    s.text(20, 764, "Shaded: what a model learns from (research/dataset.py) - with trades.entry_context, the three "
                    "populations of the learning guide.", 8.5, fill="#555")
    return s.render("Figure 6 - The database (persistence/models_orm.py): thirteen tables, SQLite by default.")


# ----------------------------------------------------------------------------------------------
#  Figure 4 - the life of a play (sequence diagram)
# ----------------------------------------------------------------------------------------------
def fig_sequence() -> str:
    lifelines = ["scan-loop\n(thread)", "Scanner", "MarketData\n/ IBKR", "Strategy\n(x17)", "PlayBoard",
                 "AutoPilot", "Engine\nassess_play", "Executor", "Broker", "Repository", "sync-loop\nExitManager",
                 "EventBus\n-> browser"]
    n = len(lifelines)
    gap = 58
    x0 = 40
    s = Svg(720, 640)
    xs = [x0 + i * gap for i in range(n)]
    for x, name in zip(xs, lifelines):
        lines = name.split("\n")
        s.rect(x - 26, 10, 52, 30, "#e8eef8", "#333", rx=3)
        for j, ln in enumerate(lines):
            s.text(x, 22 + j * 11 - (5 if len(lines) > 1 else 0), ln, 7.5, "middle", "bold")

    y = [60]

    def msg(a, b, label, dash=None, ret=False):
        yy = y[0]
        xa, xb = xs[a], xs[b]
        if a == b:
            s.path(f"M{xa},{yy} L{xa + 22},{yy} L{xa + 22},{yy + 14} L{xa + 1},{yy + 14}", end="arr",
                   dash="3,2" if dash else None)
            s.text(xa + 26, yy + 10, label, 7.5)
            y[0] += 26
            return
        s.line(xa, yy, xb, yy, end="open" if ret else "arr", dash="3,2" if (dash or ret) else None)
        mid = (xa + xb) / 2
        s.label(mid, yy - 3, label, 7.5)
        y[0] += 20

    def note(text, ytop=None):
        yy = y[0]
        s.rect(60, yy - 8, 600, 14, "#fffbe6", "#c9b26b", rx=2)
        s.text(360, yy + 2, text, 7.5, "middle", italic=True)
        y[0] += 18

    note("every 5 s the scan loop asks _due_scan(): full at 08:30, gap check at 09:15, a cycle every 5 min, "
         "a fast hot-list cycle every 60 s, a board refresh every 15 s")
    msg(0, 1, "run_cycle(fast=False)")
    msg(1, 4, "hot list + 2 buffer names per sector", dash=True)
    msg(1, 2, "intraday(symbols) - 5-min candles")
    msg(2, 2, "history_many() cached / IBKR")
    msg(2, 1, "DataFrames", ret=True)
    msg(1, 3, "evaluate(): generate(ctx) for each strategy")
    msg(3, 3, "_mk_play(): entry, stop, targets, odds")
    msg(3, 1, "list[Play]  (+ noise flags, readings, score)", ret=True)
    msg(1, 4, "board.replace(plays)")
    msg(4, 11, "plays.updated, plays.changes")
    msg(4, 9, "record_play() for each new play")
    note("Autopilot runs on every board update (execution/autopilot.py consider())")
    msg(4, 5, "consider(plays) - highest score first")
    msg(5, 5, "daily loss stop? caps? _pre_gate()")
    msg(5, 5, "proof_missing(strategy) - the replay record")
    msg(5, 6, "assess_play(id): session, PDT, sizing, R:R")
    msg(6, 6, "size_play(): 1% risk, half-Kelly, caps")
    msg(6, 5, "{ok, can_execute, qty}", ret=True)
    msg(5, 6, "approve_play(id)")
    msg(6, 7, "execute_play(play, account)")
    msg(7, 8, "place_order / place_bracket(entry, target, stop)")
    msg(8, 7, "OrderResult (FILLED or WORKING)", ret=True)
    msg(7, 9, "open_trade(...)  -> trades row + fill")
    msg(7, 11, "autopilot.entered, play.decided")
    note("from here the sync loop (every 4 s) owns the position: order sync, then the exit manager")
    msg(10, 8, "get_quote(symbol)")
    msg(10, 10, "_manage(): stop? target? time? trail?")
    msg(10, 7, "close_trade(id, reason[, qty])")
    msg(7, 8, "place_order(exit)")
    msg(7, 9, "close_trade() / reduce_trade()")
    msg(10, 11, "exit.triggered / exit.scaled / trade.closed")
    note("the snapshot loop (30 s) re-reads the account and reconciles records against the broker's positions")
    bottom = y[0] + 6
    for x in xs:
        s.parts.insert(0, f'<line x1="{x}" y1="40" x2="{x}" y2="{bottom}" stroke="#999" stroke-dasharray="4,3"/>')
    s.h = bottom + 6
    return s.render("Figure 4 - Sequence: from a scan cycle to an order and, later, to the exit that closes it.")


# ----------------------------------------------------------------------------------------------
#  Figure 5 - threads and loops
# ----------------------------------------------------------------------------------------------
def fig_threads(journal_s, pairs_s) -> str:
    s = Svg(720, 470)
    s.rect(15, 15, 690, 440, "#fbfbfb", "#333", rx=6)
    s.text(360, 33, "One process:  python run.py  ->  uvicorn (asyncio main thread)  ->  create_app()  ->  "
                    "TradingEngine.start()", 10.5, "middle", "bold")
    main = s.box(30, 48, 320, 60, "Main thread - uvicorn / asyncio",
                 ["FastAPI request handlers (engine calls run in a", "thread pool), the WebSocket fan-out of "
                  "EventBus events"], lsize=8.5, fill="#eef4ff")
    ibt = s.box(370, 48, 320, 60, "IBKR loop thread  (brokers/ibkr_adapter.py _IBSession)",
                ["the only place ib_async runs; other threads call", "session.call(fn, timeout) / run_coro() "
                 "and wait"], lsize=8.5, fill="#fff3e0")
    rows = [
        ("scan-loop", "every 5 s: _due_scan() -> run the scan that is due (full / gappers / cycle / fast / plays); "
                      "a scan runs on this thread"),
        ("sync-loop", "every 4 s: executor.sync_open_orders(), exit_manager.run_once(), quit progress"),
        ("snapshot-loop", "every 30 s (10 s while day-trading): connections.refresh(), Gateway watch, "
                          "get_account(), reconcile records, publish account.snapshot"),
        ("orders-loop", "every 5 s: the list of orders working at the broker, for the dashboard"),
        ("signals-loop", "every 15 s: SEC Form 4 feed (3 min), news (15 min), Finnhub calendar (6 h)"),
        ("journal-loop", f"every {journal_s:.0f} s: earnings-ahead warnings on positions; the 16:15 review "
                         "when due (+ the movers report)"),
        ("pairs-loop", f"every {pairs_s:.0f} s: refresh the pair list after a new session, watch z-scores, "
                       "manage open pairs"),
    ]
    y = 125
    s.text(30, y - 4, "Engine daemon threads (engine.start())", 9.5, weight="bold")
    for name, desc in rows:
        s.rect(30, y + 4, 110, 22, "#e8eef8", "#333", rx=3)
        s.text(85, y + 19, name, 9, "middle", "bold", mono=True)
        s.rect(150, y + 4, 540, 22, "#fff", "#333", rx=3)
        s.text(156, y + 19, desc, 8, fill="#222")
        y += 30
    rp = s.box(30, y + 12, 320, 62, "replay thread (research/runner.py)", [
        "started from the Research panel; ProcessPoolExecutor with", "cpu-2 workers, one job per stock "
        "(day trades: 10 sessions each);", "results -> data/research/replay.json + replay_runs.jsonl"],
        lsize=8.5, fill="#f3f0ff")
    s.box(370, y + 12, 320, 62, "shared state and locks", [
        "_scan_lock, _orders_lock, _quit_lock, _switch_lock (RLock);", "the board and watchlist under their "
        "own locks; every loop", "sleeps on the one _stop Event, so stop() ends them together"], lsize=8.5)
    return s.render("Figure 3 - Threads: everything runs in one Python process; the engine's loops are "
                    "daemon threads, the replay uses worker processes.")


# ----------------------------------------------------------------------------------------------
#  Figure 6 - state machines
# ----------------------------------------------------------------------------------------------
def fig_states() -> str:
    s = Svg(720, 360)

    def st(x, y, name, w=96, fill="#fff"):
        s.rect(x, y, w, 26, fill, "#333", rx=13)
        s.text(x + w / 2, y + 17, name, 9, "middle", "bold")
        return (x, y, w, 26)

    s.text(20, 22, "Play (core/enums.py PlayStatus)", 10, weight="bold")
    s.parts.append('<circle cx="30" cy="55" r="6" fill="#333"/>')
    a = st(50, 42, "PROPOSED")
    b = st(190, 42, "ACCEPTED")
    c = st(330, 42, "SUBMITTED")
    d = st(470, 42, "WORKING")
    e = st(610, 42, "FILLED", fill="#e8f5e9")
    s.line(36, 55, 50, 55, end="arr")
    s.line(146, 55, 190, 55, end="arr")
    s.label(168, 50, "approve", 7.5)
    s.line(286, 55, 330, 55, end="arr")
    s.label(308, 50, "order sent", 7.5)
    s.line(426, 55, 470, 55, end="arr")
    s.line(566, 55, 610, 55, end="arr")
    s.label(588, 50, "fill", 7.5)
    s.path("M378,42 C420,10 560,10 640,42", end="arr")
    s.label(510, 22, "immediate fill (marketable / simulator)", 7.5)
    r = st(50, 100, "REJECTED", fill="#fdecea")
    x = st(190, 100, "EXPIRED", fill="#fdecea")
    k = st(470, 100, "CANCELED", fill="#fdecea")
    er = st(330, 100, "ERROR", fill="#fdecea")
    s.line(98, 68, 98, 100, end="arr")
    s.label(120, 88, "reject / filters", 7.5)
    s.path("M120,68 L238,100", end="arr")
    s.label(250, 84, "aged out", 7.5)
    s.line(378, 68, 378, 100, end="arr")
    s.label(400, 88, "broker error", 7.5)
    s.line(518, 68, 518, 100, end="arr")
    s.label(545, 88, "cancelled", 7.5)
    s.text(610, 118, "FILLED -> a trades row (OPEN)", 8, fill="#444")

    s.text(20, 165, "Trade (persistence: trades.status) and what the exit manager does to it", 10, weight="bold")
    s.parts.append('<circle cx="30" cy="200" r="6" fill="#333"/>')
    o = st(50, 187, "OPEN", 90)
    p = st(240, 187, "OPEN, reduced", 110, fill="#fff8e1")
    z = st(450, 187, "CLOSED", 90, fill="#e8f5e9")
    s.line(36, 200, 50, 200, end="arr")
    s.line(140, 200, 240, 200, end="arr")
    s.label(190, 194, "target-1: scale out 50%", 7.5)
    s.line(350, 200, 450, 200, end="arr")
    s.label(400, 194, "stop / target / time", 7.5)
    s.path("M95,213 C150,262 400,262 470,213", end="arr")
    s.label(280, 258, "stop, target, trailing-stop, eod-flatten, time-stop, manual, quit", 7.5)
    s.rect(50, 275, 640, 72, "#fffbe6", "#c9b26b", rx=3)
    s.text(60, 290, "While OPEN, every 4 s (_manage): update MFE/MAE and the high-water mark; exit at the stop or "
                    "target; flatten day trades 10 min before the close;", 8)
    s.text(60, 304, "close swings after 10 days; once +1.3R move the stop to lock +0.3R; past +2R trail the stop at "
                    "half the gain. The stop never crosses the current price.", 8)
    s.text(60, 318, "A record whose position disappears at the broker is settled from the broker's own fills "
                    "(_settle_gone / _exit_fill), never deleted on a stale answer.", 8)
    s.text(60, 332, "Positions with no record show as 'untracked' on the blotter and can be closed by hand.", 8)
    s.text(560, 320, "", 1)
    return s.render("Figure 5 - State machines: a play's status and a trade's life under the exit manager.")


# ----------------------------------------------------------------------------------------------
#  Figure 7 - the research loop
# ----------------------------------------------------------------------------------------------
def fig_research() -> str:
    s = Svg(720, 420)
    a = s.box(20, 30, 200, 70, "Replay (backtest)", ["research/replay.py + runner.py",
                                                    "5-bps slippage, 1-bp commission,", "latest third held out"],
              lsize=8.5, fill="#f3f0ff")
    b = s.box(260, 30, 200, 70, "Records per strategy", ["trades, win rate, expectancy R,",
                                                        "out_of_sample block,", "learned noise skips"],
              lsize=8.5, fill="#f3f0ff")
    c = s.box(500, 30, 200, 70, "Autopilot proof rule", ["n >= 30, R >= +0.05 net of drift,",
                                                        "held-out R > 0, luck p <= 0.10,", "costs <= 1/3 of the edge"],
              lsize=8.5, fill="#fff3e0")
    d = s.box(20, 170, 200, 70, "Live trades + journal", ["research/journal.py, movers.py",
                                                         "16:15 review: mistakes, shadows,", "lessons, market movers"],
              lsize=8.5, fill="#e8f5e9")
    e = s.box(260, 170, 200, 70, "Evidence weights", ["research/weights.py",
                                                     "multiplier 0.5-1.5 per strategy,", "live trades count double"],
              lsize=8.5, fill="#e8f5e9")
    f = s.box(500, 170, 200, 70, "Calibrated odds + sizing", ["pooled_odds() blends a play's own",
                                                              "odds with the record; half-Kelly", "caps the risk %"],
              lsize=8.5, fill="#e8f5e9")
    connect(s, a, b, "records")
    connect(s, b, c, "proof")
    connect(s, d, e, "live R")
    connect(s, b, e, "replay side", offset=0)
    connect(s, e, f, "weights")
    s.path("M600,170 L600,100", end="arr")
    s.label(640, 140, "records -> odds", 8)
    s.path("M120,170 L120,100", end="arr", dash="4,3")
    s.label(150, 140, "same exit rules", 8)
    g = s.box(20, 300, 200, 70, "Significance", ["research/significance.py: bootstrap,",
                                                "reality check, SQN, marble bag,", "drift and cost per trade"],
              lsize=8.5, fill="#f3f0ff")
    h = s.box(260, 300, 200, 70, "Meta-label model", ["research/model.py: boosted trees,",
                                                    "purged walk-forward verdict,", "retrained nightly, shadow first"],
              lsize=8.5, fill="#e3f2fd")
    i = s.box(500, 300, 200, 70, "Execution quality", ["the quote at the decision, the fill",
                                                     "against it (slippage bps), the", "spread gate, entry time-outs"],
              lsize=8.5, fill="#e3f2fd")
    s.path("M120,300 L120,240", end="none", stroke="none")
    s.path("M60,300 L60,250 L8,250 L8,65 L20,65", end="arr", dash="4,3")
    s.label(40, 270, "judges", 8)
    connect(s, d, h, "rows", offset=0)
    s.path("M360,300 L360,240", end="none", stroke="none")
    s.path("M600,300 L600,240", end="arr", dash="4,3")
    s.label(650, 275, "real costs", 8)
    s.text(20, 400, "Everything on this page is stored under data/research/ and data/journal/, and shown on the "
                    "Research and Reports panels.", 8.5, fill="#555")
    return s.render("Figure 7 - The evidence loop: the replay proves a setup, live results re-weight it, "
                    "and both calibrate the odds the plays state.")


# ----------------------------------------------------------------------------------------------
#  The document
# ----------------------------------------------------------------------------------------------
CSS = """
@page { size: A4; margin: 15mm 14mm 16mm 14mm; }
html, body { margin: 0; padding: 0; }
body { font-family: "Segoe UI", Arial, Helvetica, sans-serif; font-size: 10.5pt; line-height: 1.42; color: #1b1b1b; }
h1 { font-size: 22pt; margin: 0 0 6px 0; color: #1f3a68; }
h2 { font-size: 15pt; color: #1f3a68; border-bottom: 2px solid #1f3a68; padding-bottom: 3px; margin: 0 0 10px 0;
     page-break-before: always; }
h2.first { page-break-before: auto; }
h3 { font-size: 12pt; color: #2b4a7a; margin: 16px 0 6px 0; }
h4 { font-size: 10.5pt; margin: 12px 0 4px 0; }
p { margin: 0 0 8px 0; }
ul, ol { margin: 0 0 8px 0; padding-left: 22px; }
li { margin-bottom: 3px; }
table { border-collapse: collapse; width: 100%; font-size: 9.2pt; margin: 6px 0 10px 0; page-break-inside: auto; }
th, td { border: 1px solid #b9b9b9; padding: 3px 6px; vertical-align: top; text-align: left; }
th { background: #e8eef8; }
tr { page-break-inside: avoid; }
code { font-family: Consolas, Menlo, monospace; font-size: 9.2pt; background: #f2f2f2; padding: 0 3px; border-radius: 2px; }
pre { font-family: Consolas, Menlo, monospace; font-size: 8.8pt; background: #f6f6f6; border: 1px solid #ddd;
      padding: 6px 8px; white-space: pre-wrap; page-break-inside: avoid; }
.fig { margin: 8px 0 12px 0; page-break-inside: avoid; }
.cap { font-size: 9pt; color: #555; text-align: center; margin-top: 4px; }
.cover { height: 250mm; display: flex; flex-direction: column; justify-content: center; }
.cover .t { font-size: 34pt; font-weight: bold; color: #1f3a68; }
.cover .s { font-size: 15pt; color: #444; margin-top: 8px; }
.cover .m { font-size: 10.5pt; color: #666; margin-top: 40px; }
.box { border: 1px solid #c9b26b; background: #fffbe6; padding: 6px 10px; margin: 8px 0; border-radius: 3px; }
.toc li { margin-bottom: 2px; }
.small { font-size: 9pt; color: #444; }
.kv td:first-child { width: 28%; font-weight: bold; background: #f7f7f7; }
"""


def table(headers, rows, cls=""):
    h = "".join(f"<th>{c}</th>" for c in headers)
    b = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<table class="{cls}"><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table>'


def c(s):  # inline code
    return f"<code>{esc(s)}</code>"


def read_const(path, name, default):
    try:
        m = re.search(rf"^\s*{name}\s*[:=]\s*(?:float\s*=\s*)?([0-9.]+)", (ROOT / path).read_text("utf-8"), re.M)
        return float(m.group(1)) if m else default
    except Exception:
        return default


def build() -> str:
    journal_s = read_const("tos_bot/engine/journal_ops.py", "JOURNAL_POLL_S", 60.0)
    pairs_s = read_const("tos_bot/engine/pairs_ops.py", "PAIRS_POLL_S", 30.0)
    parts = [f"<title>AutoTradeBot Architecture</title><style>{CSS}</style>"]
    A = parts.append

    # ---- cover ------------------------------------------------------------------------------
    A('<div class="cover"><div class="t">AutoTradeBot</div>'
      '<div class="s">How the app works - an architecture guide for a computer scientist</div>'
      '<div class="m">Repository: omar3903/ClaudeAutoBot (master, 16 September 2026)<br>'
      'Python 3.10 · FastAPI · SQLAlchemy · ib_async · pandas/numpy · plain ES-module dashboard<br><br>'
      'Contents: the twelve books · the big picture · the runtime · a play\'s life · the strategies · '
      'the data and the databases · the research loop and the learning storage · the dashboard and the API · '
      'the important functions · a glossary of the trading terms<br><br>'
      'Companion: AutoTradeBot-learning.pdf, the machine-learning plan in detail</div></div>')

    # ---- 1 the books ------------------------------------------------------------------------
    A('<h2 class="first">1. The twenty books the app was trained on</h2>')
    A('<p>The PDFs live in <i>OneDrive/Desktop/Training books &amp; Documentation papers/Financial-Economic books/'
      'Used books/</i>. The first four gave the app its trade setups and the way it explains a trade; the next '
      'eight gave it the statistics behind the replay, the noise checks, the volatility model, the market regime '
      'and the pairs desk. The last eight (added 2026-09-18) are about not fooling yourself: whether a record is '
      'luck, what trading really costs, a model that learns which plays pay, and two more books of setups. '
      'The last column says where each idea lives in the code.</p>')
    books = [
        ("How to Day Trade for a Living", "Andrew Aziz", "self-published, 2016",
         "the day-trade setups (opening-range breakout, VWAP, flags, ABCD, reversals), 2:1 reward-to-risk, the 2% "
         "rule, time-of-day weights, the daily maximum loss, gappers watchlist, scaling out at the first target",
         "strategies/technical.py, analysis/levels.py, risk/position_sizing.py, execution/autopilot.py "
         "(daily loss), scanner/heat.py (gappers), execution/exit_manager.py (scale-out)"),
        ("Trading in the Zone", "Mark Douglas", "Prentice Hall 2000 (Wiley 2021 printing)",
         "thinking in probabilities: every play states an edge, its odds, and what invalidates it",
         "strategies/base.py (the explanation, invalidation and odds fields on every Play)"),
        ("Technical Analysis of the Financial Markets", "John J. Murphy", "New York Institute of Finance, 1999",
         "horizontal support and resistance, oscillator divergence, trend reading",
         "analysis/levels.py, indicators/ta.py (rsi_divergence, macd_divergence), the divergence_reversal and "
         "sr_bounce setups"),
        ("Financial Modeling and Valuation: A Practical Guide to Investment Banking and Private Equity, 2nd ed.",
         "Paul Pignataro", "Wiley, 2022",
         "enterprise value, comparable multiples, discounted cash flow, the football-field band",
         "valuation/ (dcf.py, multiples.py, football_field.py, projections.py), strategies/fundamental.py, "
         "data/sec_edgar.py (the financials)"),
        ("Quantitative Trading: How to Build Your Own Algorithmic Trading Business", "Ernest P. Chan",
         "Wiley, 2009",
         "backtests must charge costs, keep a held-out sample, and paper-trade before trusting; half-Kelly sizing",
         "research/replay.py (costs, held-out third), quant/sizing.py, research/weights.py (live trades count "
         "double)"),
        ("Algorithmic Trading: Winning Strategies and Their Rationale", "Ernest P. Chan", "Wiley, 2013",
         "stationarity tests (ADF, Hurst, variance ratio), half-life of mean reversion, buy-on-gap, post-earnings "
         "drift, momentum fails in turbulent markets",
         "quant/stationarity.py, quant/readings.py, scanner/noise.py (not_trending, not_mean_reverting, "
         "turbulent_market), strategies/statistical.py"),
        ("Analysis of Financial Time Series, 3rd ed.", "Ruey S. Tsay", "Wiley, 2010",
         "GARCH(1,1) volatility forecasting, the market model",
         "quant/volatility.py, quant/market_model.py (abnormal moves vs SPY)"),
        ("Applied Econometric Time Series, 4th ed.", "Walter Enders", "Wiley, 2014",
         "the same volatility toolkit (RiskMetrics EWMA) and unit-root testing",
         "quant/volatility.py (ewma_var), quant/stationarity.py"),
        ("Time Series Analysis", "James D. Hamilton", "Princeton University Press, 1994",
         "chapter 22: Markov switching regimes fitted by EM - the calm/turbulent market pill",
         "quant/regime.py, engine/market_regime.py (SPY, 3 years of daily returns)"),
        ("Pairs Trading: Quantitative Methods and Analysis", "Ganapathy Vidyamurthy", "Wiley, 2004",
         "spread construction, the nonparametric band that maximises profit per crossing, holding periods",
         "quant/bands.py, pairs/model.py, pairs/desk.py"),
        ("Likelihood-Based Inference in Cointegrated Vector Autoregressive Models", "Søren Johansen",
         "Oxford University Press, 1995",
         "the Johansen trace test used to confirm a pair is cointegrated",
         "quant/cointegration.py (johansen), pairs/finder.py"),
        ("The Cointegrated VAR Model: Methodology and Applications", "Katarina Juselius",
         "Oxford University Press, 2007",
         "how to read the cointegration rank in practice; Engle-Granger as the first pass",
         "quant/cointegration.py (engle_granger), pairs/finder.py"),
        ("Evidence-Based Technical Analysis", "David R. Aronson", "Wiley, 2006",
         "a rule's record is a hypothesis test: bootstrap the mean against zero, test the best of many rules "
         "against the best that luck makes (White's reality check), and detrend first so being long in a rising "
         "market is not an edge",
         "research/significance.py (luck_test, reality_check), research/replay.py (drift_r on every trade), "
         "execution/autopilot.py (proof_p_value in the proof rule)"),
        ("Advances in Financial Machine Learning", "Marcos López de Prado", "Wiley, 2018",
         "meta-labelling (the setups pick the side, a model decides whether to act and how big), sample weights by "
         "uniqueness with time decay, purged walk-forward folds with an embargo, out-of-sample feature importance, "
         "bet size from the predicted probability",
         "research/model.py (Boosted, sample_weights, importance, bet_size), research/validate.py (the folds)"),
        ("Machine Learning for Algorithmic Trading, 2nd ed.", "Stefan Jansen", "Packt, 2020",
         "gradient boosting on tabular features with gaps, calibrated probabilities, the information coefficient "
         "of a feature, time-series cross-validation",
         "research/model.py (HistGradientBoosting, isotonic calibration, information_coefficients), "
         "scripts/train_model.py"),
        ("Trade Your Way to Financial Freedom, 2nd ed.", "Van K. Tharp", "McGraw-Hill, 2006",
         "R-multiples and expectancy (the app's unit of account), the quality of an R-multiple distribution (the "
         "number he later named SQN), the marble-bag simulation of drawdowns, the percent-risk sizing model",
         "research/significance.py (sqn, marble_bag), risk/position_sizing.py"),
        ("Systematic Trading", "Robert Carver", "Harriman House, 2015",
         "costs are the one number known in advance: a rule that pays more than a third of its pre-cost return "
         "in costs trades too fast (the speed limit)",
         "research/replay.py (cost_r on every trade), research/significance.py (cost_share, SPEED_LIMIT), the "
         "proof rule"),
        ("Trading and Exchanges: Market Microstructure for Practitioners", "Larry Harris",
         "Oxford University Press, 2003",
         "implementation shortfall (the fill against the price at the decision), the spread as the price of "
         "immediacy, why a stale limit order is a free option for someone else",
         "engine.py (_chase_check: spread gate, the quote kept at the decision), execution/executor.py "
         "(expire_entries), trades.entry_slippage_bps / exit_slippage_bps, research/journal.py (execution_quality)"),
        ("The Art and Science of Technical Analysis", "Adam H. Grimes", "Wiley, 2012",
         "market structure tested statistically; the two templates he trades: the failure test (Wyckoff's spring "
         "and upthrust) and the pullback after a momentum thrust, read with a 20-EMA inside 2.25-ATR Keltner channels",
         "strategies/patterns.py (failure_test, trend_pullback)"),
        ("Encyclopedia of Chart Patterns, 2nd ed.", "Thomas N. Bulkowski", "Wiley, 2005",
         "measured statistics for chart patterns: twin bottoms fail 64% of the time until a close confirms them; "
         "confirmed, the projected height is met about two times in three and half throw back first",
         "strategies/patterns.py (double_bottom: confirmation, measure rule, a stop inside the pattern)"),
    ]
    A(table(["Title", "Author", "Edition", "What it contributed", "Where in the code"],
            [(f"<b>{esc(t)}</b>", esc(a), esc(e), esc(w), c(w2)) for t, a, e, w, w2 in books]))
    A('<p class="small">Only Murphy and Hamilton are scanned images; the rest are text PDFs. None of the '
      'statistics needs SciPy or statsmodels: the models in <code>quant/</code> and '
      '<code>research/significance.py</code> are hand-written NumPy (OLS, Nelder-Mead, EM, the bootstrap). Only '
      'the learned model (<code>research/model.py</code>) uses scikit-learn, and the app runs without it. '
      '<code>scripts/r/audit_records.R</code> recomputes the record statistics in base R as an independent '
      'check; nothing live ever waits on R.</p>')

    # ---- 2 big picture --------------------------------------------------------------------------
    A('<h2>2. The big picture</h2>')
    A('<p><b>What it is.</b> AutoTradeBot is a single-process Python application that watches the US stock '
      'market through an Interactive Brokers (IBKR) connection, proposes trades ("plays") from a catalogue of '
      'strategies, optionally enters them on its own ("Autopilot"), always manages the exits, and keeps a '
      'record of everything in a database. A browser dashboard on <code>127.0.0.1:8787</code> shows the state '
      'and takes the operator\'s decisions.</p>')
    A('<p><b>In one sentence per layer:</b></p><ul>'
      '<li><b>Dashboard</b> (<code>tos_bot/web/</code>): static HTML plus ES modules; talks REST for actions '
      'and listens on a WebSocket for events.</li>'
      '<li><b>Server</b> (<code>tos_bot/server/app.py</code>): a FastAPI app whose routes are thin wrappers over '
      'engine methods; <code>/ws</code> streams the event bus.</li>'
      '<li><b>Engine</b> (<code>tos_bot/engine/</code>): the conductor. Owns every service, runs seven daemon '
      'threads, and is the only writer of the runtime state.</li>'
      '<li><b>Scanner and strategies</b> (<code>scanner/</code>, <code>strategies/</code>): turn candles into '
      'plays on a schedule.</li>'
      '<li><b>Execution</b> (<code>execution/</code>): Autopilot gates, the Executor sends orders, the '
      'ExitManager manages open positions.</li>'
      '<li><b>Research</b> (<code>research/</code>, <code>quant/</code>): the replay (backtest), the daily '
      'journal, the evidence weights, and the statistical models.</li>'
      '<li><b>Data and brokers</b> (<code>data/</code>, <code>brokers/</code>, <code>persistence/</code>): '
      'market data with an on-disk cache, the IBKR adapter and a simulator, and the SQL layer.</li></ul>')
    A(fig_components())
    A('<h3>The core classes</h3>')
    A('<p>Figure 2 is the class view of the same thing. The engine is a composition root: it constructs every '
      'service in <code>__init__</code>, hands them what they need (the repository, the market data, the event '
      'bus) and exposes the operator actions the server calls. The mixin classes only split the engine\'s source '
      'into files by topic; at runtime there is one object. Strategies are stateless: a <code>StrategyContext</code> '
      'per stock carries the data and the memoised indicators, and <code>generate()</code> returns plays.</p>')
    A(fig_classes())
    A('<h3>Technology stack</h3>')
    A(table(["Concern", "Choice", "Notes"], [
        ("Language / runtime", "Python 3.10, one process", "Started by <code>run.py</code>; "
         "<code>scripts/run_24_7.bat</code> keeps it up"),
        ("Web server", "FastAPI + uvicorn, WebSocket", "REST routes call the engine in a thread pool; "
         "<code>security.py</code> restricts sensitive routes to the same machine"),
        ("Broker / prices", "ib_async against IB Gateway", "Paper on port 4002, live on 4001. The only price "
         "source; delayed data is accepted"),
        ("Simulator", "<code>brokers/paper_adapter.py</code>", "Fills on IBKR prices; its state is in "
         "<code>data/paper_state.json</code>"),
        ("Database", "SQLAlchemy 2 ORM; SQLite by default", "MySQL/PyMySQL if <code>DATABASE_URL</code> is set; "
         "falls back to SQLite when MySQL is unreachable"),
        ("Numerics", "pandas, numpy", "No TA-Lib, no SciPy: <code>indicators/ta.py</code> and "
         "<code>quant/</code> are vectorised by hand"),
        ("Config", "pydantic-settings", "<code>.env</code> for secrets, <code>config/config.yaml</code> for "
         "defaults, <code>data/runtime.json</code> for dashboard choices (it wins)"),
        ("Dashboard", "Vanilla JS ES modules", "No bundler, no framework; <code>state.js</code> is a small "
         "pub/sub store"),
        ("Tests", "pytest, 474 tests in 52 files", "<code>tests/fakes.py</code> fakes the Gateway; "
         "<code>-m slow</code> boots the whole engine"),
        ("Optional ML", "transformers + torch (FinBERT)", "Headline sentiment; the app runs without it"),
        ("Optional learning", "scikit-learn", "The meta-label model (<code>research/model.py</code>): boosted "
         "trees and isotonic calibration; the app runs without it, with no model"),
        ("Independent audit", "base R (optional)", "<code>scripts/r/audit_records.R</code> recomputes the record "
         "statistics offline; nothing live waits on R"),
    ]))
    A('<h3>How the repository is laid out</h3>')
    A('<pre>run.py                      boot the engine + dashboard (Ctrl+C follows the quit rules)\n'
      'config/config.yaml          defaults for every panel (config.example.yaml is the template)\n'
      'data/                       everything the app writes (git-ignored): SQLite, candle cache, journal ...\n'
      'tos_bot/\n'
      '  config.py                 .env + config.yaml -> Settings (pydantic)\n'
      '  core/                     enums, dataclasses (Play, Account, OrderRequest ...), EventBus\n'
      '  engine/                   TradingEngine + mixins, board, connections, reconcile, runtime, regime\n'
      '  scanner/                  schedule, heat, watchlist, evaluator, filters, noise, scanner\n'
      '  strategies/               base (StrategyContext, Strategy), technical, statistical, fundamental, insider\n'
      '  execution/                order_builder, executor, exit_manager, autopilot\n'
      '  risk/                     position_sizing, pdt_guard\n'
      '  research/                 replay, runner, history, weights, journal, movers\n'
      '  quant/                    stationarity, cointegration, volatility, regime, bands, sizing, market_model\n'
      '  pairs/                    model, finder, backtest, desk\n'
      '  signals/                  service, store, book, form4, edgar, news, finnhub, sentiment, calendar\n'
      '  data/                     market_data, bars, listings, symbols, sec_edgar, fundamentals, fx, sectors\n'
      '  valuation/                enterprise_value, multiples, dcf, projections, football_field\n'
      '  analysis/                 levels (support/resistance), candles\n'
      '  indicators/ta.py          SMA/EMA/RSI/MACD/ATR/Bollinger/Keltner/ADX/VWAP/opening range/divergence\n'
      '  brokers/                  base (BrokerAdapter), venues, ibkr_adapter, paper_adapter\n'
      '  persistence/              db (engine + migration), models_orm (tables), repository (queries)\n'
      '  server/                   app.py (routes + WebSocket), security.py\n'
      '  web/                      index.html, styles.css, js/*.js\n'
      '  util/                     clock (NYSE calendar to 2028), logging, keep_awake, net\n'
      'tests/                      pytest (fakes.py = a synthetic Gateway)\n'
      'scripts/                    init_db.py (MySQL schema), ibkr_setup.py (Gateway doctor), run_24_7.bat</pre>')

    # ---- 3 runtime ------------------------------------------------------------------------------
    A('<h2>3. The runtime: process, threads and loops</h2>')
    A('<p><code>python run.py</code> builds the FastAPI app, which constructs one <code>TradingEngine</code> '
      'and calls <code>start()</code>. Start binds the broker chosen by the routing (<code>brokers/venues.py</code>: '
      'IBKR paper, IBKR live, or the simulator on IBKR prices), reads the account once, and launches the seven '
      'daemon threads below. Every thread sleeps on the same <code>threading.Event</code>, so <code>stop()</code> '
      'ends them together. Ctrl+C goes through the quit rules: in paper it closes positions and exits; in live with '
      'positions open it asks in the dashboard first.</p>')
    A(fig_threads(journal_s, pairs_s))
    A('<h3>The scan schedule (scanner/schedule.py)</h3>')
    A(table(["Scan kind", "When", "What it reads", "What it produces"], [
        ("<b>full</b>", "once a day at 08:30 ET (settable 04:00-09:00); also on start-up if today's is missing",
         "the Nasdaq listing of ~5,700 stocks and ADRs, IBKR contract details, one year of daily candles for "
         "every tradable name (top-ups after the first day), SEC financials for the leaders",
         "a ranked universe by daily heat, the day's hot list (20) and per-sector buffers (25 each), swing plays "
         "for the top 400 names, valuation plays"),
        ("<b>gappers</b>", "09:15 ET (settable 08:00-09:25)", "pre-market 5-minute candles of the hot list and "
         "buffers", "swaps the coolest non-gapping hot names for stocks gapping 2%+ on 50k+ shares"),
        ("<b>cycle</b>", "every 5 minutes while the market is open", "5-minute candles of the hot list + 2 buffer "
         "names per sector", "day-trade and swing plays; adopt / keep / drop decisions on the buffers"),
        ("<b>fast</b>", "every 60 s while Autopilot is day-trading", "the hot list only", "fresh day-trade plays"),
        ("<b>plays</b>", "every 15 s", "the stocks that already have plays on the board", "re-evaluates them so "
         "stale plays leave"),
    ]))
    A('<p>The design is deliberate: a full sweep of the market takes about ten minutes of Gateway time (each '
      'candle request takes the Gateway about half a second whatever its length; twelve in flight read ~17 '
      'stocks/s, and thirty-two time out). So the whole market is read once a day and the '
      'day\'s attention goes to a small, sector-diverse list. Swing setups read daily candles, which change once '
      'a day, so rescanning everything intraday would not find more of them.</p>')

    # ---- 4 a play's life ------------------------------------------------------------------------
    A('<h2>4. The life of a play</h2>')
    A('<p>A <b>play</b> is the app\'s unit of work: a proposed trade with a symbol, a side (LONG or SHORT), an '
      'entry price, a stop (where the idea is wrong), one or two targets, the setup\'s confidence and odds, '
      'noise flags, and a rank score. It is a plain dataclass (<code>core/models.py Play</code>). Figure 4 '
      'follows one from a scan cycle to its exit.</p>')
    A(fig_sequence())
    A('<h3>Step by step</h3><ol>'
      '<li><b>Candles.</b> <code>MarketData.intraday()</code> returns a pandas DataFrame per symbol (5-minute '
      'OHLCV in New York time). Daily candles come from the pickle cache in <code>data/bars/</code>, topped up '
      'from IBKR.</li>'
      '<li><b>Evaluate.</b> <code>scanner/evaluator.py evaluate()</code> builds a <code>StrategyContext</code> '
      '(candles, quote, activity, signals, market regime, the strategy records) and calls each enabled '
      'strategy\'s <code>generate(ctx)</code>. Expensive series (ATR, VWAP, levels) are memoised on the context '
      'so seventeen strategies share one computation.</li>'
      '<li><b>Geometry guard rails.</b> <code>Strategy._mk_play()</code> refuses plays whose stop is too tight '
      '(below 0.6% intraday / 1.5% swing, below 0.9 ATR, or below the GARCH volatility floor) or whose '
      'reward-to-risk is outside 0.4-8. A tightened stop cannot fake a good ratio.</li>'
      '<li><b>Noise and readings.</b> <code>scanner/noise.py</code> flags a play that goes against the trend, '
      'against VWAP, into a gap, on a news-driven move, in a turbulent market, or on a non-trending stock '
      '(ADF/Hurst). Readings (price character, volatility forecast, next earnings) go into <code>evidence</code>.</li>'
      '<li><b>Score.</b> <code>rank_score()</code> = expected value in R units times the strategy weight, plus '
      'small bumps for unusual volume, a gap and enough range.</li>'
      '<li><b>Board.</b> <code>PlayBoard.replace()</code> keeps the top 80, notes what joined and left, and '
      'publishes <code>plays.updated</code>. Every new play is logged to <code>play_logs</code>.</li>'
      '<li><b>Autopilot.</b> <code>AutoPilot.consider()</code> walks the board highest score first and applies '
      'the gates in the next table. The first failing gate is the reason shown on the dashboard.</li>'
      '<li><b>Assessment and sizing.</b> <code>engine.assess_play()</code> checks the session, the pattern-day-'
      'trader rule (live only), and calls <code>risk/position_sizing.py size_play()</code>: risk 1% of equity per '
      'trade, lowered by the strategy\'s half-Kelly (a quarter of the risk until the replay has proven the '
      'strategy), the mid-day factor, the open-risk ceiling (4%), the per-'
      'position cap (12% of equity) and the per-symbol cap (15%).</li>'
      '<li><b>Order.</b> <code>Executor.execute_play()</code> builds a limit order (5 bps through the price) and, '
      'where the broker supports it, a native bracket with the stop and target attached. An immediate fill opens '
      'the trade; otherwise the order is tracked and <code>sync_open_orders()</code> books the fill later.</li>'
      '<li><b>Exit.</b> The sync loop runs <code>ExitManager.run_once()</code> every 4 s on every open trade '
      'of this venue (figure 5).</li>'
      '<li><b>A stop that outlives the app.</b> On IBKR the executor also keeps a good-till-cancelled stop order '
      'resting at the broker for every open trade (<code>execution/protective_stops.py</code>), made to match the '
      'trade record on every pass. It is cancelled, and the cancel confirmed, before the app sends any exit of its '
      'own; if it filled first, that fill is booked and nothing else is sent; a stop whose trade is no longer open '
      'is swept away. Beside the stop rests a limit order at the target, in one one-cancels-all group with it (IBKR '
      'reduces the other by what fills): profit is taken at the target on real prices, even on delayed quotes or with '
      'the app off, and the two can never both fill for the whole position.</li></ol>')
    A('<h3>The Autopilot gates, in order (execution/autopilot.py)</h3>')
    A(table(["Gate", "Rule", "Setting"], [
        ("switched on, venue ok", "Autopilot on; live trading only when the live checks pass", "enabled"),
        ("daily stop", "no entries once today's closed trades lost 2% of equity, or gave back 30% of the day's "
         "peak gain (after +0.25%)", "max_daily_loss_pct, max_giveback_pct"),
        ("per-cycle / per-day / concurrent caps", "1 new entry per pass, 5 per day, 10 open, 2 per strategy",
         "max_new_per_cycle, max_auto_trades_per_day, max_auto_positions, max_per_strategy"),
        ("trade type", "day and swing follow the filter bar; pairs separately", "trade_types"),
        ("confidence", "the setup's own conviction >= the floor (0.62 default; PR #22 adds a swing floor of 0.5)",
         "min_confidence"),
        ("reward-to-risk", ">= 2.0 (Aziz)", "min_reward_risk"),
        ("kind", "valuation plays are never auto-traded", "-"),
        ("noise", "none of the chosen flags, nor the ones the replay taught it to skip", "skip_noise + learned"),
        ("confirmations", "a day-trade setup must appear in 2 scans in a row", "min_confirmations"),
        ("catalyst", "optional: a news or earnings tag", "require_catalyst"),
        ("already in it", "not holding the symbol, no entry working, not stopped out today (cooldown)", "-"),
        ("late in the day", "no new day trade with fewer than 30 minutes to the close", "min_minutes_to_close"),
        ("<b>proof</b>", "the strategy's replay record: >= 30 trades at >= +0.05R (also net of the stocks' own "
         "drift), a positive held-out sample of >= 10 trades, a reality-check p-value <= 0.10 across every setup "
         "tried, and costs within a third of the pre-cost edge", "require_proven, proof_p_value"),
        ("the learned model", "in gate or size mode, and only while its own walk-forward verdict calls it usable: "
         "plays it gives under 55% are refused; shadow (the default) only logs its odds",
         "model_mode, model_min_p"),
        ("engine assessment", "session valid, PDT ok, size > 0, R:R ok, open-risk and gross-exposure ceilings",
         "risk.*, max_gross_exposure_pct"),
        ("the last look", "at the live quote: refused when the spread is over 0.10R of the risk or the price has "
         "run 0.25R past the entry; within that the limit is priced off the quote; a day-trade entry unfilled "
         "after 10 minutes is cancelled", "execution.max_spread_r, max_chase_r, entry_timeout_min"),
    ]))
    A(fig_states())

    # ---- 5 strategies ---------------------------------------------------------------------------
    A('<h2>5. The strategy catalogue</h2>')
    A('<p>A strategy is a class with a <code>key</code>, a <code>kind</code> (technical or fundamental), a '
      '<code>timeframe</code> (INTRADAY = day trade, SWING = multi-day), a <code>style</code> (momentum trades '
      'with the move, reversal fades it, value ignores price action) and a time-of-day profile that scales '
      'confidence by the session hour. Each is switched on and weighted in the Strategies panel; the weight '
      'multiplies the rank score, never the geometry. The registry (<code>strategies/registry.py</code>) builds '
      'them from <code>config.yaml</code> plus the dashboard overrides.</p>')
    A(table(["Key", "Title", "Timeframe", "Style", "Idea in one line", "Book"], [
        ("opening_range_breakout", "Opening-Range Breakout", "day", "momentum", "break of the first N minutes' range "
         "on volume", "Aziz"),
        ("vwap_reclaim", "VWAP Reclaim / Loss", "day", "reversal", "price crosses back over the volume-weighted "
         "average price", "Aziz"),
        ("ema_pullback_trend", "Trend Pullback to Moving Average", "day", "momentum (trend)", "pullback to the "
         "9/20 EMA in a trend", "Aziz"),
        ("gap_and_go", "Gap &amp; Go", "day", "momentum", "a gapper that holds its gap and pushes on", "Aziz"),
        ("abcd_pattern", "ABCD Pattern", "day", "momentum", "the A-B-C-D continuation", "Aziz"),
        ("bull_bear_flag", "Bull / Bear Flag", "day", "momentum (open)", "a pole and a tight flag in the first "
         "hour", "Aziz"),
        ("red_to_green", "Red-to-Green (prior-day close)", "day", "reversal", "crossing yesterday's close", "Aziz"),
        ("intraday_reversal", "Top / Bottom Reversal", "day", "reversal", "an extended move, a reversal candle, "
         "entry on the next candle's new high/low", "Aziz"),
        ("sr_bounce", "Horizontal Support / Resistance", "day", "reversal", "a bounce off a clustered level",
         "Murphy, Aziz"),
        ("rsi2_mean_reversion", "RSI(2) Mean Reversion", "swing", "reversal", "a 2-period RSI extreme in a trend",
         "Chan"),
        ("bollinger_fade", "Bollinger Band Fade", "swing", "reversal", "a close outside the band, fade back to the "
         "mean", "Murphy"),
        ("atr_channel_breakout", "Keltner / ATR Channel Breakout", "swing", "momentum", "a close outside the ATR "
         "channel", "Murphy"),
        ("week52_breakout", "52-Week High/Low Momentum", "swing", "momentum", "a new 52-week high on projected "
         "volume", "Murphy"),
        ("divergence_reversal", "Oscillator Divergence at a Level", "swing", "reversal", "RSI/MACD divergence at "
         "support or resistance", "Murphy"),
        ("failure_test", "Failure Test (spring / upthrust)", "swing", "reversal", "a probe through a swing level "
         "that closes back inside; the stop goes just beyond the test", "Grimes"),
        ("trend_pullback", "Pullback After a Momentum Thrust", "swing", "momentum", "the first shallow, quiet "
         "pullback to the 20-EMA after a close outside the 2.25-ATR Keltner channel", "Grimes"),
        ("double_bottom", "Confirmed Double Bottom / Top", "swing", "momentum", "twin lows 2-7 weeks apart, taken "
         "only on the close beyond the peak between them; the stop sits inside the pattern", "Bulkowski, Murphy"),
        ("gap_reversion", "Gap reversion", "day", "reversal", "buy-on-gap: an overnight gap against the trend "
         "reverts", "Chan (Algorithmic)"),
        ("earnings_drift", "Post-earnings drift", "day", "momentum", "drift after an 8-K earnings surprise",
         "Chan (Algorithmic)"),
        ("relative_value_comps", "Relative Value vs. Peers", "swing", "value", "cheap vs the sector's median "
         "multiples", "Pignataro"),
        ("dcf_fair_value_gap", "DCF Fair-Value Gap", "swing", "value", "price far below a discounted-cash-flow "
         "value", "Pignataro"),
        ("valuation_football_field", "Football-Field Fair-Value Band", "swing", "value", "price outside the band "
         "of all methods", "Pignataro"),
        ("insider_buying", "Insider buying", "swing", "value", "clustered open-market buys by officers (Form 4)",
         "signals"),
    ]))
    A('<p class="small">The engine reports 17 technical/statistical/fundamental strategies loaded plus the '
      'insider one; valuation plays are shown but never auto-traded.</p>')
    A('<h3>What every play carries (Play fields you will see in the API and the database)</h3>')
    A(table(["Field", "Meaning"], [
        ("entry, stop, targets", "prices; the stop is the invalidation, targets are where profit is taken (the "
         "first target scales out half if there is a second)"),
        ("reward_risk", "(target - entry) / (entry - stop); Aziz wants >= 2"),
        ("confidence", "the setup's own conviction, 0-1, scaled by time of day"),
        ("probability", "the odds the play pays, calibrated against the strategy's pooled record"),
        ("score", "the rank on the board (expected R times weight plus bumps)"),
        ("noise", "list of flags such as against_trend, news_driven_move, turbulent_market, not_trending"),
        ("confirmations", "how many scans in a row showed this setup"),
        ("evidence", "a dict with the numbers behind the play: expected_r, vol_forecast, price_character, "
         "market_regime, next_earnings, a sparkline"),
        ("suggested_qty, dollar_risk, notional", "the sizing result"),
        ("status, trade_id", "the state machine of figure 6 and the trade it became"),
    ]))

    # ---- 6 data ---------------------------------------------------------------------------------
    A('<h2>6. Data: the database, the files and the configuration</h2>')
    A('<h3>The database</h3>')
    A('<p><code>persistence/db.py</code> builds one SQLAlchemy engine. With no <code>DATABASE_URL</code> in '
      '<code>.env</code> it tries the MySQL URL built from the <code>DB_*</code> settings and, when that is not '
      'reachable, falls back to <b>SQLite at <code>data/autotradebot.sqlite</code></b> - which is what runs today. '
      '<code>create_all()</code> creates the tables and <code>_add_missing_columns()</code> adds any column the '
      'ORM has and the table lacks, so a new field never needs a manual migration. Every '
      '<code>Repository</code> method opens its own short session (<code>session_scope()</code>) and returns '
      'plain dicts, so the rest of the app never touches ORM objects.</p>')
    A(fig_er())
    A(table(["Table", "One row is", "Written by"], [
        ("scan_runs", "one scan (full, gappers, cycle, fast, plays) with its timings and counts",
         "engine._run_scan -> repo.record_scan"),
        ("play_logs", "one play shown on the board, with its decision (approved, rejected, expired, auto)",
         "repo.record_play / set_play_status"),
        ("trades", "one position from entry to exit, including partial exits (banked_pl), the R multiple, the "
         "play's features at the decision (entry_context) and what the fills cost: decision_price, spread_bps, "
         "entry_slippage_bps, exit_decision_price, exit_slippage_bps",
         "repo.open_trade / reduce_trade / close_trade"),
        ("fills", "each broker fill, entry or exit leg, with commission", "executor via the repository"),
        ("account_snapshots", "equity, cash, buying power every snapshot-loop pass", "engine snapshot loop"),
        ("order_audit", "every order request and the broker's answer (JSON)", "executor._audit"),
        ("daily_reviews", "the 16:15 review per session (JSON) plus its headline numbers", "JournalOps"),
        ("news_items", "a headline from IBKR, SEC 8-K or Finnhub, with FinBERT sentiment", "SignalStore"),
        ("insider_trades / filings_read", "Form 4 open-market trades and which filings were parsed", "SignalStore"),
        ("pair_trades", "a two-leg pair position with its z-score model and both trade ids", "PairDesk"),
        ("sim_trades", "one simulated trade of a replay run, with the play's features at the signal, a held_out "
         "flag, and what the stock's own drift made (drift_r) and costs took (cost_r) while it was held",
         "the runner's sink -> repo.save_sim_trades"),
        ("shadow_trades", "one play shown and not taken, followed to its outcome on the session's candles, with "
         "its features", "the 16:15 review -> repo.save_shadow_trades"),
    ]))
    A('<p>Three of the columns above are the learning storage: <code>trades.entry_context</code> holds the '
      'play\'s features at the fill (with <code>submitted_at</code> and <code>mfe_at</code>), and the two '
      'shaded tables hold the plays not taken and the replay\'s trades with the same features. '
      '<code>research/features.py</code> is the one function that produces them, versioned by '
      '<code>FEATURE_SCHEMA</code>; <code>research/dataset.py</code> joins the three into one training table and '
      '<code>scripts/export_training_set.py</code> writes it as CSV. The learning guide '
      '(<code>docs/AutoTradeBot-learning.pdf</code>) explains what is done with it.</p>')
    A('<h3>Files under data/ (git-ignored, all rebuildable)</h3>')
    A(table(["Path", "Format", "Holds"], [
        ("bars/&lt;SYMBOL&gt;.pkl", "pickled DataFrame", "one year of daily candles per stock (5,500+ files)"),
        ("research/daily/&lt;SYMBOL&gt;.pkl", "pickled DataFrame", "three years of daily candles (replay.daily_years) "
         "for the stocks the replay runs on; one request per stock the first time, then topped up from bars/"),
        ("research/models/", "joblib + current.json", "the trained meta-label models and the latest model's card"),
        ("cache/", "pickles + JSON", "5-minute candle windows, the Nasdaq listing, SEC financials (7 days)"),
        ("symbols.json", "JSON", "IBKR contract ids, stock types, sectors (the SymbolMaster)"),
        ("watchlists/watchlist_&lt;date&gt;.json", "JSON", "the day's hot list, buffers and decisions (5 days kept)"),
        ("runtime.json", "JSON", "every dashboard choice: mode, filters, strategies, capital, scan settings, "
         "Autopilot settings - wins over config.yaml"),
        ("day_state.bin", "compressed pickle", "the current session so far: the board's plays, the setups settled, "
         "the gap check's pre-market levels, the last wide scan's time, the last scans' summaries - a restart "
         "picks the day up from it (engine/day_state.py)"),
        ("paper_state.json", "JSON", "the simulator's cash, positions and round trips"),
        ("research/replay.json, replay_runs.jsonl", "JSON", "the latest replay's records and every run's summary"),
        ("research/training_set.csv", "CSV", "the exported training set (scripts/export_training_set.py)"),
        ("research/intraday/", "pickles", "5-minute history kept for the replay"),
        ("research/benchmark_spy.pkl", "pickle", "SPY daily returns and the fitted regime model"),
        ("journal/&lt;date&gt;.json", "JSON", "the daily review with the movers report"),
        ("signals/state.json, earnings_calendar.json, earnings/", "JSON", "signal service state and earnings dates"),
        ("pairs/watch.json", "JSON", "the cointegrated pairs being watched"),
    ]))
    A('<h3>Configuration precedence</h3>')
    A('<ol><li><code>.env</code> (secrets: IBKR host/ports/client id/account, Finnhub key, DATABASE_URL). '
      'Written only through the Connections panel by <code>secrets_store.py</code>, an allow-listed writer.</li>'
      '<li><code>config/config.yaml</code>: defaults for every section (<code>config.py</code> AppConfig: account, '
      'risk, scanner, strategies, valuation, execution, exit_manager, autopilot, noise, replay, journal, pairs, '
      'signals, database, app).</li>'
      '<li><code>data/runtime.json</code>: what you set in the dashboard. Loaded at start and applied over the '
      'YAML, so a slider you moved survives a restart and beats the file.</li></ol>')

    # ---- 7 research -----------------------------------------------------------------------------
    A('<h2>7. The research loop: replay, records, journal</h2>')
    A(fig_research())
    A('<p><b>Replay</b> (<code>research/replay.py</code>). A backtest that walks candles bar by bar and lets the '
      'same strategy code propose plays as if it were live: day trades on 5-minute candles over the last 60 '
      'sessions, swings on three years of daily candles (700 sessions; each setup still sees the 300 it would see '
      'live), fills at the next bar or next open, 5 bps slippage '
      'and 1 bp commission each way, the same exit rules as the exit manager (including scale-out), news flags '
      'from the stored headlines, and the market regime as it was known that day. The latest third of sessions '
      'is <b>held out</b>: a strategy must also work on data it was not tuned on.</p>')
    A('<p><b>Runner</b> (<code>research/runner.py</code>). Splits the work into jobs (one stock, or ten sessions '
      'of one stock for day trades), runs them on a <code>ProcessPoolExecutor</code> with every core but two, '
      'and merges the simulated trades into per-strategy records: trade count, win rate, expectancy in R, '
      'profit factor, worst drawdown, and the same block for the held-out sample. It also learns which noise '
      'checks are worth skipping (<code>learned_skips</code>).</p>')
    A('<p><b>Proof rule.</b> Autopilot, with <code>require_proven</code> on, refuses a strategy whose record '
      'does not show at least 30 trades averaging +0.05R with a positive held-out sample of at least 10. That is '
      'why no swing trade was taken on 16 September: the records were negative or too thin. In <b>Live</b> the '
      'rule is in force whatever the setting says (<code>AutoPilot.proof_required</code>); on paper it is the '
      'user\'s choice, so unproven setups can be practised - at a quarter of the usual risk '
      '(<code>research_ops.PRACTICE_RISK</code>), with the settings in force kept on every trade.</p>')
    A('<p><b>Evidence weights</b> (<code>research/weights.py</code>). Each strategy gets a multiplier between '
      '0.5 and 1.5 from its pooled record (live trades count double), applied to the rank score. '
      '<code>pooled_odds()</code> blends a play\'s own stated odds with the record so the probability shown is '
      'calibrated, and <code>quant/sizing.py half_kelly_risk_pct()</code> can only lower the risk per trade.</p>')
    A('<p><b>Journal</b> (<code>research/journal.py</code>, <code>movers.py</code>). At 16:15 the journal loop '
      'builds the day\'s review: every trade with its mistakes (entered against a flag, exited early, oversized), '
      'the shadow outcomes of plays that were shown but not taken, per-strategy tables, and the session\'s '
      'biggest movers with why they moved and whether the bot traded, offered or missed them. Saved to '
      '<code>daily_reviews</code> and <code>data/journal/</code>, shown on the Reports page.</p>')
    A('<p><b>The learning storage.</b> Everything above judges strategies as a whole. To judge one play at a '
      'time, the app keeps what every play looked like at the decision next to what happened to it, for three '
      'populations: the trades taken (<code>trades.entry_context</code>), the plays shown and not taken '
      '(<code>shadow_trades</code>, from the review) and the replay\'s simulated trades (<code>sim_trades</code>, '
      'from a sink on the runner). One function, <code>research/features.py play_features()</code>, describes a '
      'play with a fixed, versioned set of keys, and <code>research/dataset.py</code> joins the three. The '
      'companion guide <code>docs/AutoTradeBot-learning.pdf</code> covers the model to be trained on it, its '
      'validation and how it plugs into the gates.</p>')
    A('<p><b>Market regime</b> (<code>engine/market_regime.py</code>, <code>quant/regime.py</code>). A two-state '
      'Markov switching model fitted by EM on three years of SPY daily returns gives the probability the market is '
      'in its turbulent state today. It feeds the <code>turbulent_market</code> noise flag and the header pill.</p>')

    # ---- 8 dashboard + API -----------------------------------------------------------------------
    A('<h2>8. The dashboard and the API</h2>')
    A('<p>The browser loads <code>index.html</code>, which imports <code>js/main.js</code>. Each panel is a module '
      'with an <code>init()</code>; <code>state.js</code> holds the shared state and a tiny pub/sub '
      '(<code>on(topic)</code>, <code>emit(topic)</code>); <code>events.js</code> opens the WebSocket, and every '
      'event either patches the state or triggers a fetch. Nothing is computed in the browser that the engine '
      'already knows: the engine\'s <code>snapshot()</code> is the single source of truth.</p>')
    A(table(["Module", "Panel / job"], [
        ("topbar.js, ui.js, tooltips.js", "the header pills (connection, market session, regime, equity), the drawer "
         "and tabs, hover help"),
        ("plays.js, chart.js, sheet.js", "the board of plays, the detail panel with the candle chart and exit routes"),
        ("autopilot.js", "Autopilot on/off, dry run, types, the confidence / R:R / loss-stop sliders"),
        ("filters.js, strategies.js, settings.js", "the filter bar (sides, timeframes, sectors), the strategy "
         "catalogue with weights in plain words, the scan schedule"),
        ("blotter.js, orders.js, notes.js", "open and closed trades, untracked shares, working orders, "
         "Autopilot notes"),
        ("scan.js, watchlist.js", "the scan status bar and the hot list / buffers / decisions"),
        ("reports.js, journal.js, movers.js", "the Reports page: reviews, movers, strategy tables"),
        ("signals.js, pairs.js, connections.js, quit.js", "the Signals page, the pairs desk, the Connections "
         "drawer (routing, .env), the quit dialog"),
    ]))
    A('<h3>REST routes (server/app.py)</h3>')
    A(table(["Route", "Method", "Engine call"], [
        ("/api/state", "GET", "snapshot(): account, positions, autopilot, filters, market, scan, pnl, venue ..."),
        ("/api/plays, /api/plays/{id}/assess | approve | reject | chart", "GET/POST",
         "current_plays(), assess_play(), approve_play(), reject_play(), play_chart()"),
        ("/api/trades, /api/trades/{id}/close | managed | record, /api/trades/close-all", "GET/POST",
         "trade lists, close_position(), set_trade_managed(), trade_record(), close_all_positions()"),
        ("/api/positions/untracked/{symbol}/close", "POST", "close_untracked()"),
        ("/api/orders, /api/pnl, /api/account/refresh", "GET/POST", "active_orders(), pnl, refresh_account_now()"),
        ("/api/autopilot, /api/filters, /api/strategies[/{key}|/reset]", "GET/POST",
         "set_autopilot(), set_filters(), set_strategy(), reset_strategies()"),
        ("/api/scan, /api/settings, /api/watchlist", "GET/POST", "request_scan(kind), set_scan_settings(), "
         "watchlist_state()"),
        ("/api/replay[/history], /api/journal[/{session}|/review], /api/signals[...]", "GET/POST",
         "start_replay(), replay_state(), journal_review(), review_session(), signals_state(), check_signals()"),
        ("/api/pairs, /api/pairs/enter, /api/pairs/trades/{id}/close, /api/pairs/chart", "GET/POST",
         "pairs_state(), enter_pair(), close_pair(), pair_chart()"),
        ("/api/mode, /api/paper/reset, /api/capital[/split]", "POST", "set_mode(), reset_paper(), set_capital(), "
         "set_capital_split()"),
        ("/api/setup[/secrets|/reconnect|/ibkr/test|/paper-platform], /api/quit", "GET/POST",
         "same-machine only: setup_state(), save_secrets(), reconnect(), probe_ibkr(), begin_quit()"),
        ("/ws", "WebSocket", "a queue on the EventBus; first message is a full snapshot"),
    ]))
    A('<h3>Events on the bus (core/eventbus.py)</h3>')
    A('<p><code>engine.started</code>, <code>account.snapshot</code>, <code>plays.updated</code>, '
      '<code>plays.changes</code>, <code>play.decided</code>, <code>scan.started</code>, <code>scan.failed</code>, '
      '<code>watchlist.updated</code>, <code>orders.updated</code>, <code>trade.closed</code>, '
      '<code>trades.removed</code>, <code>exit.triggered</code>, <code>exit.scaled</code>, '
      '<code>exit.stop_moved</code>, <code>exit.failed</code>, <code>exit.not_held</code>, '
      '<code>autopilot.entered</code>, <code>autopilot.would_enter</code>, <code>autopilot.skipped</code>, '
      '<code>autopilot.blocked</code>, <code>autopilot.daily_loss</code>, <code>autopilot.config</code>, '
      '<code>broker.connected</code> / <code>disconnected</code> / <code>reconnected</code> / <code>down</code> / '
      '<code>switched</code>, <code>positions.mismatch</code>, <code>position.earnings_ahead</code>, '
      '<code>journal.updated</code>, <code>pairs.updated</code>, <code>capital.updated</code>, '
      '<code>filters.updated</code>, <code>strategies.updated</code>, <code>settings.updated</code>, '
      '<code>quit.requested</code> / <code>started</code> / <code>progress</code> / <code>done</code>, '
      '<code>engine.disarmed</code>.</p>')

    # ---- 9 the important functions ---------------------------------------------------------------
    A('<h2>9. The functions that matter most</h2>')
    A('<p>If you read only thirty things in the code, read these. Paths are relative to <code>tos_bot/</code>.</p>')
    fns = [
        ("engine/engine.py", "TradingEngine.start()", "binds the broker, reads the account, launches the seven loops"),
        ("engine/engine.py", "_due_scan()", "decides which scan is due (full / gappers / cycle / fast / plays)"),
        ("engine/engine.py", "_run_scan(kind)", "runs it, records the scan_runs row, replaces the board, notes changes, "
         "hands the board to Autopilot"),
        ("engine/engine.py", "snapshot()", "the dict the dashboard renders from; everything else is derived from it"),
        ("engine/engine.py", "assess_play(id)", "session validity, PDT, sizing, arm state, R:R - the yes/no before "
         "any order"),
        ("engine/engine.py", "approve_play(id)", "executes a play and books the trade"),
        ("engine/engine.py", "_reconcile_open_trades()", "compares records with the broker's positions; settles a "
         "gone position from its fills (_settle_gone, _exit_fill); reports size mismatches"),
        ("engine/engine.py", "_refresh_account()", "reads the account; keeps the last snapshot when IBKR is slow (a "
         "slow answer never reads as an empty account)"),
        ("scanner/scanner.py", "Scanner.run_full() / run_cycle() / run_gappers()", "the three scans (section 3)"),
        ("scanner/heat.py", "daily_metrics(), rank_by_daily_heat(), intraday_metrics(), rank_gappers()",
         "how a stock earns attention: relative volume, move in ATRs, distance to the 20-day extreme, range, "
         "dollar volume"),
        ("scanner/watchlist.py", "DayWatchlist.apply_cycle()", "adopt a buffer name into the hot list, keep it "
         "waiting, or drop it"),
        ("scanner/evaluator.py", "evaluate()", "context, strategies, noise flags, readings, score, signal nudges"),
        ("strategies/base.py", "StrategyContext", "memoised indicators shared by all setups on one stock"),
        ("strategies/base.py", "Strategy._mk_play()", "builds a Play with the geometry floors and the "
         "Douglas-style explanation"),
        ("strategies/base.py", "calibrated_probability()", "blends the setup's odds with the pooled record"),
        ("scanner/noise.py", "context_flags(), quant_flags(), event_flags()", "the noise checks from the books"),
        ("scanner/filters.py", "expected_r(), rank_score()", "a play's value per unit of risk, and its rank"),
        ("risk/position_sizing.py", "size_play()", "shares from the risk budget, with every cap and its label"),
        ("execution/autopilot.py", "AutoPilot.consider(), _pre_gate(), proof_missing(), daily_loss_reason()",
         "the gates of section 4"),
        ("execution/executor.py", "execute_play(), close_trade(), sync_open_orders(), adopt_working_orders()",
         "orders in and out, fills booked, orders already at the broker followed after a restart"),
        ("execution/exit_manager.py", "ExitManager.run_once() -> _manage()", "stop, target, scale-out, EOD flatten, "
         "time stop, break-even, trailing (figure 5)"),
        ("execution/protective_stops.py", "_protect_positions(), _stand_down(), _watch_stops(), _book_target_fill()",
         "the stop and the target resting at IBKR for each open trade, in one one-cancels-all group: placed, moved with "
         "the record, rebuilt after the scale-out, stood down before the app's own exits, booked when they fill, swept "
         "when their trade is gone"),
        ("execution/order_builder.py", "plan_order(), build_entry_order()", "which order type and session a play "
         "may use; the limit offset"),
        ("brokers/ibkr_adapter.py", "IbkrBroker (get_account, history_many, place_bracket, get_fills, "
         "news_headlines)", "everything IBKR, on one asyncio loop thread with timeouts"),
        ("brokers/paper_adapter.py", "PaperBroker.place_order(), poll()", "the simulator's fills on real prices"),
        ("engine/day_state.py", "DayStateOps._save_day(), _restore_day()", "saves the board and the scans' state as "
         "the day goes; start() picks up the current session's, never a price"),
        ("engine/reconcile.py", "PositionCheck.gone(), share_counts()", "when a missing position counts as gone "
         "(fresh account, settled connection, grace, two misses)"),
        ("persistence/repository.py", "open_trade(), reduce_trade(), close_trade(), trade_record(), pnl_summary()",
         "the trade ledger and its R multiples"),
        ("research/replay.py", "replay_intraday(), replay_swing(), summarize(), learned_skips()", "the backtest "
         "and its records"),
        ("research/runner.py", "ReplayRunner.start(), replay_job(), session_chunks()", "the parallel replay"),
        ("research/weights.py", "evidence_multiplier(), pooled_odds()", "how records change scores and odds"),
        ("research/features.py", "play_features()", "a play as one flat, versioned row of features - the same "
         "for live, shadow and replayed plays"),
        ("research/dataset.py", "training_rows(), write_csv()", "the training set joined from the three "
         "populations"),
        ("persistence/repository.py", "save_sim_trades(), save_shadow_trades()", "the learning tables' writers"),
        ("research/journal.py", "build_review(), find_mistakes(), shadow_outcomes(), lessons()", "the daily review"),
        ("pairs/desk.py", "PairDesk.refresh(), watch(), enter(), manage(), close()", "the pairs desk: both legs, "
         "the z-score bands, the time stop"),
        ("quant/*", "adf(), hurst(), half_life(), fit_garch11(), fit_markov_switching(), johansen(), best_band()",
         "the books' models in NumPy"),
        ("util/clock.py", "market_status(), current_session(), time_of_day(), next_trading_day()", "the NYSE "
         "calendar and sessions everything is timed by"),
        ("server/app.py", "create_app()", "routes to engine methods; the WebSocket fan-out"),
    ]
    A(table(["File", "Function(s)", "What it does"], [(c(f), c(n), esc(d)) for f, n, d in fns]))

    # ---- 10 glossary --------------------------------------------------------------------------
    A('<h2>10. Trading terms, explained for a computer scientist</h2>')
    A(table(["Term", "Meaning in this app"], [
        ("Candle / bar (OHLCV)", "one time bucket of prices: open, high, low, close and volume. Daily candles for "
         "swings, 5-minute candles for day trades. Stored as pandas DataFrames indexed by New York time."),
        ("Long / short", "long = buy first, profit if the price rises; short = sell borrowed shares first, profit "
         "if it falls. Side.sign is +1 / -1 and every formula multiplies by it."),
        ("Entry, stop, target", "entry: the price the plan wants; stop: the price at which the idea is wrong and the "
         "position is closed; target: where profit is taken. The stop is a sunk-cost guard, not a prediction."),
        ("R (risk unit), R multiple", "R = |entry - stop| per share, the amount risked. A trade that gains 2R made "
         "twice what it risked. Expectancy in R is the average R per trade, the one number that compares "
         "strategies."),
        ("Reward-to-risk (R:R)", "(target - entry) / (entry - stop). Aziz requires >= 2; the ranking uses expected "
         "value instead so a tightened stop cannot game it."),
        ("Position sizing, the 2% rule", "shares = (equity x risk%) / R. Risk 1% per trade here (Aziz allows 2%), "
         "at most 4% open across positions."),
        ("Half-Kelly", "the Kelly criterion gives the bet fraction that maximises log growth from win rate and "
         "payoff; half of it is the conventional safe version. It can only lower the configured risk %."),
        ("Slippage, commission, bps", "the cost of a fill vs the quoted price, and the broker's fee. 1 bp = 0.01%. "
         "The replay charges 5 bps slippage and 1 bp commission on every fill."),
        ("Bracket order", "an entry order with a stop order and a target order attached; the broker cancels one "
         "when the other fills. IBKR supports it natively."),
        ("Day trade vs swing", "a day trade opens and closes the same session (and counts against the "
         "pattern-day-trader rule in a small live account); a swing is held days."),
        ("PDT rule", "US rule: under $25k equity, at most 3 day trades in 5 sessions. risk/pdt_guard.py enforces "
         "it in live mode."),
        ("VWAP", "volume-weighted average price since the open: the day's fair price for institutions. Being above "
         "it is bullish context."),
        ("ATR", "average true range: the typical candle range over 14 bars. Used as a volatility yardstick for "
         "stops and floors."),
        ("EMA, RSI, MACD, Bollinger, Keltner, ADX", "standard indicators in indicators/ta.py: smoothed price, "
         "momentum oscillators, volatility bands, a trend-strength measure."),
        ("Support / resistance", "price levels where reversals clustered before (analysis/levels.py clusters "
         "swing highs and lows)."),
        ("Gap, gapper, RVOL", "a gap is today's open far from yesterday's close; a gapper is a stock gapping "
         "pre-market on volume; RVOL is today's volume relative to the average - attention."),
        ("Mean reversion vs momentum", "two regimes of price behaviour. ADF / Hurst / variance-ratio tests "
         "(quant/stationarity.py) say which one a series is in; reversal setups are refused on a trending series "
         "and vice versa."),
        ("Half-life", "for a mean-reverting series, the time for a deviation to halve (from an AR(1) fit). Sets the "
         "expected hold of a reversal trade."),
        ("GARCH(1,1), EWMA", "volatility models where today's variance depends on yesterday's shock and variance. "
         "The forecast sets a stop floor while volatility is rising."),
        ("Regime (Markov switching)", "a hidden two-state model (calm / turbulent) fitted to SPY returns; the "
         "filtered probability of the turbulent state is the market pill."),
        ("Cointegration, pairs, z-score", "two stocks whose price spread is stationary. The spread's z-score says "
         "how stretched it is; enter at the band, exit at the mean, stop past the stop band. Engle-Granger and "
         "Johansen are the tests."),
        ("Held-out (out of sample)", "the latest third of the replay's sessions, judged separately: a setup must "
         "work on data it was not chosen on."),
        ("Expectancy, profit factor, drawdown", "average R per trade; gross wins / gross losses; the worst peak-"
         "to-trough run of losses."),
        ("Paper vs live", "paper = a practice account with real prices and no money (IBKR's, or the built-in "
         "simulator). Live = real money on port 4001."),
        ("Scale-out", "sell half at the first target and move the stop to break-even; let the rest run to the "
         "second target."),
        ("MFE / MAE", "maximum favourable / adverse excursion: the best and worst the trade went while open."),
        ("Form 4, 8-K, EDGAR", "SEC filings: insider trades (Form 4), material events including earnings (8-K "
         "item 2.02), and EDGAR is the SEC's free API for them."),
    ]))
    A('<p class="small">Generated from the code as of 16 September 2026 (master plus the learning storage). '
      'When the code changes, the README and this guide should be updated together.</p>')
    return "".join(parts)


if __name__ == "__main__":
    OUT.write_text(build(), encoding="utf-8")
    print("wrote", OUT, OUT.stat().st_size, "bytes")
