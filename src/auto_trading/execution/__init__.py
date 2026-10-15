"""Executor and the paper IBKR adapter. Orders pass through the risk gate."""

from auto_trading.execution.executor import Executor
from auto_trading.execution.ibkr import IbkrBroker

__all__ = ["Executor", "IbkrBroker"]
