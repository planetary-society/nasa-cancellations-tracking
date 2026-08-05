"""Invariants every stored-name translation table must hold.

These are properties of the tables themselves, checked once for all of them,
because the ways an alias table goes wrong are silent: a self-referential entry
corrupts a file that was already migrated, and two entries pointing at one name
drop a column's data with no error.
"""

import os

import pytest

import csv_aliases
from utils import read_rows


@pytest.mark.parametrize("table", csv_aliases.ALL_TABLES)
def test_no_current_name_is_also_a_stored_name(table):
    """Otherwise a second read would rename an already-migrated column.

    Producers rewrite their own files, so every table is applied to files that
    already carry the current vocabulary. `{"A": "B", "B": "C"}` would turn a
    migrated B into C.
    """
    assert not set(table.values()) & set(table)


@pytest.mark.parametrize("table", csv_aliases.ALL_TABLES)
def test_no_two_stored_names_collapse_onto_one(table):
    """Two columns merging into one loses a column's data without an error."""
    current = list(table.values())
    assert len(current) == len(set(current))


def test_aliases_are_resolved_from_the_path():
    assert csv_aliases.aliases_for("verification/auto_verification.csv") is (
        csv_aliases.AUTO_VERDICTS
    )
    assert csv_aliases.aliases_for("verification/dropped_award_status.csv") is (
        csv_aliases.HUMAN_VERDICTS
    )
    assert csv_aliases.aliases_for("consolidated/master_ledger.csv") is (
        csv_aliases.LEDGER
    )


def test_the_two_verified_date_columns_get_different_tables():
    """The reason the tables are keyed by file rather than merged into one.

    `Verified Date` is when a human recorded a verdict in one file and when
    automation last changed one in the other. A single flat table could not
    translate both.
    """
    human = csv_aliases.aliases_for("verification/dropped_award_status.csv")
    auto = csv_aliases.aliases_for("verification/auto_verification.csv")

    assert human is not auto


def test_any_consolidated_csv_but_the_ledger_is_a_snapshot():
    """Snapshot filenames have changed twice; the directory has not."""
    assert csv_aliases.is_snapshot("consolidated/nasa_contract_cancellations_x.csv")
    assert csv_aliases.is_snapshot("consolidated/nasa_cancellations_20250405.csv")
    assert not csv_aliases.is_snapshot("consolidated/master_ledger.csv")
    assert not csv_aliases.is_snapshot("verification/auto_verification.csv")


def test_an_unknown_file_gets_no_translation():
    assert csv_aliases.aliases_for("data/some_export.csv") == {}


def test_read_rows_applies_the_table_for_the_path(tmp_path, write_csv, monkeypatch):
    """The default is a lookup, so a caller cannot forget to pass one."""
    monkeypatch.setitem(csv_aliases.AUTO_VERDICTS, "Verified Date", "Verdict Date")
    path = str(tmp_path / "verification" / "auto_verification.csv")
    os.makedirs(os.path.dirname(path))
    write_csv(path, ["Award ID", "Verified Date"], [{"Award ID": "A-1"}])

    assert read_rows(path)[0].keys() == {"Award ID", "Verdict Date"}
    # An explicit empty table is how a caller opts out.
    assert read_rows(path, aliases={})[0].keys() == {"Award ID", "Verified Date"}
