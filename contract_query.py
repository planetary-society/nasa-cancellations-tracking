#!/usr/bin/env python3

import glob
import os
import re
import sys
from abc import ABC, abstractmethod
from datetime import date

import pandas as pd

from detection_methods import DETECTION_METHODS
from utils import read_rows


def load_snapshot(
    path: str, *, aliases: dict[str, str] | None = None
) -> dict[str, dict]:
    """
    Load a consolidated snapshot CSV into a dict keyed by Award ID.

    Rows without an Award ID are skipped. Shared by validate_snapshot.py and
    build_master_ledger.py so both agree on the snapshot read contract. Reads
    leniently: the 400-odd archived snapshots carry free-text descriptions
    copied from upstream, and one bad byte in one of them must not abort a
    rebuild of all of them.
    """
    rows = read_rows(path, aliases=aliases, errors="replace")
    return {r["Award ID"]: r for r in rows if r.get("Award ID")}


def find_most_recent_csv(
    directory: str, filename_base: str, exclude_file: str | None = None
) -> str | None:
    """
    Find the most recent CSV file matching the base name pattern.

    Args:
        directory: Directory to search in
        filename_base: Base filename (without date and extension)
        exclude_file: Optional file path to exclude from search (e.g., the file being written)

    Returns:
        Path to most recent matching file, or None if no matches found
    """
    pattern = os.path.join(directory, f"{filename_base}_*.csv")
    files = glob.glob(pattern)

    # Exclude the specified file if provided
    if exclude_file:
        exclude_file = os.path.normpath(exclude_file)
        files = [f for f in files if os.path.normpath(f) != exclude_file]

    if not files:
        return None

    # Extract dates from filenames and sort
    date_pattern = re.compile(r"_(\d{4}-\d{2}-\d{2})\.csv$")
    dated_files = []

    for f in files:
        match = date_pattern.search(f)
        if match:
            dated_files.append((match.group(1), f))

    if not dated_files:
        return None

    # Sort by date descending and return most recent
    dated_files.sort(key=lambda x: x[0], reverse=True)
    return dated_files[0][1]


def csv_files_equal(file1: str, file2: str) -> bool:
    """
    Compare two CSV files for byte-for-byte equality.

    This is more reliable than DataFrame comparison since it avoids
    issues with type coercion, NaN handling, and float precision.

    Args:
        file1: Path to first CSV file
        file2: Path to second CSV file

    Returns:
        True if files are identical, False otherwise
    """
    import filecmp

    return filecmp.cmp(file1, file2, shallow=False)


# --- Configuration ---
# Defines the standard column structure for the output DataFrame
FINAL_COLUMNS = [
    "Award ID",  # Unique identifier, either PIID or FAIN
    "source_type",  # e.g., 'Contract', 'Grant'
    "recipient",  # Vendor name for contracts, Recipient for grants
    "value",  # Contract or Grant dollar value
    "savings",  # Reported savings value
    "status",  # Status information (if available, e.g., FPDS status)
    "source_url",  # Link to the source record (e.g., FPDS, USASpending)
    "description",  # Description of the contract or grant purpose
    "agency",  # The agency name as found in the source record
    "claim_date",  # Date the source asserted the cancellation (claim sources only)
    # --- Tracking-window enforcement contract (see tracking_window.py) ---
    # Every source must declare these two. They are not descriptive metadata:
    # search.py gates ingest on them, so a source that leaves them blank has
    # its rows quarantined rather than admitted. That is the point - the window
    # used to be enforced source by source, and the sources that had no gate
    # (NPDV, DOGE) leaked pre-window actions straight into the ledger.
    "action_date",  # ISO date of the federal action this row detected
    "detection_basis",  # "evidence" or "inference" - see below
    "detection_method",  # structured primary signal; see detection_methods.py
]

# What a source is claiming when it sets `detection_basis`:
#
#   evidence  - the source saw a termination action directly: an FPDS action
#               code of F/N, or termination language in the transaction text.
#               Gated on action_date alone. A retroactive period-of-performance
#               end date is an ordinary closeout artifact here, and rejecting
#               those would evict real cancellations.
#
#   inference - the source deduced a cancellation from the shape of the data
#               with no termination evidence at all: an end date yanked
#               backwards, or money clawed back mid-performance. Gated on
#               action_date AND on the effect landing inside the window,
#               because an in-window mod can encode a pre-window decision.
#
# A claim source (DOGE) asserts a cancellation on someone else's authority
# rather than observing one, so it declares "evidence" and carries the claim
# date as its action date - the assertion is the action being tracked.
DETECTION_BASES = ("evidence", "inference")


