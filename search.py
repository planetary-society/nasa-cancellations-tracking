import logging
import os
import shutil
import sys
import tempfile
from datetime import datetime

import pandas as pd
from usaspending import Award, USASpendingClient
from usaspending.exceptions import USASpendingError

import award_period_change_facts
import award_transaction_facts as transaction_facts
import build_master_ledger
import sources
from contract_query import csv_files_equal, find_most_recent_csv, validate_source_frame
from doge_search import DOGEQuery
from initial_end_dates import (
    TRANSIENT_STATUSES,
    InitialEndDateResult,
    InitialEndDateTarget,
    initial_end_date_category,
)
from local_usaspending_mirror_query import (
    LocalMirrorUnavailableError,
    LocalUSASpendingMirrorQuery,
)
from nasa_grants_query import NASAGrantsQuery
from npdv_query import NPDVQuery
from tracking_window import TRACKING_WINDOW_START, in_window
from usaspending_terminations_query import USASpendingTerminationsQuery
from utils import (
    canonical_generated_award_id,
    canonical_usaspending_url,
    congressional_district,
    is_generated_award_id,
    read_rows,
    write_sidecar_csv,
)
from validate_snapshot import validate

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
    sources.DOGE: DOGEQuery,
    sources.NPDV: NPDVQuery,
    sources.NASA_GRANTS: NASAGrantsQuery,
    sources.USASPENDING_TERMINATIONS: USASpendingTerminationsQuery,
    # Last on purpose: dict order is first-source-wins for an award's snapshot
    # row, and the mirror is a local Postgres depth net that lags the live API
    # by 2-6 weeks (and replays its last export when the DB is unreachable),
    # so it should only own rows no other source found.
    sources.LOCAL_MIRROR: LocalUSASpendingMirrorQuery,
}

# Sources that publish an external *claim* of a cancellation, as opposed to
# sources where we infer one from award data. A claim is the fact being
# tracked, so it is recorded even when the award turns out to have merely
# expired or grown - and it is attached to the consolidated row regardless of
# which source won that row (see _build_claim_index).
CLAIM_SOURCES = frozenset({sources.DOGE})

# Ledger statuses that mean "left the snapshot and nobody has said why yet".
# Everything else is either currently flagged, adjudicated, or excluded on
# purpose - see the build_master_ledger docstring for the full vocabulary.
UNEXPLAINED_STATUSES = {"unflagged_pending_review", "needs_manual_review"}

# Claim fields: what an external source asserted, kept separate from what the
# award data shows actually happened. Imported, not restated: the ledger treats
# exactly these columns as write-once, so a fifth claim column listed here but
# not there would be refresh-clobbered instead of retained - which is the
# failure STICKY_COLUMNS exists to prevent.
CLAIM_COLUMNS = build_master_ledger.STICKY_COLUMNS

