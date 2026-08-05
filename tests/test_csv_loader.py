"""The shared CSV read contract.

Every reader of a repo-owned CSV goes through `contract_query.read_rows`, so
this is where a stored header is translated into the vocabulary the code uses.
These tests pin that translation, because the failure mode it guards against is
silent: a reader that keeps using a stored name gets `None` from `row.get()`
rather than an error, and a guard built on that comparison quietly stops
guarding.
"""

import glob
import os

import pytest

from contract_query import load_snapshot, read_rows

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_read_rows_returns_stored_header_when_no_aliases(tmp_path, write_csv):
    path = str(tmp_path / "snap.csv")
    write_csv(
        path, ["Award ID", "End Date"], [{"Award ID": "A-1", "End Date": "2026-01-01"}]
    )

    names, rows = read_rows(path)

    assert names == ["Award ID", "End Date"]
    assert rows == [{"Award ID": "A-1", "End Date": "2026-01-01"}]


def test_aliases_rekey_rows_not_just_the_header(tmp_path, write_csv):
    """The rename must reach the rows, or callers silently read blanks."""
    path = str(tmp_path / "snap.csv")
    write_csv(
        path, ["Award ID", "End Date"], [{"Award ID": "A-1", "End Date": "2026-01-01"}]
    )

    names, rows = read_rows(path, aliases={"End Date": "Current End Date"})

    assert names == ["Award ID", "Current End Date"]
    assert rows[0]["Current End Date"] == "2026-01-01"
    assert "End Date" not in rows[0]


def test_unmapped_columns_pass_through(tmp_path, write_csv):
    path = str(tmp_path / "snap.csv")
    write_csv(path, ["Award ID", "Recipient"], [{"Award ID": "A-1", "Recipient": "X"}])

    names, rows = read_rows(path, aliases={"End Date": "Current End Date"})

    assert names == ["Award ID", "Recipient"]
    assert rows[0]["Recipient"] == "X"


def test_alias_map_is_idempotent(tmp_path, write_csv):
    """Applying the map to an already-current header must be a no-op.

    Ledger and sidecar files are rewritten in place by their producers, so the
    same map is applied to files that have already been migrated. If a new name
    were itself a key in the map, a second pass would rename it again.
    """
    aliases = {"End Date": "Current End Date", "Sources": "Flagged By"}
    assert not set(aliases.values()) & set(aliases)

    path = str(tmp_path / "snap.csv")
    write_csv(
        path,
        ["Award ID", "Current End Date"],
        [{"Award ID": "A-1", "Current End Date": "2026-01-01"}],
    )

    names, rows = read_rows(path, aliases=aliases)

    assert names == ["Award ID", "Current End Date"]
    assert rows[0]["Current End Date"] == "2026-01-01"


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

    with pytest.raises(RuntimeError, match="collapses distinct columns"):
        read_rows(
            path,
            aliases={
                "Verified Date": "Automated Verdict Date",
                "Auto Verified Date": "Automated Verdict Date",
            },
        )


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


def test_archived_snapshot_header_generations_are_known():
    """Every archived snapshot must match a header generation we know about.

    A full ledger rebuild replays every file in consolidated/, so an unknown
    header generation is exactly the thing that would need an alias entry. This
    fails loudly when one appears rather than letting the rebuild read blanks.
    """
    known = {
        # Original shape.
        "Source,District,Recipient,Award ID,Latest Modification Number,"
        "Latest Modification Date,Start Date,End Date,Award Amount,Total Outlays,"
        "Description,Business Categories,URL",
        # Claim columns added.
        "Source,District,Recipient,Award ID,Latest Modification Number,"
        "Latest Modification Date,Start Date,End Date,Award Amount,Total Outlays,"
        "Description,Business Categories,URL,Claiming Source,Claimed Status,"
        "Claimed Savings,Claim Date",
        # Latest Modification Date replaced by the transaction-history group,
        # plus Detection.
        "Source,District,Recipient,Award ID,Latest Modification Number,"
        "First Action Type,First Action Type Description,First Action Date,"
        "Latest Action Type,Latest Action Type Description,Latest Action Date,"
        "Termination Modification Number,Termination Action Date,"
        "Closeout Modification Number,Closeout Action Date,Start Date,End Date,"
        "Initial Reported End Date,Award Amount,Total Outlays,Description,"
        "Detection,Business Categories,URL,Claiming Source,Claimed Status,"
        "Claimed Savings,Claim Date",
    }

    pattern = os.path.join(
        REPO_ROOT, "consolidated", "nasa_contract_cancellations_*.csv"
    )
    paths = sorted(glob.glob(pattern))
    if not paths:
        pytest.skip("no archived snapshots checked out")

    unknown = {}
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as fh:
            header = fh.readline().strip().lstrip("﻿")
        if header not in known:
            unknown.setdefault(header, []).append(os.path.basename(path))

    assert not unknown, "unrecognised snapshot header generation(s): " + "; ".join(
        f"{files[0]} (+{len(files) - 1} more): {header}"
        for header, files in unknown.items()
    )
