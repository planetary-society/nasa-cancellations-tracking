"""Output row dataclasses, column order, and CSV read/write helpers.

Field order IS column order: there is no separate header list to drift out of
sync with the dataclass. Every value renders to text the same way whichever row
type it sits in, so `git diff` on an output file shows real changes only.
"""

import csv
import typing
from dataclasses import dataclass, fields
from datetime import date
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TerminationRow:
    """One accepted termination (output/terminations.csv, and the part files)."""

    award_key: str
    award_id: str
    generated_award_id: str
    award_type: str  # contract | idv | grant
    award_type_code: str = ""  # the explicit USASpending type code ("A", "IDV_C", "02", ...)
    recipient_name: str = ""
    action_date: date | None = None
    action_type: str = ""  # F | N | ""
    modification_number: str = ""
    transaction_amount: Decimal | None = None
    transaction_description: str = ""
    award_description: str = ""  # the award's current USASpending summary
    # The award-level recipient and place-of-performance locations; see
    # criteria.Location for why a POP has no address columns.
    recipient_address1: str = ""
    recipient_address2: str = ""
    recipient_city: str = ""
    recipient_state: str = ""
    recipient_zip: str = ""
    recipient_district: str = ""  # congressional district code; current-vintage via the mirror
    pop_city: str = ""
    pop_state: str = ""
    pop_zip: str = ""
    pop_district: str = ""
    total_obligated: Decimal | None = None
    total_potential_value: Decimal | None = None  # base-and-all-options; blank for grants
    detected_by: str = ""  # action_code | description | both
    sources: str = ""  # api | mirror | api;mirror
    override_status: str = ""


@dataclass(frozen=True, slots=True)
class DogeClaimRow:
    """One DOGE "wall of receipts" claim plus factual USASpending status."""

    claim_type: str
    doge_award_id: str
    recipient: str = ""
    doge_value: Decimal | None = None
    doge_savings: Decimal | None = None
    doge_claim_date: date | None = None
    doge_status: str = ""
    source_url: str = ""
    usaspending_found: bool = False
    generated_award_id: str = ""
    award_type: str = ""
    award_type_code: str = ""  # the explicit USASpending type code, when found
    has_explicit_termination: bool = False
    # A transaction vacating the termination exists. Facts, not a verdict: the
    # pair (terminated, vacated) lets the reader see a court-undone termination
    # without cross-referencing terminations.csv, which excludes such awards.
    termination_vacated: bool = False
    latest_action_date: date | None = None
    latest_action_type: str = ""
    latest_description: str = ""
    current_obligation: Decimal | None = None
    current_end_date: date | None = None
    # The award-level locations, present only when the award was found.
    recipient_address1: str = ""
    recipient_address2: str = ""
    recipient_city: str = ""
    recipient_state: str = ""
    recipient_zip: str = ""
    recipient_district: str = ""
    pop_city: str = ""
    pop_state: str = ""
    pop_zip: str = ""
    pop_district: str = ""
    # current_obligation above already carries the award's total obligation, so
    # only the potential value is added here rather than a duplicate column.
    total_potential_value: Decimal | None = None
    checked_date: date | None = None


@dataclass(frozen=True, slots=True)
class PopChangeRow:
    """One award whose period of performance was pulled back (a lead sheet, not a verdict)."""

    award_id: str
    generated_award_id: str
    award_type: str
    award_type_code: str = ""  # the explicit USASpending type code
    recipient_name: str = ""
    award_description: str = ""  # the award's current USASpending summary
    # As on TerminationRow: the award-level locations.
    recipient_address1: str = ""
    recipient_address2: str = ""
    recipient_city: str = ""
    recipient_state: str = ""
    recipient_zip: str = ""
    recipient_district: str = ""
    pop_city: str = ""
    pop_state: str = ""
    pop_zip: str = ""
    pop_district: str = ""
    total_obligated: Decimal | None = None
    total_potential_value: Decimal | None = None
    original_end_date: date | None = None
    max_end_date: date | None = None
    current_end_date: date | None = None
    days_shortened: int = 0
    last_action_date: date | None = None
    transaction_count: int = 0


@dataclass(frozen=True, slots=True)
class CancellationAwardsByFiscalYearRow:
    """Adjudicated terminations for convenience anchored in one fiscal year (never signals)."""

    fiscal_year: int
    terminated_awards: int


def _render(value) -> str:
    """One value as CSV text: ISO dates, plain Decimals, "" for None, one line."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")  # no exponent, no $, no thousands separators
    # Descriptions arrive with embedded newlines; a one-line cell keeps the
    # committed CSV diffable.
    return " ".join(str(value).split())


def _parse(text: str, declared):
    """Invert `_render` for one field, given its declared type."""
    # The type behind an `X | None` annotation, or the annotation itself.
    args = [arg for arg in typing.get_args(declared) if arg is not type(None)]
    base = args[0] if len(args) == 1 else declared
    if text == "":
        return "" if base is str else None
    if base is bool:
        return text == "true"
    if base is date:
        return date.fromisoformat(text)
    if base is Decimal:
        return Decimal(text)
    if base is int:
        return int(text)
    return text


def write_csv(path, rows) -> None:
    """Write pre-sorted rows to `path`. Column order is field order; UTF-8, "\\n"."""
    rows = list(rows)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    columns = [f.name for f in fields(rows[0])]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([_render(getattr(row, column)) for column in columns])


def read_csv(path, row_type) -> list:
    """Read rows written by `write_csv` back into `row_type` instances.

    Field order and field types both come from `dataclasses.fields`, the same
    source `write_csv` uses - this module declares no `from __future__ import
    annotations`, so `f.type` is the live type object rather than a string.
    """
    path = Path(path)
    # write_csv renders "no rows" as an empty file, which is exactly this.
    if not path.exists() or path.stat().st_size == 0:
        return []
    declared = {f.name: f.type for f in fields(row_type)}
    with path.open(encoding="utf-8", newline="") as handle:
        return [
            row_type(**{name: _parse(record[name], hint) for name, hint in declared.items()})
            for record in csv.DictReader(handle)
        ]
