import pandas as pd
from datetime import datetime
from typing import List, Dict
import csv
import os
import sys
import logging
from doge_search import DOGEQuery
from npdv_query import NPDVQuery
from nasa_grants_query import NASAGrantsQuery
from usaspending_terminations_query import USASpendingTerminationsQuery
from local_usaspending_mirror_query import LocalUSASpendingMirrorQuery
from usaspending import USASpendingClient, Award
from contract_query import find_most_recent_csv, csv_files_equal
from utils import canonical_usaspending_url, is_generated_award_id
from validate_snapshot import validate
import build_master_ledger

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Source label (as written to the "Source" column) -> query class.
# FPDS removed 2026-07: fpds.gov/ezsearch was retired (redirects to
# sam.gov/contracting) and silently zeroed out from 2026-02-25.
# USAspendingTerminations replaces it with transaction-level keyword
# search against the USAspending API (same underlying FPDS data).
SOURCES = {
    "DOGE": DOGEQuery,
    "NPDV": NPDVQuery,
    "NASAGrants": NASAGrantsQuery,
    "USAspendingTerminations": USASpendingTerminationsQuery,
    # Last on purpose: dict order is first-source-wins for an award's snapshot
    # row, and the mirror is a local Postgres depth net that lags the live API
    # by 2-6 weeks (and replays its last export when the DB is unreachable),
    # so it should only own rows no other source found.
    "LocalUSASpendingMirror": LocalUSASpendingMirrorQuery,
}

# Sources that publish an external *claim* of a cancellation, as opposed to
# sources where we infer one from award data. A claim is the fact being
# tracked, so it is recorded even when the award turns out to have merely
# expired or grown - and it is attached to the consolidated row regardless of
# which source won that row (see _build_claim_index).
CLAIM_SOURCES = {"DOGE"}

# Ledger statuses that mean "left the snapshot and nobody has said why yet".
# Everything else is either currently flagged, adjudicated, or excluded on
# purpose - see the build_master_ledger docstring for the full vocabulary.
UNEXPLAINED_STATUSES = {"dropped_pending_review", "needs_manual_review"}

# Claim fields: what an external source asserted, kept separate from what the
# award data shows actually happened.
CLAIM_COLUMNS = (
    "Claiming Source",
    "Claimed Status",
    "Claimed Savings",
    "Claim Date",
)

# Column order of the consolidated snapshot CSV.
SNAPSHOT_COLUMNS = [
    "Source",
    "District",
    "Recipient",
    "Award ID",
    "Latest Modification Number",
    "Latest Modification Date",
    "Start Date",
    "End Date",
    "Award Amount",
    "Total Outlays",
    "Description",
    # Why the winning source flagged this award, in its own words. Each query
    # module already composes one ("Terminate-for-convenience action P00180 on
    # 2026-05-06"); until this column existed it was dropped here, so the
    # published data could say an award was cancelled but never on what
    # evidence. Distinct from Claimed Status, which is an outside assertion.
    "Detection",
    "Business Categories",
    "URL",
    *CLAIM_COLUMNS,
]


def _normalize_newlines(value: str) -> str:
    """Use one line-ending convention inside multi-line CSV fields."""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _cell(row, column: str) -> str:
    """Read one cell as a clean string, treating NaN/None as empty."""
    value = row.get(column)
    if value is None or pd.isna(value):
        return ""
    return _normalize_newlines(str(value)).strip()


def _award_end_date(award: Award) -> str:
    """Return the applicable end date for an award or IDV vehicle.

    USAspending's IDV search response reports the ordering-period boundary as
    ``Last Date to Order`` while leaving the generic period end absent. Keep
    the regular period end authoritative and use the IDV field only as its
    documented fallback.
    """
    end_date = award.period_of_performance.end_date
    if not end_date and award.category == "idv":
        raw = award.raw or {}
        end_date = raw.get("Last Date to Order") or raw.get("last_date_to_order")
    return end_date or ""


