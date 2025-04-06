#!/usr/bin/env python3

import pandas as pd
import sys
import os
from datetime import date
from typing import List
from abc import ABC, abstractmethod

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
        appends the current date to the filename.

        Args:
            data: The DataFrame to export.
            filename: The name of the output CSV file.
        """
        # Ensure the data directory exists
        os.makedirs("data", exist_ok=True)
        
        # Construct the full file path
        filename_base = os.path.join("data", filename_base)
        filename = filename_base + "_" + date.today().strftime("%Y-%m-%d") + ".csv"
        
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