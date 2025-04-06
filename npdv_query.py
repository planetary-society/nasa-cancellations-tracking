#!/usr/bin/env python3

import pandas as pd
import requests
import logging
import re
import os
import csv # Import csv module
from urllib.parse import urlparse
from typing import List, Tuple, Dict, Any, Optional
from datetime import datetime
from utils import parse_mod_number # Import parse_mod_number function from utils.py

# Assuming contract_query.py contains the base class and FINAL_COLUMNS
try:
    from contract_query import ContractQuery, FINAL_COLUMNS
except ImportError:
    logging.error("Failed to import ContractQuery base class. Ensure contract_query.py exists.")
    # Define dummy base class and columns if import fails, for basic structure
    FINAL_COLUMNS = ['Award ID', 'source_type', 'recipient', 'value', 'savings', 'status', 'source_url', 'description', 'agency']
    class ContractQuery: # Dummy class
        def __init__(self, final_columns): self.final_columns = final_columns
        def search(self, **kwargs): raise NotImplementedError
        def __repr__(self): return f"{self.__class__.__name__}()"

# --- Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
# DEFAULT_CHUNK_SIZE is no longer needed for this approach

# --- Class Implementation ---

class NPDVQuery(ContractQuery):
    """
    Queries NASA contract data from a specific CSV file, caching it locally.
    Identifies potentially terminated/stopped contracts based on keywords found
    *only in the latest modification* for each unique Award ID, using csv.DictReader.
    """
    DEFAULT_SEARCH_PHRASES = ["termination", "stop work", "terminated", "terminates", "effectuated"]
    DEFAULT_CSV_URL = "https://raw.githubusercontent.com/planetary-society/nasa-contracts/master/data/nasa_contracts_2025.csv"
    AGENCY_NAME = "National Aeronautics and Space Administration"

    def __init__(self,
                 csv_url: str = DEFAULT_CSV_URL,
                 search_phrases: Optional[List[str]] = None,
                 local_cache_dir: str = ".",
                 # chunk_size parameter removed
                 final_columns: List[str] = FINAL_COLUMNS):
        """Initializes the query object."""
        super().__init__(final_columns=final_columns)
        self.csv_url = csv_url
        self.search_phrases = search_phrases if search_phrases is not None else self.DEFAULT_SEARCH_PHRASES
        self.local_cache_dir = local_cache_dir
        # self.chunk_size attribute removed
        self._local_filename = self._generate_local_filename(csv_url)
        self._local_filepath = os.path.join(self.local_cache_dir, self._local_filename) if self._local_filename else None
        if self.search_phrases:
            self._search_pattern_re = re.compile(
                r'\b(?:' + '|'.join(re.escape(phrase) for phrase in self.search_phrases) + r')\b',
                re.IGNORECASE
            )
        else:
            logging.warning("No search phrases provided; search will likely return no results.")
            self._search_pattern_re = None

        logging.info(f"{self.__class__.__name__} initialized. URL='{self.csv_url}', "
                     f"CacheFile='{self._local_filepath}', Phrases={self.search_phrases}") # Removed ChunkSize log

    def _generate_local_filename(self, url: str) -> Optional[str]:
        """Generates a safe filename from a URL. (Implementation unchanged)"""
        try:
            parsed_url = urlparse(url)
            filename = os.path.basename(parsed_url.path)
            filename = re.sub(r'[^a-zA-Z0-9_.-]', '_', filename)
            return filename if filename else "cached_data.csv"
        except Exception as e:
            logging.error(f"Failed to generate filename from URL '{url}': {e}")
            return None

    @staticmethod
    def _format_date_iso(date_str: str) -> str:
        """
        Attempts to parse a date string (expected format M/D/YY) and
        formats it as YYYY-MM-DD.

        Args:
            date_str: The input date string (e.g., "10/31/23", "4/26/25").

        Returns:
            The formatted date string (YYYY-MM-DD) or an empty string if
            input is empty or parsing fails.
        """
        if not date_str or not date_str.strip():
            return "" # Handle empty or whitespace strings

        input_format = '%m/%d/%Y'
        output_format = '%Y-%m-%d'

        try:
            # Parse the input string using the expected format
            parsed_date = datetime.strptime(date_str.strip(), input_format)
            # Format the datetime object into the desired output string format
            formatted_date = parsed_date.strftime(output_format)
            return formatted_date
        except (ValueError, TypeError) as e:
            # Log a warning if parsing fails
            logging.warning(f"Could not parse date string '{date_str}' using format '{input_format}'. Returning empty string. Error: {e}")
            return "" # Return empty string on failure

    def _download_file(self, filepath: str) -> bool:
        """Downloads the file from self.csv_url to the specified filepath. (Implementation unchanged)"""
        # (Code from previous version remains the same)
        logging.info(f"Attempting to download data from {self.csv_url} to {filepath}")
        try:
            with requests.get(self.csv_url, stream=True, timeout=60) as r:
                r.raise_for_status()
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                with open(filepath, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192): f.write(chunk)
            logging.info(f"Successfully downloaded file to {filepath}")
            return True
        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to download file from {self.csv_url}: {e}")
            if os.path.exists(filepath):
                 try: os.remove(filepath)
                 except OSError as rm_err: logging.error(f"Failed to remove incomplete download {filepath}: {rm_err}")
            return False
        except OSError as e: logging.error(f"Failed to write downloaded file to {filepath}: {e}"); return False
        except Exception as e: logging.error(f"An unexpected error during download: {e}", exc_info=True); return False


    def _get_data_filepath(self, force_reload: bool = False) -> Optional[str]:
        """Ensures the data file is available locally, downloading if needed. (Implementation unchanged)"""
        # (Code from previous version remains the same)
        if not self._local_filepath: logging.error("Local file path could not be determined."); return None
        file_exists = os.path.exists(self._local_filepath)
        if force_reload:
            logging.info(f"Force reload requested. Downloading fresh data to {self._local_filepath}...")
            if self._download_file(self._local_filepath): return self._local_filepath
            else: return None
        elif file_exists: logging.info(f"Using cached file: {self._local_filepath}"); return self._local_filepath
        else:
            logging.info(f"Local file not found. Downloading to {self._local_filepath}...")
            if self._download_file(self._local_filepath): return self._local_filepath
            else: return None

    def search(self, force_reload: bool = False, **kwargs) -> pd.DataFrame:
        """
        Fetches (using local cache) and processes NASA contract data using
        csv.DictReader. Identifies contracts where the *most recent modification*
        contains termination-related keywords in its description.

        Args:
            force_reload (bool, optional): Forces re-download, bypassing cache. Defaults to False.
            **kwargs: Other keyword arguments (currently ignored).

        Returns:
            pandas.DataFrame: Contains contracts potentially terminated based on their
                              latest modification description, conforming to self.final_columns.
        """
        force_reload = kwargs.pop('force_reload', force_reload)
        if kwargs:
             logging.warning(f"{self.__class__.__name__}.search received unused parameters {kwargs.keys()}.")

        if not self._search_pattern_re:
             logging.error("Search cannot proceed without valid search phrases/pattern.")
             return pd.DataFrame(columns=self.final_columns)

        # 1. Get the path to the local data file
        local_filepath = self._get_data_filepath(force_reload=force_reload)
        if not local_filepath:
            logging.error("Could not obtain local data file. Aborting search.")
            return pd.DataFrame(columns=self.final_columns)

        # Dictionary to hold the data for the latest modification found per Award ID
        # Structure: {award_id: (mod_num, row_dict)}
        latest_rows: Dict[str, Tuple[int, Dict[str, str]]] = {}
        required_cols = ['Contract/Mod Number', 'Description', 'Award Type', 'Completion Date', 'Contractor']

        logging.info(f"Scanning CSV file to find latest modifications: {local_filepath}")
        try:
            # 2. First Pass: Read CSV and find the latest modification row for each Award ID
            with open(local_filepath, mode='r', newline='', encoding='utf-8') as csvfile:
                # Handle potential dialect issues like quote characters if needed
                reader = csv.DictReader(csvfile)

                # Check header row
                if not reader.fieldnames:
                    logging.error(f"CSV file '{local_filepath}' appears to be empty or header is missing.")
                    return pd.DataFrame(columns=self.final_columns)
                if not all(col in reader.fieldnames for col in required_cols):
                    missing = [col for col in required_cols if col not in reader.fieldnames]
                    logging.error(f"CSV file is missing required columns: {missing}. Aborting.")
                    return pd.DataFrame(columns=self.final_columns)

                row_count = 0
                for row in reader:
                    row_count += 1
                    try:
                        contract_mod_str = row.get('Contract/Mod Number', '')
                        award_id, mod_num = parse_mod_number(contract_mod_str)

                        if not award_id: # Skip if award ID couldn't be parsed
                            continue

                        # Check if this mod is later than or equal to the one stored
                        stored_data = latest_rows.get(award_id)
                        if stored_data is None or mod_num >= stored_data[0]:
                             # Store/update with the current mod_num and the raw row dict
                             latest_rows[award_id] = (mod_num, row)

                    except Exception as e:
                         # Log error processing a specific row but continue scanning
                         logging.error(f"Error processing row {row_count} during scan: {row}. Error: {e}", exc_info=False)

                logging.info(f"Finished scanning {row_count} rows. Found {len(latest_rows)} unique Award IDs with latest modifications.")

        except FileNotFoundError:
            logging.error(f"Cached file not found: {local_filepath}")
            return pd.DataFrame(columns=self.final_columns)
        except Exception as e:
            logging.error(f"An unexpected error occurred during CSV scanning: {e}", exc_info=True)
            return pd.DataFrame(columns=self.final_columns)


        # 3. Second Pass: Filter the latest modifications based on the description
        final_results_list: List[Dict[str, Any]] = []
        logging.info(f"Filtering {len(latest_rows)} latest modifications for termination phrases...")

        for award_id, (mod_num, row_dict) in latest_rows.items():
            try:
                description = row_dict.get('Description', '')

                # Apply the search pattern filter
                if description and self._search_pattern_re.search(description):
                    # If description matches, format this row for final output
                    award_type_str = row_dict.get('Award Type', '').lower()
                    source_type = "Grant" if "grant" in award_type_str else "Contract"

                    # --- Format the Completion Date ---
                    completion_date_str = row_dict.get('Completion Date', '')
                    # Call the new helper method to parse and format
                    formatted_deleted_date = self._format_date_iso(completion_date_str)
                    logging.debug(f"Award ID {award_id}: Original Completion Date='{completion_date_str}', Formatted='{formatted_deleted_date}'")
                    # --- End Formatting ---
                    
                    output_row_data = {
                        'Award ID': award_id, # Use the reliable parsed award_id
                        'source_type': source_type,
                        'deleted date': formatted_deleted_date,
                        'recipient': row_dict.get('Contractor', ''),
                        'value': "",
                        'savings': "",
                        'status': "",
                        'source_url': "",
                        'description': description, # Use the description we filtered on
                        'agency': self.AGENCY_NAME
                    }
                    # Ensure only columns defined in self.final_columns are included
                    filtered_output_data = {k: v for k, v in output_row_data.items() if k in self.final_columns}
                    final_results_list.append(filtered_output_data)

            except Exception as e:
                logging.error(f"Error filtering or formatting latest mod for Award ID {award_id}: {e}", exc_info=False)


        # 4. Create the final DataFrame
        if not final_results_list:
            logging.info("No records found where the latest modification matched the termination criteria.")
            return pd.DataFrame(columns=self.final_columns)
        else:
            try:
                final_df = pd.DataFrame(final_results_list, columns=self.final_columns)
                # Reindex to ensure exact column order and presence
                final_df = final_df.reindex(columns=self.final_columns)
                logging.info(f"Created final DataFrame with {len(final_df)} terminated contracts based on latest modification.")
                if not final_df.empty:
                    self.export_to_csv(final_df, "npdv_potential_cancellations")
                
                return final_df
            except Exception as e:
                 logging.error(f"Failed to create final DataFrame from results: {e}", exc_info=True)
                 return pd.DataFrame(columns=self.final_columns)

# --- Example Usage ---
if __name__ == "__main__":
    print("--- Running NPDVQuery Example (Filtering by Latest Mod using csv.DictReader) ---")
    nasa_query = NPDVQuery()

    print("\nCalling search()...")
    canceled_contracts_df = nasa_query.search()

    if not canceled_contracts_df.empty:
        print(f"\nFound {len(canceled_contracts_df)} potentially canceled contracts (based on latest mod):")
        pd.set_option('display.max_columns', None)
        print(canceled_contracts_df.to_markdown(index=False))
        pd.reset_option('display.max_columns')
    else:
        print("\nNo potentially canceled contracts found matching the criteria on the latest modification.")

    print("\n--- Example Finished ---")