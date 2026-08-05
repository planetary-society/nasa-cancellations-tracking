"""Tracking-window enforcement: the Jan 20 2025 gate, everywhere.

The window used to be enforced source by source, on action dates only. Two
sources had no gate at all, a third was off by a day, and nothing re-checked at
ingest - so a pre-window cancellation only had to slip past one query to reach
the published ledger. These tests pin the gate down at every layer it now runs
at, and the GeoCarb case that exposed the gap gets its own regression test.
"""

from datetime import date

import pandas as pd
import pytest

import local_usaspending_mirror_query as mirror
import search as s
from contract_query import DETECTION_BASES, FINAL_COLUMNS, validate_source_frame
from tracking_window import (
    TRACKING_WINDOW_START,
    TRACKING_WINDOW_START_DATE,
    in_window,
    to_iso,
)

# --- the predicate itself --------------------------------------------------


def test_the_window_opens_on_inauguration_day_inclusive():
    """2025-01-20 is IN. The grants source used 2025-01-21 before the window
    was centralised, silently dropping anything done on the day itself."""
    assert in_window("2025-01-20")
    assert not in_window("2025-01-19")


@pytest.mark.parametrize(
    "value",
    [
        "2025-06-01",
        date(2025, 6, 1),
        "2025-06-01T00:00:00",
        "2025-06-01 00:00:00",
    ],
)
def test_every_form_a_source_hands_over_a_date_in_is_understood(value):
    """psycopg returns date, replayed CSVs return text, the API returns ISO
    strings that sometimes carry a time."""
    assert in_window(value)


@pytest.mark.parametrize("value", ["", None, "   ", "not-a-date", "2025-06"])
def test_an_unknown_date_is_not_in_the_window(value):
    """The gate keeps pre-window actions out; an unknown date is not evidence
    of an in-window one, so it must not be treated as a free pass."""
    assert not in_window(value)


def test_to_iso_normalises_for_the_shared_action_date_column():
    """Sources fill `action_date` through this, so the column means the same
    thing whoever wrote it."""
    assert to_iso("2025-06-01T00:00:00") == "2025-06-01"
    assert to_iso(date(2025, 6, 1)) == "2025-06-01"
    assert to_iso("not-a-date") == ""


def test_the_window_start_string_and_date_cannot_drift():
    assert TRACKING_WINDOW_START_DATE.isoformat() == TRACKING_WINDOW_START


# --- the mirror's truncation net -------------------------------------------


def test_truncation_net_anchors_the_new_end_date_to_the_window():
    """The gate that let GeoCarb in was a rolling offset from the mod date, so
    it admitted ever-earlier end dates as the window aged. It must be anchored
    to the window instead."""
    # Comments are stripped first: the replacement's own comment quotes the old
    # gate verbatim to explain why it went, and matching that would pass this
    # test while the executed SQL still had the bug.
    sql = "\n".join(
        line
        for line in mirror.Q3_END_DATE_TRUNCATION.splitlines()
        if not line.strip().startswith("--")
    )
    assert f"end_date BETWEEN '{TRACKING_WINDOW_START}'" in sql
    assert "BETWEEN -365 AND 60" not in sql
    # A resulting date after the processing date is a future re-baseline, not
    # an already-effective suspicious shortening.
    assert mirror.PERIOD_CHANGE_RUN_DATE in sql


def test_every_net_still_bounds_its_action_date():
    """Also load-bearing for performance: without this bound Postgres cannot
    use the index on transaction_search."""
    for net in mirror.NETS:
        assert f"'{TRACKING_WINDOW_START}'" in net.sql, net.name


# --- what the mirror declares about its own detections ---------------------


def _mirror_row(method, aid="A-1", action_date="2025-06-01", **extra):
    base = {
        "award_id_native": aid,
        "generated_unique_award_id": f"CONT_AWD_{aid}",
        "is_fpds": True,
        "modification_number": "P00001",
        "action_date": action_date,
        "transaction_description": "routine mod",
        "federal_action_obligation": -1000,
        "recipient_name": "Recip",
        "detection_method": method,
    }
    base.update(extra)
    return base


@pytest.mark.parametrize(
    "method,expected",
    [
        ("action_code", "evidence"),
        ("description_regex", "evidence"),
        ("end_date_truncation", "inference"),
        ("clawback", "inference"),
    ],
)
def test_each_net_declares_whether_it_saw_evidence_or_inferred(method, expected):
    df = mirror._combine([_mirror_row(method)])
    assert df.iloc[0]["detection_basis"] == expected


