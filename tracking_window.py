#!/usr/bin/env python3
"""The single definition of the tracking window, and the gate that enforces it.

Every source in this repo answers the same question - "was this award cancelled
by the current administration?" - and that question has exactly one date bound:
the second-term inauguration. Before this module the bound was copied into four
files, and they had already drifted (the grants source used 2025-01-21, an
off-by-one that silently dropped inauguration-day actions). Two sources had no
bound at all. It lives here now so widening or narrowing the window is one edit.

`in_window` answers the only question this module decides: is this date inside
the window? It is applied twice, to two different dates, and the difference is
load-bearing:

  * to an ACTION date, for every detection from every source. This is the
    primary gate.

  * to an EFFECT date - the period-of-performance end an award was moved to -
    but ONLY for detections that INFER a cancellation from the shape of the
    data rather than from termination evidence. A mod dated inside the window
    that pulls an award's period of performance back to a date before the
    window did not cancel anything: the work had already stopped, and the mod
    is closeout paperwork for an earlier decision.

Which detections get the second application is decided by `detection_basis`;
see contract_query.DETECTION_BASES for that contract, and README "The Tracking
Window" for the case that motivated the split.
"""

from collections.abc import Sequence
from datetime import date, datetime

# The window opens at the second-term inauguration. In the mirror's SQL this
# bound does double duty: without an action_date bound Postgres cannot use the
# index on transaction_search and every net degrades into a seq scan over
# ~236M rows.
TRACKING_WINDOW_START = "2025-01-20"

# Derived, never written twice. A module whose entire purpose is that this date
# exists once should not open by copying it.
TRACKING_WINDOW_START_DATE = date.fromisoformat(TRACKING_WINDOW_START)


def as_date(value, extra_formats: Sequence[str] = ()):
    """Parse a date, datetime, or date-ish string, or return None.

    Sources hand dates over in whatever form their upstream produced: psycopg
    returns `date`, a replayed CSV returns text, and the USAspending API returns
    ISO strings that sometimes carry a time component.

    This genuinely parses rather than comparing ISO text, because a string
    comparison silently accepts garbage: 'not-a-date' happens to be ten
    characters and sorts after '2025-01-20', so a text-only gate would have
    called it in-window.

    `extra_formats` are strptime patterns a particular source has been observed
    to emit. They live at the call site rather than here so that the set of
    forms accepted into `action_date` stays visible in one function, instead of
    each source growing a private normaliser that admits a different set.
    """
    if value is None:
        return None
    # datetime is a subclass of date, so this order matters: returning the
    # datetime itself would make to_iso emit '2025-09-02T00:00:00'.
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    # '2025-09-02T00:00:00' and '2025-09-02 00:00:00' both truncate correctly.
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    for fmt in extra_formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def in_window(action_date) -> bool:
    """True when an action falls on or after the window start.

    A missing or unparseable date is NOT in the window. This is deliberate: the
    gate's job is to keep pre-window actions out, and an unknown date is not
    evidence of an in-window one. Callers that would rather investigate than
    drop should check for emptiness themselves before calling.
    """
    parsed = as_date(action_date)
    return parsed is not None and parsed >= TRACKING_WINDOW_START_DATE


def to_iso(value, extra_formats: Sequence[str] = ()) -> str:
    """Normalise a date-ish value to an ISO string, or "" if it is not one.

    Sources use this to fill the shared `action_date` column, so that the
    column means the same thing whoever wrote it. Blank is not a free pass:
    `in_window("")` is False, so an unparseable date quarantines its row rather
    than slipping through. See `as_date` for `extra_formats`.
    """
    parsed = as_date(value, extra_formats)
    return parsed.isoformat() if parsed else ""