# Column order of the consolidated snapshot CSV.
SNAPSHOT_COLUMNS = [
    "Source",
    "Recipient Congressional District",
    "Recipient Name",
    "Award ID",
    "Latest Modification Number",
    *build_master_ledger.TRANSACTION_HISTORY_COLUMNS,
    "Start Date",
    "Current End Date",
    # Which measure the column above carries. USAspending publishes no period
    # of performance for an IDV, so those rows fall back to the ordering-period
    # boundary - a different thing, in the same column.
    "End Date Basis",
    "Initial Reported End Date",
    "Current Obligated Amount",
    "Total Outlays",
    "Award or Action Description",
    # Structured counterpart to Detection Evidence: the primary signal that caused the
    # winning source to include this award.
    "Primary Detection Method",
    # Why the winning source flagged this award, in its own words. Each query
    # module already composes one ("Terminate-for-convenience action P00180 on
    # 2026-05-06"); until this column existed it was dropped here, so the
    # published data could say an award was cancelled but never on what
    # evidence. Distinct from DOGE Claimed Status, an outside assertion.
    "Detection Evidence",
    "Recipient Business Categories",
    "USAspending URL",
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


PERIOD_OF_PERFORMANCE_BASIS = "period_of_performance"
IDV_ORDERING_PERIOD_BASIS = "idv_last_date_to_order"


def _award_end_date(award: Award) -> tuple[str, str]:
    """The applicable end date for an award or IDV vehicle, and its basis.

    USAspending's IDV search response reports the ordering-period boundary as
    ``Last Date to Order`` while leaving the generic period end absent. Keep
    the regular period end authoritative and use the IDV field only as its
    documented fallback.

    The basis is returned rather than inferred downstream because this is the
    only place that knows which branch fired. An IDV ordering-period boundary
    is when orders may no longer be placed, not when work ends, so a column
    holding both measures is unusable in aggregate unless each row says which
    one it carries. It cannot be recovered from the award category alone: an
    IDV that does publish a period end is reported on the same footing as any
    other award.
    """
    end_date = award.period_of_performance.end_date
    if end_date:
        return end_date, PERIOD_OF_PERFORMANCE_BASIS
    if award.category == "idv":
        raw = award.raw or {}
        end_date = raw.get("Last Date to Order") or raw.get("last_date_to_order")
        if end_date:
            return end_date, IDV_ORDERING_PERIOD_BASIS
    return "", ""


def _history_key(award: Award) -> str:
    """Cache key for one award's transaction history and derived facts."""
    generated_id = str(getattr(award, "generated_unique_award_id", "") or "")
    return canonical_generated_award_id(generated_id) or str(
        getattr(award, "award_identifier", "") or ""
    )


def _generated_id_from_url(value: str) -> str:
    marker = "/award/"
    if marker not in (value or ""):
        return ""
    return value.split(marker, 1)[1].split("/", 1)[0]


def _initial_end_date_row(result: InitialEndDateResult, checked: str) -> dict:
    return {
        "Award ID": result.award_id,
        "Generated Award ID": result.generated_award_id,
        "Award Category": result.category,
        "Initial Reported End Date": result.initial_end_date,
        "Source Transaction ID": result.transaction_id,
        "Source Action Date": result.action_date,
        "Source Modification Number": result.modification_number,
        "Source Basis": result.basis,
        "Lookup Status": result.status,
        "Last Checked Date": checked,
    }


def _write_initial_end_dates(rows: dict[str, dict]) -> None:
    """Atomically replace the machine-owned Initial End Date sidecar."""
    write_sidecar_csv(
        build_master_ledger.INITIAL_END_DATES_PATH,
        build_master_ledger.INITIAL_END_DATE_COLUMNS,
        rows,
    )


class Search:
    """
    Orchestrates the contract/grant cancellation search across multiple data sources.

    Queries DOGE API, NPDV CSV, NASA Grants API, and USAspending transaction search
    for potential NASA award cancellations/terminations, then enriches results with
    USAspending.gov data.
    """

    def __init__(self):
        self.client = USASpendingClient()
        self.sources = dict(SOURCES)
        # Populated by _collect_source_data, which is the single place a source
        # is skipped: an unconfigured or unreachable mirror raises the narrow
        # LocalMirrorUnavailableError from search() and is recorded there. A
        # second gate here produced the same end state by a second mechanism.
        self.skipped_sources: set[str] = set()
        self.sources_cancellation_data: dict[
            str, pd.DataFrame
        ] = {}  # key: source name, value: source dataframe
        self.unique_award_ids: list[str] = []
        self.unique_cancellations: dict[
            str, dict
        ] = {}  # key: award_id, value: snapshot row keyed by column name
        self.awards: list[Award] = []
        self.awards_by_id: dict[
            str, Award
        ] = {}  # keyed by the id the SOURCE used, which may be a generated id
        self.claims: dict[
            str, dict[str, str]
        ] = {}  # key: award_id, value: claim fields
        self.unresolved: dict[
            str, list[str]
        ] = {}  # key: award_id, value: sources that flagged it
        self.initial_end_date_rows: dict[str, dict] = {}
        self.initial_end_dates_changed = False
        # Complete award transaction histories, loaded lazily and at most once
        # per generated award id.  Several source rows can resolve to the same
        # award, and both the tracking-window gate and snapshot enrichment need
        # the history; neither is allowed to issue its own duplicate lookup.
        self.transaction_histories: dict[str, list] = {}
        self.history_facts: dict[str, object] = {}
        self.transaction_facts_changed = False
        # Rows the tracking window kept out, with the reason for each. Reported
        # at the end of every run: an exclusion the operator cannot see is
        # indistinguishable from a source quietly breaking.
        self.window_rejects: list[dict[str, str]] = []
        self.ignore_award_ids: list[str] = [
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
        self._collect_source_data()
        self._filter_nasa_grant_period_changes()

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
        self._enrich_initial_reported_end_dates()

        print(f"Found {len(self.awards)} awards from USASpending API.")

        for source in self.sources_cancellation_data:
            # Get the source award IDs from the source dataframe
            source_award_ids = (
                self.sources_cancellation_data[source]["Award ID"].astype(str).tolist()
            )
            # Add the source awards to the cancellations dictionary
            self._add_source_awards(source, source_award_ids)

        # Persist successful complete-history facts independently of whether
        # the candidate snapshot below is accepted or quarantined.
        self._enrich_transaction_facts()

        print(f"Found {len(self.unique_cancellations)} unique cancellations.")

        output_data = list(self.unique_cancellations.values())

        df = pd.DataFrame(output_data, columns=SNAPSHOT_COLUMNS)
        df.sort_values(by=["Recipient Name", "Latest Action Date"], inplace=True)

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
                if self.initial_end_dates_changed or self.transaction_facts_changed:
                    # A historical-only award can gain its first transaction
                    # provenance without changing the current daily snapshot.
                    build_master_ledger.build()
                # Still report: an unresolved source id is a standing problem
                # whether or not today's snapshot moved.
                self._report_window_rejects()
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
        ok, messages = validate(
            csv_filename,
            most_recent,
            skipped_sources=getattr(self, "skipped_sources", ()),
        )
        for msg in messages:
            print(msg)
        if not ok:
            # The candidate remains quarantined, but successful enrichment is
            # independent metadata. Rebuild strictly from accepted snapshots
            # and the sidecars so membership/status cannot leak across the
            # validation boundary.
            if self.initial_end_dates_changed or self.transaction_facts_changed:
                build_master_ledger.build()
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

        self._report_window_rejects()
        self._report_review_queue()

    def _collect_source_data(self) -> None:
        """Query sources, skipping only an unavailable local mirror."""
        # Query all sources and collect both their returned dataframes and a
        # list of unique award ids.
        # FAIL-LOUD POLICY: a source that errors or returns zero rows aborts
        # the run. Every silent data loss in the 2025-2026 audit traced back
        # to a source failing open (empty frame == "no cancellations").
        for source, query_class in self.sources.items():
            try:
                df = query_class().search()
            except LocalMirrorUnavailableError as e:
                if source != sources.LOCAL_MIRROR:
                    raise RuntimeError(
                        f"Source '{source}' incorrectly reported a local-mirror "
                        f"availability error: {e}"
                    ) from e
                self.skipped_sources.add(source)
                print(f"Skipping {source}: {e}.", file=sys.stderr)
                continue
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
            df = self._enforce_declared_window(source, df)
            self.sources_cancellation_data[source] = df
            award_ids = df["Award ID"].astype(str).tolist()
            for award_id in award_ids:
                if award_id not in self.unique_award_ids:
                    self.unique_award_ids.append(award_id)

    def _filter_nasa_grant_period_changes(self) -> None:
        """Require a persisted mirror transaction fact for NASA Grants rows.

        The NASA Grants API labels these rows with a workflow task containing
        both ``Decrease`` and ``Change Pop End Date``. That label does not say
        which transaction changed the date, what the prior date was, or even
        that the date (rather than another administrative field) decreased.
        Only the persisted result of the mirror's complete transaction walk is
        strong enough to admit the row. A temporarily unavailable mirror leaves
        prior successful facts intact; an unconfirmed new label is retried on a
        later run rather than guessed into the snapshot.
        """
        source = sources.NASA_GRANTS
        frame = self.sources_cancellation_data.get(source)
        if frame is None or frame.empty:
            return

        facts = award_period_change_facts.load_facts()
        keep_indices = []
        rejected: list[str] = []
        for index, row in frame.iterrows():
            award_id = _cell(row, "Award ID")
            fact = facts.get(award_id)
            if fact is None:
                rejected.append(award_id)
                continue
            keep_indices.append(index)
            # Replace the ambiguous NASA workflow prose with the exact
            # transaction-level fact that confirmed it, including the actual
            # action date used by the downstream tracking-window gate.
            frame.at[index, "status"] = award_period_change_facts.detection_text(fact)
            frame.at[index, "action_date"] = fact["Action Date"]
            frame.at[index, "detection_basis"] = "inference"

        kept = frame.loc[keep_indices].copy()
        if kept.empty and sources.LOCAL_MIRROR in self.skipped_sources:
            # Nothing could be confirmed, and the only thing that can produce a
            # confirmation did not run. That is an unknown, not a zero, and the
            # difference decides whether the snapshot is publishable: reported
            # as zero it trips the source-presence and shrinkage guards and
            # quarantines every run for as long as the mirror is away, which
            # freezes the ledger without saying so anywhere a reader can see.
            # Declared skipped, it degrades exactly as the mirror it depends on
            # already does. A partial confirmation is NOT a skip - prior facts
            # outliving the mirror is the documented behaviour above.
            self.skipped_sources.add(source)
            print(
                f"Skipping {source}: no period-change facts are available to "
                f"confirm its {len(rejected)} candidate(s), and "
                f"{sources.LOCAL_MIRROR} - their only producer - was skipped "
                f"this run.",
                file=sys.stderr,
            )
        self.sources_cancellation_data[source] = kept

        # These candidates were needed for enrichment so the comparison could
        # be made, but after rejection they are no longer current source awards
        # unless another source independently detected them.
        remaining_ids = {
            str(award_id)
            for source_frame in self.sources_cancellation_data.values()
            for award_id in source_frame["Award ID"].dropna().tolist()
            if str(award_id)
        }
        self.unique_award_ids = [
            award_id for award_id in self.unique_award_ids if award_id in remaining_ids
        ]

        if rejected:
            print(
                f"{source}: excluded {len(rejected)} unconfirmed period-change "
                f"candidate(s); no qualifying mirror transaction fact:",
                file=sys.stderr,
            )
            for award_id in rejected:
                print(f"  {award_id}", file=sys.stderr)

    def _reject(self, source: str, award_id: str, reason: str) -> None:
        """Record one row the tracking window kept out, for the end-of-run report."""
        self.window_rejects.append(
            {"Award ID": award_id, "Source": source, "Reason": reason}
        )

    def _enforce_declared_window(self, source: str, df: pd.DataFrame) -> pd.DataFrame:
        """Drop rows whose source-declared action date predates the window.

        First of the two ingest gates. It runs before enrichment, on the
        source's own declaration, so a pre-window row never even costs an API
        lookup. The second gate (_passes_tracking_window) runs after
        enrichment, where the award's real transaction history is available.

        A blank declaration is NOT dropped here: it means the source could not
        observe an action date, not that the action is out of window. The
        second gate derives a real date from USAspending and re-gates, so
        dropping blanks here would delete such a source's entire contribution
        before the derivation ever ran.
        """
        validate_source_frame(source, df)

        # Blanks pass this gate undecided; only a date that is present AND
        # pre-window is rejected here.
        keep = df["action_date"].map(
            lambda v: pd.isna(v) or not str(v).strip() or in_window(v)
        )
        for _, row in df[~keep].iterrows():
            self._reject(
                source,
                _cell(row, "Award ID"),
                f"action date {_cell(row, 'action_date')} precedes tracking "
                f"window start {TRACKING_WINDOW_START}",
            )
        return df[keep]

    def _ledger_awards(self) -> list[tuple[str, str]]:
        """(award id, generated award id) for every award in the stored ledger.

        Both sidecars backfill ledger-only awards using the same extraction, so
        it lives here rather than being restated per sidecar, and the memo
        keeps it to one parse across its callers. Returns [] when no ledger
        exists yet, which is the first-run case.
        """
        cache = getattr(self, "_ledger_award_ids", None)
        if cache is None:
            cache = []
            if os.path.exists(build_master_ledger.LEDGER_PATH):
                for row in read_rows(build_master_ledger.LEDGER_PATH):
                    aid = (row.get("Award ID") or "").strip()
                    if aid:
                        url = row.get("USAspending URL") or ""
                        cache.append((aid, _generated_id_from_url(url)))
            self._ledger_award_ids = cache
        return cache

    def _cache(self, name: str) -> dict:
        """Return a named per-run memo dict, creating it on first use.

        Some unit tests construct Search without calling __init__, so every
        cache attribute has to tolerate not existing yet. Doing that in one
        place keeps the three memoised lookups below to their actual logic.
        """
        cache = getattr(self, name, None)
        if cache is None:
            cache = {}
            setattr(self, name, cache)
        return cache

    def _transaction_history(self, award: Award) -> list:
        """Fetch and cache one award's complete transaction history.

        This is one ORM query walk, which can span more than one HTTP page for
        unusually large awards.  The returned list is the sole input for the
        first/latest action fields, formal termination/closeout provenance,
        and the derived tracking-window action date.
        """
        cache = self._cache("transaction_histories")
        key = _history_key(award)
        if key not in cache:
            query = award.transactions
            if hasattr(query, "order_by"):
                cache[key] = transaction_facts.fetch_transaction_query(query)
            else:
                # Test doubles and callers may already hold a materialized
                # history.  Copy it into the same cache rather than imposing
                # the ORM interface on an already-complete list.
                cache[key] = list(query)
        return cache[key]

    def _transaction_history_by_generated_id(self, generated_award_id: str) -> list:
        """Fetch a ledger-only award while sharing the per-run history cache."""
        cache = self._cache("transaction_histories")
        key = canonical_generated_award_id(generated_award_id)
        if not key:
            raise ValueError("missing generated award id")
        if key not in cache:
            cache[key] = transaction_facts.fetch_transactions(self.client, key)
        return cache[key]

    def _history_facts(self, award: Award):
        """The transaction-derived facts for one award, computed once.

        A pure function of the history and the action-code vocabulary, so an
        award revisited by a second source re-uses the first source's result
        instead of re-sorting and re-scanning the whole history.
        """
        cache = self._cache("history_facts")
        award_key = _history_key(award)
        if award_key not in cache:
            cache[award_key] = transaction_facts.transaction_history_facts(
                self._transaction_history(award),
                is_contract=transaction_facts.uses_contract_action_codes(
                    getattr(award, "generated_unique_award_id", ""),
                    award.category,
                ),
            )
        return cache[award_key]

    def _passes_tracking_window(self, source: str, award_id: str, award, row) -> bool:
        """Second ingest gate: does this award belong in the window at all?

        Runs after USAspending enrichment, which is what makes it a genuine
        backstop rather than a restatement of what the source already claimed:

        1. The action date is the source's declaration when it made one, and
           otherwise is DERIVED from the award's latest USAspending
           transaction. An award whose most recent federal action predates the
           window had nothing done to it inside the window, whatever a source
           believes.

        2. For an `inference` detection, the EFFECT must land in the window
           too - see contract_query.DETECTION_BASES for what that means and
           README "The Tracking Window" for the case that motivated it.
        """
        declared = _cell(row, "action_date")
        if declared:
            effective, origin = declared, "declared"
        else:
            # Only this branch needs the history, so an award the source dated
            # itself is gated without paying for a transaction walk it may be
            # about to discard.
            latest = self._history_facts(award).latest
            effective = transaction_facts.transaction_value(latest, "action_date")
            origin = "derived from latest USAspending transaction"

        if not in_window(effective):
            self._reject(
                source,
                award_id,
                f"action date {effective or '(none)'} ({origin}) precedes "
                f"tracking window start {TRACKING_WINDOW_START}",
            )
            return False

        # The basis vocabulary is validated per source in _enforce_declared_window,
        # so by here it can only be one of DETECTION_BASES.
        if _cell(row, "detection_basis") == "inference":
            end_date, _basis = _award_end_date(award)
            if not in_window(end_date):
                self._reject(
                    source,
                    award_id,
                    f"inferred cancellation, but period of performance ends "
                    f"{end_date or '(none)'}, before tracking window start "
                    f"{TRACKING_WINDOW_START}; the action on {effective} is "
                    f"closeout of an earlier decision",
                )
                return False

        return True

    def _report_window_rejects(self):
        """Print every row the tracking window kept out, and why.

        Rejections are reported, never silent. A gate that quietly shrinks the
        snapshot is indistinguishable from a source breaking - which is the
        same failure mode the fail-loud policy above exists to prevent.

        Awards that a later source went on to claim on its own evidence are
        labelled rather than dropped from the report: the exclusion really
        happened, but reporting it unqualified would say an award is missing
        from the snapshot when it is in fact present.
        """
        if not self.window_rejects:
            return
        print(
            f"\nTracking window ({TRACKING_WINDOW_START}) excluded "
            f"{len(self.window_rejects)} row(s):"
        )
        for reject in self.window_rejects:
            aid = reject["Award ID"]
            admitted = (
                " (admitted via another source)"
                if aid in self.unique_cancellations
                else ""
            )
            print(f"  {aid} [{reject['Source']}] - {reject['Reason']}{admitted}")

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

    def _enrich_initial_reported_end_dates(self):
        """Backfill ledger awards and enrich every resolved current award."""
        existing = build_master_ledger.load_initial_end_dates()
        targets: dict[str, InitialEndDateTarget] = {}

        for aid, gid in self._ledger_awards():
            if aid in existing:
                continue
            targets[aid] = InitialEndDateTarget(
                aid, gid, initial_end_date_category(gid)
            )

        # Current award objects carry the authoritative generated id and
        # therefore replace any less-complete target recovered from the stored
        # ledger, whose URL may not have been canonicalised.
        for aid in self.unique_award_ids:
            if aid in existing:
                continue
            award = self.awards_by_id.get(aid)
            if award is None:
                continue
            gid = str(getattr(award, "generated_unique_award_id", "") or "")
            targets[aid] = InitialEndDateTarget(
                aid, gid, initial_end_date_category(gid)
            )

        today = datetime.now().date().isoformat()
        fetchable = []
        new_results = []
        for target in targets.values():
            if target.generated_award_id and target.category:
                fetchable.append(target)
            else:
                # No usable generated id yet - typically a ledger row whose URL
                # has not been canonicalised. Transient, not a verdict on the
                # award, so it is retried rather than recorded (see
                # TRANSIENT_STATUSES).
                new_results.append(
                    InitialEndDateResult.unresolved(target, "unsupported_award_id")
                )

        # Mirror-only, by design - see initial_end_dates. Skipped without
        # dialling when the run has already established the mirror is
        # unavailable; the except is the narrow guard for a host that dies
        # between the source query and here.
        if fetchable and sources.LOCAL_MIRROR not in self.skipped_sources:
            try:
                new_results.extend(
                    LocalUSASpendingMirrorQuery().fetch_initial_reported_end_dates(
                        fetchable
                    )
                )
            except LocalMirrorUnavailableError as exc:
                print(
                    f"Skipping Initial Reported End Date enrichment for "
                    f"{len(fetchable)} award(s): {exc}. Values already recorded "
                    f"in {build_master_ledger.INITIAL_END_DATES_PATH} are "
                    f"unaffected.",
                    file=sys.stderr,
                )

        transient = [r for r in new_results if r.status in TRANSIENT_STATUSES]
        if transient:
            print(
                f"{len(transient)} award(s) left unresolved this run "
                f"(transient); retrying next run.",
                file=sys.stderr,
            )

        merged = dict(existing)
        for result in new_results:
            # A transient status describes this RUN, not the award. The sidecar
            # is write-once and this method skips anything already in it, so
            # persisting one would retire the award from lookup forever over a
            # condition that resolves itself.
            if result.status in TRANSIENT_STATUSES:
                continue
            # Existing terminal results are never replaced automatically. A
            # deliberate removal from the sidecar is the refresh mechanism.
            if result.award_id not in merged:
                merged[result.award_id] = _initial_end_date_row(result, today)

        self.initial_end_dates_changed = merged != existing
        if self.initial_end_dates_changed:
            _write_initial_end_dates(merged)
        self.initial_end_date_rows = merged

    def _enrich_transaction_facts(self) -> None:
        """Persist current histories and backfill missing ledger-only awards.

        This sidecar is deliberately written before snapshot validation.  It
        contains only facts from complete successful transaction lookups, so a
        candidate may be quarantined without discarding useful enrichment or
        changing which awards belong to the authoritative snapshot.
        """
        existing = transaction_facts.load_facts()
        merged = dict(existing)
        checked = datetime.now().date().isoformat()
        attempted: set[str] = set()
        unresolved: list[tuple[str, str]] = []

        # Current source awards refresh daily.  Several source ids may resolve
        # to one generated award id; _transaction_history caches by that id so
        # each complete history is still fetched at most once per run.
        for aid in self.unique_award_ids:
            award = self.awards_by_id.get(aid)
            if award is None:
                continue
            attempted.add(aid)
            generated_id = canonical_generated_award_id(
                str(getattr(award, "generated_unique_award_id", "") or "")
            )
            try:
                merged[aid] = transaction_facts.build_fact_row(
                    aid,
                    generated_id,
                    getattr(award, "category", ""),
                    self._transaction_history(award),
                    checked=checked,
                    # Already derived for the tracking-window gate and the
                    # snapshot row; re-deriving would sort and scan the whole
                    # history a second time.
                    facts=self._history_facts(award),
                )
            except (USASpendingError, OSError, ValueError) as exc:
                unresolved.append((aid, str(exc)))

        # A first run also backfills awards that live only in the append-only
        # ledger.  Once a row exists it is refreshed by weekly re-verification,
        # not by adding hundreds of daily calls here.
        for aid, gid in self._ledger_awards():
            if aid in merged or aid in attempted:
                continue
            attempted.add(aid)
            generated_id = canonical_generated_award_id(gid)
            try:
                merged[aid] = transaction_facts.build_fact_row(
                    aid,
                    generated_id,
                    transaction_facts.award_category(generated_id),
                    self._transaction_history_by_generated_id(generated_id),
                    checked=checked,
                )
            except (USASpendingError, OSError, ValueError) as exc:
                unresolved.append((aid, str(exc)))

        self.transaction_facts_changed = merged != existing
        if self.transaction_facts_changed:
            transaction_facts.write_facts(merged)

        if unresolved:
            print(
                f"{len(unresolved)} transaction-history enrichment lookup(s) "
                f"left unresolved; prior facts were retained and missing "
                f"awards will be retried.",
                file=sys.stderr,
            )
            for aid, error in unresolved:
                print(f"  {aid}: {error}", file=sys.stderr)

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
        rows = read_rows(build_master_ledger.LEDGER_PATH)

        pending = [r for r in rows if r["Tracking Status"] in UNEXPLAINED_STATUSES]
        if pending:
            print(f"\nLedger awards awaiting review ({len(pending)}):")
            for r in sorted(
                pending, key=lambda r: (r["Tracking Status"], r["Award ID"])
            ):
                print(
                    f"   {r['Award ID']:18s} {r['Tracking Status']:24s} "
                    f"last seen {r['Last Flagged Date']}"
                )
        else:
            print("\nNo ledger awards awaiting review.")

        disagree = [
            r
            for r in rows
            if r.get("Automated Verdict")
            and r.get("Automated Verdict") != r["Tracking Status"]
            and r["Tracking Status"] not in ("currently_flagged", "excluded_by_design")
        ]
        if disagree:
            print(
                f"\nMachine verdict differs from the recorded status "
                f"({len(disagree)}) - never auto-applied, review in "
                f"verification/auto_verification.csv:"
            )
            for r in sorted(disagree, key=lambda r: r["Award ID"]):
                print(
                    f"   {r['Award ID']:18s} ledger={r['Tracking Status']:22s} "
                    f"auto={r['Automated Verdict']}"
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
                    "Claimed By": source,
                    "DOGE Claimed Status": _cell(row, "status"),
                    "DOGE Claimed Savings": _cell(row, "savings"),
                    "DOGE Claim Date": _cell(row, "claim_date"),
                }

    def _add_source_awards(self, source_name: str, source_award_ids: list[str]):
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
        # Indexed once per source rather than rescanned per award: the row is
        # needed three times below (window gate, description, detection), and
        # a boolean mask over the whole frame for each award made this
        # quadratic in the source's row count. First row wins, matching the
        # .iloc[0] this replaced.
        source_df = self.sources_cancellation_data[source_name]
        rows_by_id = {}
        for _, source_row in source_df.iterrows():
            rows_by_id.setdefault(str(source_row["Award ID"]), source_row)

        for award_id in source_award_ids:
            # Check if the award_id is already in the cancellations dictionary
            # If it is, we skip to the next award_id
            if award_id in self.unique_cancellations:
                continue
            award = self.awards_by_id.get(award_id)
            if award is not None:
                # The award ids came from this frame, so the row is always
                # there; indexing rather than guarding keeps a future change
                # that breaks that assumption loud.
                source_row = rows_by_id[award_id]

                # The tracking-window backstop, applied here because this is
                # the one place that has both the source's claim and the
                # enriched award. A rejected award is not written to the
                # snapshot at all, and `continue` (rather than break) lets a
                # LATER source still claim it on its own evidence.
                #
                # Gated BEFORE the transaction history is fetched: a rejected
                # award needs at most the latest action date, so a full paged
                # walk here would be thrown away for every exclusion.
                if not self._passes_tracking_window(
                    source_name, award_id, award, source_row
                ):
                    continue

                # One complete transaction fetch supplies every transaction-
                # derived field below.  The cache also covers the case where a
                # source's row is rejected by the tracking window and a later
                # source independently admits the same award.
                history_facts = self._history_facts(award)

                original_description = source_row["description"]
                if isinstance(original_description, str):
                    # Preserve source whitespace exactly while making newline
                    # representation deterministic across API responses.
                    original_description = _normalize_newlines(original_description)

                # The source's own detection evidence. Sources that infer a
                # cancellation from award data compose a sentence here; NPDV
                # leaves it blank, and an all-blank column arrives from pandas
                # as NaN, so it is coerced rather than stringified into the
                # snapshot.
                detection = _cell(source_row, "status")
                # Hoisted so the dict below stays one column per line: it is
                # read as a table against SNAPSHOT_COLUMNS, and wrapped entries
                # are what make a misplaced field hard to spot.
                district = congressional_district(award.recipient.location)
                end_date, end_date_basis = _award_end_date(award)
                description = original_description or award.description
                categories = ", ".join(award.recipient.business_types)
                url = canonical_usaspending_url(award.usa_spending_url)
                initial_end = (
                    getattr(self, "initial_end_date_rows", {})
                    .get(award_id, {})
                    .get("Initial Reported End Date", "")
                )

                # Keyed by column name: SNAPSHOT_COLUMNS drives the output
                # order, so a new field cannot silently land in the wrong
                # column.
                self.unique_cancellations[award_id] = {
                    "Source": source_name,
                    "Recipient Congressional District": district,
                    "Recipient Name": award.recipient.name,
                    "Award ID": award_id,
                    # Same producer as the sidecar row, so the two cannot
                    # disagree about a transaction-derived column.
                    **transaction_facts.history_columns(history_facts),
                    "Start Date": award.period_of_performance.start_date,
                    "Current End Date": end_date,
                    "End Date Basis": end_date_basis,
                    # Derived independently of which source won this snapshot
                    # row, so a DOGE/NPDV row can retain USAspending history.
                    "Initial Reported End Date": initial_end,
                    "Current Obligated Amount": award.award_amount,
                    "Total Outlays": award.total_outlay,
                    "Award or Action Description": description,
                    "Primary Detection Method": _cell(source_row, "detection_method"),
                    "Detection Evidence": detection,
                    "Recipient Business Categories": categories,
                    "USAspending URL": url,
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
