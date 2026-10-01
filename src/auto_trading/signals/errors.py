"""Errors raised while reading or validating a signals file."""


class SignalError(Exception):
    """Base error for signal loading and validation."""


class SignalFileError(SignalError):
    """The signals path is missing, unreadable, or not valid JSON."""


class SignalValidationError(SignalError):
    """The file is JSON but does not match schema 1.0."""
