"""Behavior reference for pending alerts. Import the installed module."""

from auto_trading.alerts.holdings_alert_io import require_hm_rule, write_hm_alert

__all__ = ["require_hm_rule", "write_hm_alert"]
