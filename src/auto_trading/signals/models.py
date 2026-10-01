"""Typed model of the upstream signals file (schema_version 1.0)."""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HKT = timezone(timedelta(hours=8))
HKT_OFFSET = timedelta(hours=8)
SUPPORTED_SCHEMA_VERSION = "1.0"
SIDES = frozenset({"BUY", "SELL", "HOLD"})
_HKT_SUFFIX = re.compile(r"\+08:00$")
_TIMESTAMP_ERROR = "must be an ISO-8601 timestamp with a +08:00 offset"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


def _as_hkt_datetime(value: Any, field_name: str) -> datetime:
    message = f"{field_name} {_TIMESTAMP_ERROR}"
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if _HKT_SUFFIX.search(text) is None:
            raise ValueError(message)
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(message) from exc
    else:
        raise ValueError(message)
    offset = parsed.utcoffset()
    if parsed.tzinfo is None or offset != HKT_OFFSET:
        raise ValueError(message)
    return parsed


class Signal(BaseModel):
    """One equity signal from the daily research file."""

    model_config = ConfigDict(extra="ignore")

    signal_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    market: str = Field(min_length=1)
    name: str = Field(min_length=1)
    asset_type: str = Field(min_length=1)
    currency: str = Field(min_length=1)
    side: Side
    confidence: int
    fair_value: float
    market_price: float
    price_date: date
    upside_pct: float
    horizon: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    report_path: str = Field(min_length=1)
    expires_at: datetime
    expired: bool = False

    @model_validator(mode="before")
    @classmethod
    def _drop_derived_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            data.pop("expired", None)
        return data

    @field_validator("side", mode="before")
    @classmethod
    def _side(cls, value: Any) -> Any:
        if value not in SIDES:
            raise ValueError("side must be one of BUY, SELL, HOLD")
        return value

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("confidence must be an integer between 0 and 100")
        if value < 0 or value > 100:
            raise ValueError("confidence must be an integer between 0 and 100")
        return value

    @field_validator("expires_at", mode="before")
    @classmethod
    def _expires_at(cls, value: Any) -> datetime:
        return _as_hkt_datetime(value, "expires_at")

    @field_validator("fair_value", "market_price", "upside_pct")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("must be a finite number")
        return value


class SignalFile(BaseModel):
    """Top-level daily signals document."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str
    generated_at: datetime
    generated_by: str = Field(min_length=1)
    price_date: date
    signals: list[Signal]

    @field_validator("schema_version")
    @classmethod
    def _schema_version(cls, value: str) -> str:
        if value != SUPPORTED_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {value!r}; expected {SUPPORTED_SCHEMA_VERSION!r}"
            )
        return value

    @field_validator("generated_at", mode="before")
    @classmethod
    def _generated_at(cls, value: Any) -> datetime:
        return _as_hkt_datetime(value, "generated_at")
