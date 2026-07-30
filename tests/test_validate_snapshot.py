"""Snapshot guards. These encode the failure modes from the 2025-2026 audit."""

import csv
import os

import pytest

import validate_snapshot as vs

COLS = ["Source", "District", "Recipient", "Award ID", "Description"]


def rows(n, source="DOGE", start=0):
    return [
        {
            "Source": source,
            "District": "",
            "Recipient": f"R{i}",
            "Award ID": f"A-{i}",
            "Description": f"terminated {i}",
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


def test_disappearance_log_is_not_written_when_quarantined(workdir):
    """A rejected snapshot must not pollute the review queue."""
    write("consolidated/prev.csv", rows(20))
    write("consolidated/new.csv", rows(10))
    vs.validate("consolidated/new.csv", "consolidated/prev.csv")
    assert not os.path.exists(vs.DISAPPEARANCE_LOG)


@pytest.mark.xfail(
    reason="KNOWN HOLE: the shrinkage guard is on NET rows and the presence "
    "check only fires at exactly zero, so a partial collapse of one "
    "source hides behind another's growth. Verified against real data: "
    "NPDV 38->3 with DOGE padded by 35 is accepted. The ledger still "
    "retains the awards, but the run commits as if healthy.",
    strict=True,
)
def test_partial_source_collapse_masked_by_another_source(workdir):
    write("consolidated/prev.csv", rows(38, "NPDV") + rows(82, "DOGE", start=1000))
    write("consolidated/new.csv", rows(3, "NPDV") + rows(117, "DOGE", start=1000))
    ok, _ = vs.validate("consolidated/new.csv", "consolidated/prev.csv")
    assert not ok, "a 38->3 collapse of one source should be caught"
