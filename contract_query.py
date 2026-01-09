#!/usr/bin/env python3

import pandas as pd
import sys
import os
import glob
import re
from datetime import date
from typing import List, Optional
from abc import ABC, abstractmethod


def find_most_recent_csv(directory: str, filename_base: str) -> Optional[str]:
    """
    Find the most recent CSV file matching the base name pattern.

    Args:
        directory: Directory to search in
        filename_base: Base filename (without date and extension)

    Returns:
        Path to most recent matching file, or None if no matches found
    """
    pattern = os.path.join(directory, f"{filename_base}_*.csv")
    files = glob.glob(pattern)

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


def dataframes_equal(df1: pd.DataFrame, df2: pd.DataFrame, sort_col: str = 'Award ID') -> bool:
    """
    Compare two DataFrames for equality after sorting.

    Args:
        df1: First DataFrame
        df2: Second DataFrame
        sort_col: Column to sort by before comparison

    Returns:
        True if DataFrames are equal, False otherwise
    """
    # Check if columns match
    if set(df1.columns) != set(df2.columns):
        return False

    # Check if row counts match
    if len(df1) != len(df2):
        return False

    # Sort both DataFrames by sort column if it exists
    if sort_col in df1.columns:
        df1_sorted = df1.sort_values(by=sort_col).reset_index(drop=True)
        df2_sorted = df2.sort_values(by=sort_col).reset_index(drop=True)
    else:
        df1_sorted = df1.reset_index(drop=True)
        df2_sorted = df2.reset_index(drop=True)

    # Ensure columns are in same order
    df2_sorted = df2_sorted[df1_sorted.columns]

    return df1_sorted.equals(df2_sorted)

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
        # Ensure the data directory exists
        os.makedirs("data", exist_ok=True)

        # Construct the full file path
        filename = os.path.join("data", f"{filename_base}_{date.today().strftime('%Y-%m-%d')}.csv")

        # Check if data matches most recent existing file
        most_recent = find_most_recent_csv("data", filename_base)
        if most_recent:
            try:
                existing_data = pd.read_csv(most_recent)
                if dataframes_equal(data, existing_data):
                    print(f"No changes from prior file, skipping export to {filename}", file=sys.stderr)
                    return
            except Exception as e:
                print(f"Warning: Could not compare with prior file: {e}", file=sys.stderr)

        data.to_csv(filename, index=False)
        print(f"Data exported to {filename}", file=sys.stderr)

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