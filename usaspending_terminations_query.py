#!/usr/bin/env python3
"""
Replacement for the retired FPDS ezsearch source.

FPDS's public ezsearch endpoint (fpds.gov/ezsearch) was retired in late
February 2026 - it now redirects to sam.gov/contracting - which silently
removed 21+ terminated contracts from the daily consolidated snapshots
between 2026-02-24 and 2026-02-25.

This module recovers and extends that signal from the USAspending.gov
transaction search API (the same FPDS/FABS data, republished with ~a few days'
lag). It searches NASA contract awards, delivery orders, and IDV/BPA vehicles
at the *transaction* level for termination language and formal convenience or
legal-cancellation action codes. Transaction-level matching is deliberate: it
also fixes the NPDV blind spot where a later closeout or settlement
modification overwrites the "latest mod" description and a still-terminated
award falls out of a latest-mod-only keyword query.

A separate FABS pass detects grant kills expressed only as large
deobligations: at least $10,000, at least 25% of the pre-clawback obligation,
and strictly before the award's current period-of-performance end date.
NASAGrants still covers status and performance-period changes; this pass
covers the money-only case that source cannot see.

One explicit live-data exception reconciles that gate with the canonical Brown
case: a 100% clawback to a zero balance can rewrite the award's current end to
the preceding day in the same action. That exact full-zero/day-before shape is
treated as a same-action end rewrite, not a pre-existing expiry. Equality with
the action date and every other post-expiry shape remain excluded.

Network/API failures propagate rather than returning an empty frame, and
search.py aborts the run on an empty result: callers must be able to
distinguish "no terminations" from "source down" (fail-loud policy adopted
after the FPDS silent failure).
"""

import csv
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import islice
from typing import Iterable, List, Optional, Sequence

import pandas as pd
from usaspending import Award, Transaction, TransactionsSearch, USASpendingClient

from contract_query import ContractQuery, FINAL_COLUMNS
from termination_vocabulary import is_cause, is_reversal, is_vacatur

# Plain strings sent to the API as `filters.keywords`; they cannot be regexes.
# The narrow classification patterns live in termination_vocabulary.py.
#
# Termination-for-cause is deliberately excluded downstream (see commit
# 08a52cf); it indicates contractor failure, not a policy cancellation.
#
# Verified against the live API on 2026-07-30: "terminated for convenience"
# matches nothing at all, and "stop-work" returns a byte-identical result set
# to "stop work" (the API normalises the hyphen), so both were dropped - three
# keywords cover the same 92 awards in three sweeps instead of five.
SEARCH_KEYWORDS = [
    "terminate for convenience",
    "termination for convenience",
    "stop work",
]

# FPDS reason-for-modification codes for "TERMINATE FOR CONVENIENCE (COMPLETE
# OR PARTIAL)" and "LEGAL CONTRACT CANCELLATION". The keyword sweep above
# cannot find these on its own: many formal mods carry only the project name,
# while N-coded cancellations can say only "no longer required". FPDS ezsearch
# matched these codes server-side; USAspending has no filter for them, so we
# sort by action type and read each code block instead.
TERMINATION_ACTION_CODES = ("F", "N")
ACTION_CODE_KINDS = {
    "F": "Terminate-for-convenience action",
    "N": "Legal-contract-cancellation action",
}
EXCLUDED_ACTION_CODES = {"E", "X"}
ACTION_TYPE_SORT = "Action Type"
TRANSACTION_AMOUNT_SORT = "Transaction Amount"
PAGE_SIZE = 100
CLAWBACK_AMOUNT_THRESHOLD = Decimal("-10000")
CLAWBACK_FRACTION_THRESHOLD = Decimal("0.25")
SAME_ACTION_END_REWRITE_LAG = timedelta(days=1)