class Search:
    """
    Orchestrates the contract/grant cancellation search across multiple data sources.

    Queries DOGE API, NPDV CSV, NASA Grants API, and USAspending transaction search
    for potential NASA award cancellations/terminations, then enriches results with
    USAspending.gov data.
    """

    def __init__(self):
        self.client = USASpendingClient()
        # Copy so the availability gate below cannot mutate the module constant.
        self.sources = dict(SOURCES)
        # The one exception to the fail-loud policy: an unavailable mirror has
        # nothing to query and nothing to replay, so drop it before the loop
        # can abort on it. Every other source stays fail-loud - and the hard
        # del keeps this gate loud too if the registry key is ever renamed.
        if not LocalUSASpendingMirrorQuery.is_available():
            del self.sources["LocalUSASpendingMirror"]
            print(
                "Skipping LocalUSASpendingMirror: no DB credentials and no "
                "prior export to replay.",
                file=sys.stderr,
            )
        self.sources_cancellation_data: Dict[
            str, pd.DataFrame
        ] = {}  # key: source name, value: source dataframe
        self.unique_award_ids: List[str] = []
        self.unique_cancellations: Dict[
            str, Dict
        ] = {}  # key: award_id, value: snapshot row keyed by column name
        self.awards: List[Award] = []
        self.awards_by_id: Dict[
            str, Award
        ] = {}  # keyed by the id the SOURCE used, which may be a generated id
        self.claims: Dict[
            str, Dict[str, str]
        ] = {}  # key: award_id, value: claim fields
        self.unresolved: Dict[
            str, List[str]
        ] = {}  # key: award_id, value: sources that flagged it
        self.ignore_award_ids: List[str] = [
            "80LARC19F0086",
            "80LARC25F7014",
            "80JSC024F0024",
            "80JSC024F0026",
            "80LARC21F0053",
            "80NSSC19K0714",
        ]

    def search(self):
        """Execute the workflow and always release the USAspending client."""
        try:
            return self._search()
        finally:
            self.client.close()

    def _search(self):
        """
        Execute the full search workflow.

        1. Query all data sources for potential cancellations
        2. Collect unique award IDs
        3. Enrich with USAspending.gov details
        4. Export consolidated CSV to consolidated/ directory
        """
        # Query all sources and collect both their returned dataframes and a list of unique award ids
        # FAIL-LOUD POLICY: a source that errors or returns zero rows aborts
        # the run. Every silent data loss in the 2025-2026 audit traced back
        # to a source failing open (empty frame == "no cancellations").
        for source, query_class in self.sources.items():
            try:
                df = query_class().search()
            except Exception as e:
                raise RuntimeError(
                    f"Source '{source}' failed: {e}. Aborting run - a missing "
                    f"source would silently shrink the consolidated snapshot."
                ) from e
            if df.empty:
                raise RuntimeError(
                    f"Source '{source}' returned zero rows. Historically every "
                    f"source has had nonzero results; treating empty as a "
                    f"fetch failure (see 2026-02-25 FPDS retirement and the "
                    f"recurring NPDV outages). Aborting run."
                )
            self.sources_cancellation_data[source] = df
            award_ids = df["Award ID"].astype(str).tolist()
            for award_id in award_ids:
                if award_id not in self.unique_award_ids:
                    self.unique_award_ids.append(award_id)

        self._build_claim_index()

        # Remove empty strings from the list
        self.unique_award_ids = [
            award_id for award_id in self.unique_award_ids if award_id
        ]

        # Remove award IDs that are in the ignore list
        self.unique_award_ids = [
            award_id
            for award_id in self.unique_award_ids
            if award_id not in self.ignore_award_ids
        ]

        self._fetch_awards()
        self._resolve_stragglers()

        print(f"Found {len(self.awards)} awards from USASpending API.")

        for source in self.sources:
            # Get the source award IDs from the source dataframe
            source_award_ids = (
                self.sources_cancellation_data[source]["Award ID"].astype(str).tolist()
            )
            # Add the source awards to the cancellations dictionary
            self._add_source_awards(source, source_award_ids)

        print(f"Found {len(self.unique_cancellations)} unique cancellations.")

        output_data = list(self.unique_cancellations.values())

        df = pd.DataFrame(output_data, columns=SNAPSHOT_COLUMNS)
        df.sort_values(by=["Recipient", "Latest Modification Date"], inplace=True)

        import tempfile
        import shutil

        # Make output directory if it doesn't exist
        os.makedirs("consolidated", exist_ok=True)

        csv_filename = os.path.join(
            "consolidated",
            f"nasa_contract_cancellations_{datetime.now().strftime('%Y-%m-%d')}.csv",
        )

        # Write to temp file first
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".csv", delete=False
            ) as tmp:
                tmp_path = tmp.name
                df.to_csv(tmp_path, index=False)

            # Compare with most recent existing file
            most_recent = find_most_recent_csv(
                "consolidated", "nasa_contract_cancellations", exclude_file=csv_filename
            )
            if most_recent and csv_files_equal(tmp_path, most_recent):
                print(f"No changes from prior file, skipping export to {csv_filename}")
                # Still report: an unresolved source id is a standing problem
                # whether or not today's snapshot moved.
                self._report_review_queue()
                return

            # Move temp file to final location
            shutil.move(tmp_path, csv_filename)
            tmp_path = None  # Prevent cleanup since file was moved
            print(f"CSV saved at {csv_filename}")
        finally:
            # Clean up temp file if it still exists
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

        # Validate the new snapshot against the previous accepted one.
        # On failure the snapshot is quarantined and the run exits nonzero
        # so the GitHub Action surfaces it instead of committing bad data.
        ok, messages = validate(csv_filename, most_recent)
        for msg in messages:
            print(msg)
        if not ok:
            raise SystemExit(1)

        # Merge the accepted snapshot into the append-only master ledger:
        # awards are never deleted, only reclassified (see build_master_ledger).
        #
        # Deliberately a FULL rebuild, not update_only. The incremental path
        # rebuilds the description history from the latest snapshot alone, so
        # classify() cannot see the older text it reasons over - on 2026-07-30
        # that silently downgraded six rescinded awards from `reinstated` to
        # `dropped_pending_review`. Re-reading 400 local CSVs costs ~2 seconds
        # against a run that spends minutes on API calls.
        build_master_ledger.build()

        self._report_review_queue()

    def _fetch_awards(self):
        """Fetch every award category emitted by the configured sources."""
        contracts = (
            self.client.awards.search()
            .award_ids(*self.unique_award_ids)
            .contracts()
            .all()
        )
        idvs = (
            self.client.awards.search().award_ids(*self.unique_award_ids).idvs().all()
        )
        grants = (
            self.client.awards.search().award_ids(*self.unique_award_ids).grants().all()
        )
        self.awards = contracts + idvs + grants

    def _resolve_stragglers(self):
        """Second chance for ids the batch award lookup could not match.

        The batch search takes PIIDs and FAINs. Anything still unmatched that
        looks like a USAspending *generated* id is retried through the endpoint
        that does accept one, so a source reporting the composite form is
        recovered rather than dropped. Normally a no-op: doge_search now
        extracts the FAIN up front. One request per straggler.
        """
        self.awards_by_id = {a.award_identifier: a for a in self.awards}
        stragglers = [
            award_id
            for award_id in self.unique_award_ids
            if award_id not in self.awards_by_id and is_generated_award_id(award_id)
        ]
        if not stragglers:
            return

        print(
            f"Retrying {len(stragglers)} award id(s) by generated id...",
            file=sys.stderr,
        )
        for award_id in stragglers:
            try:
                award = self.client.awards.find_by_generated_id(award_id)
            except Exception as e:  # noqa: BLE001 - one bad id must not abort
                print(f"  {award_id}: lookup failed ({e})", file=sys.stderr)
                continue
            if award is None:
                print(f"  {award_id}: not found", file=sys.stderr)
                continue
            # Indexed under the id the source used, so the lookup matches.
            # award_identifier is read-only, so it cannot be rewritten - the
            # index is what carries the alias.
            self.awards_by_id[award_id] = award
            self.awards.append(award)

    def _report_review_queue(self):
        """Print the award IDs a person needs to look at, and why.

        Three things can leave an award unaccounted for, and none of them is
        visible in the consolidated CSV itself:

          * a source flagged it but USAspending could not resolve the ID, so it
            never reached the snapshot at all;
          * it is in the ledger with a status that means "unexplained";
          * the weekly re-verification disagrees with a human verdict.
        """
        print("\n" + "=" * 66)
        print("REVIEW QUEUE")
        print("=" * 66)

        if self.unresolved:
            print(
                f"\nFlagged by a source but NOT found in USAspending "
                f"({len(self.unresolved)}) - absent from the snapshot entirely:"
            )
            for award_id, sources in sorted(self.unresolved.items()):
                print(f"   {award_id:34s} flagged by {', '.join(sorted(set(sources)))}")
            generated = [a for a in self.unresolved if is_generated_award_id(a)]
            if generated:
                print(
                    f"\n   {len(generated)} of these are USAspending generated ids "
                    f"rather than a PIID/FAIN, so the award lookup cannot match "
                    f"them.\n   The real id is embedded: ASST_NON_<FAIN>_<code>."
                )
        else:
            print("\nAll source-flagged award ids resolved against USAspending.")

        self._report_ledger_review()
        print("=" * 66)

    def _report_ledger_review(self):
        """Ledger rows whose status means 'nobody has explained this yet'."""
        if not os.path.exists(build_master_ledger.LEDGER_PATH):
            return
        with open(build_master_ledger.LEDGER_PATH, encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))

        pending = [r for r in rows if r["Status"] in UNEXPLAINED_STATUSES]
        if pending:
            print(f"\nLedger awards awaiting review ({len(pending)}):")
            for r in sorted(pending, key=lambda r: (r["Status"], r["Award ID"])):
                print(
                    f"   {r['Award ID']:18s} {r['Status']:24s} "
                    f"last seen {r['Last Seen']}"
                )
        else:
            print("\nNo ledger awards awaiting review.")

        disagree = [
            r
            for r in rows
            if r.get("Auto Status")
            and r.get("Auto Status") != r["Status"]
            and r["Status"] not in ("listed", "excluded_by_design")
        ]
        if disagree:
            print(
                f"\nMachine verdict differs from the recorded status "
                f"({len(disagree)}) - never auto-applied, review in "
                f"verification/auto_verification.csv:"
            )
            for r in sorted(disagree, key=lambda r: r["Award ID"]):
                print(
                    f"   {r['Award ID']:18s} ledger={r['Status']:22s} "
                    f"auto={r['Auto Status']}"
                )

    def _build_claim_index(self):
        """
        Record the external claim behind each award, keyed by Award ID.

        Built from CLAIM_SOURCES only. This is deliberately independent of
        which source "wins" an award's consolidated row: _add_source_awards
        keeps the first source that reported an award, so on a day when DOGE
        drops an award but NPDV still flags it, the DOGE claim would otherwise
        vanish from the snapshot entirely.
        """
        for source in CLAIM_SOURCES:
            # Keyed by the snapshot column names directly, so the claim dict
            # can be spliced into the output row without a second renaming.
            for row in self.sources_cancellation_data[source].to_dict("records"):
                award_id = _cell(row, "Award ID")
                if not award_id or award_id in self.claims:
                    continue
                self.claims[award_id] = {
                    "Claiming Source": source,
                    "Claimed Status": _cell(row, "status"),
                    "Claimed Savings": _cell(row, "savings"),
                    "Claim Date": _cell(row, "claim_date"),
                }

    def _add_source_awards(self, source_name: str, source_award_ids: List[str]):
        """
        Adds awards from a specific source to the cancellations dictionary.

        Args:
            source_name (str): The name of the source (e.g., "DOGE", "NPDV").
            source_award_ids (List[str]): A list of award IDs from the source.

        Looks each id up in self.awards_by_id, which is keyed by the id the
        *source* used - so an award recovered via its generated id still
        matches. Ids with no award are recorded in self.unresolved rather than
        dropped.
        """
        for award_id in source_award_ids:
            # Check if the award_id is already in the cancellations dictionary
            # If it is, we skip to the next award_id
            if award_id in self.unique_cancellations:
                continue
            award = self.awards_by_id.get(award_id)
            if award is not None:
                # Search the relevant source dataframe for the original description
                # and add it to the award object
                source_df = self.sources_cancellation_data[source_name]
                # Get the original description from the source dataframe
                # We use .loc to find the row where the award_id matches
                # and then get the original description
                original_description = source_df.loc[
                    source_df["Award ID"] == award_id, "description"
                ].values[0]
                if isinstance(original_description, str):
                    # Preserve source whitespace exactly while making newline
                    # representation deterministic across API responses.
                    original_description = _normalize_newlines(original_description)

                # Same .loc lookup for the source's own detection evidence.
                # Sources that infer a cancellation from award data compose a
                # sentence here; NPDV leaves it blank, and an all-blank column
                # arrives from pandas as NaN, so it is coerced rather than
                # stringified into the snapshot.
                detection = source_df.loc[
                    source_df["Award ID"] == award_id, "status"
                ].values[0]
                detection = (
                    _normalize_newlines(detection).strip()
                    if isinstance(detection, str)
                    else ""
                )

                # The USAspending API stopped returning
                # period_of_performance.last_modified_date on 2026-04-08
                # (blanked the column for every source with no code
                # change). Fall back to the latest transaction's
                # action_date, which carries the same information.
                mod_date = award.period_of_performance.last_modified_date
                if not mod_date and award.transactions:
                    mod_date = award.transactions[0].action_date

                # Keyed by column name: SNAPSHOT_COLUMNS drives the output
                # order, so a new field cannot silently land in the wrong
                # column.
                self.unique_cancellations[award_id] = {
                    "Source": source_name,
                    "District": award.recipient.location.district,
                    "Recipient": award.recipient.name,
                    "Award ID": award_id,
                    "Latest Modification Number": award.transactions[
                        0
                    ].modification_number
                    if award.transactions
                    else "",
                    "Latest Modification Date": mod_date,
                    "Start Date": award.period_of_performance.start_date,
                    "End Date": _award_end_date(award),
                    "Award Amount": award.award_amount,
                    "Total Outlays": award.total_outlay,
                    "Description": (original_description or award.description),
                    "Detection": detection,
                    "Business Categories": ", ".join(award.recipient.business_types),
                    "URL": canonical_usaspending_url(award.usa_spending_url),
                    **{
                        col: self.claims.get(award_id, {}).get(col, "")
                        for col in CLAIM_COLUMNS
                    },
                }
            else:
                # No USAspending award matched this ID, so it cannot be
                # enriched and never reaches the snapshot. Recorded rather than
                # dropped silently: a source flagged it, so somebody should
                # know why it went nowhere.
                #
                # Blank and deliberately-ignored ids are excluded from the
                # lookup upstream, so their absence here is expected, not a
                # problem to report.
                if award_id and award_id not in self.ignore_award_ids:
                    self.unresolved.setdefault(award_id, []).append(source_name)


if __name__ == "__main__":
    search = Search()
    search.search()
