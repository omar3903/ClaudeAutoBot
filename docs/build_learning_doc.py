"""Build the learning guide: how AutoTradeBot is meant to learn from its own trades - an HTML
page with inline-SVG figures, rendered to PDF by headless Edge (see docs/README.md). Shares the
SVG toolkit and the page style with build_architecture_doc.py."""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from build_architecture_doc import CSS, Svg, c, connect, esc, table  # noqa: E402

OUT = pathlib.Path(__file__).with_name("AutoTradeBot-learning.html")
ROOT = pathlib.Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT))
try:
    from tos_bot.research.features import FEATURE_KEYS, FEATURE_SCHEMA  # noqa: E402
except Exception:  # noqa: BLE001 - the guide still builds without the package
    FEATURE_KEYS, FEATURE_SCHEMA = (), 1


# ----------------------------------------------------------------------------------------------
#  Figure 1 - where the rows come from
# ----------------------------------------------------------------------------------------------
def fig_pipeline() -> str:
    s = Svg(720, 470)
    live = s.box(20, 20, 210, 80, "The app trading", ["scanner -> plays -> Autopilot / you", "-> Executor "
                                                      "-> a fill", "features taken at the decision"],
                 lsize=8.5, fill="#e8f5e9")
    rev = s.box(255, 20, 210, 80, "The 16:15 review", ["research/journal.py", "every day-trade play shown and not "
                                                       "taken,", "followed on the session's candles"], lsize=8.5,
                fill="#fff8e1")
    rep = s.box(490, 20, 210, 80, "The replay", ["research/replay.py + runner.py", "60 day sessions, 250 swing "
                                                 "sessions,", "costs charged, latest third held out"], lsize=8.5,
                fill="#f3f0ff")
    t1 = s.box(20, 150, 210, 74, "trades.entry_context", ["+ submitted_at, mfe_at", "one JSON row of features per "
                                                          "trade,", "outcome: r_multiple, mfe, exit_reason"],
               lsize=8.5)
    t2 = s.box(255, 150, 210, 74, "shadow_trades", ["one row per play not taken", "filled? r, mfe_r, exit_reason",
                                                    "features at the sighting"], lsize=8.5)
    t3 = s.box(490, 150, 210, 74, "sim_trades", ["one row per simulated trade, per run", "r, mfe_r, exit_reason, "
                                                 "held_out", "features at the signal"], lsize=8.5)
    connect(s, live, t1, "at the fill")
    connect(s, rev, t2, "after the close")
    connect(s, rep, t3, "when a run ends (sink)")
    feat = s.box(180, 260, 360, 56, "research/features.py  play_features()",
                 [f"one function, one schema (FEATURE_SCHEMA = {FEATURE_SCHEMA}), {len(FEATURE_KEYS)} keys, "
                  "always all present", "the same code path for the three sources - no drift between them"],
                 lsize=8.5, fill="#eef4ff")
    s.line(125, 224, 125, 288, dash="4,3")
    s.line(125, 288, 180, 288, end="arr", dash="4,3")
    s.line(360, 224, 360, 260, end="arr", dash="4,3")
    s.line(595, 224, 595, 288, dash="4,3")
    s.line(595, 288, 540, 288, end="arr", dash="4,3")
    ds = s.box(180, 350, 360, 56, "research/dataset.py  ->  data/research/training_set.csv",
               ["scripts/export_training_set.py joins the three with a source column", "columns: source, ids, "
                "outcome (r, win, mfe_r), held_out, then the features"], lsize=8.5, fill="#eef4ff")
    connect(s, feat, ds, "training_rows()")
    s.text(360, 440, "The app keeps running throughout: collection is passive, and the export reads the database "
                     "without stopping anything.", 9, "middle", italic=True, fill="#444")
    return s.render("Figure 1 - Where the training rows come from: the trades taken, the plays not taken, and "
                    "the replay, all described by the same feature function.")