def test_an_award_with_any_evidence_is_not_treated_as_inferred():
    """Strongest claim wins: an award found by BOTH a truncation and a real
    termination action has evidence behind it, so the effect gate must not
    apply and evict a genuine cancellation."""
    rows = [
        _mirror_row("end_date_truncation", days_truncated=400),
        _mirror_row("action_code", action_type="F"),
    ]
    assert mirror._combine(rows).iloc[0]["detection_basis"] == "evidence"


def test_every_registered_net_declares_a_basis():
    """A net cannot be added without classifying it: `basis` is a required
    field on the Net tuple, so omitting it fails at import, and this pins the
    values so a typo cannot slip through as a third, unhandled basis."""
    assert {net.basis for net in mirror.NETS} <= set(DETECTION_BASES)
    assert len(mirror._NETS_BY_NAME) == len(mirror.NETS)


def test_the_mirror_reports_the_action_date_it_detected():
    df = mirror._combine([_mirror_row("action_code", action_date="2025-09-02")])
    assert df.iloc[0]["action_date"] == "2025-09-02"


# --- the shared output contract --------------------------------------------


def test_every_source_must_declare_the_window_fields():
    assert "action_date" in FINAL_COLUMNS
    assert "detection_basis" in FINAL_COLUMNS
    assert "detection_method" in FINAL_COLUMNS


# --- ingest gate 1: the source's own declaration ---------------------------


def _search_obj():
    obj = s.Search.__new__(s.Search)
    obj.window_rejects = []
    obj.unique_cancellations = {}
    obj.claims = {}
    obj.unresolved = {}
    obj.ignore_award_ids = []
    return obj


def _frame(*rows):
    return pd.DataFrame(
        [
            {
                "detection_basis": "evidence",
                "detection_method": "description_keyword",
                **row,
            }
            for row in rows
        ]
    )


def test_a_pre_window_declaration_is_dropped_before_it_costs_an_api_call():
    obj = _search_obj()
    kept = obj._enforce_declared_window(
        "NASA Procurement Data View",
        _frame(
            {"Award ID": "OLD-1", "action_date": "2024-11-01"},
            {"Award ID": "NEW-1", "action_date": "2025-06-01"},
        ),
    )
    assert kept["Award ID"].tolist() == ["NEW-1"]
    assert obj.window_rejects[0]["Award ID"] == "OLD-1"
    assert "precedes tracking window" in obj.window_rejects[0]["Reason"]


def test_a_blank_declaration_survives_the_first_gate():
    """A blank means the source could not observe a date, not that the action
    is out of window. The second gate derives a real one and re-gates, so
    dropping blanks here would delete such a source's whole contribution."""
    obj = _search_obj()
    kept = obj._enforce_declared_window(
        "NASA Grants", _frame({"Award ID": "G-1", "action_date": ""})
    )
    assert kept["Award ID"].tolist() == ["G-1"]


# --- the source contract, validated where it is declared -------------------


def test_a_source_that_declares_no_action_date_aborts_the_run():
    with pytest.raises(RuntimeError, match="action_date"):
        validate_source_frame("Rogue", pd.DataFrame([{"Award ID": "X-1"}]))


def test_a_source_that_omits_detection_basis_aborts_the_run():
    with pytest.raises(RuntimeError, match="detection_basis"):
        validate_source_frame(
            "Rogue", pd.DataFrame([{"Award ID": "X-1", "action_date": "2025-06-01"}])
        )


def test_a_misspelled_basis_is_caught_at_the_source_boundary():
    """Not per-award after enrichment: a typo must abort before the run has
    spent minutes on API lookups, and must name the source that caused it."""
    with pytest.raises(RuntimeError, match="Evidence"):
        validate_source_frame(
            "Rogue",
            pd.DataFrame(
                [
                    {
                        "Award ID": "X-1",
                        "action_date": "2025-06-01",
                        "detection_basis": "Evidence",
                        "detection_method": "description_keyword",
                    }
                ]
            ),
        )


def test_a_source_that_omits_detection_method_aborts_the_run():
    with pytest.raises(RuntimeError, match="detection_method"):
        validate_source_frame(
            "Rogue",
            pd.DataFrame(
                [
                    {
                        "Award ID": "X-1",
                        "action_date": "2025-06-01",
                        "detection_basis": "evidence",
                    }
                ]
            ),
        )


