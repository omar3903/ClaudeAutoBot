#!/usr/bin/env python
"""Run a single scan cycle from the CLI and print the plays. Handy for cron
or for eyeballing output without the dashboard.

    python scripts/run_scan_once.py
    python scripts/run_scan_once.py --universe nasdaq100 --max 60 --json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tos_bot.config import get_settings
from tos_bot.data.fundamentals import YFinanceFundamentals
from tos_bot.data.market_data import MarketDataService
from tos_bot.brokers import get_broker
from tos_bot.scanner import Scanner
from tos_bot.strategies import build_enabled_strategies
from tos_bot.util.logging_setup import setup_logging


def main() -> None:
    setup_logging("INFO")
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe")
    ap.add_argument("--max", type=int)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    s = get_settings()
    if args.universe:
        s.config.scanner.universe = args.universe
    if args.max:
        s.config.scanner.max_symbols_scanned = args.max

    md = MarketDataService()
    scanner = Scanner(s, md, YFinanceFundamentals(), build_enabled_strategies(s))
    broker = get_broker(s.secrets.broker,
                        **({"data_service": md} if s.secrets.broker == "paper" else {}))
    try:
        broker.connect()
        scanner.set_account(broker.get_account())
    except Exception as e:  # noqa: BLE001
        print("broker unavailable, scanning without account context:", e)

    res = scanner.run_cycle()
    if args.json:
        print(json.dumps({"summary": res.summary(),
                          "plays": [p.to_row() for p in res.plays]}, indent=2, default=str))
        return
    print("\n", res.summary(), "\n")
    for p in res.plays[:30]:
        print(f"{p.symbol:6s} {p.side.value:5s} {p.strategy:24s} "
              f"e{p.entry:>8.2f} s{p.stop:>8.2f} t{(p.targets or [0])[0]:>8.2f} "
              f"RR{p.reward_risk:>4.1f} score {p.score:.3f}  | {p.rationale}")


if __name__ == "__main__":
    main()
