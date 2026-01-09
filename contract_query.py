#!/usr/bin/env python3

import pandas as pd
import sys
import os
import glob
import re
from datetime import date
from typing import List, Optional
from abc import ABC, abstractmethod


def find_most_recent_csv(directory: str, filename_base: str, exclude_file: Optional[str] = None) -> Optional[str]:
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
    date_pattern = re.compile(r'_(\d{4}-\d{2}-\d{2})\.csv$')
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
    'Award ID',    # Unique identifier, either PIID or FAIN
    'source_type', # e.g., 'Contract', 'Grant'
    'recipient',   # Vendor name for contracts, Recipient for grants
    'value',       # Contract or Grant dollar value
    'savings',     # Reported savings value
    'status',      # Status information (if available, e.g., FPDS status)
    'source_url',  # Link to the source record (e.g., FPDS, USASpending)
    'description', # Description of the contract or grant purpose
    'agency'       # The agency name as found in the source record
]

# --- Base Class Definition ---

class ContractQuery(ABC):
    """
    Abstract Base Class defining a common interface for querying contract/grant
    data sources. Subclasses are expected to implement the search method to
    retrieve and standardize data according to specific source requirements.
    """

    def __init__(self, final_columns: List[str] = FINAL_COLUMNS):
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
        import tempfile
        import shutil

        # Ensure the data directory exists
        os.makedirs("data", exist_ok=True)

        # Construct the full file path
        filename = os.path.join("data", f"{filename_base}_{date.today().strftime('%Y-%m-%d')}.csv")

        # Write to temp file first
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as tmp:
                tmp_path = tmp.name
                data.to_csv(tmp_path, index=False)

            # Compare with most recent existing file
            most_recent = find_most_recent_csv("data", filename_base, exclude_file=filename)
            if most_recent and csv_files_equal(tmp_path, most_recent):
                print(f"No changes from prior file, skipping export to {filename}", file=sys.stderr)
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
        pass