# The compact transaction-history endpoint omits period-of-performance dates.
# The search-download endpoint returns the underlying transaction rows, so it
# is the public, mirror-free source for an award's earliest reported end date.
INITIAL_END_DATE_START = "2007-10-01"
INITIAL_END_DATE_DOWNLOAD_TIMEOUT = 1800
INITIAL_END_DATE_POLL_INTERVAL = 5

INITIAL_END_DATE_CATEGORIES = {
    "contract": "contracts",
    "idv": "idvs",
    "assistance": "grants",
}

_MOD_NUMBER_PARTS = re.compile(r"(\d+)")
_ZERO_MODIFICATION = re.compile(r"0+")

_AWARD_ID_FIELDS = (
    "award_id_piid",
    "award_id_fain",
    "award_id_uri",
    "award_id",
)
_TRANSACTION_ID_FIELDS = (
    "contract_transaction_unique_key",
    "assistance_transaction_unique_key",
    "idv_transaction_unique_key",
    "transaction_unique_key",
    "transaction_id",
)
_END_DATE_FIELDS = ("period_of_performance_current_end_date",)
_IDV_END_DATE_FIELDS = (
    "last_date_to_order",
    "period_of_performance_current_end_date",
)

NASA_AGENCY_FILTER = [
    {
        "type": "awarding",
        "tier": "toptier",
        "name": "National Aeronautics and Space Administration",
    }
]


@dataclass(frozen=True)
class ClawbackHit:
    """A grant deobligation that passes the fraction and prematurity gates."""

    transaction: Transaction
    fraction: Decimal
    pre_clawback_total: Decimal


@dataclass(frozen=True)
class InitialEndDateTarget:
    """One award whose transaction download should be inspected."""

    award_id: str
    generated_award_id: str
    category: str
    lookup_award_id: str = ""

    @property
    def native_award_id(self) -> str:
        """PIID/FAIN used by search-download filters and transaction CSVs."""
        return self.lookup_award_id or self.award_id


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


def initial_end_date_category(generated_award_id: str) -> str:
    """Map a USAspending generated award id to its download category."""
    gid = (generated_award_id or "").upper()
    if gid.startswith("CONT_AWD_"):
        return "contract"
    if gid.startswith("CONT_IDV_"):
        return "idv"
    if gid.startswith("ASST_NON_"):
        return "assistance"
    return ""


def _header_key(value: str) -> str:
    """Canonicalise either raw download headers or display-name headers."""
    return re.sub(r"[^a-z0-9]+", "_", (value or "").lstrip("\ufeff").lower()).strip(
        "_"
    )


def _first_value(row: dict, fields: Sequence[str]) -> str:
    for field in fields:
        value = row.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _iso_date(value: str, field: str) -> str:
    """Return YYYY-MM-DD for an ISO date/datetime, failing on malformed data."""
    text = str(value or "").strip()
    if not text:
        return ""
    candidate = text[:10]
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError as exc:
        raise RuntimeError(f"USAspending download returned invalid {field}: {text!r}") from exc


def _natural_modification_key(value: str) -> tuple:
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part.casefold())
        for part in _MOD_NUMBER_PARTS.split(str(value or ""))
        if part
    )


def _download_row_sort_key(row: dict) -> tuple:
    return (
        _iso_date(row.get("action_date", ""), "action_date"),
        _natural_modification_key(row.get("modification_number", "")),
        row.get("transaction_id", ""),
    )


def select_initial_reported_end_date(
    target: InitialEndDateTarget, rows: Sequence[dict]
) -> InitialEndDateResult:
    """Select a base-transaction date, else the earliest nonblank reported date."""
    if not rows:
        raise RuntimeError(
            f"USAspending transaction download omitted award {target.award_id!r}"
        )

    ordered = sorted(rows, key=_download_row_sort_key)
    dated = []
    for row in ordered:
        end_date = _iso_date(row.get("end_date", ""), "end_date")
        if end_date:
            dated.append((row, end_date))

    if not dated:
        return InitialEndDateResult(
            target.award_id,
            target.generated_award_id,
            target.category,
            "",
            "",
            "",
            "",
            "",
            "no_reported_end_date",
        )

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
        row.get("transaction_id", ""),
        _iso_date(row.get("action_date", ""), "action_date"),
        row.get("modification_number", ""),
        "base_transaction" if base else "earliest_nonblank",
        "resolved",
    )


