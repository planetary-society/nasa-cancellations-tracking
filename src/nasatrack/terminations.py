"""Txn-to-row conversion, dedupe, deterministic part-file merge, and human override application.

The two doors run on different cadences - the API daily in CI, the mirror
locally about once a month - so neither can write the published file on its
own. Each writes a PART, and this module is the pure function that turns the
two parts plus the human override file into `output/terminations.csv`. No
network, no clock, no file writing beyond the override reader: `cli.py` owns
the I/O, and everything here can be tested with rows built in memory.
"""

import csv
from dataclasses import replace
from datetime import date
from pathlib import Path

from nasatrack import criteria
from nasatrack.criteria import Txn
from nasatrack.schema import CancellationAwardsByFiscalYearRow, TerminationRow

# ---------------------------------------------------------------------------
# One transaction as one output row
# ---------------------------------------------------------------------------


def txn_to_row(txn: Txn) -> TerminationRow:
    """The accepted termination transaction as its published row."""
    return TerminationRow(
        award_key=txn.award_key,
        award_id=txn.award_id,
        generated_award_id=txn.generated_award_id,
        award_type=txn.award_type,
        award_type_code=txn.award_type_code,
        recipient_name=txn.recipient_name,
        action_date=txn.action_date,
        action_type=txn.action_type,
        modification_number=txn.modification_number,
        transaction_amount=txn.amount,
        transaction_description=txn.description,
        award_description=txn.award_description,
        recipient_address1=txn.recipient_location.address1,
        recipient_address2=txn.recipient_location.address2,
        recipient_city=txn.recipient_location.city,
        recipient_state=txn.recipient_location.state,
        recipient_zip=txn.recipient_location.zip,
        recipient_district=txn.recipient_location.district,
        pop_city=txn.pop_location.city,
        pop_state=txn.pop_location.state,
        pop_zip=txn.pop_location.zip,
        pop_district=txn.pop_location.district,
        total_obligated=txn.total_obligated,
        total_potential_value=txn.total_potential_value,
        detected_by=criteria.detected_by(txn),
        # A part file names its own door; the merge is what ever writes
        # "api;mirror".
        sources=txn.source,
        override_status="",
    )


# ---------------------------------------------------------------------------
# The merge
# ---------------------------------------------------------------------------


def _union_sources(left: str, right: str) -> str:
    """The doors behind a merged row, sorted and ";"-joined."""
    return ";".join(sorted({part for value in (left, right) for part in value.split(";") if part}))


def _has_mirror(row: TerminationRow) -> bool:
    """True when the mirror door is one of the doors behind this row."""
    return "mirror" in row.sources.split(";")


def source_count(row: TerminationRow) -> int:
    """How many doors reported this row. The ";" join encoding stays in here."""
    return len([part for part in row.sources.split(";") if part])


def order(rows) -> list[TerminationRow]:
    """The committed sort order: newest action first, ties broken by award_key.

    A missing action_date sorts last rather than raising; the doors only ever
    accept dated transactions, so this is a guard, not a case.
    """
    ordered = sorted(rows, key=lambda row: row.award_key)
    ordered.sort(key=lambda row: row.action_date or date.min, reverse=True)
    return ordered


def _collapse_piid_keys(rows: dict[str, TerminationRow]) -> None:
    """Fold a `PIID:<id>`-keyed row into the generated-id row for the same award.

    Mutates `rows` in place.

    Some IDV transactions carry no generated award id, so both doors fall back
    to the namespaced PIID key. When the OTHER door reported that same award
    with its generated id, the two keys are one award and the union by key
    cannot see it. Award type has to match too: a PIID and a FAIN can be the
    same string on unrelated awards, which is why the fallback is namespaced in
    the first place.

    The surviving row keeps the generated-id row's IDENTITY (its key and
    generated award id, which is what the fallback row lacks) but `merge`'s
    field precedence: the mirror's values win wherever the two doors disagree.
    Keeping the API's fields on a row stamped `api;mirror` would invert that
    rule for exactly the awards - the ones with no generated id - where the
    doors are most likely to disagree.
    """
    by_identity = {
        (row.award_id, row.award_type): key
        for key, row in rows.items()
        if not criteria.is_fallback_key(key)
    }
    for key, row in list(rows.items()):
        if not criteria.is_fallback_key(key):
            continue
        target = by_identity.get((row.award_id, row.award_type))
        if target is None:
            continue
        kept = rows[target]
        if _has_mirror(row) and not _has_mirror(kept):
            kept = replace(
                row, award_key=kept.award_key, generated_award_id=kept.generated_award_id
            )
        rows[target] = replace(kept, sources=_union_sources(rows[target].sources, row.sources))
        del rows[key]


def merge(api_rows, mirror_rows) -> list[TerminationRow]:
    """The two part files as one deduped, deterministically ordered row set.

    Union by award_key. Where both doors report the same award the mirror wins
    every shared field - it is the authoritative published record, where the
    API door is the recency one - and `sources` records both.
    """
    merged: dict[str, TerminationRow] = {row.award_key: row for row in api_rows}
    for row in mirror_rows:
        existing = merged.get(row.award_key)
        merged[row.award_key] = (
            row
            if existing is None
            else replace(row, sources=_union_sources(existing.sources, row.sources))
        )
    _collapse_piid_keys(merged)
    return order(merged.values())


# ---------------------------------------------------------------------------
# Human overrides
# ---------------------------------------------------------------------------

# `verification/dropped_award_status.csv` is human-owned and read-only to this
# code. Its header predates the new schema, so it is read with the plain csv
# module rather than schema.read_csv.
OVERRIDE_ID_COLUMN = "Award ID"
OVERRIDE_STATUS_COLUMN = "Tracking Status"

