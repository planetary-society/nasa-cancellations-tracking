"""CSV rendering is stable, plain, and round-trips."""

from datetime import date
from decimal import Decimal

import pytest

from nasatrack.schema import (
    DogeClaimRow,
    PopChangeRow,
    TerminationRow,
    read_csv,
    write_csv,
)
from tests.test_merge import row as a_termination_row

TERMINATION_COLUMNS = [
    "award_key",
    "award_id",
    "generated_award_id",
    "award_type",
    "recipient_name",
    "action_date",
    "action_type",
    "modification_number",
    "transaction_amount",
    "transaction_description",
    "detected_by",
    "sources",
    "override_status",
]

DOGE_COLUMNS = [
    "claim_type",
    "doge_award_id",
    "recipient",
    "doge_value",
    "doge_savings",
    "doge_claim_date",
    "doge_status",
    "source_url",
    "usaspending_found",
    "generated_award_id",
    "award_type",
    "has_explicit_termination",
    "latest_action_date",
    "latest_action_type",
    "latest_description",
    "current_obligation",
    "current_end_date",
    "checked_date",
]

POP_CHANGE_COLUMNS = [
    "award_id",
    "generated_award_id",
    "award_type",
    "recipient_name",
    "original_end_date",
    "max_end_date",
    "current_end_date",
    "days_shortened",
    "last_action_date",
    "transaction_count",
]


def a_termination(**overrides):
    """One TerminationRow, built by the suite's shared row factory."""
    key = overrides.pop("award_key", "80NSSC25C0001")
    return a_termination_row(key, source=overrides.pop("sources", "api;mirror"), **overrides)


def a_doge_claim(**overrides):
    values = {
        "claim_type": "contract",
        "doge_award_id": "80NSSC25C0001",
        "recipient": "ACME AEROSPACE",
        "doge_value": Decimal("1000000"),
        "doge_savings": Decimal("250000.50"),
        "doge_claim_date": date(2025, 3, 4),
        "doge_status": "Terminated",
        "source_url": "https://example.gov/award/1",
        "usaspending_found": True,
        "generated_award_id": "CONT_AWD_80NSSC25C0001",
        "award_type": "contract",
        "has_explicit_termination": False,
        "latest_action_date": date(2025, 6, 1),
        "latest_action_type": "F",
        "latest_description": "TERMINATION FOR CONVENIENCE",
        "current_obligation": Decimal("0"),
        "current_end_date": date(2025, 9, 30),
        "checked_date": date(2026, 8, 20),
    }
    return DogeClaimRow(**{**values, **overrides})


def a_pop_change(**overrides):
    values = {
        "award_id": "80NSSC25C0001",
        "generated_award_id": "CONT_AWD_80NSSC25C0001",
        "award_type": "contract",
        "recipient_name": "ACME AEROSPACE",
        "original_end_date": date(2027, 12, 31),
        "max_end_date": date(2028, 6, 30),
        "current_end_date": date(2025, 9, 30),
        "days_shortened": 1004,
        "last_action_date": date(2025, 6, 1),
        "transaction_count": 12,
    }
    return PopChangeRow(**{**values, **overrides})


@pytest.mark.parametrize(
    ("rows", "columns"),
    [
        ([a_termination()], TERMINATION_COLUMNS),
        ([a_doge_claim()], DOGE_COLUMNS),
        ([a_pop_change()], POP_CHANGE_COLUMNS),
    ],
)
def test_declared_columns_in_order(tmp_path, rows, columns):
    path = tmp_path / "rows.csv"
    write_csv(path, rows)
    assert path.read_text(encoding="utf-8").splitlines()[0] == ",".join(columns)


def test_value_rendering(tmp_path):
    path = tmp_path / "terminations.csv"
    write_csv(
        path,
        [
            a_termination(
                transaction_amount=Decimal("-1234567.89"),
                transaction_description="LINE ONE\nLINE TWO\r\nLINE THREE",
            )
        ],
    )
    body = path.read_text(encoding="utf-8").splitlines()[1]
    assert "2025-06-01" in body
    assert "-1234567.89" in body  # plain: no $, no thousands separators
    assert "LINE ONE LINE TWO LINE THREE" in body
    assert "\n" not in body


def test_empty_and_none_render_as_blank(tmp_path):
    path = tmp_path / "terminations.csv"
    write_csv(path, [a_termination(action_date=None, transaction_amount=None, action_type="")])
    row = path.read_text(encoding="utf-8").splitlines()[1].split(",")
    assert row[5] == ""  # action_date
    assert row[6] == ""  # action_type
    assert row[8] == ""  # transaction_amount


def test_booleans_render_as_words(tmp_path):
    path = tmp_path / "doge.csv"
    write_csv(path, [a_doge_claim(usaspending_found=True, has_explicit_termination=False)])
    row = path.read_text(encoding="utf-8").splitlines()[1].split(",")
    assert row[8] == "true"
    assert row[11] == "false"


def test_decimal_never_uses_exponent_notation(tmp_path):
    path = tmp_path / "doge.csv"
    write_csv(path, [a_doge_claim(doge_value=Decimal("1E+7"))])
    assert "1E+7" not in path.read_text(encoding="utf-8")
    assert "10000000" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("rows", "row_type"),
    [
        (
            [
                a_termination(),
                a_termination(
                    award_key="B",
                    action_date=None,
                    transaction_amount=None,
                    override_status="continued",
                ),
            ],
            TerminationRow,
        ),
        ([a_doge_claim(), a_doge_claim(doge_award_id="X", checked_date=None)], DogeClaimRow),
        ([a_pop_change(), a_pop_change(award_id="X", days_shortened=91)], PopChangeRow),
    ],
)
def test_round_trip(tmp_path, rows, row_type):
    path = tmp_path / "rows.csv"
    write_csv(path, rows)
    assert read_csv(path, row_type) == rows


def test_round_trip_after_newline_collapse(tmp_path):
    path = tmp_path / "rows.csv"
    write_csv(path, [a_termination(transaction_description="LINE ONE\nLINE TWO")])
    (parsed,) = read_csv(path, TerminationRow)
    assert parsed.transaction_description == "LINE ONE LINE TWO"


def test_empty_rows_reads_back_empty(tmp_path):
    path = tmp_path / "rows.csv"
    write_csv(path, [])
    assert read_csv(path, TerminationRow) == []


def test_missing_file_reads_back_empty(tmp_path):
    assert read_csv(tmp_path / "absent.csv", TerminationRow) == []
