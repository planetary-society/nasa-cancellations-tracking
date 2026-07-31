"""Per-source detection evidence survives from the snapshot into the ledger.

The "why we flagged this" sentence each source composes used to stop at the
snapshot boundary, so the published ledger could say an award was cancelled
without ever saying on what evidence. Detection carries it through, refreshed
from the newest observation the way End Date is - and every snapshot archived
before the column existed has to keep building without it.
"""

import csv

import build_master_ledger as bml

# The snapshot shape as of the Detection column. Deliberately a literal copy
# rather than an import of SNAPSHOT_COLUMNS: these tests are about what the
# ledger reads off disk, so a column reordered upstream should show up here as
# a failure rather than be followed silently.
COLS = [
    "Source",
    "District",
    "Recipient",
    "Award ID",
    "Latest Modification Number",
    "Latest Modification Date",
    "Start Date",
    "End Date",
    "Award Amount",
    "Total Outlays",
    "Description",
    "Detection",
    "Business Categories",
    "URL",
    "Claiming Source",
    "Claimed Status",
    "Claimed Savings",
    "Claim Date",
]

# What each source actually emits, from the 2026-07-30 source query CSVs.
CONVENIENCE = "Terminate-for-convenience action P00180 on 2026-05-06"
TRUNCATION = "End date truncated 893 days by mod P00001 on 2026-01-20"
CLAWBACK = "Clawback of 100% ($448,257) on 2026-01-14"


def row(aid, source, detection="", **extra):
    r = {c: "" for c in COLS}
    r.update(
        {
            "Source": source,
            "Award ID": aid,
            "Recipient": f"R {aid}",
            "Description": "terminate for convenience",
            "Detection": detection,
        }
    )
    r.update(extra)
    return r


def write_snap(path, rows, fieldnames=COLS):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def ledger():
    with open(bml.LEDGER_PATH, encoding="utf-8") as fh:
        return {r["Award ID"]: r for r in csv.DictReader(fh)}


# A snapshot with no NPDV row is treated as a degraded run and skipped, so
# every fixture below carries one.
def keep():
    return row("KEEP-1", "NPDV")


# --- the column ------------------------------------------------------------


def test_detection_is_a_refreshed_ledger_column():
    assert "Detection" in bml.LEDGER_COLUMNS
    assert "Detection" in bml.REFRESHED_COLUMNS
    assert "Detection" not in bml.STICKY_COLUMNS


# --- full rebuild ----------------------------------------------------------


def test_full_rebuild_carries_detection_from_the_snapshot(workdir):
    write_snap(
        "consolidated/nasa_x_2026-07-30.csv",
        [
            keep(),
            row("A-1", "USAspendingTerminations", CONVENIENCE),
            row("G-1", "LocalUSASpendingMirror", f"{TRUNCATION}; {CLAWBACK}"),
        ],
    )
    bml.build()

    led = ledger()
    assert led["A-1"]["Detection"] == CONVENIENCE
    assert led["G-1"]["Detection"] == f"{TRUNCATION}; {CLAWBACK}"
    assert led["KEEP-1"]["Detection"] == ""


def test_newest_observation_wins(workdir):
    """Refreshed, not write-once: a later mod supersedes earlier evidence."""
    write_snap(
        "consolidated/nasa_x_2026-07-29.csv",
        [keep(), row("A-1", "USAspendingTerminations", TRUNCATION)],
    )
    write_snap(
        "consolidated/nasa_x_2026-07-30.csv",
        [keep(), row("A-1", "USAspendingTerminations", CONVENIENCE)],
    )
    bml.build()

    assert ledger()["A-1"]["Detection"] == CONVENIENCE


def test_a_blank_day_does_not_erase_a_recorded_detection(workdir):
    """NPDV wins the row on day two and names no evidence. The award is still
    the same award, so the sentence that explained it must not be blanked."""
    write_snap(
        "consolidated/nasa_x_2026-07-29.csv",
        [keep(), row("A-1", "USAspendingTerminations", CONVENIENCE)],
    )
    write_snap("consolidated/nasa_x_2026-07-30.csv", [keep(), row("A-1", "NPDV")])
    bml.build()

    assert ledger()["A-1"]["Detection"] == CONVENIENCE


# --- archived snapshots predating the column -------------------------------


def test_snapshots_without_the_column_build_an_empty_detection(workdir):
    """Every snapshot before 2026-07-30 lacks it; the build must not crash."""
    legacy = [c for c in COLS if c != "Detection"]
    write_snap(
        "consolidated/nasa_x_2026-07-29.csv",
        [keep(), row("A-1", "USAspendingTerminations", CONVENIENCE)],
        fieldnames=legacy,
    )
    bml.build()

    led = ledger()
    assert led["A-1"]["Detection"] == ""
    assert led["A-1"]["Status"] == "listed"