def _transaction_download_rows(
    files: Iterable[str], category: str, expected_award_ids: set[str]
) -> dict[str, list[dict]]:
    """Read transaction CSVs from one completed USAspending download job."""
    expected = {award_id.casefold(): award_id for award_id in expected_award_ids}
    by_award = {award_id: [] for award_id in expected_award_ids}
    found_transaction_csv = False

    for path in files:
        if not str(path).lower().endswith(".csv"):
            continue
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            headers = {_header_key(name) for name in (reader.fieldnames or [])}
            end_fields = _IDV_END_DATE_FIELDS if category == "idv" else _END_DATE_FIELDS
            required_groups = (
                _AWARD_ID_FIELDS,
                _TRANSACTION_ID_FIELDS,
                ("action_date",),
                ("modification_number",),
                end_fields,
            )
            if not all(any(field in headers for field in group) for group in required_groups):
                continue

            found_transaction_csv = True
            for raw in reader:
                row = {_header_key(key): value for key, value in raw.items() if key}
                downloaded_id = _first_value(row, _AWARD_ID_FIELDS)
                award_id = expected.get(downloaded_id.casefold())
                if not award_id:
                    continue
                by_award[award_id].append(
                    {
                        "transaction_id": _first_value(row, _TRANSACTION_ID_FIELDS),
                        "action_date": _first_value(row, ("action_date",)),
                        "modification_number": _first_value(
                            row, ("modification_number",)
                        ),
                        "end_date": _first_value(row, end_fields),
                    }
                )

    if not found_transaction_csv:
        raise RuntimeError(
            f"USAspending {category} download contained no transaction CSV with "
            "award, transaction, action-date, modification, and end-date columns"
        )
    missing = sorted(award_id for award_id, rows in by_award.items() if not rows)
    if missing:
        raise RuntimeError(
            f"USAspending {category} download omitted requested award(s): "
            f"{', '.join(missing)}"
        )
    return by_award


def fetch_initial_reported_end_dates(
    client: USASpendingClient,
    targets: Sequence[InitialEndDateTarget],
    *,
    end_date: Optional[str] = None,
) -> List[InitialEndDateResult]:
    """Batch-download transaction histories and derive one result per target."""
    grouped = {category: [] for category in INITIAL_END_DATE_CATEGORIES}
    for target in targets:
        if target.category not in grouped:
            raise ValueError(f"Unsupported award category: {target.category!r}")
        grouped[target.category].append(target)

    results = []
    through = end_date or date.today().isoformat()
    with tempfile.TemporaryDirectory(prefix="initial-end-dates-") as destination:
        for category, category_targets in grouped.items():
            if not category_targets:
                continue
            query = client.awards.search()
            query = getattr(query, INITIAL_END_DATE_CATEGORIES[category])()
            # Although the API contract documents quoted award ids as its
            # exact-match syntax, the production download worker currently
            # fails every job carrying those quotes (validated 2026-07-31).
            # Send the native ids unquoted and enforce exactness while parsing
            # the CSV: unrelated fuzzy matches are ignored and a missing exact
            # id aborts the batch below.
            query = query.award_ids(
                *(target.native_award_id for target in category_targets)
            ).time_period(INITIAL_END_DATE_START, through)
            job = client.downloads.search(
                query,
                spending_level=["transactions"],
                destination_dir=destination,
            )
            files = job.wait_for_completion(
                timeout=INITIAL_END_DATE_DOWNLOAD_TIMEOUT,
                poll_interval=INITIAL_END_DATE_POLL_INTERVAL,
                cleanup_zip=True,
            )
            rows_by_award = _transaction_download_rows(
                files,
                category,
                {target.native_award_id for target in category_targets},
            )
            results.extend(
                select_initial_reported_end_date(
                    target, rows_by_award[target.native_award_id]
                )
                for target in category_targets
            )
    return results


