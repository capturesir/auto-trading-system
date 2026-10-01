"""Personal auto-trading system.

Flow: daily signals file, market data and indicators, versioned strategy
specs, backtest, risk gate, executor, then the IBKR broker. The risk gate
is the only path that may place an order. An LLM never places orders.
"""

__version__ = "0.1.0"
