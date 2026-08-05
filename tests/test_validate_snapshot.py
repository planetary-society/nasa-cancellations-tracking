"""Snapshot guards. These encode the failure modes from the 2025-2026 audit."""

import csv
import os

import pytest

import validate_snapshot as vs

COLS = [
    "Source",
    "Recipient Congressional District",
    "Recipient Name",
    "Award ID",
    "Award or Action Description",
]


def rows(n, source="DOGE", start=0):
    return [
        {
            "Source": source,
            "Recipient Congressional District": "",
            "Recipient Name": f"R{i}",
            "Award ID": f"A-{i}",
            "Award or Action Description": f"terminated {i}",
        }
        for i in range(start, start + n)
    ]


def write(path, rs):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        w.writerows(rs)


def test_no_previous_snapshot_is_accepted_as_baseline(workdir):
    write("consolidated/new.csv", rows(10))
    ok, msgs = vs.validate("consolidated/new.csv", None)
    assert ok
    assert "baseline" in msgs[0]


def test_unchanged_snapshot_passes(workdir):
    write("consolidated/prev.csv", rows(10))
    write("consolidated/new.csv", rows(10))
    ok, _ = vs.validate("consolidated/new.csv", "consolidated/prev.csv")
    assert ok
    assert os.path.exists("consolidated/new.csv")


def test_single_disappearance_is_legal_but_logged(workdir):
    """One award may legitimately leave; it must still leave a trail."""
    write("consolidated/prev.csv", rows(10))
    write("consolidated/new.csv", rows(9))
    ok, msgs = vs.validate("consolidated/new.csv", "consolidated/prev.csv")
    assert ok
    assert os.path.exists(vs.DISAPPEARANCE_LOG)
    with open(vs.DISAPPEARANCE_LOG, encoding="utf-8") as fh:
        logged = list(csv.DictReader(fh))
    assert [r["Award ID"] for r in logged] == ["A-9"]
    assert logged[0]["Review Status"] == "pending"
    assert any("disappeared" in m for m in msgs)


def test_mass_shrinkage_quarantines_the_snapshot(workdir):
    write("consolidated/prev.csv", rows(20))
    write("consolidated/new.csv", rows(10))
    ok, msgs = vs.validate("consolidated/new.csv", "consolidated/prev.csv")
    assert not ok
    assert not os.path.exists("consolidated/new.csv"), (
        "candidate must not stay published"
    )
    assert os.path.exists(os.path.join(vs.QUARANTINE_DIR, "new.csv"))
    assert any("FAIL shrinkage" in m for m in msgs)


def test_a_source_going_to_zero_fails_even_without_shrinkage(workdir):
    """The FPDS retirement shape: one source silently stops producing rows."""
    write("consolidated/prev.csv", rows(10, "DOGE") + rows(5, "FPDS", start=100))
    # Same total row count, but FPDS is gone and DOGE grew to cover it.
    write("consolidated/new.csv", rows(15, "DOGE"))
    ok, msgs = vs.validate("consolidated/new.csv", "consolidated/prev.csv")
    assert not ok
    assert any("FAIL source_presence" in m and "FPDS" in m for m in msgs)


def test_an_explicitly_skipped_optional_source_does_not_fail_validation(workdir):
    """The local mirror is allowed to disappear only when collection records
    that it was unavailable; this must not weaken checks for other sources."""
    write(
        "consolidated/prev.csv",
        rows(10, "DOGE") + rows(5, "Local USAspending Mirror", start=100),
    )
    write("consolidated/new.csv", rows(10, "DOGE"))

    ok, msgs = vs.validate(
        "consolidated/new.csv",
        "consolidated/prev.csv",
        skipped_sources={"Local USAspending Mirror"},
    )

    assert ok
    assert not any("FAIL" in msg for msg in msgs)
    assert not os.path.exists(vs.DISAPPEARANCE_LOG)


def test_disappearance_log_is_not_written_when_quarantined(workdir):
    """A rejected snapshot must not pollute the review queue."""
    write("consolidated/prev.csv", rows(20))
    write("consolidated/new.csv", rows(10))
    vs.validate("consolidated/new.csv", "consolidated/prev.csv")
    assert not os.path.exists(vs.DISAPPEARANCE_LOG)


def test_reviewed_methodology_removals_do_not_trigger_shrinkage(workdir):
    write("consolidated/prev.csv", rows(20))
    write("consolidated/new.csv", rows(10))

    ok, messages = vs.validate(
        "consolidated/new.csv",
        "consolidated/prev.csv",
        reviewed_removals={f"A-{i}" for i in range(10, 20)},
    )

    assert ok
    assert not any("FAIL shrinkage" in message for message in messages)
    assert any("reviewed excluded_by_design" in message for message in messages)
    assert not os.path.exists(vs.DISAPPEARANCE_LOG)


def test_unreviewed_disappearances_still_quarantine(workdir):
    write("consolidated/prev.csv", rows(20))
    write("consolidated/new.csv", rows(10))

    ok, messages = vs.validate(
        "consolidated/new.csv",
        "consolidated/prev.csv",
        reviewed_removals={"A-19"},
    )

    assert not ok
    assert any("FAIL shrinkage" in message for message in messages)


def test_only_excluded_by_design_is_loaded_as_a_reviewed_removal(workdir):
    os.makedirs(os.path.dirname(vs.REVIEWED_REMOVALS_PATH), exist_ok=True)
    with open(vs.REVIEWED_REMOVALS_PATH, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["Award ID", "Tracking Status", "Verified Date", "Evidence"]
        )
        writer.writeheader()
        writer.writerows(
            [
                {"Award ID": "A-1", "Tracking Status": "excluded_by_design"},
                {"Award ID": "A-2", "Tracking Status": "continued"},
            ]
        )

    assert vs.load_reviewed_removals() == {"A-1"}


@pytest.mark.xfail(
    reason="KNOWN HOLE: the shrinkage guard is on NET rows and the presence "
    "check only fires at exactly zero, so a partial collapse of one "
    "source hides behind another's growth. Verified against real data: "
    "NPDV 38->3 with DOGE padded by 35 is accepted. The ledger still "
    "retains the awards, but the run commits as if healthy.",
    strict=True,
)
def test_partial_source_collapse_masked_by_another_source(workdir):
    write(
        "consolidated/prev.csv",
        rows(38, "NASA Procurement Data View") + rows(82, "DOGE", start=1000),
    )
    write(
        "consolidated/new.csv",
        rows(3, "NASA Procurement Data View") + rows(117, "DOGE", start=1000),
    )
    ok, _ = vs.validate("consolidated/new.csv", "consolidated/prev.csv")
    assert not ok, "a 38->3 collapse of one source should be caught"


def test_the_two_declarations_of_the_human_verdict_header_agree():
    """validate_snapshot restates the schema rather than importing it.

    Both modules read verification/dropped_award_status.csv and both now assert
    its header, so a hand-edit that satisfies one reader and not the other would
    be caught in a different place depending on which ran first.
    """
    import build_master_ledger

    assert vs.REVIEWED_REMOVALS_COLUMNS == build_master_ledger.VERIFICATION_COLUMNS
