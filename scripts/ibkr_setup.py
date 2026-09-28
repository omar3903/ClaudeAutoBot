#!/usr/bin/env python
"""IB Gateway connectivity doctor + setup guide.

    python scripts/ibkr_setup.py            # probe both ports, connect read-only, report
    python scripts/ibkr_setup.py --live     # prefer the live port
    python scripts/ibkr_setup.py --symbol MSFT
    python scripts/ibkr_setup.py --guide    # just print the setup walkthrough

It never sends an order. It connects read-only under its own client id, prints
the account, a sample quote and a few candles (saying whether the data is
real-time or delayed), then reminds you how to make the daily Gateway login
hands-off with IBC.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from autotradebot.config import get_settings  # noqa: E402

#: the app uses IBKR_CLIENT_ID, and +50 for the dashboard's Test buttons
DOCTOR_CLIENT_OFFSET = 60

GUIDE = r"""
============================================================================
 Interactive Brokers - one-time setup
============================================================================

IBKR has NO API token and NO expiry. The API is a socket into a running
IB Gateway (or TWS). The login is the Gateway's, which IBKR restarts once a
day. IBC makes that hands-off.

1. PAPER ACCOUNT
   Client Portal -> Settings -> Account Settings -> "Paper Trading Account".
   Enable it. You get a separate username (starts DU...). Tick
   "Share real-time market data subscriptions with paper account" so the
   paper feed matches live.

2. MARKET DATA (for real-time quotes; skip for delayed)
   Client Portal -> Settings -> Market Data Subscriptions. For US stocks the
   cheap option is "US Securities Snapshot and Futures Value Bundle"
   (~USD 10/mo, waived if commissions >= USD 30/mo). Without a subscription
   the bot still works: it scans on IBKR's candles, prices stops and targets
   off the latest one-minute candle, and labels the feed "delayed".

3. IB GATEWAY  (lighter than TWS - install this)
   https://www.interactivebrokers.com/en/trading/ibgateway-stable.php
   Launch it, log in with your PAPER user first.
   Configure -> Settings -> API -> Settings:
     [x] Enable ActiveX and Socket Clients   (older versions only - newer
                                              ones have the API on already)
     [ ] Read-Only API           (untick so the bot can place orders;
                                  leave ticked + set IBKR_READONLY=1 for
                                  data-only)
     Socket port:  4002  (paper)   /  4001  (live)
     Trusted IPs:  127.0.0.1
   Configure -> Settings -> Lock and Exit -> "Auto restart" (NOT auto
   logoff) at 09:00 PM New York time. After-hours trading has ended
   (8:00 PM), IBKR's nightly maintenance (about 11:45 PM - 12:45 AM ET)
   hasn't started, and pre-market (4:00 AM) and the bot's pre-market scan
   are hours away. The bot reconnects by itself; IBKR still asks for a
   full login about once a week.

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

5. .env   (in this project - or use Connections in the dashboard)
       PAPER_PLATFORM=ibkr             # or simulator
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

    from autotradebot.brokers.ibkr_adapter import IbkrBroker
    from autotradebot.util.net import port_is_open

    s = get_settings().secrets
    host = s.ibkr_host or "127.0.0.1"
    ports = {account: s.ibkr_port_for(account) for account in ("paper", "live")}
    print(f"probing {host} ...")
    up = {account: port_is_open(host, port) for account, port in ports.items()}
    for account, port in ports.items():
        print(f"  {account:<5} port {port}: {'OPEN' if up[account] else 'closed'}")
    if not any(up.values()):
        print("\nNo Gateway/TWS is listening. Run  python scripts/ibkr_setup.py --guide\n")
        sys.exit(1)

    mode = "live" if (args.live and up["live"]) or not up["paper"] else "paper"
    print(f"\nconnecting to the {mode} account (port {ports[mode]}), read-only ...")
    broker = IbkrBroker(port=ports[mode], mode=mode, readonly=True,
                        client_id=int(s.ibkr_client_id) + DOCTOR_CLIENT_OFFSET)
    try:
        broker.connect()
    except Exception as e:  # noqa: BLE001
        print(f"  connect failed: {e}")
        sys.exit(1)

    status = broker.session_status()
    print(f"  connected, using {status['market_data']} market data")
    try:
        acc = broker.get_account()
        print(f"  account   : {acc.account_id}")
        print(f"  equity    : {acc.equity:,.2f} USD" + (f"  ({acc.base_currency} account)" if acc.base_currency != "USD" else ""))
        print(f"  positions : {len(acc.positions)}")
    except Exception as e:  # noqa: BLE001
        print(f"  account failed: {e}")

    try:
        q = broker.get_quote(args.symbol)
        print(f"  {args.symbol:<6}    : last {q.last}  bid {q.bid}  ask {q.ask}  "
              f"({'delayed' if status['market_data'] == 'delayed' else 'real-time'})")
    except Exception as e:  # noqa: BLE001
        print(f"  quote failed: {e}")

    try:
        candles = broker.history_many({args.symbol: ("5 mins", "2 D")}).get(args.symbol)
        if candles is None:
            print("  candles   : none returned")
        else:
            print(f"  candles   : {len(candles)} x 5-min bars, latest close "
                  f"{candles['close'].iloc[-1]:.2f} @ {candles.index[-1]}")
    except Exception as e:  # noqa: BLE001
        print(f"  candles failed: {e}")

    broker.close()
    print("\nOK. Start the app with  python run.py")
    if status["market_data"] == "delayed":
        print("Tip: subscribe to US market data (step 2 in --guide) for real-time quotes.")


if __name__ == "__main__":
    main()