# The statuses that say "this is not an active cancellation": out of scope,
# vacated by a court, or continued after the flag. Every other status the file
# uses (closed_out, descoped, needs_manual_review) is carried on a row that
# stays - though `descoped` is more than an annotation now: `partition_descoped`
# below reads it and routes the row to descoped.csv instead of terminations.csv.
EXCLUDING_STATUSES = frozenset({"excluded_by_design", "vacated", "continued"})


def load_overrides(path) -> dict[str, str]:
    """The human verification file as {award id: tracking status}, or {} if absent."""
    path = Path(path)
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as handle:
        return {
            record[OVERRIDE_ID_COLUMN].strip(): (record.get(OVERRIDE_STATUS_COLUMN) or "").strip()
            for record in csv.DictReader(handle)
            if (record.get(OVERRIDE_ID_COLUMN) or "").strip()
        }


def apply_overrides(rows, overrides: dict[str, str]) -> tuple[list[TerminationRow], list[str]]:
    """Rows with human judgement applied, plus warnings for overrides that matched nothing.

    An override removes a row or annotates it; it never creates one. An entry
    naming an award the doors did not report is a warning rather than a silent
    no-op, because a stale override usually means the award's detection changed
    and nobody noticed.
    """
    kept: list[TerminationRow] = []
    matched: set[str] = set()
    for row in rows:
        hits = [identity for identity in (row.award_id, row.award_key) if identity in overrides]
        matched.update(hits)
        status = overrides[hits[0]] if hits else ""
        if status in EXCLUDING_STATUSES:
            continue
        kept.append(replace(row, override_status=status) if status else row)
    warnings = [
        f"unmatched override: {identity} ({status})"
        for identity, status in overrides.items()
        if identity not in matched
    ]
    return kept, warnings


# ---------------------------------------------------------------------------
# De-scoped awards, published apart
# ---------------------------------------------------------------------------

# The human status that says "work was pulled out, the award lives on".
DESCOPED_STATUS = "descoped"


def is_descoped(row: TerminationRow) -> bool:
    """True when this row is a partial de-scope rather than a termination.

    Human judgement wins in BOTH directions and is tested first. A `descoped`
    override moves the row even when its transaction carries an F code -
    NNG09FA40C is the case: an EO 14148 mod de-scoped the DEI work off an IRIS
    contract that went on collecting new obligations, and only a human reading
    the award could see that. Any OTHER explicit status (termination_confirmed,
    closed_out, needs_manual_review) is a human who looked at this award and did
    not call it a de-scope, so it pins the row to terminations.csv and the
    classifier below never runs.

    With no override the row is classified from its language, but never over a
    standalone F code: "F" is TERMINATE FOR CONVENIENCE (COMPLETE OR PARTIAL) -
    the reported termination act itself - so "FINALIZE THE PARTIAL TERMINATION
    SETTLEMENT" on an F-coded transaction is a termination that happened to be
    partial, not a de-scope. Grants and prose-only contracts have no such code
    to beat, which is what `criteria.has_standalone_termination_code` is
    checking for.

    The text read is `transaction_description` alone - the detection basis, the
    text that made this award a row at all. `award_description` is the award's
    current USASpending summary, written for the award as a whole and refreshed
    long after the action; letting it classify would let today's summary
    reclassify last year's termination.
    """
    if row.override_status == DESCOPED_STATUS:
        return True
    if row.override_status:
        return False
    return criteria.is_descope(
        row.transaction_description
    ) and not criteria.has_standalone_termination_code(row.award_type, row.action_type)


def partition_descoped(rows) -> tuple[list[TerminationRow], list[TerminationRow]]:
    """(terminations, descoped), preserving the committed order of both.

    Runs after `apply_overrides`, so rows an excluding status already dropped
    never reach either side. Both lists come out in the order `merge` put them
    in: splitting a sorted list keeps each half sorted.
    """
    kept: list[TerminationRow] = []
    descoped: list[TerminationRow] = []
    for row in rows:
        (descoped if is_descoped(row) else kept).append(row)
    return kept, descoped


# ---------------------------------------------------------------------------
# The historical fiscal-year report
# ---------------------------------------------------------------------------


def count_by_fiscal_year(
    txns, overrides: dict[str, str] | None = None, *, start_fiscal_year: int, today: date
) -> list[CancellationAwardsByFiscalYearRow]:
    """Distinct terminated awards per fiscal year, adjudicated as terminations.csv is.

    The same pipeline the published list goes through - `accept_award` with the
    window widened to the report's start, the human overrides, the de-scope
    routing - with each award counted once, in the fiscal year of its anchor
    transaction. `txns`, `overrides` and `today` all come from the caller:
    this module reads no network, no files and no clock.

    Zero-filled through `today`'s fiscal year, whose count is a partial-year
    figure by construction.
    """
    window_start = date(start_fiscal_year - 1, 10, 1)
    anchors = (
        criteria.accept_award(group, window_start=window_start)
        for group in criteria.group_by_award(txns).values()
    )
    rows = [txn_to_row(anchor) for anchor in anchors if anchor is not None]
    rows, _ = apply_overrides(rows, overrides or {})
    rows, _ = partition_descoped(rows)

    counts: dict[int | None, int] = {}
    for row in rows:
        fy = criteria.fiscal_year(row.action_date)
        counts[fy] = counts.get(fy, 0) + 1

    return [
        CancellationAwardsByFiscalYearRow(fiscal_year=fy, terminated_awards=counts.get(fy, 0))
        for fy in range(start_fiscal_year, criteria.fiscal_year(today) + 1)
    ]
