"""Intraday re-evaluation for the paper account.

The morning runner still writes the daily intent files. This package records
the names that passed the trigger and missed confirm, then re-runs that same
daily confirm with the latest last price treated as today's close.
"""
