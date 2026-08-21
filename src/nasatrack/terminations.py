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
from nasatrack.schema import TerminationRow

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
# uses (still_terminated, closed_out, descoped, needs_manual_review) is an
# annotation on a row that stays.
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
