"""Quantitative models from the books the strategies learn from, in plain numpy.

- ``stationarity``: is a price mean reverting, trending or a random walk - ADF, Hurst,
  variance ratio, half-life, zero crossings (Chan, *Algorithmic Trading* ch. 2;
  Enders ch. 4; Hamilton ch. 17; Vidyamurthy ch. 7).
- ``cointegration``: pairs whose spread keeps coming back - Engle-Granger and Johansen
  (Chan ch. 2-4; Vidyamurthy ch. 5-7; Johansen; Juselius; Enders ch. 6; Hamilton ch. 19-20).
- ``volatility``: GARCH(1,1) and RiskMetrics volatility forecasts (Tsay ch. 3; Enders ch. 3;
  Hamilton ch. 21).
- ``regime``: calm and turbulent market regimes, Markov switching (Hamilton ch. 22).
- ``sizing``: half-Kelly risk per trade (Chan, *Quantitative Trading* ch. 6; *Algorithmic
  Trading* ch. 8).
- ``bands``: the entry band for a spread (Vidyamurthy ch. 8).
- ``readings``: what those models say about one stock right now, as the scans and the replay
  ask for it - price character and tomorrow's volatility.
"""
