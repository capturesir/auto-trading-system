"""Load and validate an upstream signals file."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from auto_trading.config import get_settings
from auto_trading.signals.errors import SignalFileError, SignalValidationError
from auto_trading.signals.models import HKT, SignalFile


def load_signals(path: str | Path | None = None, *, now: datetime | None = None) -> SignalFile:
    """Read a signals file, validate it, and flag expired rows.

    ``path`` defaults to the ``SIGNALS_PATH`` environment variable, or
    ``/workspace/invest/signals.json`` when that variable is unset.

    An empty ``signals`` array is valid. A signal is marked ``expired`` when
    ``now`` is strictly after ``expires_at``. US, Japan, Hong Kong, Korea,
    and Taiwan listings are all in scope; this loader does not drop tickers.

    Invalid JSON raises :class:`SignalFileError`. Schema failures raise
    :class:`SignalValidationError` and list every problem that was found.
    """
    source = Path(path) if path is not None else get_settings().signals_path
    payload = _read_json(source)
    try:
        document = SignalFile.model_validate(payload)
    except ValidationError as exc:
        raise SignalValidationError(_format_validation_error(source, exc)) from exc

    clock = _resolve_now(now)
    for signal in document.signals:
        signal.expired = clock > signal.expires_at
    return document


def _resolve_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(HKT)
    if now.tzinfo is None or now.utcoffset() is None:
        raise SignalValidationError(
            "now must be timezone-aware so expiry can be compared with +08:00 timestamps"
        )
    return now


def _read_json(path: Path) -> object:
    if not path.exists():
        raise SignalFileError(f"signals file not found: {path}")
    if not path.is_file():
        raise SignalFileError(f"signals path is not a file: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SignalFileError(f"signals file is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise SignalFileError(f"could not read signals file {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SignalFileError(
            f"signals file is not valid JSON: {path} "
            f"(line {exc.lineno}, column {exc.colno}: {exc.msg})"
        ) from exc


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    lines = [f"signals file failed validation: {path}"]
    for err in exc.errors():
        location = ".".join(str(part) for part in err["loc"]) or "(root)"
        lines.append(f"- {location}: {err['msg']}")
    return "\n".join(lines)
