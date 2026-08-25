"""The historical fiscal-year report: the shared verdict, per-FY, one count per award."""

from datetime import date

from nasatrack.criteria import fiscal_year
from nasatrack.schema import CancellationAwardsByFiscalYearRow
from nasatrack.terminations import count_by_fiscal_year
from tests.test_accept_award import txn

TODAY = date(2026, 8, 25)


def test_fiscal_year_boundaries():
    # The federal fiscal year runs Oct 1 - Sep 30 and is named for its END.
    assert fiscal_year(date(2024, 9, 30)) == 2024
    assert fiscal_year(date(2024, 10, 1)) == 2025
    assert fiscal_year(date(2025, 1, 20)) == 2025
    assert fiscal_year("2025-10-01") == 2026
    assert fiscal_year(None) is None


def award(award_id, *txn_args):
    """One award's transactions, each (day, action_type, description)."""
    return [
        txn(
            day,
            action_type=action_type,
            description=description,
            award_id=award_id,
        )
        for day, action_type, description in txn_args
    ]


def test_each_award_counts_once_in_its_anchor_fiscal_year():
    txns = [
        # Stop-work language in FY2025, the F code in FY2026: the coded-first
        # anchor dates the award FY2026, and the FY2025 language signal - which
        # the old per-signal report counted separately - counts nowhere.
        *award(
            "80HQTR24F0012",
            ("2025-04-08", "", "STOP WORK ORDER ISSUED"),
            ("2026-04-14", "F", "TERMINATION"),
        ),
        # A plain FY2025 termination.
        *award("80NSSC25C0001", ("2025-06-01", "F", "ADMIN MOD")),
        # Terminated FY2025, vacated FY2026: counts in NO year.
        *award(
            "80NSSC21K1443",
            ("2025-05-12", "", "TERMINATION FOR CONVENIENCE"),
            ("2026-02-05", "", "THE TERMINATION HAS BEEN VACATED"),
        ),
        # Pre-tracking-window history: the report's own bound admits it.
        *award("NNX10AA01C", ("2010-03-15", "F", "TERMINATE FOR CONV")),
    ]
    report = count_by_fiscal_year(txns, start_fiscal_year=2010, today=TODAY)

    by_fy = {r.fiscal_year: r.terminated_awards for r in report}
    assert by_fy[2010] == 1  # the historical award, adjudicated windowlessly
    assert by_fy[2025] == 1  # 80NSSC25C0001 only - no language double count
    assert by_fy[2026] == 1  # 80HQTR24F0012 anchored by its F code
    assert sum(by_fy.values()) == 3  # the vacated award counts in no year

    # Zero-filled through today's fiscal year, every year present.
    assert [r.fiscal_year for r in report] == list(range(2010, 2027))
    assert by_fy[2013] == 0


def test_overrides_and_descope_routing_apply_to_the_report():
    txns = [
        # A human excluded this award from the published list; the report
        # honors the same ruling in whatever year the anchor fell.
        *award("80LARC19F0086", ("2025-03-01", "F", "TERM SETTLEMENT")),
        # A de-scope-anchored award leaves the count as it leaves the list.
        *award("80GRC024CA008", ("2025-04-01", "", "STOP WORK - INTENT TO DESCOPE")),
        # An ordinary termination stays.
        *award("80NSSC25C0001", ("2025-06-01", "F", "")),
    ]
    report = count_by_fiscal_year(
        txns,
        {"80LARC19F0086": "excluded_by_design"},
        start_fiscal_year=2025,
        today=TODAY,
    )
    assert {r.fiscal_year: r.terminated_awards for r in report}[2025] == 1


def test_empty_history_still_zero_fills():
    assert count_by_fiscal_year([], start_fiscal_year=2024, today=TODAY) == [
        CancellationAwardsByFiscalYearRow(fiscal_year=2024, terminated_awards=0),
        CancellationAwardsByFiscalYearRow(fiscal_year=2025, terminated_awards=0),
        CancellationAwardsByFiscalYearRow(fiscal_year=2026, terminated_awards=0),
    ]
