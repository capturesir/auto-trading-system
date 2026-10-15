"""Reference package. The installed monitor is ``auto_trading.alerts``.

Promoted modules:

- ``monitor.py`` → ``auto_trading.alerts.holdings_monitor``
- ``checks.py`` → ``auto_trading.alerts.holdings_ta``
- ``positions.py`` → ``auto_trading.alerts.holdings_positions``
- ``state_io.py`` → ``auto_trading.alerts.holdings_state``
- ``alert_io.py`` → ``auto_trading.alerts.holdings_alert_io``
"""

from auto_trading.alerts.holdings_monitor import run_forever, run_once

__all__ = ["run_forever", "run_once"]
