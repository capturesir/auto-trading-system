# Holdings monitor reference

The running code is the `auto_trading.alerts` package:

| Reference | Installed module |
|---|---|
| `monitor.py` | `auto_trading.alerts.holdings_monitor` |
| `checks.py` | `auto_trading.alerts.holdings_ta` |
| `positions.py` | `auto_trading.alerts.holdings_positions` |
| `state_io.py` | `auto_trading.alerts.holdings_state` |
| `alert_io.py` | `auto_trading.alerts.holdings_alert_io` |

These modules re-export that package so the reference and the service stay on one implementation.

The service is paper account `DUR243736` only. `IB_CLIENT_ID_HOLDINGS` is `15`. Client ids 11, 12, 13, 14, and 31 are refused. Alerts are `alert/1.0` files in `alerts/pending/` with `hm_*` rule ids.

VPS canonical snapshot: `/srv/trading/state/holdings_snapshot.json`.

A-group reads `/workspace/invest/b-group/holdings_snapshot.json`, a symlink to the paper-home file `state/holdings_snapshot.json`.

That snapshot is listed in `vps/deploy/sync_vps_to_box.txt` and is kept out of `vps/deploy/sync_box_to_vps.txt`.

`vps/deploy/trading-holdings-monitor.service` is the unit file. Enable and start stay outside this change.