# ----------------------------------------------------------------------------------------------
#  Figure 2 - purged walk-forward validation
# ----------------------------------------------------------------------------------------------
def fig_validation() -> str:
    s = Svg(720, 330)
    x0, w = 60, 620
    s.text(x0, 24, "sessions, oldest to newest  ->", 9, fill="#444")
    s.line(x0, 30, x0 + w, 30, end="arr")
    folds = 4
    seg = w / (folds + 1)
    for k in range(folds):
        y = 50 + k * 52
        s.text(20, y + 22, f"fold {k + 1}", 9, weight="bold")
        train_w = seg * (k + 1)
        s.rect(x0, y, train_w, 30, "#dbe7fb", "#333", rx=2)
        s.text(x0 + train_w / 2, y + 19, "train", 9, "middle")
        gap = seg * 0.18
        s.rect(x0 + train_w, y, gap, 30, "#fdecea", "#333", rx=0)
        s.text(x0 + train_w + gap / 2, y + 19, "purge", 7, "middle")
        s.rect(x0 + train_w + gap, y, seg - gap, 30, "#e8f5e9", "#333", rx=2)
        s.text(x0 + train_w + gap + (seg - gap) / 2, y + 19, "test", 9, "middle")
    s.rect(x0, 262, w, 46, "#fffbe6", "#c9b26b", rx=3)
    s.text(x0 + 8, 278, "Purge: rows whose trade was still open when the test window starts are dropped from "
                        "training, so nothing the model sees overlaps in time with what it is judged on.", 8.5)
    s.text(x0 + 8, 292, "Embargo (not drawn): a further few sessions after each test window are kept out of "
                        "the next fold's training, against slow-moving leakage.", 8.5)
    s.text(x0 + 8, 304, "Score = the average over folds. Never a random split: rows from one session are not "
                        "independent.", 8.5)
    return s.render("Figure 2 - Purged walk-forward validation: each fold trains on the past only, skips a purge "
                    "gap, and is judged on the sessions after it (Lopez de Prado, ch. 7).")


# ----------------------------------------------------------------------------------------------
#  Figure 3 - how the model plugs into the app
# ----------------------------------------------------------------------------------------------
def fig_integration() -> str:
    s = Svg(720, 360)
    play = s.box(20, 30, 170, 70, "A play on the board", ["from a strategy, with its", "evidence and noise flags",
                                                          "confidence: the setup's own"], lsize=8.5)
    feat = s.box(230, 30, 170, 70, "play_features()", ["the same row the training", "set was built from",
                                                       "(schema checked)"], lsize=8.5, fill="#eef4ff")
    model = s.box(440, 30, 260, 70, "The model  (research/model.py, later)", [
        "P(reaches its target before its stop | features)", "calibrated; per strategy where there are rows,",
        "pooled otherwise; a version id on every answer"], lsize=8.5, fill="#f3f0ff")
    connect(s, play, feat)
    connect(s, feat, model)
    prob = s.box(440, 140, 260, 50, "probability on the play", ["replaces calibrated_probability()'s blend",
                                                                "shown with its top three reasons"], lsize=8.5)
    connect(s, model, prob, "P, reasons")
    gate = s.box(20, 230, 200, 70, "Autopilot gate", ["P >= the floor for its timeframe", "(instead of the flat "
                                                      "confidence);", "proof rule stays"], lsize=8.5, fill="#fff3e0")
    size = s.box(260, 230, 200, 70, "Sizing", ["half-Kelly from P and the payoff;", "can only lower the "
                                              "configured risk;", "the caps stay"], lsize=8.5, fill="#fff3e0")
    never = s.box(500, 230, 200, 70, "Never touched by the model", ["stops, targets, exits,", "the daily loss stop, "
                                                                     "the PDT rule,", "the position caps"],
                  lsize=8.5, fill="#fdecea")
    s.path("M570,190 L570,210 L120,210 L120,230", end="arr")
    s.path("M570,190 L570,210 L360,210 L360,230", end="arr")
    s.text(360, 335, "First two weeks in shadow: the model's verdict is logged next to Autopilot's ('would enter' "
                     "/ 'would skip') and nothing changes until the two are compared.", 8.5, "middle", italic=True,
           fill="#444")
    return s.render("Figure 3 - Where the model's probability goes: the gate and the size, never the exits.")


