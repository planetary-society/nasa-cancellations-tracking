
import requests
import pandas as pd
import sys
from typing import List, Dict, Any, Optional, Tuple
import time
from contract_query import ContractQuery, FINAL_COLUMNS
import datetime
from urllib.parse import urlparse, parse_qs
import csv
from utils import smart_sentence_case, contracts_titlecase, format_as_currency

# Default DoGE API settings
DOGE_CONTRACTS_ENDPOINT = "https://api.doge.gov/savings/contracts"
DOGE_GRANTS_ENDPOINT = "https://api.doge.gov/savings/grants"
DOGE_PER_PAGE = 500
DOGE_REQUEST_TIMEOUT = 30

class DOGEQuery(ContractQuery):
    """
    Implementation of ContractQuery specifically for fetching NASA contracts
    and grants from the api.doge.gov endpoints. The target agency (NASA)
    is hardcoded. Extracts Award ID from contract URLs.
    """
    # Hardcode NASA agency names for filtering
    TARGET_AGENCY_FULL_LOWER = "national aeronautics and space administration"
    TARGET_AGENCY_SHORT_LOWER = "nasa"
    TARGET_AGENCY_DISPLAY_NAME = "National Aeronautics and Space Administration"

    def __init__(self,
                 final_columns: List[str] = FINAL_COLUMNS,
                 contracts_endpoint: str = DOGE_CONTRACTS_ENDPOINT,
                 grants_endpoint: str = DOGE_GRANTS_ENDPOINT,
                 per_page: int = DOGE_PER_PAGE,
                 timeout: int = DOGE_REQUEST_TIMEOUT,
                 verbose: bool = True):
        """
        Initializes the DOGEQuery for NASA data.

        Args:
            final_columns: Desired output columns.
            contracts_endpoint: URL for the contracts API.
            grants_endpoint: URL for the grants API.
            per_page: Items per page for API requests.
            timeout: Request timeout in seconds.
            verbose: Print progress messages to stderr.
        """
        # Ensure "Award ID" is in the final columns list for this class instance
        if "Award ID" not in final_columns:
             final_columns = ["Award ID"] + final_columns # Prepend if missing
             print(f"Warning: 'Award ID' added to final_columns for {self.__class__.__name__}", file=sys.stderr)

        super().__init__(final_columns=final_columns)
        self.contracts_endpoint = contracts_endpoint
        self.grants_endpoint = grants_endpoint
        self.per_page = per_page
        self.timeout = timeout
        self.verbose = verbose

    def _log(self, message: str, end: str = '\n'):
        """Helper method for conditional logging based on the verbose flag."""
        if self.verbose:
            print(message, file=sys.stderr, end=end)

    def _is_nasa_agency(self, item: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Checks if an item from the DoGE API belongs to NASA, using the
        hardcoded agency names (case-insensitive check on 'agency' field).

        Args:
            item: A dictionary representing a single record (contract/grant).

        Returns:
            A tuple: (True if match, False otherwise), Actual Agency Name found or None.
        """
        agency_value = item.get("agency") # DoGE specific field name
        if isinstance(agency_value, str):
            lowercase_agency = agency_value.lower()
            # Compare against the hardcoded NASA names
            if lowercase_agency == self.TARGET_AGENCY_FULL_LOWER or \
               lowercase_agency == self.TARGET_AGENCY_SHORT_LOWER:
                return True, agency_value # Return match and the original agency name
        return False, None # No match or agency field not found/not string

    def _extract_award_id_from_contract_url(self, url: Optional[str]) -> str:
        """
        Attempts to extract the 'PIID' parameter value from a URL string.

        Args:
            url: The URL string (typically the fpds_link).

        Returns:
            The extracted PIID value as a string, or an empty string if not found
            or if an error occurs during parsing.
        """
        award_id = "" # Default to empty string
        if isinstance(url, str) and url:
            try:
                parsed_url = urlparse(url)
                query_params = parse_qs(parsed_url.query)
                # parse_qs returns lists for values, get the first 'PIID' if present
                piid_list = query_params.get('PIID')
                if piid_list:
                    award_id = piid_list[0] # Take the first value found
            except Exception as e:
                # Log safely, avoiding printing potentially sensitive URLs directly in logs if needed
                self._log(f"Warning: Could not parse Award ID (PIID) from a provided URL. Error: {e}")
                # award_id remains ""
        return award_id
    
    def _extract_usa_spending_award_id_from_grant_url(self, url: Optional[str]) -> str:
        """
        Attempts to extract the 'fain' parameter value from a URL string.

        Args:
            url: The URL string in the form of https://usaspending.gov/award/ASST_NON_23K75IL000001_1605

        Returns:
            The extracted fain value as a string, or an empty string if not found
            or if an error occurs during parsing.
        """
        award_id = ""
        if isinstance(url, str) and url:
            try:
                parsed_url = urlparse(url)
                # Split the path and take the last non-empty segment
                path_parts = [part for part in parsed_url.path.split('/') if part]
                if path_parts:
                    award_id = path_parts[-1]
                    award_id = award_id.replace("/","") # Remove any slashes
            except Exception as e:
                self._log(f"Warning: Could not extract Award ID from usaspending URL. Error: {e}")
        return award_id

    def _standardize_doge_item(self, item: Dict[str, Any], item_type: str, agency_name: str) -> Optional[Dict[str, Any]]:
        """
        Transforms a raw DoGE contract or grant dictionary into the standardized schema,
        including extracting the Award ID for contracts.

        Args:
            item: The raw dictionary from the API.
            item_type: Either 'Contract' or 'Grant'.
            agency_name: The agency name confirmed during filtering (should be NASA).

        Returns:
            A dictionary conforming to self.final_columns or None if invalid type.
        """
        standardized: Optional[Dict[str, Any]] = None # Initialize

        if item_type == "Contract":
            # Extract Award ID specifically for contracts from fpds_link
            award_id = self._extract_award_id_from_contract_url(item.get("fpds_link"))
            savings = item.get("savings", "")
            if savings:
                savings = format_as_currency(savings)
            else:
                savings = "$0"
            standardized = {
                "Award ID": award_id,
                "source_type": "Contract",
                "deleted date": item.get("deleted_date"),
                "recipient": contracts_titlecase(item.get("vendor")),
                "value": item.get("value"),
                "savings": item.get("savings"),
                "status": item.get("fpds_status", ""),
                "source_url": item.get("fpds_link"),
                "description": "Status: " + item.get("fpds_status", "") + ". Reported savings: " + savings + ". DOGE Action Date: " + item.get("deleted_date") + ". " + smart_sentence_case(item.get("description").replace("\n", " ")),
                "agency": agency_name
            }
        elif item_type == "Grant":
            # Grants get an empty Award ID in this implementation
            award_id = ""
            url = item.get("link", "")
            if url:
                award_id = self._extract_usa_spending_award_id_from_grant_url(url)
            else:
                award_id = ""
            savings = item.get("savings", "")
            if savings:
                savings = format_as_currency(savings)
            else:
                savings = "$0"
            standardized = {
                "Award ID": award_id,
                "source_type": "Grant",
                "deleted date": item.get("date"),
                "recipient": contracts_titlecase(item.get("recipient")),
                "value": item.get("value"),
                "savings": item.get("savings"),
                "status": "",
                "source_url": item.get("link"),
                "description": "DOGE Action Date: " + item.get("date") + ". Reported savings: " + savings + ". " + smart_sentence_case(item.get("description").replace("\n", " ")),
                "agency": agency_name
            }
            time.sleep(0.1) # Optional delay to avoid overwhelming the API
        else:
            self._log(f"Warning: Unknown item_type '{item_type}' during DoGE standardization.")
            return None

        # Ensure all columns defined in FINAL_COLUMNS exist in the output dict
        # This loop is important if new columns are added to FINAL_COLUMNS later
        for col in self.final_columns:
            if col not in standardized:
                 # Use None or pd.NA for missing values for consistency in DataFrame
                standardized[col] = pd.NA
        return standardized


    def _fetch_and_process_endpoint(self, endpoint_url: str, data_key: str, item_type_name: str) -> List[Dict[str, Any]]:
        """
        Fetches all pages for a single DoGE endpoint, filters for NASA items,
        and standardizes them. Internal helper method.

        (No changes needed in the fetching loop itself, only in the standardization call)
        """
        all_standardized_nasa_items = []
        page = 1
        total_pages = None

        self._log(f"\n--- Starting DoGE fetch for NASA {item_type_name}s from {endpoint_url} ---")

        while True:
            params = {'page': page, 'per_page': self.per_page}
            self._log(f"Fetching page {page}...", end='')
            if total_pages: self._log(f"/{total_pages}", end='')
            self._log(" ", end='')

            try:
                response = requests.get(endpoint_url, params=params, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()

                # --- API Response validation ---
                if not data or not data.get("success"):
                    self._log(f"\nAPI returned unsuccessful status or invalid data on page {page}. Response: {data}")
                    break
                result_data = data.get("result", {})
                items_on_page = result_data.get(data_key, [])

                # --- Pagination info ---
                if page == 1 and "meta" in data and "pages" in data["meta"]:
                    total_pages = data["meta"]["pages"]
                    self._log(f"(Estimated total pages: {total_pages})")
                else:
                    self._log("") # Newline

                if not items_on_page:
                    self._log(f"No more {item_type_name}s found. Fetch complete for this endpoint.")
                    break

                # --- Filter, Standardize, Append ---
                nasa_items_found_on_page = 0
                for item in items_on_page:
                    is_match, agency_name_found = self._is_nasa_agency(item)
                    if is_match:
                        # Standardization now handles Award ID extraction internally
                        standardized = self._standardize_doge_item(item, item_type_name, agency_name_found)
                        if standardized:
                            all_standardized_nasa_items.append(standardized)
                            nasa_items_found_on_page += 1

                if nasa_items_found_on_page > 0:
                    self._log(f"  -> Found and processed {nasa_items_found_on_page} NASA {item_type_name}(s) on this page.")

                page += 1
                # time.sleep(0.1) # Optional delay

            # --- Error Handling ---
            except requests.exceptions.Timeout:
                self._log(f"\nRequest timed out while fetching page {page}. Stopping fetch for {endpoint_url}.")
                break
            except requests.exceptions.RequestException as e:
                self._log(f"\nNetwork/HTTP error fetching page {page} for {endpoint_url}: {e}")
                self._log(f"Stopping fetch for {endpoint_url}.")
                break
            except Exception as e:
                 self._log(f"\nUnexpected error processing page {page} for {endpoint_url}: {e}")
                 self._log(f"Stopping fetch for {endpoint_url}.")
                 break

        self._log(f"--- Finished DoGE fetch for {item_type_name}s. Found {len(all_standardized_nasa_items)} NASA items. ---")
        return all_standardized_nasa_items


    def search(self, **kwargs) -> pd.DataFrame:
        """
        Performs the hardcoded search for NASA contracts and grants on the
        DoGE API endpoints. Extracts Award ID for contracts. Ignores kwargs.

        Args:
            **kwargs: Ignored in this implementation.

        Returns:
            A Pandas DataFrame with the combined NASA contract and grant results,
            structured according to self.final_columns (including 'Award ID').
        """
        if kwargs:
            self._log(f"Note: search() called with keyword arguments {kwargs.keys()}, which are ignored by this NASA-specific implementation.")

        self._log(f"Initiating DoGE search specifically for NASA ({self.TARGET_AGENCY_DISPLAY_NAME})...")

        # 1. Fetch NASA Contracts
        contracts_data = self._fetch_and_process_endpoint(
            endpoint_url=self.contracts_endpoint,
            data_key="contracts",
            item_type_name="Contract"
        )

        # 2. Fetch NASA Grants
        grants_data = self._fetch_and_process_endpoint(
            endpoint_url=self.grants_endpoint,
            data_key="grants",
            item_type_name="Grant"
        )

        # 3. Combine the results
        combined_data = contracts_data + grants_data
        self._log(f"\nCombining data. Total NASA items found: {len(combined_data)}")

        # 4. Create the final DataFrame
        final_df = pd.DataFrame(columns=self.final_columns) # Ensure correct columns even if empty
        if combined_data:
            # Create DF from the collected standardized data
            temp_df = pd.DataFrame(combined_data)
            # Ensure all final columns exist and handle potential missing ones
            # (The standardization step should have added all columns, but this is a safeguard)
            for col in self.final_columns:
                if col not in temp_df.columns:
                     self._log(f"Note: Adding missing column '{col}' to the DataFrame (unexpected).")
                     temp_df[col] = pd.NA
            # Select columns in the defined order
            if not temp_df.empty:
                # Ensure correct column order
                final_df = temp_df[self.final_columns]
        else:
            self._log("\nNo NASA contracts or grants were found in the DoGE datasets.")
            # final_df is already an empty DF with correct columns

        # Save the final DataFrame to CSV
        if not final_df.empty:
            # Sort by Award ID for deterministic ordering
            final_df = final_df.sort_values(by=['Award ID']).reset_index(drop=True)
            self.export_to_csv(final_df, "doge_contracts_and_grants_query")

        return final_df


# --- Example Usage (for CLI execution) ---

if __name__ == "__main__":
    print("--- Running DoGE Query Script for NASA Data ---")

    # 1. Instantiate the NASA-specific query class
    # Pass the updated FINAL_COLUMNS list if it wasn't modified globally
    doge_nasa_query = DOGEQuery(final_columns=FINAL_COLUMNS, verbose=True)

    # 2. Perform the search (no arguments needed)
    try:
        nasa_data_df = doge_nasa_query.search()

        # 3. Print the result to standard output
        print("\n--- NASA Contracts and Grants Search Results (including Award ID) ---")
        if not nasa_data_df.empty:
            # Configure pandas display options if needed for wide columns
            # pd.set_option('display.max_columns', None)
            # pd.set_option('display.width', 1000)
            today = datetime.datetime.today().strftime('%Y-%m-%d')
            filename = f"doge_contracts_{today}.csv"
            nasa_data_df.to_csv(filename, index=False, quoting=csv.QUOTE_ALL, escapechar='\\')
            print(f"CSV file written to {filename}")
        else:
            print("The search returned no NASA results.")

    except Exception as e:
        print(f"\nAn unexpected error occurred during execution: {e}", file=sys.stderr)