def validate_source_frame(source: str, df: pd.DataFrame) -> None:
    """Check a source's output against the tracking-window contract above.

    Lives here, beside the columns it validates, rather than in the consumer
    that happens to read them - otherwise every new consumer re-derives what a
    valid row is, and a source with a typo (`"Evidence"`) survives the source
    boundary to abort the run minutes later, mid-enrichment, on one arbitrary
    award.

    Raises rather than filtering: a source that cannot describe its own
    detections is broken, and the fail-loud policy says a broken source aborts
    the run instead of quietly shrinking the snapshot.
    """
    missing = [
        col
        for col in ("action_date", "detection_basis", "detection_method")
        if col not in df
    ]
    if missing:
        raise RuntimeError(
            f"Source '{source}' returned no {' or '.join(missing)} column. "
            f"Every source must declare the action it detected and how it "
            f"detected it, so the tracking window can be enforced at ingest; "
            f"see contract_query.FINAL_COLUMNS and tracking_window.py."
        )

    bad = sorted(
        {
            str(value)
            for value in df["detection_basis"]
            if str(value) not in DETECTION_BASES
        }
    )
    if bad:
        raise RuntimeError(
            f"Source '{source}' declared detection_basis {bad}; expected one "
            f"of {list(DETECTION_BASES)}. The ingest gate cannot tell whether "
            f"the effect gate applies, and guessing would either admit "
            f"pre-window closeouts or evict real cancellations."
        )

    bad_methods = sorted(
        {
            str(value)
            for value in df["detection_method"]
            if str(value) not in DETECTION_METHODS
        }
    )
    if bad_methods:
        raise RuntimeError(
            f"Source '{source}' declared detection_method {bad_methods}; expected "
            f"one of {list(DETECTION_METHODS)}. Every included award must name "
            f"the primary signal that caused its inclusion."
        )


# --- Base Class Definition ---


class ContractQuery(ABC):
    """
    Abstract Base Class defining a common interface for querying contract/grant
    data sources. Subclasses are expected to implement the search method to
    retrieve and standardize data according to specific source requirements.
    """

    def __init__(self, final_columns: list[str] = FINAL_COLUMNS):
        """
        Initializes the query object.

        Args:
            final_columns: The desired list of column names for the output DataFrame.
                           Defaults to the globally defined FINAL_COLUMNS.
        """
        self.final_columns = final_columns
        print("Base ContractQuery initialized.", file=sys.stderr)

    def export_to_csv(self, data: pd.DataFrame, filename_base: str):
        """
        Exports the provided DataFrame to a CSV file to the data directory.
        Appends the current date to the filename. Skips writing if data
        is identical to the most recent existing file.

        Args:
            data: The DataFrame to export.
            filename_base: The base name of the output CSV file.
        """
        import shutil
        import tempfile

        # Ensure the data directory exists
        os.makedirs("data", exist_ok=True)

        # Construct the full file path
        filename = os.path.join(
            "data", f"{filename_base}_{date.today().strftime('%Y-%m-%d')}.csv"
        )

        # Write to temp file first
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".csv", delete=False
            ) as tmp:
                tmp_path = tmp.name
                data.to_csv(tmp_path, index=False)

            # Compare with most recent existing file
            most_recent = find_most_recent_csv(
                "data", filename_base, exclude_file=filename
            )
            if most_recent and csv_files_equal(tmp_path, most_recent):
                print(
                    f"No changes from prior file, skipping export to {filename}",
                    file=sys.stderr,
                )
                return

            # Move temp file to final location
            shutil.move(tmp_path, filename)
            tmp_path = None  # Prevent cleanup since file was moved
            print(f"Data exported to {filename}", file=sys.stderr)
        finally:
            # Clean up temp file if it still exists
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

    @abstractmethod
    def search(self, **kwargs) -> pd.DataFrame:
        """
        Performs a search based on provided criteria, fetches data from the
        specific source, filters, standardizes, and returns the results
        as a Pandas DataFrame conforming to self.final_columns.

        Args:
            **kwargs: Search criteria specific to the subclass implementation
                      (e.g., date_range, keywords). The exact parameters
                      depend on the data source and subclass implementation.

        Returns:
            A Pandas DataFrame with the search results, structured according
            to self.final_columns.

        This method MUST be implemented by subclasses.
        """
