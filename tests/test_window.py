"""The tracking window opens on inauguration day, and only for real dates."""

from datetime import date, datetime

import pytest

from nasatrack.criteria import WINDOW_START, WINDOW_START_ISO, as_date, in_window


def test_window_start():
    assert WINDOW_START_ISO == "2025-01-20"
    assert as_date(WINDOW_START_ISO) == WINDOW_START


@pytest.mark.parametrize("value", ["2025-01-20", date(2025, 1, 20), "2026-08-01"])
def test_in_window(value):
    assert in_window(value)


@pytest.mark.parametrize(
    "value",
    [
        "2025-01-19",
        date(2025, 1, 19),
        "",
        None,
        # Ten characters, and sorts after "2025-01-20" as text - a string
        # comparison would have called this in-window.
        "not-a-date",
    ],
)
def test_out_of_window(value):
    assert not in_window(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2025-09-02T00:00:00", date(2025, 9, 2)),
        ("2025-09-02 00:00:00", date(2025, 9, 2)),
        (datetime(2025, 9, 2, 13, 45), date(2025, 9, 2)),
        (date(2025, 9, 2), date(2025, 9, 2)),
        ("  2025-09-02  ", date(2025, 9, 2)),
    ],
)
def test_as_date_normalises(value, expected):
    parsed = as_date(value)
    assert parsed == expected
    assert type(parsed) is date  # never a datetime, or ISO output grows a time


@pytest.mark.parametrize("value", [None, "", "not-a-date", "2025-13-01"])
def test_as_date_returns_none(value):
    assert as_date(value) is None
