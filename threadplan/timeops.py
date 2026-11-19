"""Deterministic instant validation and elapsed-time arithmetic; no language rules."""

from datetime import timedelta, timezone
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .codec import _aware_time


def instant(value, zone=None):
    parsed = _aware_time(value)
    if parsed is None:
        raise ValueError("invalid_aware_time")
    if zone is not None:
        try:
            named = ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError, TypeError) as error:
            raise ValueError("invalid_event_timezone") from error
        local = parsed.astimezone(named)
        # A UTC round trip detects nonexistent local times and incorrect offsets.
        # For a repeated time an explicit valid offset identifies its occurrence;
        # an offset-free wall time is never guessed or admitted.
        if (local.replace(tzinfo=None) != parsed.replace(tzinfo=None)
                or local.utcoffset() != parsed.utcoffset()):
            raise ValueError("event_timezone_offset_mismatch")
        return local
    return parsed


def minutes(value, *, signed=False):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or abs(value) > 525600 or (not signed and value <= 0)):
        raise ValueError("invalid_minutes")
    return value


def add_minutes(value, amount, zone=None):
    start = instant(value, zone)
    try:
        shifted = start.astimezone(timezone.utc) + timedelta(minutes=minutes(amount, signed=True))
        return shifted.astimezone(start.tzinfo)
    except OverflowError as error:
        raise ValueError("time_arithmetic_out_of_range") from error


def elapsed_minutes(start, end):
    low, high = instant(start), instant(end)
    duration = (high.astimezone(timezone.utc) - low.astimezone(timezone.utc)).total_seconds() / 60
    if duration <= 0:
        raise ValueError("original_duration_unknown")
    return duration
