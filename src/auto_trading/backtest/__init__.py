"""Backtests of approved strategy specs against stored bars.

A backtest must not place live orders. The odd-lot toggle defaults to false.
"""

from auto_trading.backtest.engine import Bar, BtSignal, LookaheadError, run_backtest

__all__ = ["Bar", "BtSignal", "LookaheadError", "run_backtest"]
