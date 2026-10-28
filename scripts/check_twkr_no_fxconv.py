"""CI check: paper TWD/KRW buys never emit FXCONV or any standalone FX order.

Mode A (補7b #19) sends the stock, or holds it. It does not build a Gateway
conversion into TWD or KRW.
"""

from __future__ import annotations

import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from auto_trading.broker import OrderIntent, OrderSide
from auto_trading.execution.fx_convert import assess, standalone_fx_forbidden, venue_for

PIN = "DUR243736"
WHEN = datetime(2026, 10, 2, 9, 30, tzinfo=ZoneInfo("Asia/Hong_Kong"))
RATES = {"HKD": 1.0, "USD": 7.8, "KRW": 0.0056, "TWD": 0.244}


def _buy(currency: str, symbol: str, market: str) -> OrderIntent:
    quantity = 1000 if currency == "TWD" else 1
    price = 100.0 if currency == "TWD" else 70_000.0
    return OrderIntent(
        client_id=f"cid-{currency}",
        intent_id=f"intent-{currency}",
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=quantity,
        limit_price=price,
        currency=currency,
        market=market,
        account=PIN,
        valid_from=WHEN,
    )


def main() -> int:
    failures: list[str] = []
    for currency in ("TWD", "KRW"):
        if not standalone_fx_forbidden(currency):
            failures.append(f"{currency} is not forbidden")
        try:
            venue_for(currency)
        except ValueError:
            pass
        else:
            failures.append(f"venue_for({currency}) did not refuse")
    books = (
        {"USD": 100_000.0, "KRW": 0.0, "TWD": 0.0, "HKD": 0.0},
        {"USD": 100_000.0, "KRW": 10_000_000.0, "TWD": 10_000_000.0},
        {"USD": 0.0, "HKD": 100_000.0, "KRW": 0.0, "TWD": 0.0},
        {"USD": 1.0, "HKD": 1.0, "KRW": 0.0, "TWD": 0.0},
        {"USD": 100_000.0, "KRW": -10.0, "TWD": -10.0},
    )
    orders = (
        _buy("KRW", "005930.KS", "KR"),
        _buy("TWD", "2330.TW", "TW"),
    )
    for verified in (False, True):
        for cash in books:
            for intent in orders:
                attempt = assess(
                    intent,
                    cash_by_ccy=cash,
                    fx_to_base=RATES,
                    verified=verified,
                )
                order = attempt.order
                if order is None:
                    continue
                failures.append(
                    f"{intent.currency} verified={verified} cash={cash} "
                    f"produced {order.order_type}/{order.venue}"
                )
                if order.order_type == "FXCONV" or order.target_ccy in {"TWD", "KRW"}:
                    failures.append(f"FXCONV or target FX for {order.target_ccy}")
    if failures:
        print("TW/KR FXCONV check failed:", file=sys.stderr)
        for line in failures:
            print(line, file=sys.stderr)
        return 1
    print("TW/KR FXCONV check: no standalone FX for TWD or KRW")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
