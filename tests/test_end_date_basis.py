"""Current End Date carries two different measures, and says which.

USAspending publishes no period of performance for an IDV vehicle, only the
ordering-period boundary it calls Last Date to Order - when orders may no
longer be placed, not when work ends. 27 ledger rows are IDVs, so one column
holds both measures and is unusable in aggregate unless each row declares
which one it is.

The date itself is deliberately NOT blanked for IDVs: in_window("") is False
by design, so blanking would reject every IDV at the ingest gate, and the
ordering boundary compares like-for-like against an Initial Reported End Date
resolved the same way - which is what makes End Date Trend meaningful there.
"""

import build_master_ledger as bml
import search
from search import IDV_ORDERING_PERIOD_BASIS, PERIOD_OF_PERFORMANCE_BASIS


class FakePeriod:
    def __init__(self, end_date=None):
        self.end_date = end_date
        self.start_date = "2020-01-01"


class FakeAward:
    def __init__(self, end_date=None, category="contract", raw=None):
        self.period_of_performance = FakePeriod(end_date)
        self.category = category
        self.raw = raw or {}


def test_a_period_of_performance_end_date_is_reported_as_one():
    assert search._award_end_date(FakeAward("2026-09-30")) == (
        "2026-09-30",
        PERIOD_OF_PERFORMANCE_BASIS,
    )


def test_an_idv_falls_back_to_the_ordering_boundary_and_says_so():
    """The live shape: USAspending returns an IDV with a start date and no end
    date at all (verified against 80GRC022AA005)."""
    award = FakeAward(None, "idv", {"Last Date to Order": "2025-04-15"})

    assert search._award_end_date(award) == (
        "2025-04-15",
        IDV_ORDERING_PERIOD_BASIS,
    )


def test_an_idv_that_does_publish_a_period_end_uses_it():
    """Why the basis cannot be back-inferred from the award category."""
    award = FakeAward("2027-05-31", "idv", {"Last Date to Order": "2025-04-15"})

    assert search._award_end_date(award) == (
        "2027-05-31",
        PERIOD_OF_PERFORMANCE_BASIS,
    )


def test_a_non_idv_never_reads_the_ordering_boundary():
    """The fallback is scoped to IDVs; an ordinary contract with no period end
    reports none rather than borrowing a different measure."""
    award = FakeAward(None, "contract", {"Last Date to Order": "2025-04-15"})

    assert search._award_end_date(award) == ("", "")


def test_no_end_date_leaves_the_basis_blank_too():
    """A basis on a blank date would claim a measurement that was not made."""
    assert search._award_end_date(FakeAward(None, "idv")) == ("", "")


def test_the_basis_travels_from_the_snapshot_to_the_ledger():
    """Present in both column lists, and refreshed rather than write-once, so
    an award that gains a period end stops being reported as an IDV boundary."""
    assert "End Date Basis" in search.SNAPSHOT_COLUMNS
    assert "End Date Basis" in bml.LEDGER_COLUMNS
    assert "End Date Basis" in bml.REFRESHED_COLUMNS
    assert bml.LEDGER_COLUMNS.index("End Date Basis") == (
        bml.LEDGER_COLUMNS.index("Current End Date") + 1
    )
