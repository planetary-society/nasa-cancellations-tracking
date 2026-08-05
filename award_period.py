"""Shared configuration and arithmetic for award-period shortening checks."""

import os

from dotenv import load_dotenv

from tracking_window import as_date

load_dotenv()

SHORTENING_MIN_DAYS_ENV = "AWARD_PERIOD_SHORTENING_MIN_DAYS"
DEFAULT_SHORTENING_MIN_DAYS = 90


def _configured_min_days(raw: str | None = None) -> int:
    """Return the non-negative shortening threshold from configuration."""
    value = raw if raw is not None else os.environ.get(SHORTENING_MIN_DAYS_ENV)
    if value is None or not str(value).strip():
        return DEFAULT_SHORTENING_MIN_DAYS
    try:
        days = int(str(value).strip())
    except ValueError as exc:
        raise RuntimeError(
            f"{SHORTENING_MIN_DAYS_ENV} must be a non-negative integer number "
            f"of days; got {value!r}."
        ) from exc
    if days < 0:
        raise RuntimeError(
            f"{SHORTENING_MIN_DAYS_ENV} must be a non-negative integer number "
            f"of days; got {value!r}."
        )
    return days


SHORTENING_MIN_DAYS = _configured_min_days()


def shortening_days(previous_end_date, current_end_date) -> int | None:
    """Days removed from an award period; extensions return a negative value.

    Date parsing is tracking_window.as_date, so the shapes accepted here are
    exactly the shapes the window gate accepts. A private normaliser would let
    a form one of them admits be silently unparseable to the other.
    """
    previous = as_date(previous_end_date)
    current = as_date(current_end_date)
    if previous is None or current is None:
        return None
    return (previous - current).days


def is_significant_shortening(
    previous_end_date,
    current_end_date,
    *,
    min_days: int = SHORTENING_MIN_DAYS,
) -> bool:
    """True only when the current end is more than ``min_days`` earlier.

    Strictly greater, matching the mirror's consecutive-date SQL predicate:
    the persisted fact validator and source query must agree on the boundary
    case or a row could be written that its consumer refuses to load.
    """
    return significant_shortening(
        shortening_days(previous_end_date, current_end_date), min_days=min_days
    )


def significant_shortening(days: int | None, *, min_days: int = SHORTENING_MIN_DAYS):
    """The threshold test, for callers that already hold `shortening_days`."""
    return days is not None and days > min_days
