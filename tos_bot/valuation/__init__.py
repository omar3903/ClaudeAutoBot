"""Valuation engine derived from Pignataro, *Financial Modeling and Valuation*
(2nd ed., Wiley 2022):

* :mod:`enterprise_value` - Ch. 8 "What Is Value?"  (EV = Mkt Cap + Net Debt + ...)
* :mod:`multiples`        - Ch. 8 & 10  (P/E, EV/EBITDA, EV/EBIT, EV/Sales; comps)
* :mod:`projections`      - Ch. 1  ("Seven Methods of Projections")
* :mod:`dcf`              - Ch. 9  (UFCF, CAPM cost of equity, WACC, terminal value)
* :mod:`football_field`   - Ch. 12 (blended low/high fair-value band incl. 52-wk)
"""

from .dcf import DcfInputs, DcfResult, capm_cost_of_equity, dcf_fair_value, wacc
from .enterprise_value import enterprise_value, equity_value_from_ev, implied_share_price
from .football_field import MethodRange, football_field, band_verdict
from .multiples import (
    Multiples,
    compute_multiples,
    implied_price_from_multiple,
    peer_median_multiples,
    relative_value_signal,
)
from .projections import SEVEN_METHODS, project_series

__all__ = [
    "DcfInputs", "DcfResult", "capm_cost_of_equity", "dcf_fair_value", "wacc",
    "enterprise_value", "equity_value_from_ev", "implied_share_price",
    "MethodRange", "football_field", "band_verdict",
    "Multiples", "compute_multiples", "implied_price_from_multiple",
    "peer_median_multiples", "relative_value_signal",
    "SEVEN_METHODS", "project_series",
]
