"""The shared CSV read contract.

Every reader of a repo-owned CSV goes through `utils.read_rows`, so this is
where a stored header is translated into the vocabulary the code uses. These
tests pin that translation, because the failure mode it guards against is
silent: a reader that keeps using a stored name gets `None` from `row.get()`
rather than an error, and a guard built on that comparison quietly stops
guarding.
"""

import os

import pytest

import build_master_ledger
import search
from contract_query import load_snapshot
from utils import read_header, read_rows

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_read_rows_returns_rows_keyed_by_stored_header(tmp_path, write_csv):
    path = str(tmp_path / "snap.csv")
    write_csv(
        path, ["Award ID", "End Date"], [{"Award ID": "A-1", "End Date": "2026-01-01"}]
    )

    assert read_rows(path) == [{"Award ID": "A-1", "End Date": "2026-01-01"}]


def test_aliases_rekey_rows_not_just_the_header(tmp_path, write_csv):
    """The rename must reach the rows, or callers silently read blanks."""
    path = str(tmp_path / "snap.csv")
    write_csv(
        path, ["Award ID", "End Date"], [{"Award ID": "A-1", "End Date": "2026-01-01"}]
    )

    rows = read_rows(path, aliases={"End Date": "Current End Date"})

    assert rows[0]["Current End Date"] == "2026-01-01"
    assert "End Date" not in rows[0]


def test_unmapped_columns_pass_through(tmp_path, write_csv):
    path = str(tmp_path / "snap.csv")
    write_csv(path, ["Award ID", "Recipient"], [{"Award ID": "A-1", "Recipient": "X"}])

    rows = read_rows(path, aliases={"End Date": "Current End Date"})

    assert rows[0]["Recipient"] == "X"


def test_applying_aliases_to_an_already_migrated_file_is_a_no_op(tmp_path, write_csv):
    """Producers rewrite their own files, so the map meets migrated headers.

    Reading a file that already carries the current vocabulary must leave it
    alone. This holds as long as no new name is itself a key in the map, which
    is asserted directly of the real table wherever one is defined.
    """
    aliases = {"End Date": "Current End Date"}
    path = str(tmp_path / "snap.csv")
    write_csv(
        path,
        ["Award ID", "Current End Date"],
        [{"Award ID": "A-1", "Current End Date": "2026-01-01"}],
    )

    assert read_rows(path, aliases=aliases) == read_rows(path)


def test_collapsing_two_columns_onto_one_raises(tmp_path, write_csv):
    """A dict-based rename can silently merge two columns; it must not.

    `Verified Date` and `Auto Verified Date` are the live example - they live in
    different files and mean different things, so a map that carried both keys
    could collapse them and drop a column's data with no error.
    """
    path = str(tmp_path / "sidecar.csv")
    write_csv(
        path,
        ["Verified Date", "Auto Verified Date"],
        [{"Verified Date": "2026-01-01", "Auto Verified Date": "2026-02-02"}],
    )

    with pytest.raises(RuntimeError, match="duplicate column name"):
        read_rows(
            path,
            aliases={
                "Verified Date": "Automated Verdict Date",
                "Auto Verified Date": "Automated Verdict Date",
            },
        )


def test_required_columns_are_checked(tmp_path, write_csv):
    path = str(tmp_path / "sidecar.csv")
    write_csv(path, ["Award ID"], [{"Award ID": "A-1"}])

    with pytest.raises(RuntimeError, match="missing column"):
        read_rows(path, columns=["Award ID", "Last Checked Date"])


def test_exact_columns_rejects_reordering(tmp_path, write_csv):
    """One sidecar pins column order, so a reordered header must not pass."""
    path = str(tmp_path / "sidecar.csv")
    write_csv(path, ["Action Date", "Award ID"], [{"Award ID": "A-1"}])

    with pytest.raises(RuntimeError, match="expected"):
        read_rows(path, columns=["Award ID", "Action Date"], exact_columns=True)


def test_a_byte_order_mark_does_not_change_the_first_column_name(tmp_path):
    """dropped_award_status.csv is hand-edited; an editor may add a BOM.

    It was previously read as utf-8 in build_master_ledger and utf-8-sig in
    validate_snapshot, so the same file parsed two different ways.
    """
    path = tmp_path / "human.csv"
    path.write_text("﻿Award ID,Status\nA-1,excluded_by_design\n", encoding="utf-8")

    assert read_rows(str(path)) == [{"Award ID": "A-1", "Status": "excluded_by_design"}]


def test_load_snapshot_forwards_aliases_and_skips_blank_ids(tmp_path, write_csv):
    path = str(tmp_path / "snap.csv")
    write_csv(
        path,
        ["Award ID", "Sources"],
        [{"Award ID": "A-1", "Sources": "DOGE"}, {"Award ID": "", "Sources": "NPDV"}],
    )

    snap = load_snapshot(path, aliases={"Sources": "Flagged By"})

    assert list(snap) == ["A-1"]
    assert snap["A-1"]["Flagged By"] == "DOGE"


def test_every_replayed_snapshot_uses_known_column_names():
    """No archived snapshot may carry a column the code does not know about.

    A full rebuild replays every file `snapshot_files()` returns, so an unknown
    column is exactly what would need an alias entry. Asserted against the
    code's own column list rather than transcribed headers, so adding a column
    to SNAPSHOT_COLUMNS does not require editing this test - only a genuinely
    unrecognised name fails.

    Column *sets*, not ordered headers: read_rows keys rows by name, so column
    order is not something the loader can get wrong.
    """
    known = set(search.SNAPSHOT_COLUMNS) | {
        # Removed in favour of the Latest Action Date pairing; still present in
        # the 398 snapshots archived before that change.
        "Latest Modification Date",
    }

    cwd = os.getcwd()
    os.chdir(REPO_ROOT)
    try:
        paths = [path for _, path in build_master_ledger.snapshot_files()]
    finally:
        os.chdir(cwd)
    if not paths:
        pytest.skip("no archived snapshots checked out")

    for path in paths:
        unknown = set(read_header(path)) - known
        assert not unknown, f"{os.path.basename(path)} has unknown column(s): {unknown}"