# ----------------------------------------------------------------------------------------------
#  Figure 4 - the timeline
# ----------------------------------------------------------------------------------------------
def fig_timeline() -> str:
    s = Svg(720, 300)
    weeks = 7
    x0, w = 150, 550
    seg = w / weeks
    for k in range(weeks + 1):
        x = x0 + k * seg
        s.line(x, 30, x, 250, stroke="#ddd")
        if k < weeks:
            s.text(x + seg / 2, 24, f"week {k + 1}", 8.5, "middle", fill="#444")
    rows = [
        ("storage PR, restart", 0, 0.3, "#dbe7fb"),
        ("first full-universe replay", 0.3, 0.6, "#f3f0ff"),
        ("validation harness PR", 0.5, 1.5, "#dbe7fb"),
        ("first model on replay rows", 1.2, 2.2, "#f3f0ff"),
        ("paper trading, rows accumulate", 0.3, 7, "#e8f5e9"),
        ("shadow mode: model vs Autopilot", 3, 5, "#fff3e0"),
        ("decision: gate on P, or not", 5, 5.4, "#fdecea"),
        ("retrain weekly, watch drift", 5.4, 7, "#f3f0ff"),
    ]
    for i, (label, a, b, fill) in enumerate(rows):
        y = 40 + i * 26
        s.text(x0 - 8, y + 14, label, 8.5, "end")
        s.rect(x0 + a * seg, y + 2, (b - a) * seg, 18, fill, "#333", rx=3)
    s.text(360, 285, "The long pole is the paper trading: real fills are the test Chan asks for, and fifty to a "
                     "hundred of them take weeks.", 8.5, "middle", italic=True, fill="#444")
    return s.render("Figure 4 - The timeline: about a week to a first model judged on replay rows, four to six "
                    "weeks to one that includes real paper fills.")


# ----------------------------------------------------------------------------------------------
#  The document
# ----------------------------------------------------------------------------------------------
FEATURE_NOTES = {
    "schema": ("the layout version", "features.py"),
    "strategy": ("which setup proposed the play", "the play"),
    "kind": ("TECHNICAL or FUNDAMENTAL", "the play"),
    "timeframe": ("INTRADAY (day trade) or SWING", "the play"),
    "side": ("LONG or SHORT", "the play"),
    "sector": ("the stock's sector (IBKR contract details)", "the play"),
    "confidence": ("the setup's own conviction, 0-1, scaled by time of day", "the strategy"),
    "probability": ("the odds the play stated (calibrated against the record)", "strategies/base.py"),
    "reward_risk": ("(target - entry) / (entry - stop)", "the play"),
    "score": ("the board rank: expected R x weight + bumps", "scanner/filters.py"),
    "expected_r": ("odds x reward - (1 - odds), before costs", "scanner/filters.py"),
    "evidence_weight": ("the strategy's multiplier from its record, 0.5-1.5", "research/weights.py"),
    "stop_pct": ("distance to the stop, % of entry", "geometry"),
    "target_pct": ("distance to the first target, % of entry", "geometry"),
    "n_targets": ("1 or 2 (2 = scale-out at the first)", "geometry"),
    "noise": ("the flags raised: against_trend, news_driven_move, turbulent_market ...", "scanner/noise.py"),
    "n_noise": ("how many flags", "scanner/noise.py"),
    "confirmations": ("scans in a row the setup showed", "the board"),
    "tags": ("gap, catalyst, earnings ...", "the strategy"),
    "has_catalyst": ("a gap or catalyst tag", "the strategy"),
    "minutes_since_open": ("when in the session the play was seen", "util/clock.py"),
    "time_of_day": ("OPEN, LATE_MORNING, MIDDAY, CLOSE, OFF (Aziz's sessions)", "util/clock.py"),
    "weekday": ("0 = Monday", "the clock"),
    "p_turbulent": ("probability the market is in its turbulent state", "engine/market_regime.py"),
    "regime": ("calm or turbulent", "engine/market_regime.py"),
    "rvol": ("volume vs the usual at this time", "scanner/heat.py"),
    "gap_pct": ("today's open vs yesterday's close, %", "scanner/heat.py"),
    "atr_pct": ("the daily average true range, % of price", "scanner/heat.py"),
    "change_pct": ("today's move so far, %", "scanner/heat.py"),
    "range_atr": ("today's range in daily ATRs", "scanner/heat.py"),
    "heat": ("the attention score", "scanner/heat.py"),
    "dollar_volume": ("20-session average dollar volume (swing plays)", "scanner/heat.py"),
    "move_atr": ("last session's move in ATRs (swing plays)", "scanner/heat.py"),
    "extreme": ("0 mid-range .. 1 at a 20-session high or low (swing plays)", "scanner/heat.py"),
    "vol": ("tomorrow's forecast daily volatility (GARCH)", "quant/volatility.py"),
    "recent_vol": ("the last 60 sessions' volatility", "quant/volatility.py"),
    "vol_ratio": ("forecast / recent - above 1 means rising", "quant/volatility.py"),
    "vol_model": ("garch or ewma", "quant/volatility.py"),
    "price_character": ("trending, mean reverting or random walk", "quant/readings.py"),
    "hurst": ("the Hurst exponent", "quant/stationarity.py"),
    "variance_ratio_z": ("the variance-ratio test statistic", "quant/stationarity.py"),
    "half_life_bars": ("mean-reversion half-life, in bars", "quant/stationarity.py"),
    "market_z": ("today's move vs what the market model expected, in sigmas", "quant/market_model.py"),
    "market_beta": ("beta to SPY", "quant/market_model.py"),
    "market_move_pct": ("today's move, %", "quant/market_model.py"),
    "news_since_move": ("headlines since the move started", "signals"),
    "sessions_to_earnings": ("sessions until the next report", "signals/calendar.py"),
    "signal_nudge": ("the score nudge from insider / news signals", "signals/book.py"),
    "replay_expectancy_r": ("the strategy's replayed average R when the trade was taken", "live trades only"),
    "replay_trades": ("how many replayed trades that rests on", "live trades only"),
    "held_out_r": ("the held-out average R", "live trades only"),
    "risk_pct": ("the risk % it was sized at (half-Kelly)", "live trades only"),
    "unproven": ("whether the proof rule would have refused it", "live trades only"),
    "by": ("autopilot or operator", "live trades only"),
}


