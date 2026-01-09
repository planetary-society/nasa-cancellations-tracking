#!/usr/bin/env python3

import pandas as pd
import requests
import sys
import time
import os
from typing import List, Dict, Any, Optional
from datetime import date
from bs4 import BeautifulSoup
from contract_query import ContractQuery, FINAL_COLUMNS
from utils import smart_sentence_case, contracts_titlecase

class FPDSQuery(ContractQuery):
    """
    Implementation of ContractQuery to fetch contract data from the FPDS
    (Federal Procurement Data System) CSV export and HTML detail pages.

    Downloads CSV data for NASA "Terminate for Convenience" contracts,
    then fetches HTML details for each contract to extract full descriptions.
    """

    # Base URLs for FPDS system
    CSV_BASE_URL = "https://www.fpds.gov/ezsearch/fpdsportal"
    HTML_DETAIL_BASE_URL = "https://fpds.gov/ezsearch/jsp/viewLinkController.jsp"

    # CSV query parameters (template)
    CSV_PARAMS_TEMPLATE = {
        's': 'FPDS.GOV',
        'indexName': 'awardfull',
        'templateName': 'CSV',
        'q': '"Terminate for Convenience" DEPARTMENT_FULL_NAME:"NATIONAL AERONAUTICS AND SPACE ADMINISTRATION" LAST_MOD_DATE:[{start_date},{end_date}]',
        'renderer': 'jsp',
        'length': '162'
    }

    def __init__(self, final_columns: List[str] = FINAL_COLUMNS):
        """
        Initializes the FPDSQuery.

        Args:
            final_columns: The desired list of column names for the output DataFrame.
                           Defaults to the globally defined FINAL_COLUMNS.
        """
        super().__init__(final_columns)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1'
        })

    def _format_date_for_fpds(self, date_obj: date) -> str:
        """Formats a date object into YYYY/MM/DD string for FPDS query."""
        return date_obj.strftime('%Y/%m/%d')

    def _parse_mod_number(self, mod_str: Any) -> int:
        """
        Parse modification number to integer for comparison.

        Handles both formats:
        - PXXXXX format (e.g., P00001, P00010)
        - Simple integer format (e.g., 1, 10)

        Args:
            mod_str: The modification number string

        Returns:
            Integer value for comparison (0 if parsing fails)
        """
        if pd.isna(mod_str):
            return 0

        mod_str = str(mod_str).strip()
        if not mod_str:
            return 0

        if mod_str.startswith('P'):
            # Extract number from PXXXXX format
            try:
                return int(mod_str[1:])
            except (ValueError, IndexError):
                return 0
        else:
            # Simple integer format
            try:
                return int(mod_str)
            except ValueError:
                return 0

    def _is_bpa(self, award_type: str) -> bool:
        """
        Check if the award type indicates a Blanket Purchase Agreement (BPA).

        Args:
            award_type: The award type string from CSV

        Returns:
            True if this is a BPA contract, False otherwise
        """
        if not award_type:
            return False

        award_type_lower = str(award_type).lower()
        return 'bpa' in award_type_lower or 'blanket purchase agreement' in award_type_lower or 'idc indefinite delivery contract' in award_type_lower

    def _is_bpa_call(self, award_type: str) -> bool:
        """
        Check if the award type indicates a BPA Call Blanket Purchase Agreement.

        Args:
            award_type: The award type string from CSV

        Returns:
            True if this is a BPA Call contract, False otherwise
        """
        if not award_type:
            return False

        award_type_lower = str(award_type).lower()
        return 'bpa call' in award_type_lower

    def _load_or_download_csv(self, start_date: date, end_date: date) -> pd.DataFrame:
        """
        Load CSV from cache if available for current date, otherwise download.

        Args:
            start_date: Start date for the query
            end_date: End date for the query

        Returns:
            DataFrame containing the CSV data
        """
        # Check for existing raw CSV file for today
        today_str = date.today().strftime("%Y-%m-%d")
        raw_csv_path = f"data/fpds_raw_csv_{today_str}.csv"
        if os.path.exists(raw_csv_path):
            print(f"Loading existing raw CSV from {raw_csv_path}", file=sys.stderr)
            try:
                df = pd.read_csv(raw_csv_path)
                return df
            except Exception as e:
                print(f"Error loading cached CSV: {e}", file=sys.stderr)
                print("Falling back to downloading fresh data", file=sys.stderr)

        # Download fresh data if no cache exists or loading failed
        return self._download_csv(start_date, end_date)

    def _deduplicate_by_latest_mod(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Remove duplicate Award IDs, keeping only the latest modification number.

        Args:
            df: DataFrame with contract data

        Returns:
            DataFrame with duplicates removed, keeping latest modification
        """
        if df.empty or 'Contract ID' not in df.columns:
            return df

        print(f"Deduplicating {len(df)} contracts by latest modification", file=sys.stderr)

        # Add parsed modification numbers for sorting
        df = df.copy()
        df['_mod_num_parsed'] = df['Modification Number'].apply(self._parse_mod_number)

        # Sort by Contract ID and modification number (descending - latest first)
        df_sorted = df.sort_values(['Contract ID', '_mod_num_parsed'], ascending=[True, False])

        # Keep only the first (latest) modification for each Contract ID
        df_dedup = df_sorted.groupby('Contract ID').first().reset_index()

        # Remove the temporary column
        df_dedup = df_dedup.drop('_mod_num_parsed', axis=1)

        contracts_removed = len(df) - len(df_dedup)
        if contracts_removed > 0:
            print(f"Removed {contracts_removed} duplicate contracts, keeping latest modifications", file=sys.stderr)

        return df_dedup

    def _load_existing_processed(self) -> pd.DataFrame:
        """
        Load existing processed results from today's file if available.

        Returns:
            DataFrame with existing processed results, or empty DataFrame
        """
        today_str = date.today().strftime("%Y-%m-%d")
        processed_csv_path = f"data/fpds_processed_contracts_{today_str}.csv"
        if os.path.exists(processed_csv_path):
            print(f"Loading existing processed results from {processed_csv_path}", file=sys.stderr)
            try:
                df = pd.read_csv(processed_csv_path)
                print(f"Found {len(df)} previously processed contracts", file=sys.stderr)
                return df
            except Exception as e:
                print(f"Error loading processed results: {e}", file=sys.stderr)

        return pd.DataFrame()

    def _filter_new_contracts(self, csv_df: pd.DataFrame, existing_processed: pd.DataFrame) -> pd.DataFrame:
        """
        Filter out contracts that have already been processed.

        Args:
            csv_df: New contracts from CSV
            existing_processed: Previously processed contracts

        Returns:
            DataFrame containing only new contracts to process
        """
        if existing_processed.empty:
            return csv_df

        existing_award_ids = set(existing_processed['Award ID'].tolist())
        new_contracts = csv_df[~csv_df['Contract ID'].isin(existing_award_ids)]

        skipped_count = len(csv_df) - len(new_contracts)
        if skipped_count > 0:
            print(f"Skipping {skipped_count} already-processed contracts", file=sys.stderr)

        print(f"Processing {len(new_contracts)} new contracts", file=sys.stderr)
        return new_contracts

    def search(self,
               start_date: Optional[date] = None,
               end_date: Optional[date] = None) -> pd.DataFrame:
        """
        Performs a search for NASA "Terminate for Convenience" contracts from FPDS.

        Downloads the CSV data, then fetches HTML details for each contract
        to extract full descriptions.

        Args:
            start_date: The start date for the contract modification date range.
                       Defaults to January 20, 2025.
            end_date: The end date for the contract modification date range.
                     Defaults to current date.

        Returns:
            A Pandas DataFrame containing the contract data, structured according
            to self.final_columns.
        """
        # Set default dates
        if start_date is None:
            start_date = date(2025, 1, 20)
        if end_date is None:
            end_date = date.today()

        # Validate date range
        if start_date > end_date:
            print(f"Warning: Start date ({start_date}) is after end date ({end_date}). Swapping dates.", file=sys.stderr)
            start_date, end_date = end_date, start_date

        print(f"Querying FPDS for NASA 'Terminate for Convenience' contracts from {start_date} to {end_date}", file=sys.stderr)

        # Step 1: Load or download CSV (with caching)
        csv_df = self._load_or_download_csv(start_date, end_date)
        if csv_df.empty:
            print("No CSV data retrieved from FPDS", file=sys.stderr)
            return pd.DataFrame(columns=self.final_columns)

        print(f"Found {len(csv_df)} contracts in CSV data", file=sys.stderr)

        # Step 2: Deduplicate by latest modification number
        csv_df = self._deduplicate_by_latest_mod(csv_df)
        print(f"After deduplication: {len(csv_df)} unique contracts", file=sys.stderr)

        # Step 3: Load existing processed results (for incremental processing)
        existing_processed = self._load_existing_processed()

        # Step 4: Filter out already-processed contracts
        contracts_to_process = self._filter_new_contracts(csv_df, existing_processed)

        # Step 5: Process new contracts and fetch HTML details
        new_processed_data: List[Dict[str, Any]] = []
        for index, row in contracts_to_process.iterrows():
            # Add rate limiting between requests (1 second delay)
            if index > 0:
                time.sleep(1.0)

            contract_data = self._process_contract_row(row, index)
            if contract_data:
                new_processed_data.append(contract_data)

        print(f"Successfully processed {len(new_processed_data)} new contracts", file=sys.stderr)

        # Step 6: Merge new results with existing results
        if new_processed_data:
            new_df = pd.DataFrame(new_processed_data)
            new_df = new_df.reindex(columns=self.final_columns)
        else:
            new_df = pd.DataFrame(columns=self.final_columns)

        if not existing_processed.empty:
            # Combine existing and new results
            final_df = pd.concat([existing_processed, new_df], ignore_index=True)
            print(f"Combined {len(existing_processed)} existing + {len(new_df)} new = {len(final_df)} total contracts", file=sys.stderr)
        else:
            final_df = new_df

        # Step 7: Export final results
        if not final_df.empty:
            # Sort by Award ID for deterministic ordering
            final_df = final_df.sort_values(by=['Award ID']).reset_index(drop=True)
            self.export_to_csv(final_df, "fpds_processed_contracts")

        return final_df

    def _download_csv(self, start_date: date, end_date: date) -> pd.DataFrame:
        """
        Downloads the CSV data from FPDS for the specified date range.

        Args:
            start_date: Start date for the query
            end_date: End date for the query

        Returns:
            DataFrame containing the CSV data, or empty DataFrame on error
        """
        try:
            # Format dates for FPDS query
            start_str = self._format_date_for_fpds(start_date)
            end_str = self._format_date_for_fpds(end_date)

            # Build query parameters
            params = self.CSV_PARAMS_TEMPLATE.copy()
            params['q'] = params['q'].format(start_date=start_str, end_date=end_str)

            print(f"Downloading CSV from FPDS with query: {params['q']}", file=sys.stderr)

            # Make request
            response = self.session.get(self.CSV_BASE_URL, params=params, timeout=60)
            response.raise_for_status()

            # Parse CSV
            from io import StringIO
            csv_data = StringIO(response.text)
            df = pd.read_csv(csv_data)

            # Export raw CSV for debugging
            self.export_to_csv(df, "fpds_raw_csv")

            return df

        except requests.exceptions.RequestException as e:
            print(f"Error downloading CSV from FPDS: {e}", file=sys.stderr)
            return pd.DataFrame()
        except pd.errors.EmptyDataError:
            print("FPDS returned empty CSV data", file=sys.stderr)
            return pd.DataFrame()
        except Exception as e:
            print(f"Error parsing CSV from FPDS: {e}", file=sys.stderr)
            return pd.DataFrame()

    def _process_contract_row(self, row: pd.Series, row_index: int) -> Optional[Dict[str, Any]]:
        """
        Processes a single contract row from the CSV data.

        Args:
            row: Single row from the CSV DataFrame
            row_index: Index of the row for logging purposes

        Returns:
            Dictionary containing the processed contract data, or None on error
        """
        try:
            # Extract basic contract info
            contract_id = str(row.get('Contract ID', '')).strip()
            mod_number = str(row.get('Modification Number', '')).strip()

            # Handle Reference IDV - could be empty, NaN, or a valid value
            reference_idv_raw = row.get('Reference IDV', '')
            if pd.isna(reference_idv_raw) or str(reference_idv_raw).strip().lower() in ['', 'nan', 'none']:
                reference_idv = ''
            else:
                reference_idv = str(reference_idv_raw).strip()

            # Extract Award/IDV Type for BPA handling
            award_type = str(row.get('Award/IDV Type', '')).strip()

            # Extract Contracting Agency ID
            agency_id = str(row.get('Contracting Agency ID', '8000')).strip()

            if not contract_id:
                print(f"Row {row_index}: Missing Contract ID, skipping", file=sys.stderr)
                return None

            # Extract other CSV fields
            recipient_name = contracts_titlecase(str(row.get('Legal Business Name', '')).strip())
            action_obligation = row.get('Action Obligation ($)', 0)

            # Try to parse the dollar amount
            value = self._parse_currency_value(action_obligation)

            # Fetch description from HTML page
            description = self._fetch_contract_description(contract_id, mod_number, reference_idv, award_type)
            if not description:
                description = "Description not available"

            # Build source URL
            source_url = self._build_source_url(contract_id, mod_number, reference_idv, award_type, agency_id)

            # Create standardized record
            record = {
                'Award ID': contract_id,
                'source_type': 'FPDS Contract',
                'recipient': recipient_name,
                'value': value,
                'savings': None,  # Not available in FPDS data
                'status': 'Terminate for Convenience',
                'source_url': source_url,
                'description': smart_sentence_case(description),
                'agency': 'NASA'
            }

            return record

        except Exception as e:
            print(f"Error processing row {row_index}: {e}", file=sys.stderr)
            return None

    def _parse_currency_value(self, value_str: Any) -> Optional[float]:
        """
        Parses a currency string or numeric value into a float.

        Args:
            value_str: The currency value to parse

        Returns:
            Float value or None if parsing fails
        """
        if pd.isna(value_str):
            return None

        try:
            # Convert to string and clean
            value_clean = str(value_str).strip().replace('$', '').replace(',', '')

            # Handle negative values in parentheses - fix regex pattern
            if value_clean.startswith('(') and value_clean.endswith(')'):
                inner_value = value_clean[1:-1].strip()
                if inner_value:  # Only proceed if there's content inside parentheses
                    value_clean = '-' + inner_value

            return float(value_clean) if value_clean else None
        except (ValueError, TypeError):
            return None

    def _fetch_contract_description(self, contract_id: str, mod_number: str, reference_idv: str = '', award_type: str = '') -> Optional[str]:
        """
        Fetches the contract description from the FPDS HTML detail page with retry logic.

        Args:
            contract_id: The contract ID (PIID)
            mod_number: The modification number
            reference_idv: The reference IDV contract ID (if any)
            award_type: The award/IDV type for BPA handling

        Returns:
            The description text or None if not found/error
        """
        max_retries = 3
        base_delay = 1.0

        for attempt in range(max_retries):
            try:
                # Build HTML detail URL
                is_bpa = self._is_bpa(award_type)
                is_bpa_call = self._is_bpa_call(award_type)

                # BPA Call contracts should use AWARD contract type and transaction number 0
                if is_bpa_call:
                    contract_type = 'AWARD'
                    transaction_number = '0'
                elif is_bpa:
                    contract_type = 'IDV'
                    transaction_number = ''
                else:
                    contract_type = 'AWARD'
                    transaction_number = '0'

                url_params = {
                    'agencyID': '8000',  # NASA agency ID
                    'PIID': contract_id,
                    'modNumber': mod_number,
                    'transactionNumber': transaction_number,
                    'idvAgencyID': '8000' if reference_idv else '',
                    'idvPIID': reference_idv if reference_idv else '',
                    'actionSource': 'searchScreen',
                    'actionCode': '',
                    'documentVersion': '1.5',
                    'contractType': contract_type,
                    'docType': 'B'
                }

                print(f"Fetching description for {contract_id} mod {mod_number} (attempt {attempt + 1}/{max_retries})", file=sys.stderr)

                # Add delay before request to avoid rate limiting
                if attempt > 0:
                    delay = base_delay * (2 ** (attempt - 1))  # Exponential backoff
                    print(f"Waiting {delay:.1f}s before retry...", file=sys.stderr)
                    time.sleep(delay)

                # Make request with longer timeout
                response = self.session.get(self.HTML_DETAIL_BASE_URL, params=url_params, timeout=30)
                response.raise_for_status()

                # Parse HTML and extract description
                soup = BeautifulSoup(response.text, 'html.parser')

                # Extract reason for modification
                reason_for_mod = ""
                reason_input = soup.find('input', id='reasonForModification')
                if reason_input and reason_input.get('value'):
                    reason_for_mod = reason_input.get('value').strip()

                # Extract contract requirement description
                contract_description = ""

                # Look for textarea with id="descriptionOfContractRequirement"
                desc_textarea = soup.find('textarea', id='descriptionOfContractRequirement')
                if desc_textarea and desc_textarea.get_text(strip=True):
                    contract_description = desc_textarea.get_text(strip=True)
                else:
                    # Fallback: look for input field
                    desc_input = soup.find('input', id='descriptionOfContractRequirement')
                    if desc_input and desc_input.get('value'):
                        contract_description = desc_input.get('value').strip()

                # Combine reason for modification and contract description
                combined_description = self._combine_description_parts(reason_for_mod, contract_description)

                if combined_description:
                    print(f"Successfully fetched description for {contract_id} mod {mod_number}", file=sys.stderr)
                    return combined_description

                # Debug: Save HTML when no description is found
                print(f"No description found for {contract_id} mod {mod_number}", file=sys.stderr)
                self._debug_save_html(contract_id, mod_number, response.text, reason_for_mod, contract_description)
                return None

            except (requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.Timeout) as e:
                print(f"Connection error fetching HTML for {contract_id} mod {mod_number} (attempt {attempt + 1}): {e}", file=sys.stderr)
                if attempt == max_retries - 1:
                    print(f"Max retries exceeded for {contract_id} mod {mod_number}", file=sys.stderr)
                    return None
                continue
            except requests.exceptions.RequestException as e:
                print(f"Request error fetching HTML for {contract_id} mod {mod_number}: {e}", file=sys.stderr)
                return None
            except Exception as e:
                print(f"Error parsing HTML for {contract_id} mod {mod_number}: {e}", file=sys.stderr)
                return None

        return None

    def _combine_description_parts(self, reason_for_mod: str, contract_description: str) -> str:
        """
        Combine reason for modification and contract description.

        Args:
            reason_for_mod: The reason for modification text
            contract_description: The contract requirement description

        Returns:
            Combined description with reason for modification prepended
        """
        parts = []

        if reason_for_mod:
            parts.append(f"Reason for Modification: {reason_for_mod}")

        if contract_description:
            parts.append(contract_description)

        if not parts:
            return ""

        # Join with a period and space if both parts exist
        if len(parts) > 1:
            # Ensure first part ends with period, then join with space
            first_part = parts[0]
            if first_part and first_part[-1] not in '.!?':
                first_part += "."

            second_part = parts[1]
            if second_part and second_part[-1] not in '.!?':
                second_part += "."

            return first_part + " " + second_part
        else:
            # Just one part - add period if it doesn't end with punctuation
            text = parts[0]
            if text and text[-1] not in '.!?':
                text += "."
            return text

    def _debug_save_html(self, contract_id: str, mod_number: str, html_content: str, reason_for_mod: str, contract_description: str) -> None:
        """
        Save HTML to debug file when description extraction fails.

        Args:
            contract_id: The contract ID
            mod_number: The modification number
            html_content: The full HTML response
            reason_for_mod: What was found for reason for modification
            contract_description: What was found for contract description
        """
        try:
            # Create debug directory if it doesn't exist
            debug_dir = "debug_html"
            os.makedirs(debug_dir, exist_ok=True)

            # Create filename with contract info
            filename = f"{debug_dir}/no_description_{contract_id}_{mod_number}.html"

            # Save HTML with debug info header
            debug_info = f"""<!--
DEBUG INFO for {contract_id} mod {mod_number}
==============================================
Reason for Modification Found: '{reason_for_mod}'
Contract Description Found: '{contract_description}'
Generated at: {date.today()}
URL: {self._build_source_url(contract_id, mod_number, '', '')}
==============================================
-->

"""

            with open(filename, 'w', encoding='utf-8') as f:
                f.write(debug_info)
                f.write(html_content)

            print(f"DEBUG: Saved HTML to {filename} for manual inspection", file=sys.stderr)

        except Exception as e:
            print(f"DEBUG: Failed to save HTML file: {e}", file=sys.stderr)

    def _build_source_url(self, contract_id: str, mod_number: str, reference_idv: str = '', award_type: str = '', agency_id: str = '8000') -> str:
        """
        Builds the source URL for a contract detail page.

        Args:
            contract_id: The contract ID
            mod_number: The modification number
            reference_idv: The reference IDV contract ID (if any)
            award_type: The award/IDV type for BPA handling
            agency_id: The contracting agency ID from CSV (defaults to NASA's 8000)

        Returns:
            The complete URL to the contract detail page
        """
        is_bpa = self._is_bpa(award_type)
        is_bpa_call = self._is_bpa_call(award_type)

        # BPA Call contracts should use AWARD contract type and transaction number 0
        if is_bpa_call:
            contract_type = 'AWARD'
            transaction_number = '0'
        elif is_bpa:
            contract_type = 'IDV'
            transaction_number = ''
        else:
            contract_type = 'AWARD'
            transaction_number = '0'

        params = {
            'agencyID': agency_id,
            'PIID': contract_id,
            'modNumber': mod_number,
            'transactionNumber': transaction_number,
            'idvAgencyID': agency_id if reference_idv else '',
            'idvPIID': reference_idv if reference_idv else '',
            'actionSource': 'searchScreen',
            'actionCode': '',
            'documentVersion': '1.5',
            'contractType': contract_type,
            'docType': 'B'
        }

        # Build URL with parameters
        param_str = '&'.join([f"{k}={v}" for k, v in params.items()])
        return f"{self.HTML_DETAIL_BASE_URL}?{param_str}"


# --- Example Usage ---
if __name__ == '__main__':
    print("Running FPDS Query Example...")

    # Create an instance of the query class
    fpds_query = FPDSQuery()

    try:
        # Perform the search
        results_df = fpds_query.search()

        # Display results
        if not results_df.empty:
            print("\nFPDS Contract Search Results:")
            print(results_df.to_string())
            print(f"\nTotal records retrieved: {len(results_df)}")
        else:
            print("\nNo contracts found matching the criteria or an error occurred.")

    except Exception as e:
        print(f"\nAn unexpected error occurred: {e}", file=sys.stderr)