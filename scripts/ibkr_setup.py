#!/usr/bin/env python
"""IBKR connectivity doctor + setup guide.

    python scripts/ibkr_setup.py            # probe both ports, connect, report
    python scripts/ibkr_setup.py --live     # prefer the live port
    python scripts/ibkr_setup.py --symbol MSFT
    python scripts/ibkr_setup.py --guide    # just print the setup walkthrough

It never sends an order. It connects read-only, prints your managed accounts,
net liquidation value, and a sample quote (telling you whether the feed is
real-time or 15-minute delayed), then reminds you how to make the daily
Gateway login hands-off with IBC.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tos_bot.config import get_settings  # noqa: E402

GUIDE = r"""
============================================================================
 Interactive Brokers - one-time setup
============================================================================

IBKR has NO API token and NO 60-day expiry. The API is a socket into a
running IB Gateway (or TWS). Auth = the Gateway login, which IBKR restarts
once a day. IBC makes that hands-off.

1. PAPER ACCOUNT
   Client Portal -> Settings -> Account Settings -> "Paper Trading Account".
   Enable it. You get a separate username (starts DU...). Tick
   "Share real-time market data subscriptions with paper account" so the
   paper feed matches live.

2. MARKET DATA (for real-time quotes; skip for 15-min delayed)
   Client Portal -> Settings -> Market Data Subscriptions. For US stocks the
   cheap option is "US Securities Snapshot and Futures Value Bundle"
   (~USD 10/mo, waived if commissions >= USD 30/mo). Without a subscription
   the bot still works on delayed data and labels the feed "delayed".

3. IB GATEWAY  (lighter than TWS - install this)
   https://www.interactivebrokers.com/en/trading/ibgateway-stable.php
   Launch it, log in with your PAPER user first.
   Configure -> Settings -> API -> Settings:
     [x] Enable ActiveX and Socket Clients
     [ ] Read-Only API           (untick so the bot can place orders;
                                  leave ticked + set IBKR_READONLY=1 for
                                  data-only)
     Socket port:  4002  (paper)   /  4001  (live)
     Trusted IPs:  127.0.0.1
   Configure -> Settings -> Lock and Exit -> "Auto restart" (NOT auto
   logoff) so it re-launches itself after the daily restart.

4. IBC  (auto-login + auto-restart)   https://github.com/IbcAlpha/IBC
   - Download the Windows release, install to  C:\IBC
   - Copy  config.ini.example -> config.ini  and set:
       IbLoginId=your_paper_username
       IbPassword=your_password        # stored locally; see IBC's
                                       # PasswordEncrypted / encryption notes
       TradingMode=paper
       IbDir=C:\Jts                    # where Gateway is installed
       OverrideTwsApiPort=4002
       ReloginAfterSecondFactorAuthenticationTimeout=yes
       AutoRestartTime=  (leave blank; Gateway's own auto-restart handles it)
   - Start it with  C:\IBC\StartGateway.bat  (add a Windows Task Scheduler
     entry "At log on" to make it survive reboots).

5. .env   (in this project)
       BROKER=paper
       LIVE_BROKER=ibkr
       IBKR_HOST=127.0.0.1
       IBKR_PAPER_PORT=4002
       IBKR_LIVE_PORT=4001
       IBKR_CLIENT_ID=11
       # IBKR_ACCOUNT_ID=DU1234567   # only if the login has several accounts
       # IBKR_MARKET_DATA=auto       # auto | live | delayed | delayed-frozen
       # IBKR_READONLY=0             # 1 = never send orders

Then run this script again - it should connect and print your account.
The dashboard's Paper/Live toggle switches ports (4002 <-> 4001) for you.
============================================================================
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="prefer the live port")
    ap.add_argument("--symbol", default="AAPL")
    ap.add_argument("--guide", action="store_true", help="print the setup walkthrough and exit")
    args = ap.parse_args()

    if args.guide:
        print(GUIDE)
        return

    s = get_settings().secrets
    from tos_bot.brokers.ibkr_adapter import IbkrBroker, port_is_open

    host = s.ibkr_host or "127.0.0.1"
    paper_port = s.ibkr_port or s.ibkr_paper_port
    live_port = s.ibkr_port or s.ibkr_live_port

    print(f"probing {host} ...")
    p_up = port_is_open(host, paper_port)
    l_up = port_is_open(host, live_port)
    print(f"  paper port {paper_port}: {'OPEN' if p_up else 'closed'}")
    print(f"  live  port {live_port}: {'OPEN' if l_up else 'closed'}")

    if not (p_up or l_up):
        print("\nNo Gateway/TWS is listening. Run  python scripts/ibkr_setup.py --guide\n")
        sys.exit(1)

    mode = "live" if (args.live and l_up) else ("paper" if p_up else "live")
    port = live_port if mode == "live" else paper_port
    print(f"\nconnecting {mode} (port {port}), read-only ...")
    b = IbkrBroker(port=port, mode=mode, readonly=True)
    try:
        b.connect()
    except Exception as e:  # noqa: BLE001
        print(f"  connect failed: {e}")
        sys.exit(1)

    st = b.session_status()
    print(f"  connected: account(s) via login, using {st['market_data']} market data")
    try:
        acc = b.get_account()
        print(f"  account   : {acc.account_id}")
        print(f"  equity    : {acc.equity:,.2f}")
        print(f"  cash      : {acc.cash:,.2f}")
        print(f"  positions : {len(acc.positions)}")
    except Exception as e:  # noqa: BLE001
        print(f"  get_account failed: {e}")

    try:
        q = b.get_quote(args.symbol)
        print(f"  {args.symbol:<6}    : last {q.last}  bid {q.bid}  ask {q.ask}  "
              f"({'DELAYED ~15m' if st['market_data'] == 'delayed' else 'real-time'})")
    except Exception as e:  # noqa: BLE001
        print(f"  get_quote failed: {e}")

    try:
        h = b.get_price_history(args.symbol, "5m", 2)
        print(f"  history   : {len(h)} x 5-min bars, latest close {h['close'].iloc[-1]:.2f} "
              f"@ {h.index[-1]}")
    except Exception as e:  # noqa: BLE001
        print(f"  get_price_history failed: {e}")

    b.close()
    print("\nOK. Set  LIVE_BROKER=ibkr  in .env and start the app.")
    if st["market_data"] == "delayed":
        print("Tip: subscribe to US market data (step 2 in --guide) for real-time quotes.")


if __name__ == "__main__":
    main()
