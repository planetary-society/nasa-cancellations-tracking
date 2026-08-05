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
    "Recipient Congressional District",
    "Recipient Name",
    "Award ID",
    "Latest Modification Number",
    "Start Date",
    "Current End Date",
    "Current Obligated Amount",
    "Total Outlays",
    "Award or Action Description",
    "Detection Evidence",
    "Recipient Business Categories",
    "USAspending URL",
    "Claimed By",
    "DOGE Claimed Status",
    "DOGE Claimed Savings",
    "DOGE Claim Date",
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
            "Recipient Name": f"R {aid}",
            "Award or Action Description": "terminate for convenience",
            "Detection Evidence": detection,
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
    return row("KEEP-1", "NASA Procurement Data View")


# --- the column ------------------------------------------------------------


def test_detection_is_a_refreshed_ledger_column():
    assert "Detection Evidence" in bml.LEDGER_COLUMNS
    assert "Detection Evidence" in bml.REFRESHED_COLUMNS
    assert "Detection Evidence" not in bml.STICKY_COLUMNS


# --- full rebuild ----------------------------------------------------------


def test_full_rebuild_carries_detection_from_the_snapshot(workdir):
    write_snap(
        "consolidated/nasa_x_2026-07-30.csv",
        [
            keep(),
            row("A-1", "USAspending Terminations", CONVENIENCE),
            row("G-1", "Local USAspending Mirror", f"{TRUNCATION}; {CLAWBACK}"),
        ],
    )
    bml.build()

    led = ledger()
    assert led["A-1"]["Detection Evidence"] == CONVENIENCE
    assert led["G-1"]["Detection Evidence"] == f"{TRUNCATION}; {CLAWBACK}"
    assert led["KEEP-1"]["Detection Evidence"] == ""


def test_newest_observation_wins(workdir):
    """Refreshed, not write-once: a later mod supersedes earlier evidence."""
    write_snap(
        "consolidated/nasa_x_2026-07-29.csv",
        [keep(), row("A-1", "USAspending Terminations", TRUNCATION)],
    )
    write_snap(
        "consolidated/nasa_x_2026-07-30.csv",
        [keep(), row("A-1", "USAspending Terminations", CONVENIENCE)],
    )
    bml.build()

    assert ledger()["A-1"]["Detection Evidence"] == CONVENIENCE


def test_a_blank_day_does_not_erase_a_recorded_detection(workdir):
    """NPDV wins the row on day two and names no evidence. The award is still
    the same award, so the sentence that explained it must not be blanked."""
    write_snap(
        "consolidated/nasa_x_2026-07-29.csv",
        [keep(), row("A-1", "USAspending Terminations", CONVENIENCE)],
    )
    write_snap(
        "consolidated/nasa_x_2026-07-30.csv",
        [keep(), row("A-1", "NASA Procurement Data View")],
    )
    bml.build()

    assert ledger()["A-1"]["Detection Evidence"] == CONVENIENCE


# --- archived snapshots predating the column -------------------------------


def test_snapshots_without_the_column_build_an_empty_detection(workdir):
    """Every snapshot before 2026-07-30 lacks it; the build must not crash."""
    legacy = [c for c in COLS if c != "Detection Evidence"]
    write_snap(
        "consolidated/nasa_x_2026-07-29.csv",
        [keep(), row("A-1", "USAspending Terminations", CONVENIENCE)],
        fieldnames=legacy,
    )
    bml.build()

    led = ledger()
    assert led["A-1"]["Detection Evidence"] == ""
    assert led["A-1"]["Tracking Status"] == "currently_flagged"


def test_a_column_less_snapshot_cannot_clobber_a_populated_detection(workdir):
    """The archive is read oldest-first, but a replayed old file could land
    after a new one; a missing column reads as blank and blank never wins."""
    write_snap(
        "consolidated/nasa_x_2026-07-29.csv",
        [keep(), row("A-1", "USAspending Terminations", CONVENIENCE)],
    )
    write_snap(
        "consolidated/nasa_x_2026-07-30.csv",
        [keep(), row("A-1", "USAspending Terminations")],
        fieldnames=[c for c in COLS if c != "Detection Evidence"],
    )
    bml.build()

    assert ledger()["A-1"]["Detection Evidence"] == CONVENIENCE


