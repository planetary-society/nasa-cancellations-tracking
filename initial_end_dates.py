#!/usr/bin/env python3
"""An award's ORIGINAL reported period-of-performance end date, and how to pick it.

`End Date Trend` asks whether an award was cut short. Answering that needs the
end date the award was first awarded with, which is not the same as the first
end date this tracker happened to observe: an award flagged in 2026 may have
been running since 2017, and comparing against our own first sighting would
call every long-running award "unchanged".

Only per-transaction period-of-performance dates can answer it, and the public
API does not publish them on any transaction route:

  * `award.transactions` (/api/v2/transactions/) returns 11 fields - action
    date, action type, mod number, description, obligations - and no
    period-of-performance date at all.
  * `client.transactions.search()` (/api/v2/search/spending_by_transaction/)
    accepts 45 field names, none of which is a period-of-performance end date.
    `Last Date to Order` is the IDV ordering-period boundary, not the PoP.

A bulk download job (`spending_level=["transactions"]`) does carry the column,
and this module used to drive one. That path is gone: the jobs are minutes of
server-side work and a zip per award category, which is far too expensive for
one column. The local mirror has the field directly in rpt.transaction_search,
so the provider now lives in local_usaspending_mirror_query and this module
keeps only what is source-agnostic: the target/result records and the
selection rule.

Consequence, deliberately accepted: on a machine without mirror access -
including CI - no new initial end dates are resolved. Already-recorded values
live in verification/initial_reported_end_dates.csv, which is committed and
write-once, so CI keeps using every value the mirror has ever resolved and
simply does not add to them.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from tracking_window import to_iso
from utils import natural_modification_key

# Awards whose first mod is the base transaction ("0", "00", ...) report their
# originally-awarded end date there. Everything else falls back to the earliest
# transaction carrying any end date.
_ZERO_MODIFICATION = re.compile(r"0+")


@dataclass(frozen=True)
class InitialEndDateTarget:
    """One award whose transaction history should be inspected."""

    award_id: str
    generated_award_id: str
    category: str


@dataclass(frozen=True)
class InitialEndDateResult:
    """The earliest reported end date and the transaction that supplied it."""

    award_id: str
    generated_award_id: str
    category: str
    initial_end_date: str
    transaction_id: str
    action_date: str
    modification_number: str
    basis: str
    status: str

    @classmethod
    def unresolved(
        cls, target: InitialEndDateTarget, status: str
    ) -> "InitialEndDateResult":
        """A result carrying only why no end date was found.

        Four sites need this shape, and spelling it out means five adjacent
        bare "" positionals that nothing stops you reordering.
        """
        return cls(
            target.award_id,
            target.generated_award_id,
            target.category,
            "",
            "",
            "",
            "",
            "",
            status,
        )


# Statuses that settle an award for good. The sidecar is write-once, so only
# these may be persisted - see TRANSIENT_STATUSES.
TERMINAL_STATUSES = frozenset({"resolved", "no_reported_end_date"})

# Statuses that describe a condition of THIS RUN rather than of the award.
# Persisting one would retire the award from lookup forever over something that
# resolves itself, so the caller drops them and retries next run:
#   not_in_mirror       - mirror replication lags the live API by 2-6 weeks
#   unsupported_award_id - no usable generated award id available yet
TRANSIENT_STATUSES = frozenset({"not_in_mirror", "unsupported_award_id"})

# Every status any producer can emit must be classified in exactly one set, so
# adding one forces the persist/retry decision instead of defaulting to
# whichever branch it happens to fall through. Pinned by
# tests/test_initial_reported_end_date.py.
INITIAL_END_DATE_STATUSES = TERMINAL_STATUSES | TRANSIENT_STATUSES


# Generated-id prefix -> award category. A prefix absent from this map (which
# is every ASST_AGG_ aggregate in utils.GENERATED_AWARD_ID_PREFIXES) yields a
# blank category and is therefore never looked up: NASA does not report
# aggregate assistance records, and admitting one would put a row with no
# single period of performance into a write-once provenance file.
_CATEGORY_BY_PREFIX = {
    "CONT_AWD_": "contract",
    "CONT_IDV_": "idv",
    "ASST_NON_": "assistance",
}


def initial_end_date_category(generated_award_id: str) -> str:
    """Map a USAspending generated award id to a category, or "" if unsupported.

    Two consumers: search.py treats a blank category as "this id shape cannot
    be looked up", and the value is copied into the provenance record. The
    provider does NOT branch on it - it COALESCEs ordering_period_end_date over
    the period-of-performance end for every row, which is a no-op for non-IDVs
    because that column is null there.
    """
    gid = (generated_award_id or "").upper()
    for prefix, category in _CATEGORY_BY_PREFIX.items():
        if gid.startswith(prefix):
            return category
    return ""


def _iso_or_raise(value, field: str) -> str:
    """Return YYYY-MM-DD, or raise if the value is present but unparseable.

    The parsing itself is tracking_window.to_iso - psycopg dates, CSV text and
    ISO-with-time all arrive here and that helper already knows all three. Only
    the FAILURE POLICY differs, and that difference is the whole point: to_iso
    returns "" for garbage because its caller quarantines the row and moves on,
    whereas this feeds a write-once provenance record, so garbage must abort
    rather than be stored as blank and never revisited.
    """
    if value is None or not str(value).strip():
        return ""
    iso = to_iso(value)
    if not iso:
        raise RuntimeError(f"invalid {field} in transaction history: {value!r}")
    return iso


def _row_sort_key(row: dict) -> tuple:
    return (
        _iso_or_raise(row.get("action_date", ""), "action_date"),
        natural_modification_key(row.get("modification_number")),
        str(row.get("transaction_id") or ""),
    )


def select_initial_reported_end_date(
    target: InitialEndDateTarget, rows: Sequence[dict]
) -> InitialEndDateResult:
    """Select a base-transaction date, else the earliest nonblank reported date.

    Pure: takes normalised rows - transaction_id, action_date,
    modification_number, end_date - so the rule is identical whatever provider
    read them. This is the methodology; the provider is just transport.
    """
    if not rows:
        raise RuntimeError(f"no transaction history for award {target.award_id!r}")

    dated = []
    for row in sorted(rows, key=_row_sort_key):
        end_date = _iso_or_raise(row.get("end_date", ""), "end_date")
        if end_date:
            dated.append((row, end_date))

    if not dated:
        return InitialEndDateResult.unresolved(target, "no_reported_end_date")

    base = [
        item
        for item in dated
        if _ZERO_MODIFICATION.fullmatch(
            str(item[0].get("modification_number", "") or "").strip()
        )
    ]
    row, end_date = base[0] if base else dated[0]
    return InitialEndDateResult(
        target.award_id,
        target.generated_award_id,
        target.category,
        end_date,
        str(row.get("transaction_id") or ""),
        _iso_or_raise(row.get("action_date", ""), "action_date"),
        str(row.get("modification_number") or ""),
        "base_transaction" if base else "earliest_nonblank",
        "resolved",
    )