def test_a_column_less_snapshot_cannot_clobber_a_populated_detection(workdir):
    """The archive is read oldest-first, but a replayed old file could land
    after a new one; a missing column reads as blank and blank never wins."""
    write_snap(
        "consolidated/nasa_x_2026-07-29.csv",
        [keep(), row("A-1", "USAspendingTerminations", CONVENIENCE)],
    )
    write_snap(
        "consolidated/nasa_x_2026-07-30.csv",
        [keep(), row("A-1", "USAspendingTerminations")],
        fieldnames=[c for c in COLS if c != "Detection"],
    )
    bml.build()

    assert ledger()["A-1"]["Detection"] == CONVENIENCE


def test_the_new_column_is_the_only_thing_that_changed(workdir):
    """Adding Detection must not perturb any other ledger field.

    Builds the same history twice - once from snapshots carrying the column,
    once from snapshots without it - and requires every other cell to match.
    """
    rows = [
        keep(),
        row(
            "A-1", "USAspendingTerminations", CONVENIENCE, **{"End Date": "2026-05-06"}
        ),
        row("G-1", "LocalUSASpendingMirror", CLAWBACK, **{"Award Amount": "448257"}),
    ]
    write_snap("consolidated/nasa_x_2026-07-30.csv", rows)
    bml.build()
    with_detection = ledger()

    write_snap(
        "consolidated/nasa_x_2026-07-30.csv",
        rows,
        fieldnames=[c for c in COLS if c != "Detection"],
    )
    bml.build()
    without_detection = ledger()

    assert set(with_detection) == set(without_detection)
    for aid, rec in with_detection.items():
        other = without_detection[aid]
        assert {k: v for k, v in rec.items() if k != "Detection"} == {
            k: v for k, v in other.items() if k != "Detection"
        }
    assert with_detection["A-1"]["Detection"] == CONVENIENCE
    assert without_detection["A-1"]["Detection"] == ""


# --- incremental path ------------------------------------------------------


def test_update_path_carries_detection(workdir):
    write_snap("consolidated/nasa_x_2026-07-29.csv", [keep()])
    bml.build()

    write_snap(
        "consolidated/nasa_x_2026-07-30.csv",
        [keep(), row("A-1", "USAspendingTerminations", CONVENIENCE)],
    )
    bml.build(update_only=True)

    assert ledger()["A-1"]["Detection"] == CONVENIENCE


def test_update_path_refreshes_a_detection_already_in_the_ledger(workdir):
    write_snap(
        "consolidated/nasa_x_2026-07-29.csv",
        [keep(), row("A-1", "USAspendingTerminations", TRUNCATION)],
    )
    bml.build()

    write_snap(
        "consolidated/nasa_x_2026-07-30.csv",
        [keep(), row("A-1", "USAspendingTerminations", CONVENIENCE)],
    )
    bml.build(update_only=True)

    assert ledger()["A-1"]["Detection"] == CONVENIENCE


def test_both_build_paths_agree_on_detection(workdir):
    write_snap(
        "consolidated/nasa_x_2026-07-29.csv",
        [keep(), row("A-1", "USAspendingTerminations", TRUNCATION)],
    )
    write_snap(
        "consolidated/nasa_x_2026-07-30.csv",
        [keep(), row("A-1", "USAspendingTerminations", CONVENIENCE)],
    )
    bml.build()
    after_full = ledger()

    bml.build(update_only=True)
    after_update = ledger()

    assert {aid: r["Detection"] for aid, r in after_full.items()} == {
        aid: r["Detection"] for aid, r in after_update.items()
    }


def test_a_pre_detection_ledger_read_back_on_update_gains_the_column(workdir):
    """The stored ledger has no Detection header until the first build after
    this change; reading it back must default rather than KeyError."""
    write_snap("consolidated/nasa_x_2026-07-29.csv", [keep()])
    bml.build()

    with open(bml.LEDGER_PATH, encoding="utf-8") as fh:
        stored = list(csv.DictReader(fh))
    legacy_columns = [c for c in bml.LEDGER_COLUMNS if c != "Detection"]
    with open(bml.LEDGER_PATH, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=legacy_columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(stored)

    write_snap(
        "consolidated/nasa_x_2026-07-30.csv",
        [keep(), row("A-1", "USAspendingTerminations", CONVENIENCE)],
    )
    bml.build(update_only=True)

    led = ledger()
    assert led["KEEP-1"]["Detection"] == ""
    assert led["A-1"]["Detection"] == CONVENIENCE