def test_an_unknown_detection_method_is_caught_at_the_source_boundary():
    with pytest.raises(RuntimeError, match="made_up_method"):
        validate_source_frame(
            "Rogue",
            pd.DataFrame(
                [
                    {
                        "Award ID": "X-1",
                        "action_date": "2025-06-01",
                        "detection_basis": "evidence",
                        "detection_method": "made_up_method",
                    }
                ]
            ),
        )


# --- ingest gate 2: the enriched backstop ----------------------------------


class _Txn:
    def __init__(self, action_date):
        self.action_date = action_date
        self.modification_number = "P00001"


class _Award:
    def __init__(self, end_date="2026-12-31", txn_dates=("2025-06-01",)):
        self.category = "contract"
        self.raw = {}
        self.transactions = [_Txn(d) for d in txn_dates]
        self.period_of_performance = type(
            "P", (), {"end_date": end_date, "start_date": "2020-01-01"}
        )()


def _check(basis, end_date, action_date="2025-09-02", txn_dates=("2025-09-02",)):
    obj = _search_obj()
    row = {"action_date": action_date, "detection_basis": basis}
    ok = obj._passes_tracking_window(
        "Local USAspending Mirror", "A-1", _Award(end_date, txn_dates), row
    )
    return ok, obj.window_rejects


def test_an_inferred_cancellation_must_land_its_effect_inside_the_window():
    ok, rejects = _check("inference", end_date="2024-09-30")
    assert not ok
    assert "closeout of an earlier decision" in rejects[0]["Reason"]


def test_evidence_survives_a_retroactive_end_date():
    """A closeout mod routinely backdates the period of performance. Applying
    the effect gate here would evict real cancellations."""
    ok, rejects = _check("evidence", end_date="2024-09-30")
    assert ok
    assert rejects == []


def test_an_inferred_cancellation_inside_the_window_is_kept():
    ok, _ = _check("inference", end_date="2026-03-01")
    assert ok


def test_a_missing_declaration_falls_back_to_the_award_transactions():
    ok, _ = _check(
        "evidence", end_date="2026-01-01", action_date="", txn_dates=("2025-05-01",)
    )
    assert ok


def test_the_derived_fallback_still_enforces_the_window():
    ok, rejects = _check(
        "evidence", end_date="2026-01-01", action_date="", txn_dates=("2024-05-01",)
    )
    assert not ok
    assert "derived from latest USAspending transaction" in rejects[0]["Reason"]


def test_an_award_with_no_date_from_anywhere_is_rejected_not_admitted():
    ok, rejects = _check(
        "evidence", end_date="2026-01-01", action_date="", txn_dates=()
    )
    assert not ok
    assert "(none)" in rejects[0]["Reason"]


# The basis vocabulary is validated per source at the boundary - see
# test_a_misspelled_basis_is_caught_at_the_source_boundary - so this gate never
# sees an unrecognised value and has no branch for one.


# --- the case that started this -------------------------------------------


def test_geocarb_closeout_does_not_reach_the_snapshot():
    """80LARC17C0001 (GeoCarb, University of Oklahoma). NASA cancelled the
    mission in 2023. Mod P00032 on 2025-09-02 deobligated $513K and pulled the
    period of performance back 638 days to 2024-09-30 - closeout paperwork for
    a pre-window decision. The action date is genuinely in-window, so every
    action-date gate passed it; only the effect gate catches it.
    """
    obj = _search_obj()
    row = {"action_date": "2025-09-02", "detection_basis": "inference"}
    admitted = obj._passes_tracking_window(
        "Local USAspending Mirror",
        "80LARC17C0001",
        _Award(end_date="2024-09-30", txn_dates=("2025-09-02",)),
        row,
    )

    assert not admitted
    assert obj.window_rejects[0]["Award ID"] == "80LARC17C0001"


def test_rejections_are_reported_never_silent(capsys):
    """A gate that quietly shrinks the snapshot is indistinguishable from a
    source breaking - the exact failure the fail-loud policy exists to catch."""
    obj = _search_obj()
    obj.window_rejects = [
        {
            "Award ID": "80LARC17C0001",
            "Source": "Local USAspending Mirror",
            "Reason": "inferred cancellation, but period of performance ends 2024-09-30",
        }
    ]
    obj._report_window_rejects()
    out = capsys.readouterr().out
    assert "80LARC17C0001" in out
    assert TRACKING_WINDOW_START in out