def test_the_new_column_is_the_only_thing_that_changed(workdir):
    """Adding Detection must not perturb any other ledger field.

    Builds the same history twice - once from snapshots carrying the column,
    once from snapshots without it - and requires every other cell to match.
    """
    rows = [
        keep(),
        row(
            "A-1",
            "USAspending Terminations",
            CONVENIENCE,
            **{"Current End Date": "2026-05-06"},
        ),
        row(
            "G-1",
            "Local USAspending Mirror",
            CLAWBACK,
            **{"Current Obligated Amount": "448257"},
        ),
    ]
    write_snap("consolidated/nasa_x_2026-07-30.csv", rows)
    bml.build()
    with_detection = ledger()

    write_snap(
        "consolidated/nasa_x_2026-07-30.csv",
        rows,
        fieldnames=[c for c in COLS if c != "Detection Evidence"],
    )
    bml.build()
    without_detection = ledger()

    assert set(with_detection) == set(without_detection)
    for aid, rec in with_detection.items():
        other = without_detection[aid]
        detection_fields = {"Detection Evidence", "Primary Detection Method"}
        assert {k: v for k, v in rec.items() if k not in detection_fields} == {
            k: v for k, v in other.items() if k not in detection_fields
        }
    assert with_detection["A-1"]["Detection Evidence"] == CONVENIENCE
    assert without_detection["A-1"]["Detection Evidence"] == ""


# --- incremental path ------------------------------------------------------


def test_update_path_carries_detection(workdir):
    write_snap("consolidated/nasa_x_2026-07-29.csv", [keep()])
    bml.build()

    write_snap(
        "consolidated/nasa_x_2026-07-30.csv",
        [keep(), row("A-1", "USAspending Terminations", CONVENIENCE)],
    )
    bml.build(update_only=True)

    assert ledger()["A-1"]["Detection Evidence"] == CONVENIENCE


def test_update_path_refreshes_a_detection_already_in_the_ledger(workdir):
    write_snap(
        "consolidated/nasa_x_2026-07-29.csv",
        [keep(), row("A-1", "USAspending Terminations", TRUNCATION)],
    )
    bml.build()

    write_snap(
        "consolidated/nasa_x_2026-07-30.csv",
        [keep(), row("A-1", "USAspending Terminations", CONVENIENCE)],
    )
    bml.build(update_only=True)

    assert ledger()["A-1"]["Detection Evidence"] == CONVENIENCE


def test_both_build_paths_agree_on_detection(workdir):
    write_snap(
        "consolidated/nasa_x_2026-07-29.csv",
        [keep(), row("A-1", "USAspending Terminations", TRUNCATION)],
    )
    write_snap(
        "consolidated/nasa_x_2026-07-30.csv",
        [keep(), row("A-1", "USAspending Terminations", CONVENIENCE)],
    )
    bml.build()
    after_full = ledger()

    bml.build(update_only=True)
    after_update = ledger()

    assert {aid: r["Detection Evidence"] for aid, r in after_full.items()} == {
        aid: r["Detection Evidence"] for aid, r in after_update.items()
    }


def test_a_pre_detection_ledger_read_back_on_update_gains_the_column(workdir):
    """The stored ledger has no Detection header until the first build after
    this change; reading it back must default rather than KeyError."""
    write_snap("consolidated/nasa_x_2026-07-29.csv", [keep()])
    bml.build()

    with open(bml.LEDGER_PATH, encoding="utf-8") as fh:
        stored = list(csv.DictReader(fh))
    legacy_columns = [c for c in bml.LEDGER_COLUMNS if c != "Detection Evidence"]
    with open(bml.LEDGER_PATH, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=legacy_columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(stored)

    write_snap(
        "consolidated/nasa_x_2026-07-30.csv",
        [keep(), row("A-1", "USAspending Terminations", CONVENIENCE)],
    )
    bml.build(update_only=True)

    led = ledger()
    assert led["KEEP-1"]["Detection Evidence"] == ""
    assert led["A-1"]["Detection Evidence"] == CONVENIENCE