class USASpendingTerminationsQuery(ContractQuery):
    """Queries USAspending for NASA terminations and grant clawbacks."""

    def __init__(
        self,
        final_columns: List[str] = FINAL_COLUMNS,
        client: Optional[USASpendingClient] = None,
    ):
        super().__init__(final_columns)
        self._owns_client = client is None
        self.client = client or USASpendingClient()
        self._page_cache = {}

    def _query(self, start_date: str, end_date: str) -> TransactionsSearch:
        if self.client is None:
            self.client = USASpendingClient()
        return (
            self.client.transactions.search()
            # TransactionsSearch supports mixed award categories, so one
            # globally sorted result set can cover awards, delivery orders,
            # and their IDV/BPA vehicles without splitting the seek/cache.
            .contracts()
            .idvs()
            .agency(NASA_AGENCY_FILTER[0]["name"])
            .time_period(start_date, end_date)
            .page_size(PAGE_SIZE)
        )

    def _fetch_keyword(
        self, keyword: str, start_date: str, end_date: str
    ) -> List[Transaction]:
        """Fetch all transaction pages for one keyword."""
        return (
            self._query(start_date, end_date)
            .keywords(keyword)
            .order_by("action_date", "desc")
            .all()
        )

    def _clawback_query(self, start_date: str, end_date: str) -> TransactionsSearch:
        """Build the amount-sorted FABS grant transaction query."""
        if self.client is None:
            self.client = USASpendingClient()
        return (
            self.client.transactions.search()
            .grants()
            .agency(NASA_AGENCY_FILTER[0]["name"])
            .time_period(start_date, end_date)
            .page_size(PAGE_SIZE)
        )

    def _fetch_clawback_candidates(
        self, start_date: str, end_date: str
    ) -> List[Transaction]:
        """Read ascending grant transactions through the -$10,000 cutoff."""
        query = self._clawback_query(start_date, end_date).order_by(
            "federal_action_obligation", "asc"
        )
        iterator = iter(query)
        results: List[Transaction] = []
        previous_amount: Optional[Decimal] = None
        page = 0

        while True:
            rows = list(islice(iterator, PAGE_SIZE))
            if not rows:
                break
            page += 1
            previous_amount = self._assert_amounts_sorted(rows, page, previous_amount)

            crossed_threshold = False
            for row in rows:
                amount = row.federal_action_obligation
                if amount is None or amount > CLAWBACK_AMOUNT_THRESHOLD:
                    crossed_threshold = True
                    continue
                results.append(row)

            if crossed_threshold or len(rows) < PAGE_SIZE:
                break

        print(
            f"  grant deobligations <= ${CLAWBACK_AMOUNT_THRESHOLD:,.0f}: "
            f"{len(results)} transactions ({page} pages)",
            file=sys.stderr,
        )
        return results

    @staticmethod
    def _amount_sort_key(amount: Optional[Decimal]) -> tuple[bool, Decimal]:
        """Match USAspending's ordering, where missing amounts sort last."""
        return (amount is None, amount or Decimal("0"))

    @classmethod
    def _assert_amounts_sorted(
        cls,
        rows: Sequence[Transaction],
        page: int,
        previous_amount: Optional[Decimal] = None,
    ) -> Optional[Decimal]:
        """Fail loudly if the API stops honoring Transaction Amount order."""
        amounts = [row.federal_action_obligation for row in rows]
        keys = [cls._amount_sort_key(amount) for amount in amounts]
        expected = sorted(keys)
        previous_key = (
            cls._amount_sort_key(previous_amount)
            if previous_amount is not None
            else None
        )
        if keys != expected or (
            previous_key is not None and keys and previous_key > keys[0]
        ):
            raise RuntimeError(
                f"USAspending returned page {page} unsorted by "
                f"'{TRANSACTION_AMOUNT_SORT}' ({amounts[:8]}...). The "
                f"clawback seek depends on this ordering; aborting rather "
                f"than silently returning a partial result."
            )
        return amounts[-1] if amounts else previous_amount

    @staticmethod
    def _qualify_clawback(
        transaction: Transaction, award: Award
    ) -> Optional[ClawbackHit]:
        """Apply the inclusive fraction and explicit prematurity rules."""
        deob = transaction.federal_action_obligation
        current_total = award.award_amount
        end_date = award.end_date
        action_date = transaction.action_date

        if deob is None or deob > CLAWBACK_AMOUNT_THRESHOLD:
            return None
        if current_total is None:
            raise RuntimeError(
                f"USAspending award detail for "
                f"{transaction.generated_unique_award_id!r} omitted "
                f"total_obligation; cannot calculate clawback fraction."
            )
        # A valid assistance award can legitimately have no current PoP end
        # date (80NSSC25M7006 is one live example). Without an end date the
        # strict premature gate cannot pass, so the row is unclassifiable.
        if end_date is None:
            return None
        if action_date is None:
            raise RuntimeError(
                f"USAspending transaction for "
                f"{transaction.generated_unique_award_id!r} omitted its "
                f"action date; cannot apply the premature-clawback gate."
            )

        pre_clawback_total = current_total - deob
        if pre_clawback_total <= 0:
            return None
        fraction = -deob / pre_clawback_total
        if fraction < CLAWBACK_FRACTION_THRESHOLD:
            return None

        # A full clawback can rewrite the current PoP end as part of the same
        # action. Brown (80NSSC25K0030) changed its original 2028-06-30 end to
        # the day before P00001, so using only the post-action date would make
        # that canonical premature kill look expired. The verified mirror has
        # exactly one >=25% row with this day-before/full-zero shape. Keep the
        # exception this narrow; ordinary action-on/after-expiry rows remain
        # excluded.
        same_action_end_rewrite = (
            current_total == 0
            and fraction == 1
            and end_date == action_date - SAME_ACTION_END_REWRITE_LAG
        )
        if action_date >= end_date and not same_action_end_rewrite:
            return None
        return ClawbackHit(transaction, fraction, pre_clawback_total)

    def _fetch_clawbacks(self, start_date: str, end_date: str) -> List[ClawbackHit]:
        """Batch-fetch awards only for threshold-crossing grant transactions."""
        candidates = self._fetch_clawback_candidates(start_date, end_date)
        if not candidates:
            raise RuntimeError(
                "USAspending clawback sweep returned zero threshold "
                "candidates; aborting rather than allowing a broken or "
                "silently emptied assistance pass to hide behind nonempty "
                "contract results."
            )

        award_ids = []
        for transaction in candidates:
            award_id = (transaction.award_identifier or "").strip()
            if not award_id:
                raise RuntimeError(
                    "USAspending clawback transaction omitted its Award ID; "
                    "cannot retrieve denominator/end-date fields."
                )
            if not transaction.generated_unique_award_id:
                raise RuntimeError(
                    f"USAspending transaction for {award_id!r} omitted its "
                    f"generated award ID; cannot construct the source URL."
                )
            if award_id not in award_ids:
                award_ids.append(award_id)

        award_rows = (
            self.client.awards.search()
            .award_ids(*award_ids)
            .grants()
            .page_size(PAGE_SIZE)
            .all()
        )
        awards = {
            (award.award_identifier or "").strip(): award
            for award in award_rows
            if award.award_identifier
        }
        missing = [award_id for award_id in award_ids if award_id not in awards]
        if missing:
            raise RuntimeError(
                f"USAspending batch award lookup is missing "
                f"{', '.join(missing)}; cannot calculate clawback gates."
            )

        results = []
        for transaction in candidates:
            award_id = (transaction.award_identifier or "").strip()
            hit = self._qualify_clawback(transaction, awards[award_id])
            if hit is not None:
                results.append(hit)
        print(
            f"  premature grant clawbacks >= "
            f"{CLAWBACK_FRACTION_THRESHOLD:.0%}: {len(results)} transactions",
            file=sys.stderr,
        )
        return results

    @staticmethod
    def _total_pages(query: TransactionsSearch) -> int:
        return (query.count() + PAGE_SIZE - 1) // PAGE_SIZE

    @staticmethod
    def _codes(rows: Sequence[Transaction]) -> List[str]:
        return [(row.action_type or "").upper() for row in rows]

    def _page(self, query: TransactionsSearch, page: int) -> List[Transaction]:
        """One page of `query`, cached for the life of a seek.

        The two binary searches probe from the same midpoint and only diverge
        near the code boundary, and the boundary pages are then read a third
        time by the collection loop - so most probes would otherwise re-fetch a
        page already downloaded, roughly doubling this source's request count.
        """
        rows = self._page_cache.get(page)
        if rows is None:
            start = (page - 1) * PAGE_SIZE
            rows = query[start : start + PAGE_SIZE]
            self._page_cache[page] = rows
        return rows

    def _first_code(self, query: TransactionsSearch, page: int) -> Optional[str]:
        rows = self._page(query, page)
        if not rows:
            return None
        self._assert_sorted(rows, page)
        if page > 1:
            previous_rows = self._page(query, page - 1)
            if previous_rows:
                self._assert_page_boundary(previous_rows, page - 1, rows, page)
        return self._codes(rows)[0] or None

    def _lower_bound(
        self, query: TransactionsSearch, target: str, total_pages: int
    ) -> int:
        """First page whose leading action code sorts at or after `target`."""
        low, high = 1, total_pages
        while low < high:
            mid = (low + high) // 2
            code = self._first_code(query, mid)
            if code is None or code >= target:
                high = mid
            else:
                low = mid + 1
        return low

    def _fetch_termination_codes(
        self, start_date: str, end_date: str
    ) -> List[Transaction]:
        """Fetch transactions carrying either cancellation action code.

        USAspending has no filter for action type, but it does sort by it, so
        we binary-search to each requested block instead of enumerating every
        page. The cache is shared across both seeks because they use the same
        query and ordering.
        """
        query = self._query(start_date, end_date).order_by("action_type", "asc")
        self._page_cache = {}  # keyed by page number, so it belongs to this query
        total_pages = self._total_pages(query)
        if not total_pages:
            return []

        results: List[Transaction] = []
        for code in TERMINATION_ACTION_CODES:
            results.extend(self._fetch_code_block(query, code, total_pages))
        return results

    def _fetch_code_block(
        self, query: TransactionsSearch, code: str, total_pages: int
    ) -> List[Transaction]:
        """Fetch one arbitrary action-code block from a sorted query.

        Starting one page before the lower bound catches a block that begins
        after a smaller leading code on the same page. Collection stops as soon
        as a page has advanced past the target, so no assumption about a
        one-character successor code is needed.
        """
        start = max(1, self._lower_bound(query, code, total_pages) - 1)
        results: List[Transaction] = []
        last_page = start

        for page in range(start, total_pages + 1):
            last_page = page
            rows = self._page(query, page)
            if not rows:
                break
            self._assert_sorted(rows, page)
            if page > 1:
                previous_rows = self._page(query, page - 1)
                if previous_rows:
                    self._assert_page_boundary(previous_rows, page - 1, rows, page)
            codes = self._codes(rows)
            results.extend(
                row for row, row_code in zip(rows, codes) if row_code == code
            )

            # Blank codes sort after populated codes. If the page contains any
            # value after the target and no later target can exist, the block
            # is complete.
            if any(
                self._code_sort_key(row_code) > self._code_sort_key(code)
                for row_code in codes
            ):
                if page < total_pages:
                    next_rows = self._page(query, page + 1)
                    if next_rows:
                        self._assert_page_boundary(rows, page, next_rows, page + 1)
                break

        print(
            f"  action code '{code}': {len(results)} transactions "
            f"(pages {start}-{last_page} of {total_pages})",
            file=sys.stderr,
        )
        return results

    @staticmethod
    def _code_sort_key(code: str) -> tuple[bool, str]:
        """Match USAspending's ordering, where blank action codes sort last."""
        return (not code, code)

    @classmethod
    def _assert_page_boundary(
        cls,
        previous_rows: Sequence[Transaction],
        previous_page: int,
        rows: Sequence[Transaction],
        page: int,
    ) -> None:
        """Fail if two individually sorted action-code pages are reversed."""
        cls._assert_sorted(previous_rows, previous_page)
        cls._assert_sorted(rows, page)
        previous_code = cls._codes(previous_rows)[-1]
        current_code = cls._codes(rows)[0]
        if cls._code_sort_key(previous_code) > cls._code_sort_key(current_code):
            raise RuntimeError(
                f"USAspending returned pages {previous_page}-{page} unsorted "
                f"by '{ACTION_TYPE_SORT}' ({previous_code!r} before "
                f"{current_code!r}). The termination-code seek depends on "
                f"monotonic ordering across pages; aborting rather than "
                f"silently returning a partial result."
            )

    @staticmethod
    def _assert_sorted(rows: Sequence[Transaction], page: int) -> None:
        """Fail loudly if the API stops honouring the Action Type sort.

        The whole seek strategy rests on that ordering, and this endpoint is
        known to accept unrecognised parameters silently - so a sort that
        quietly stopped working would hand us the wrong slice of the data and
        look exactly like "no terminations today".
        """
        codes = USASpendingTerminationsQuery._codes(rows)
        expected = sorted(codes, key=USASpendingTerminationsQuery._code_sort_key)
        if codes != expected:
            raise RuntimeError(
                f"USAspending returned page {page} unsorted by "
                f"'{ACTION_TYPE_SORT}' ({codes[:8]}...). The termination-code "
                f"seek depends on this ordering; aborting rather than "
                f"silently returning a partial result."
            )

    def search(
        self, start_date: Optional[date] = None, end_date: Optional[date] = None
    ) -> pd.DataFrame:
        """Search for termination actions and close any client created here."""
        try:
            return self._search(start_date, end_date)
        finally:
            if self._owns_client and self.client is not None:
                self.client.close()
                self.client = None

    def _search(
        self, start_date: Optional[date] = None, end_date: Optional[date] = None
    ) -> pd.DataFrame:
        """
        Search NASA procurement terminations and premature grant clawbacks.

        Three passes, unioned and deduplicated by Award ID:

        * keyword - catches stop-work and intent-to-terminate language, which
          often has no distinguishing action code;
        * action code - catches formal convenience/legal cancellation mods,
          which often have no configured termination language;
        * grant clawback - catches large, premature money removals with no
          termination text or performance-period truncation.

        No pass subsumes the others; dropping one loses awards.

        Returns a DataFrame in FINAL_COLUMNS format, one row per unique
        Award ID (most recent matching transaction wins the description).
        """
        start = (start_date or date(2025, 1, 20)).isoformat()
        end = (end_date or date.today()).isoformat()
        print(
            f"Querying USAspending transactions for NASA termination/clawback "
            f"actions "
            f"{start}..{end}",
            file=sys.stderr,
        )

        all_rows: List[Transaction] = []
        for kw in SEARCH_KEYWORDS:
            found = self._fetch_keyword(kw, start, end)
            print(f"  keyword '{kw}': {len(found)} transactions", file=sys.stderr)
            all_rows.extend(found)

        keyword_ids = {(row.award_identifier or "").strip() for row in all_rows}
        coded_rows = self._fetch_termination_codes(start, end)
        all_rows.extend(coded_rows)
        code_only = {
            (row.award_identifier or "").strip() for row in coded_rows
        } - keyword_ids
        print(
            f"  awards found ONLY by action code (invisible to keywords): "
            f"{len(code_only)}",
            file=sys.stderr,
        )
        clawback_hits = self._fetch_clawbacks(start, end)

        # Deduplicate by Award ID across all three signals, keeping the most
        # recent transaction. The optional hit carries the award-level
        # denominator needed only for clawback output.
        best: dict[str, tuple[Transaction, Optional[ClawbackHit]]] = {}
        for row in all_rows:
            aid = (row.award_identifier or "").strip()
            desc = (row.raw.get("Transaction Description") or "").strip()
            if not aid:
                continue
            # Skip termination-for-cause (contractor failure, not cancellation)
            action_code = (row.action_type or "").upper()
            if action_code in EXCLUDED_ACTION_CODES or is_cause(desc):
                continue
            prev = best.get(aid)
            if prev is None or (row.action_date or date.min) > (
                prev[0].action_date or date.min
            ):
                best[aid] = (row, None)

        for hit in clawback_hits:
            row = hit.transaction
            aid = (row.award_identifier or "").strip()
            if not aid:
                continue
            prev = best.get(aid)
            if prev is None or (row.action_date or date.min) > (
                prev[0].action_date or date.min
            ):
                best[aid] = (row, hit)

        records = []
        for aid, (row, clawback_hit) in best.items():
            # An award whose WINNING (latest) transaction reverses the
            # termination is not currently cancelled - but its older
            # termination mods keep matching this full-window sweep forever,
            # and a rescission's own text ("RESCINDING STOP WORK NOTICE")
            # matches the keyword sweep too. Parity with the mirror source,
            # which shipped this rule first: judged on the winning row only,
            # so a re-termination after a rescission still surfaces, and the
            # reversal row must COMPETE in the dedupe above rather than be
            # skipped there - skipping at insert would let the older
            # termination row win and keep the award listed.
            desc = (row.raw.get("Transaction Description") or "").strip()
            if is_reversal(desc) or is_vacatur(desc):
                continue

            gid = row.generated_unique_award_id or ""
            action_date = row.action_date.isoformat() if row.action_date else ""
            if clawback_hit is not None:
                fraction = f"{clawback_hit.fraction:.0%}"
                amount = f"${clawback_hit.pre_clawback_total:,.0f}"
                status = (
                    f"Pure-clawback deobligation "
                    f"{row.modification_number or ''} on {action_date} "
                    f"({fraction} of {amount})"
                )
                source_type = "Grant"
                value = clawback_hit.pre_clawback_total
            else:
                # A formal code reads differently from stop-work language, and
                # for coded transactions the description may carry no evidence.
                action_code = (row.action_type or "").upper()
                kind = ACTION_CODE_KINDS.get(
                    action_code, "Termination-language transaction"
                )
                status = f"{kind} {row.modification_number or ''} on {action_date}"
                source_type = "Contract"
                value = row.raw.get("Transaction Amount")

            records.append(
                {
                    "Award ID": aid,
                    "source_type": source_type,
                    "recipient": row.raw.get("Recipient Name") or "",
                    "value": value,
                    "savings": None,
                    "status": status,
                    "source_url": f"https://www.usaspending.gov/award/{gid}/"
                    if gid
                    else "",
                    "description": row.raw.get("Transaction Description") or "",
                    "agency": row.raw.get("Awarding Agency") or "NASA",
                }
            )

        df = pd.DataFrame(records, columns=self.final_columns)
        print(f"USAspendingTerminations: {len(df)} unique awards", file=sys.stderr)
        self.export_to_csv(df, "usaspending_terminations_query")
        return df


if __name__ == "__main__":
    print(USASpendingTerminationsQuery().search().to_string())
