#!/usr/bin/env python3
"""
Replacement for the retired FPDS ezsearch source.

FPDS's public ezsearch endpoint (fpds.gov/ezsearch) was retired in late
February 2026 - it now redirects to sam.gov/contracting - which silently
removed 21+ terminated contracts from the daily consolidated snapshots
between 2026-02-24 and 2026-02-25.

This module recovers the same signal from the USAspending.gov transaction
search API (the same FPDS data, republished with ~a few days' lag):
NASA contract transactions whose description contains termination/stop-work
language, searched at the *transaction* level. Transaction-level matching is
deliberate - it also fixes the NPDV blind spot where a later closeout or
settlement modification overwrites the "latest mod" description and a
still-terminated award falls out of a latest-mod-only keyword query.

Network/API failures propagate rather than returning an empty frame, and
search.py aborts the run on an empty result: callers must be able to
distinguish "no terminations" from "source down" (fail-loud policy adopted
after the FPDS silent failure).
"""

import sys
from datetime import date
from typing import List, Optional, Sequence

import pandas as pd
from usaspending import Transaction, TransactionsSearch, USASpendingClient

from contract_query import ContractQuery, FINAL_COLUMNS
from termination_vocabulary import is_cause

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

# FPDS reason-for-modification code for "TERMINATE FOR CONVENIENCE (COMPLETE OR
# PARTIAL)". The keyword sweep above cannot find these on its own: a great many
# termination mods carry only the project name as their description - e.g.
# 80MSFC22CA005 "MARS ASCENT VEHICLE INTEGRATED SYSTEM (MAVIS)" ($103M) and
# 80MSFC21C0010 "MARS ASCENT PROPULSION SYSTEM (MAPS)" ($86M), both terminated
# 2025-09-30 with no termination language anywhere in the text. FPDS ezsearch
# matched this code server-side; USAspending has no filter for it, so we sort
# by it and read the block instead.
TERMINATION_ACTION_CODE = "F"
ACTION_TYPE_SORT = "Action Type"
PAGE_SIZE = 100

NASA_AGENCY_FILTER = [
    {
        "type": "awarding",
        "tier": "toptier",
        "name": "National Aeronautics and Space Administration",
    }
]


class USASpendingTerminationsQuery(ContractQuery):
    """Queries USAspending transaction search for NASA termination actions."""

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
            # Includes delivery orders; assistance is covered by NPDV/NASAGrants.
            .contracts()
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
        """Fetch transactions carrying the terminate-for-convenience code.

        USAspending has no filter for action type, but it does sort by it, so
        we binary-search to the block instead of enumerating ~293 pages. Costs
        roughly two dozen requests.
        """
        query = self._query(start_date, end_date).order_by("action_type", "asc")
        self._page_cache = {}  # keyed by page number, so it belongs to this query
        total_pages = self._total_pages(query)
        if not total_pages:
            return []

        # Step one page either side: the boundary pages hold a mix of codes.
        start = max(
            1, self._lower_bound(query, TERMINATION_ACTION_CODE, total_pages) - 1
        )
        after = chr(ord(TERMINATION_ACTION_CODE) + 1)
        end = min(total_pages, self._lower_bound(query, after, total_pages) + 1)

        results: List[Transaction] = []
        for page in range(start, end + 1):
            rows = self._page(query, page)
            if not rows:
                break
            self._assert_sorted(rows, page)
            results.extend(
                row
                for row in rows
                if (row.action_type or "").upper() == TERMINATION_ACTION_CODE
            )

        print(
            f"  action code '{TERMINATION_ACTION_CODE}': {len(results)} transactions "
            f"(pages {start}-{end} of {total_pages})",
            file=sys.stderr,
        )
        return results

    @staticmethod
    def _assert_sorted(rows: Sequence[Transaction], page: int) -> None:
        """Fail loudly if the API stops honouring the Action Type sort.

        The whole seek strategy rests on that ordering, and this endpoint is
        known to accept unrecognised parameters silently - so a sort that
        quietly stopped working would hand us the wrong slice of the data and
        look exactly like "no terminations today".
        """
        codes = USASpendingTerminationsQuery._codes(rows)
        expected = sorted(codes, key=lambda code: (not code, code))
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
        Search NASA contract transactions for termination actions.

        Two passes, unioned and deduplicated by Award ID:

        * keyword - catches stop-work and intent-to-terminate language, which
          often has no distinguishing action code;
        * action code - catches formal terminate-for-convenience mods, which
          often have no termination language.

        Neither pass subsumes the other; dropping either loses awards.

        Returns a DataFrame in FINAL_COLUMNS format, one row per unique
        Award ID (most recent matching transaction wins the description).
        """
        start = (start_date or date(2025, 1, 20)).isoformat()
        end = (end_date or date.today()).isoformat()
        print(
            f"Querying USAspending transactions for NASA termination actions "
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

        # Deduplicate by Award ID, keeping the most recent transaction
        best: dict[str, Transaction] = {}
        for row in all_rows:
            aid = (row.award_identifier or "").strip()
            desc = (row.raw.get("Transaction Description") or "").strip()
            if not aid:
                continue
            # Skip termination-for-cause (contractor failure, not cancellation)
            if is_cause(desc):
                continue
            prev = best.get(aid)
            if prev is None or (row.action_date or date.min) > (
                prev.action_date or date.min
            ):
                best[aid] = row

        records = []
        for aid, row in best.items():
            gid = row.generated_unique_award_id or ""
            # Record which signal found it: a formal terminate-for-convenience
            # action reads very differently from stop-work language, and for
            # the coded ones the description carries no evidence at all.
            coded = (row.action_type or "").upper() == TERMINATION_ACTION_CODE
            kind = (
                "Terminate-for-convenience action"
                if coded
                else "Termination-language transaction"
            )
            action_date = row.action_date.isoformat() if row.action_date else ""
            records.append(
                {
                    "Award ID": aid,
                    "source_type": "Contract",
                    "recipient": row.raw.get("Recipient Name") or "",
                    "value": row.raw.get("Transaction Amount"),
                    "savings": None,
                    "status": f"{kind} {row.modification_number or ''} "
                    f"on {action_date}",
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
