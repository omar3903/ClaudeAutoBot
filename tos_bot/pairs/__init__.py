"""Pairs trading: two stocks in the same industry whose spread keeps coming back.

- ``model``: a pair's hedge ratio, band, stop and time stop, the z-score and the sizing
  (Vidyamurthy, *Pairs Trading*; Chan, *Algorithmic Trading* ch. 2-3 and 8).
- ``finder``: which pairs are worth watching - correlation, Engle-Granger and Johansen
  cointegration, half-life and zero crossings (Vidyamurthy ch. 6-7).
- ``backtest``: the rules replayed on daily closes with costs, out of sample.
- ``desk``: the live side - watching the spreads and trading both legs together.
"""
