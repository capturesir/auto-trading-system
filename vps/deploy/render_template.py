#!/usr/bin/env python3
"""Build /etc/ibc/config.paper.ini.template from the stock IBC config.ini sample.
Credential-free: login fields become placeholders. Run on the VPS as root."""
import re, sys
SRC = "/opt/ibc/config.ini.sample"
DST = "/etc/ibc/config.paper.ini.template"
settings = {
    "IbLoginId": "__IBKR_PAPER_USERNAME__",
    "IbPassword": "__IBKR_PAPER_PASSWORD__",
    "TradingMode": "paper",
    "ReadOnlyApi": "yes",
    "AcceptIncomingConnectionAction": "reject",
    "OverrideTwsApiPort": "4002",
    "AcceptNonBrokerageAccountWarning": "yes",
    "ExistingSessionDetectedAction": "secondary",
    "SecondFactorAuthenticationTimeout": "180",
    "ReloginAfterSecondFactorAuthenticationTimeout": "no",
    "ExitAfterSecondFactorAuthenticationTimeout": "yes",
    "ReadOnlyLogin": "no",
    "FIX": "no",
    "CommandServerPort": "7462",
    "BindAddress": "127.0.0.1",
    "ControlFrom": "127.0.0.1",
    "AllowBlindTrading": "no",
    # 19:30 HKT daily = 11:30 UTC (VPS clock is UTC): all 5 markets closed incl. KRX/NXT after-market (ends 20:00 KST = 19:00 HKT);
    # before the APAC 2nd reset (20:15 HKT) and US open (21:30/22:30 HKT); clear of the 04:45-07:05 HKT 1st reset
    "AutoRestartTime": "11:30 AM",
    # Sunday 18:30 HKT = 10:30 UTC (after 01:00 US/Eastern): weekly full re-login (cold restart)
    "ColdRestartTime": "10:30",
}
header = """# IBC config TEMPLATE for IB Gateway (PAPER ONLY) on trading-paper-sgp1.
# NO CREDENTIALS IN THIS FILE. The placeholders __IBKR_PAPER_USERNAME__ and
# __IBKR_PAPER_PASSWORD__ are filled at service start by /usr/local/lib/ibgw/ibgw-start.sh
# from systemd encrypted credentials (LoadCredentialEncrypted). The rendered file is kept only in
# /run/ibgateway (tmpfs, mode 0600, owner ibgw) and is removed when the service stops.
# Guard rails: TradingMode=paper, ReadOnlyApi=yes, API port 4002, incoming connections rejected,
# existing session -> secondary, exit on 2FA timeout. ibgw-start.sh refuses to run if these change.
"""
out, seen = [], set()
for ln in open(SRC, encoding="utf-8").read().splitlines():
    m = re.match(r"^([A-Za-z0-9]+)=", ln)
    if m and m.group(1) in settings:
        k = m.group(1)
        if k in seen:
            continue
        out.append(f"{k}={settings[k]}"); seen.add(k)
    else:
        out.append(ln)
for k, v in settings.items():
    if k not in seen:
        out.append(f"{k}={v}")
open(DST, "w", encoding="utf-8").write(header + "\n".join(out) + "\n")
print("wrote", DST)