def build() -> str:
    parts = [f"<title>AutoTradeBot Learning</title><style>{CSS}</style>"]
    A = parts.append
    A('<div class="cover"><div class="t">AutoTradeBot</div>'
      '<div class="s">Learning from its own trades - the machine-learning plan, in detail</div>'
      '<div class="m">A companion to the architecture guide · September 2026<br><br>'
      'What the model answers · the data and its three populations · the features · leakage and bias · '
      'validation · the model · how it plugs in · the timeline · what to read</div></div>')

    # ---- 1 --------------------------------------------------------------------------------
    A('<h2 class="first">1. What we are trying to learn, and what we are not</h2>')
    A('<p><b>The one question.</b> The app already has twenty hand-written setups that say <i>when</i> a trade '
      'is worth proposing. What it lacks is a calibrated answer to: <i>given this play, right now, how likely is '
      'it to reach its target before it hits its stop?</i> Today that number is a flat confidence the strategy '
      'states (0.55 to 0.62 for most setups), nudged by a pooled win rate. The plan is to replace it with a '
      'probability learned from what actually happened to thousands of plays like it. In the literature this is '
      '<b>meta-labelling</b> (López de Prado, ch. 3): the primary model decides the side and the moment, the '
      'secondary model decides whether to act and how much.</p>')
    A('<p><b>Why this and not a price predictor.</b> Predicting prices from a few thousand rows is hopeless and '
      'the books you chose say so. Judging a proposed trade is a much easier problem: the label is binary, the '
      'features are the readings the setup already computes, and the base rate is around 40 to 55 percent, so '
      'even a modest lift in precision at the top of the ranking is worth money. It also keeps every decision '
      'explainable: the play panel can show the probability and the three features that moved it.</p>')
    A('<p><b>What the model never does.</b> It never moves a stop or a target, never overrides the daily loss '
      'stop, the position caps or the pattern-day-trader rule, and never sizes a trade directly. It feeds two '
      'existing inputs: the gate\'s probability floor and the half-Kelly risk fraction.</p>')
    A('<p><b>Is it worth it?</b> Only with enough rows and honest validation. Section 4 is the part that decides '
      'whether the model is used at all: if it cannot beat the hand-written confidence on purged walk-forward '
      'folds, it stays in shadow mode and the app is no worse than today.</p>')

    # ---- 2 --------------------------------------------------------------------------------
    A('<h2>2. The data: three populations, one schema</h2>')
    A(fig_pipeline())
    A('<p>Learning only from the trades the app took would teach it the gates\' preferences, not the market: '
      'Autopilot refuses most plays, so the taken ones are a biased slice. Two more populations fix that.</p>')
    A(table(["Population", "Rows", "Label", "Strength", "Weakness"], [
        ("<b>live</b>", "trades the app took, paper or live; features kept at the fill in "
         "<code>trades.entry_context</code>", "the real R multiple, MFE, exit reason",
         "real fills, real slippage, the only rows that count double in the evidence weights",
         "few - tens per month - and biased by the gates"),
        ("<b>shadow</b>", "every day-trade play shown and not taken, followed by the 16:15 review on the session's "
         "5-minute candles as if it had been taken; <code>shadow_trades</code>",
         "simulated R with the exit manager's rules and the replay's costs",
         "removes the selection bias: what the gates refused, and how it went",
         "day trades only for now; swing plays not taken need days of candles (a follow-up)"),
        ("<b>replay</b>", "every simulated trade of a replay run, features at the signal; <code>sim_trades</code>",
         "simulated R, with a <code>held_out</code> flag for the latest third of sessions",
         "thousands of rows at once, across many stocks and regimes",
         "no live readings in the replay (the volatility and character readings are None there); "
         "fills are idealised"),
    ]))
    A('<h3>How many rows to expect</h3>')
    A(table(["Source", "Today", "After the first full-universe replay", "After a month of paper trading"], [
        ("live", "0 (older trades carry no context)", "0", "50 to 150"),
        ("shadow", "0", "0", "roughly 20 to 60 per session, 400 to 1,200"),
        ("replay", "0 until the next run", "about 9,000 (37 stocks gave 9,073); the full list gives more",
         "the same, refreshed weekly"),
    ]))
    A('<p>The first model will therefore rest on replay rows, be checked against shadow rows as they arrive, '
      'and be trusted only once the live rows agree with it.</p>')

    # ---- 3 --------------------------------------------------------------------------------
    A('<h2>3. The features</h2>')
    A(f'<p>One function, <code>tos_bot/research/features.py play_features()</code>, turns a play into a flat '
      f'dict of {len(FEATURE_KEYS)} keys. The same function runs at a live fill, in the review and inside the '
      f'replay\'s worker processes, so the three sources cannot drift apart. Every key is always present '
      f'(<code>None</code> when the reading was not available), and <code>FEATURE_SCHEMA</code> (now '
      f'{FEATURE_SCHEMA}) goes up whenever a key is added or changes meaning. That is what makes the collection '
      f'independent of everything else you change in the app: a row written last month is still readable and '
      f'still labelled with the version it was written under.</p>')
    A('<p><b>The rule for a feature:</b> it must have been knowable at the moment of the decision. Nothing that '
      'depends on later candles, on the fill, or on the outcome is allowed. The exit reason and the R multiple '
      'are labels, never inputs.</p>')
    rows = [(c(k), esc(FEATURE_NOTES.get(k, ("", ""))[0]), esc(FEATURE_NOTES.get(k, ("", ""))[1]))
            for k in FEATURE_KEYS]
    A(table(["Key", "Meaning", "Comes from"], rows))
    A('<h3>The labels</h3>')
    A('<ul><li><code>r</code>: the outcome in risk units (R). +2 means the trade made twice what it risked; '
      '-1 means it hit its stop.</li>'
      '<li><code>win</code>: r &gt; 0. The classification target.</li>'
      '<li><code>mfe_r</code>: the best the trade got before it closed, in R. Useful as a softer target '
      '("did it ever reach +1R?") and for judging the exit rules separately from the entries.</li>'
      '<li><code>exit_reason</code>: stop, target, trailing-stop, eod-flatten, time-stop. Lets us ask whether '
      'a setup loses because it is wrong or because it runs out of time.</li></ul>')

    # ---- 4 --------------------------------------------------------------------------------
    A('<h2>4. Leakage, bias and honest validation</h2>')
    A('<p>This is the section that decides whether the model is used. Every trap below has caught published '
      'strategies; Aronson and López de Prado each spend half a book on them.</p>')
    A('<ol>'
      '<li><b>Look-ahead leakage.</b> A feature computed with candles the decision could not have seen. Guarded '
      'by construction: the replay builds the context bar by bar and the live path takes the features at the '
      'fill. The test suite checks the schema, but a new feature must be reviewed for this by hand.</li>'
      '<li><b>Selection bias.</b> Training only on taken trades. Guarded by the shadow rows (section 2).</li>'
      '<li><b>Overlapping labels.</b> Two rows from the same stock and session are not independent: a random '
      'train/test split leaks. Guarded by walk-forward folds with a purge gap (figure 2).</li>'
      '<li><b>Multiple testing.</b> Twenty setups, dozens of features and several models means something will '
      'look good by chance. Guarded by the shuffled-label baseline and by reporting how many configurations '
      'were tried (the deflated Sharpe idea).</li>'
      '<li><b>Regime drift.</b> A model fitted on a calm year fails in a turbulent one. Guarded by the regime '
      'feature, by folds that span both, and by monitoring live outcomes against predictions after deployment.'
      '</li></ol>')
    A(fig_validation())
    A('<h3>What gets measured, per fold</h3>')
    A(table(["Metric", "Question it answers", "Pass mark"], [
        ("log loss and Brier score", "are the probabilities honest?", "better than the hand-written confidence "
         "on the same rows"),
        ("calibration curve", "does a 0.6 mean 60%?", "within 5 points across deciles"),
        ("precision at the top decile", "if we only took the plays the model likes most, how often do they win?",
         "clearly above the base rate"),
        ("expectancy of the plays above the floor", "in R, the number that matters", "above the expectancy of "
         "the plays the current gates would take"),
        ("shuffled-label baseline", "how good does a model look on random labels?", "the real model must beat "
         "the best of 100 shuffles"),
        ("held-out replay sessions", "the same test the strategies pass", "the gain survives out of sample"),
    ]))

    # ---- 5 --------------------------------------------------------------------------------
    A('<h2>5. The model</h2>')
    A('<p><b>Start simple.</b> A logistic regression on standardised features is the first model, because it is '
      'transparent, hard to overfit on a few thousand rows, and gives a sign for every feature that a trader can '
      'argue with. If it cannot beat the hand-written confidence, nothing fancier will help; the data is the '
      'problem, not the model.</p>')
    A('<p><b>Then trees.</b> Gradient-boosted trees (scikit-learn\'s <code>HistGradientBoostingClassifier</code>; '
      'no GPU, no new heavy dependency) capture interactions such as "a Bollinger fade works at midday in a calm '
      'regime but not in the first hour". Use monotone constraints where the books are clear (more confirmations '
      'should not lower the probability), small depth, and early stopping on the walk-forward folds.</p>')
    A('<p><b>Calibrate.</b> Whatever the model, its raw scores are passed through isotonic or Platt calibration '
      'fitted on the folds, so the number shown is a probability and the half-Kelly formula can use it.</p>')
    A('<p><b>Pooled or per strategy.</b> One pooled model with <code>strategy</code> as a feature, plus '
      'per-strategy models only where a strategy has more than about 300 rows. The pooled one is the fallback.</p>')
    A('<p><b>Explain every answer.</b> For each play, the three features that moved the probability most, in '
      'words ("relative volume 3.1x: +0.08", "against the trend: -0.11"). Logistic coefficients give this '
      'directly; for trees, per-row contributions (SHAP-style) are cheap at this size.</p>')
    A('<p><b>Version everything.</b> A model file carries the feature schema it was trained on, the rows it saw '
      '(counts per source, first and last session), the fold scores and a model id. The play panel and the '
      'journal show the id, so a bad week can be traced to the model that made it.</p>')
    A('<p><b>Retrain nightly.</b> The engine retrains in the background after each day\'s 16:15 review has added '
      'its rows (<code>ResearchOps.train_model</code>); <code>scripts/train_model.py</code> does the same by hand. '
      'The scorer picks a new model up within a minute, without a restart.</p>')

    # ---- 6 --------------------------------------------------------------------------------
    A('<h2>6. How it plugs into the app</h2>')
    A(fig_integration())
    A('<ol><li><b>Shadow mode first.</b> The model scores every play on the board and the verdict is logged '
      'next to Autopilot\'s. Nothing changes for two weeks. The journal reports where the two disagree and who '
      'was right.</li>'
      '<li><b>Then the probability.</b> The play\'s <code>probability</code> field comes from the model instead of '
      'the pooled blend; the board, the expected R, the rank score and the sizing all read it already.</li>'
      '<li><b>Then the gate.</b> Autopilot\'s confidence floor becomes a probability floor per timeframe. The '
      'proof rule stays: a strategy still has to earn its place in the replay.</li>'
      '<li><b>Sizing follows.</b> Half-Kelly already takes the win rate and the payoff; it now takes the play\'s '
      'own probability, still capped by the configured risk and the exposure ceilings.</li>'
      '<li><b>A kill switch.</b> One setting turns the model off and restores the hand-written confidence; the '
      'journal watches the live win rate against the predicted one and warns when they diverge.</li></ol>')

    # ---- 7 --------------------------------------------------------------------------------
    A('<h2>7. The timeline and what "done" means</h2>')
    A(fig_timeline())
    A(table(["Milestone", "Deliverable", "Acceptance"], [
        ("Storage (this week)", "the tables and the export", "rows appear for all three sources after one "
         "session and one replay"),
        ("Validation harness", "<code>research/validate.py</code>: purged folds, baselines, the metric table",
         "reproduces the hand-written confidence's numbers as the baseline"),
        ("First model", "<code>research/model.py</code>, a logistic model trained on replay rows",
         "beats the baseline on log loss and top-decile precision across folds"),
        ("Shadow mode", "verdicts logged next to Autopilot's for two weeks", "agreement report in the journal"),
        ("Go / no-go", "gate on the probability, or stay in shadow", "the live rows agree with the folds"),
        ("Operations", "weekly retrain, drift watch, model ids in the journal", "runs unattended"),
    ]))
    A('<p><b>Can the rest of the app keep changing meanwhile?</b> Yes. The rows are versioned by the feature '
      'schema and the model checks the schema it was trained on. New setups simply produce new rows; a changed '
      'setup should bump its own record (its replay rows are re-created on the next run anyway).</p>')

    # ---- 8 --------------------------------------------------------------------------------
    A('<h2>8. Where it stands (2026-09-18)</h2>')
    A('<p>Everything in the plan up to shadow mode is built. What the numbers say so far is sobering, and '
      'that is the point of measuring:</p>')
    A(table(["Piece", "What is built", "What it says today"], [
        ("Records judged for luck", "<code>research/significance.py</code>: Aronson's bootstrap and White's "
         "reality check across every setup tried, net of each stock's own drift; Tharp's quality number and "
         "marble-bag drawdowns; Carver's cost share",
         "no setup is proven: the best one's +0.10R has a 29% chance of being luck alone and 99% once the other "
         "setups tried are allowed for"),
        ("The meta-label model", "<code>research/model.py</code>: boosted trees, uniqueness weights with time "
         "decay, purged walk-forward verdict, isotonic calibration, out-of-sample feature importance; retrained "
         "nightly; scores every play",
         "not usable on 4,868 rows: it ties the stated odds on log loss and its top decile wins no more than "
         "the base rate. It runs in shadow and has no say"),
        ("Execution quality", "the quote at the decision is stored with every trade, slippage is measured at "
         "the fill, a wide spread refuses the entry on live quotes, stale day-trade entries are cancelled",
         "no live fills measured yet; the daily review reports the averages against the replay's 6 bps a side "
         "once there are five"),
        ("Three more book setups", "<code>strategies/patterns.py</code>: Grimes's failure test and pullback, "
         "Bulkowski's confirmed double bottom",
         "a year's replay on 400 stocks: -0.01R to -0.09R after costs, shorts worse than longs. They face the "
         "same proof as every other setup"),
        ("An independent audit", "<code>scripts/r/audit_records.R</code> recomputes the record statistics in "
         "base R", "it agrees with the Python numbers to the second decimal"),
    ]))
    A('<p><b>What would change the picture.</b> Rows and better features, not a cleverer model: every day adds '
      'shadow rows at no risk; real-time data makes the day-trade features current instead of 15 minutes old; '
      'and the features with the highest information coefficients so far (recent volatility, the stop distance, '
      'the market\'s move) suggest the next ones to build. The honest default until then: Autopilot\'s proof rule '
      'keeps it out of unproven setups, and paper practice with the rule switched off is for collecting rows, '
      'not for expecting profit.</p>')

    A('<h2>9. What would help most, and what to read</h2>')
    A('<p><b>Data before books.</b> The single most useful thing is to run the replay after each morning\'s full '
      'scan and to keep the app paper trading with the proof rule on. Rows are the constraint.</p>')
    A('<p><b>Books and papers, in the order they will be used:</b></p>')
    A(table(["Title", "Why it matters here", "Used in"], [
        ("<b>Advances in Financial Machine Learning</b>, Marcos López de Prado (Wiley, 2018)",
         "meta-labelling (ch. 3), purged cross-validation and embargo (ch. 7), backtest overfitting and the "
         "deflated Sharpe ratio (ch. 11-14), feature importance done honestly (ch. 8)",
         "sections 1, 4, 5"),
        ("<b>Evidence-Based Technical Analysis</b>, David Aronson (Wiley, 2006)",
         "data-mining bias in chart patterns; how to test a rule and how many rules you can test before the "
         "best one is noise", "section 4; deciding which of the 13 technical setups to keep"),
        ("<b>The Probability of Backtest Overfitting</b>, Bailey, Borwein, López de Prado and Zhu (Journal of "
         "Computational Finance, 2017)", "the CSCV method: how likely it is that the best configuration found "
         "in-sample underperforms out of sample", "section 4's multiple-testing check"),
        ("<b>Meta-labeling</b> articles by Hudson &amp; Thames (online, 2019-2021)",
         "worked examples of the secondary-model idea with code", "section 5"),
        ("<b>Trading and Exchanges</b>, Larry Harris (Oxford, 2003)",
         "market microstructure: why fills slip, how limit orders queue, what the open does; turns the replay's "
         "5 bps slippage into a modelled number", "the replay's costs; the order builder"),
        ("<b>Machine Trading</b>, Ernest Chan (Wiley, 2017)",
         "the third Chan book: practical ML for strategies, with the same scepticism as the first two",
         "background"),
    ]))
    A('<p>Only the first two are needed to build what this guide describes. The rest sharpen it.</p>')
    A('<h3>Glossary</h3>')
    A(table(["Term", "Meaning"], [
        ("Meta-labelling", "a second model that decides whether to act on a first model's signal, and how much"),
        ("Calibration", "a predicted 0.6 should come true about 60% of the time"),
        ("Purged walk-forward", "train on the past, test on the future, drop the rows that overlap the boundary"),
        ("Embargo", "extra sessions after a test window kept out of the next training set"),
        ("Shuffled-label baseline", "train on random labels to see how good 'nothing' looks"),
        ("Deflated Sharpe / PBO", "corrections for having tried many configurations before picking the best"),
        ("Base rate", "the win rate before any model: what you must beat"),
        ("Log loss / Brier score", "how far predicted probabilities are from what happened, penalising "
         "confident mistakes"),
        ("Half-Kelly", "half the bet fraction that maximises log growth, from win probability and payoff"),
        ("Shadow mode", "the model runs and is logged, but nothing acts on it"),
        ("Drift", "the world changing under a fitted model; watched by comparing live outcomes with predictions"),
    ]))
    A('<p class="small">Generated from the code on the learning-storage branch, 16 September 2026.</p>')
    return "".join(parts)


if __name__ == "__main__":
    OUT.write_text(build(), encoding="utf-8")
    print("wrote", OUT, OUT.stat().st_size, "bytes")
