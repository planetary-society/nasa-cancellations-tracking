import unittest
from unittest.mock import patch, MagicMock, call
import pandas as pd
import requests # For mocking requests exceptions
from datetime import datetime # For date formatting in description

# Assuming DOGEQuery is in 'doge_search.py'
# from doge_search import DOGEQuery
# Assuming ContractQuery is in 'contract_query.py' (or a base class file)
# from contract_query import ContractQuery
# Assuming utility functions are in 'utils.py'
# from utils import format_as_currency 

# --- Start Placeholder classes and functions ---
# These would normally be imported from other modules.

class ContractQuery: # Minimal placeholder from previous tasks
    FINAL_COLUMNS = ["Award ID", "recipient", "description", "source_url", "total_obligations"]
    def __init__(self, api_key="DEMO_KEY"):
        self.api_key = api_key
        self.final_columns = list(self.FINAL_COLUMNS) # Ensure copy

    def export_to_csv(self, df: pd.DataFrame, search_term: str) -> str:
        # Simplified mockable version
        filename = f"data/contracts_{search_term}_mock.csv"
        print(f"Mock export_to_csv called with filename: {filename}")
        return filename

def format_as_currency(amount: float) -> str:
    if amount is None:
        return "$0.00" # Or handle as per actual function
    return f"${amount:,.2f}"

class DOGEQuery(ContractQuery):
    DOGE_API_BASE_URL = "https://doge.larc.nasa.gov/search"
    CONTRACTS_ENDPOINT = "/contracts"
    GRANTS_ENDPOINT = "/grants"
    
    # Default columns that DOGE API items are mapped to.
    # These are often a superset of ContractQuery.FINAL_COLUMNS initially
    DOGE_FINAL_COLUMNS = [
        "Award ID", "source_url", "solicitation_id", "agency", 
        "recipient", "description", "start_date", "end_date", 
        "last_modified_date", "award_value", "savings" 
    ]

    def __init__(self, api_key="DEMO_KEY", verbose=False): # api_key not used by DOGE but for parent
        super().__init__(api_key=api_key)
        self.verbose = verbose
        self.doge_api_base_url = self.DOGE_API_BASE_URL
        self.session = requests.Session() # Placeholder, not used if requests.get is mocked directly

        # Ensure "Award ID" is in final_columns, as it's critical
        if "Award ID" not in self.final_columns:
            self.final_columns.append("Award ID")
        
        # For DOGE, we use its own defined columns, but ensure parent's are covered if needed
        # Or, more likely, DOGEQuery defines its own output columns.
        # For this test setup, let's assume DOGE_FINAL_COLUMNS is what DOGEQuery aims to produce.
        self.final_columns = list(self.DOGE_FINAL_COLUMNS) # Use DOGE's specific columns
        if "Award ID" not in self.final_columns: # Double check for DOGE_FINAL_COLUMNS
             self.final_columns.insert(0, "Award ID")


    def _log(self, message):
        if self.verbose:
            print(f"[DOGEQuery LOG]: {message}")

    def _is_nasa_agency(self, item_dict):
        agency_name_value = item_dict.get("agency")
        if not isinstance(agency_name_value, str):
            agency_name_lower = "" # Default to empty string if not a string or None
        else:
            agency_name_lower = agency_name_value.strip().lower()
            
        if agency_name_lower in ["national aeronautics and space administration", "nasa"]:
            return True, agency_name_lower
        return False, agency_name_lower

    def _extract_award_id_from_contract_url(self, url: str):
        if not url: return None
        try:
            # Example: https://www.fpds.gov/common/jsp/LaunchJSP.jsp? Например=DRS&PIID=NND17AA01C&PSTART=10/01/2016&PEND=09/30/2021 ...
            if "PIID=" in url:
                return url.split("PIID=")[1].split("&")[0]
        except Exception: # Broad exception for malformed URLs
            return None
        return None

    def _extract_usa_spending_award_id_from_grant_url(self, url: str):
        if not url or not url.startswith("https://usaspending.gov/award/"):
            return None
        try:
            # Example: https://usaspending.gov/award/ASST_NON_23K75IL000001_1605
            parts = url.split('/')
            if len(parts) > 4 and parts[3] == "award":
                award_id_part = parts[4]
                return award_id_part if award_id_part else None # Return None if empty
        except Exception:
            return None
        return None

    def _standardize_doge_item(self, item, item_type="Contract"):
        standardized = {col: None for col in self.final_columns}
        
        if item_type == "Contract":
            standardized["Award ID"] = self._extract_award_id_from_contract_url(item.get("fpds_link"))
            standardized["source_url"] = item.get("fpds_link")
            standardized["solicitation_id"] = item.get("solicitation_id")
            standardized["agency"] = item.get("agency")
            standardized["recipient"] = item.get("vendor") # Mapping
            
            desc_parts = [
                f"Status: {item.get('status', 'N/A')}",
                f"Est. Savings: {format_as_currency(item.get('estimated_total_savings_value')) if item.get('estimated_total_savings_value') is not None else 'N/A'}",
                f"Date: {item.get('date_added', 'N/A')}",
                item.get("description", "")
            ]
            standardized["description"] = " | ".join(filter(None, desc_parts))
            standardized["start_date"] = item.get("period_of_performance_start_date")
            standardized["end_date"] = item.get("period_of_performance_current_end_date")
            standardized["last_modified_date"] = item.get("last_modified_date")
            standardized["award_value"] = item.get("value") 
            standardized["savings"] = item.get("estimated_total_savings_value")

        elif item_type == "Grant":
            standardized["Award ID"] = self._extract_usa_spending_award_id_from_grant_url(item.get("usaspending_permalink"))
            standardized["source_url"] = item.get("usaspending_permalink")
            standardized["solicitation_id"] = item.get("solicitation_number") # Mapping
            standardized["agency"] = item.get("agency") # Assuming same key
            standardized["recipient"] = item.get("grantee") # Mapping
            
            desc_parts = [
                f"Status: {item.get('status', 'N/A')}",
                f"Title: {item.get('title', 'N/A')}",
                f"Date Added: {item.get('date_added', 'N/A')}",
            ]
            standardized["description"] = " | ".join(filter(None, desc_parts))
            standardized["start_date"] = item.get("start_date")
            standardized["end_date"] = item.get("end_date")
            standardized["last_modified_date"] = item.get("last_modified_date")
            standardized["award_value"] = item.get("total_funding_amount") # Mapping
            standardized["savings"] = None # Grants don't typically have "savings" in this context

        else:
            self._log(f"Unknown item type for standardization: {item_type}")
            return None
            
        return standardized

    def _fetch_and_process_endpoint(self, endpoint_url, item_type, params=None):
        all_items_standardized = []
        current_page = params.get("page", 1) if params else 1
        # Make a copy of params to avoid modifying the original dict passed to the function,
        # especially important if it's reused in loops or by the caller.
        # However, for pagination, we DO want to modify the 'page' param for each loop.
        # The issue is more about how the mock call verification is done vs. in-place dict modification.
        # Let's create a 'current_params' for each iteration.
        
        base_params = params.copy() if params else {}
        total_pages = current_page # Assume 1 page initially, API response will update this

        while current_page <= total_pages:
            current_params = base_params.copy() # Start with a fresh copy of base params
            current_params["page"] = current_page # Set current page for this request

            self._log(f"Fetching {item_type} page {current_page} from {endpoint_url} with params {current_params}")
            
            try:
                response = requests.get(endpoint_url, params=current_params, timeout=10) # Using requests.get directly
                response.raise_for_status()
                data = response.json()

                items_on_page = data.get("results", [])
                if not items_on_page and current_page == 1: # No results at all
                    self._log(f"No {item_type} items found at {endpoint_url}")
                    break 
                
                for item in items_on_page:
                    is_nasa, agency_name = self._is_nasa_agency(item)
                    if is_nasa:
                        standardized_item = self._standardize_doge_item(item, item_type=item_type)
                        if standardized_item:
                            all_items_standardized.append(standardized_item)
                    else:
                        self._log(f"Skipping non-NASA item from agency: {agency_name}")

                # Update total_pages based on API response (if provided)
                # This is a common pattern; specific key might vary e.g. "total_pages", "num_pages"
                # For DOGE API, it seems to be "count" and "limit" to calculate total_pages
                count = data.get("count") 
                limit = params.get("limit", 10) # Assuming a default limit or it's in params
                if count and limit:
                    total_pages = (count + limit - 1) // limit # Ceiling division
                else: # If no count/limit, assume only one page or rely on items_on_page emptiness
                    if not items_on_page and current_page > 1 : # No more items on subsequent pages
                         break 
                    elif not items_on_page and current_page == 1: # No items on first page
                         break


                if current_page >= total_pages: # Exit if we've fetched all known pages
                    break
                current_page += 1
            
            except requests.exceptions.Timeout:
                self._log(f"Timeout fetching {item_type} from {endpoint_url} page {current_page}")
                break 
            except requests.exceptions.RequestException as e:
                self._log(f"RequestException fetching {item_type}: {e}")
                break
            except ValueError: # Includes JSONDecodeError
                self._log(f"JSONDecodeError fetching {item_type}. Response: {response.text[:100]}")
                break
        
        return pd.DataFrame(all_items_standardized) if all_items_standardized else pd.DataFrame(columns=self.final_columns)

    def search(self, search_term: str = "NASA", export: bool = False):
        self._log(f"Starting DOGE search for term: '{search_term}'")
        
        # For DOGE, search_term might be part of params for _fetch_and_process_endpoint
        # Or the endpoints are fixed and we filter locally (less likely for a search API)
        # The placeholder _fetch_and_process_endpoint doesn't use search_term in its URL construction directly
        # but would pass it in params if the API supported it.
        # For this structure, let's assume the DOGE API endpoints are for general browsing by page
        # and filtering by NASA is done locally (as per _is_nasa_agency).
        # If DOGE API supports a query parameter for search_text, it should be added to params.
        # Let's assume a 'q' parameter for search_text for now.
        
        common_params = {"q": search_term, "limit": 50} # Example common params, changed search_text to search_term

        contracts_df = self._fetch_and_process_endpoint(
            f"{self.doge_api_base_url}{self.CONTRACTS_ENDPOINT}",
            item_type="Contract",
            params=common_params.copy()
        )
        self._log(f"Fetched {len(contracts_df)} contract(s).")

        grants_df = self._fetch_and_process_endpoint(
            f"{self.doge_api_base_url}{self.GRANTS_ENDPOINT}",
            item_type="Grant",
            params=common_params.copy()
        )
        self._log(f"Fetched {len(grants_df)} grant(s).")

        if contracts_df.empty and grants_df.empty:
            self._log("No contracts or grants found.")
            # Ensure consistent empty DataFrame structure
            return pd.DataFrame(columns=self.final_columns) 

        # Concatenate results. Ensure consistent columns.
        # If one DF is empty, concat still works.
        # Make sure both DFs have the same columns before concat if they might differ.
        # The _fetch_and_process_endpoint should return DFs with self.final_columns.
        final_df = pd.concat([contracts_df, grants_df], ignore_index=True)
        
        if final_df.empty: # Re-check after concat, though covered by initial check
             return pd.DataFrame(columns=self.final_columns)

        # Ensure all final_columns are present, fill with None if missing from some source
        for col in self.final_columns:
            if col not in final_df.columns:
                final_df[col] = None
        final_df = final_df[self.final_columns] # Reorder and select

        if export:
            safe_search_term = "".join(c if c.isalnum() else "_" for c in search_term)
            filename = f"DOGE_search_{safe_search_term}_{datetime.now().strftime('%Y%m%d')}.csv"
            # Using parent's export_to_csv, but DOGEQuery might have its own.
            # For this test, we'll assume it can use parent's or has a compatible one.
            # This part needs careful thought on how export is handled (e.g., if ContractQuery's export is generic enough)
            # Let's assume a direct call to a mockable export for now.
            self.export_to_csv(final_df, f"DOGE_{safe_search_term}") 
            self._log(f"Exported combined results for '{search_term}'.")
            
        return final_df

# --- End Placeholder classes and functions ---


class TestDOGEQuery(unittest.TestCase):
    def test_init(self):
        # Test with default parameters
        client = DOGEQuery()
        self.assertEqual(client.api_key, "DEMO_KEY") # From ContractQuery parent
        self.assertFalse(client.verbose)
        self.assertEqual(client.doge_api_base_url, DOGEQuery.DOGE_API_BASE_URL)
        self.assertIsInstance(client.session, requests.Session)
        
        # Check final_columns initialization (uses DOGE_FINAL_COLUMNS)
        expected_cols = list(DOGEQuery.DOGE_FINAL_COLUMNS) # Make a copy
        if "Award ID" not in expected_cols: # Ensure Award ID logic is tested
            expected_cols.insert(0, "Award ID")
        # Remove duplicates if "Award ID" was already there and also added
        final_expected_cols = []
        for col in expected_cols:
            if col not in final_expected_cols:
                final_expected_cols.append(col)

        self.assertEqual(client.final_columns, final_expected_cols)
        # Specifically check "Award ID" presence
        self.assertIn("Award ID", client.final_columns)

        # Test with verbose=True
        client_verbose = DOGEQuery(verbose=True)
        self.assertTrue(client_verbose.verbose)

        # Test if ContractQuery's FINAL_COLUMNS are handled if DOGE_FINAL_COLUMNS is modified
        # This depends on how DOGEQuery's __init__ is structured regarding parent's columns.
        # The current placeholder DOGEQuery explicitly sets self.final_columns = list(self.DOGE_FINAL_COLUMNS)
        # So, let's test that behavior.
        
        # Verify "Award ID" is added if not in DOGE_FINAL_COLUMNS (hypothetically)
        original_doge_cols = DOGEQuery.DOGE_FINAL_COLUMNS
        DOGEQuery.DOGE_FINAL_COLUMNS = ["col_a", "col_b"] # No "Award ID"
        client_no_award_id_initially = DOGEQuery()
        # The __init__ of placeholder DOGEQuery adds "Award ID" if not present in its own list
        self.assertIn("Award ID", client_no_award_id_initially.final_columns)
        self.assertEqual(len(client_no_award_id_initially.final_columns), 3) # col_a, col_b, Award ID
        DOGEQuery.DOGE_FINAL_COLUMNS = original_doge_cols # Restore

    # Placeholder for USASpendingClient instantiation test - DOGEQuery doesn't directly init USASpendingClient
    # This was a misinterpretation of the requirements for DOGEQuery.
    # DOGEQuery inherits ContractQuery but doesn't seem to use USASpendingClient in the provided structure.
    # If it were to, the test would be:
    # def test_init_usa_spending_client_instantiation(self):
    #     client = DOGEQuery()
    #     self.assertIsInstance(client.usa_spending_client, USASpendingClient) # Assuming it would have such an attribute

    def test_is_nasa_agency(self):
        client = DOGEQuery()
        
        # Exact matches (case-insensitive)
        self.assertEqual(client._is_nasa_agency({"agency": "National Aeronautics and Space Administration"}), 
                         (True, "national aeronautics and space administration"))
        self.assertEqual(client._is_nasa_agency({"agency": "NATIONAL AERONAUTICS AND SPACE ADMINISTRATION"}),
                         (True, "national aeronautics and space administration"))
        self.assertEqual(client._is_nasa_agency({"agency": "NASA"}), (True, "nasa"))
        self.assertEqual(client._is_nasa_agency({"agency": "nasa"}), (True, "nasa"))
        self.assertEqual(client._is_nasa_agency({"agency": "  NASA  "}), (True, "nasa")) # With spaces

        # Non-matching agency names
        self.assertEqual(client._is_nasa_agency({"agency": "Department of Defense"}), 
                         (False, "department of defense"))
        self.assertEqual(client._is_nasa_agency({"agency": "Not NASA"}), (False, "not nasa"))

        # Missing "agency" key
        self.assertEqual(client._is_nasa_agency({}), (False, "")) # Empty string if key missing
        
        # Non-string agency value
        self.assertEqual(client._is_nasa_agency({"agency": 123}), (False, "")) # Empty string if not a string
        self.assertEqual(client._is_nasa_agency({"agency": None}), (False, "")) # Empty string if None

    def test_extract_award_id_from_contract_url(self):
        client = DOGEQuery()
        valid_url1 = "https://www.fpds.gov/common/jsp/LaunchJSP.jsp? Например=DRS&PIID=NND17AA01C&PSTART=10/01/2016"
        self.assertEqual(client._extract_award_id_from_contract_url(valid_url1), "NND17AA01C")

        valid_url2 = "https://www.fpds.gov/ezsearch/search.do?s=FPDSNG.COM&PIID=00000GS35F0209V&DELETED=N&templateName=1.5.2"
        self.assertEqual(client._extract_award_id_from_contract_url(valid_url2), "00000GS35F0209V")
        
        url_no_piid = "https://www.fpds.gov/common/jsp/LaunchJSP.jsp?OTHER_PARAM=XYZ"
        self.assertIsNone(client._extract_award_id_from_contract_url(url_no_piid))
        
        malformed_url = "https://www.fpds.gov/common/PIIDNND17AA01C&PSTART=10/01/2016" # PIID= missing
        self.assertIsNone(client._extract_award_id_from_contract_url(malformed_url))

        url_piid_empty = "https://www.fpds.gov/common/jsp/LaunchJSP.jsp?PIID=&PSTART=10/01/2016"
        self.assertEqual(client._extract_award_id_from_contract_url(url_piid_empty), "") # Returns empty string if PIID value is empty

        self.assertIsNone(client._extract_award_id_from_contract_url(None))
        self.assertIsNone(client._extract_award_id_from_contract_url(""))
        self.assertIsNone(client._extract_award_id_from_contract_url("http://just.a.string"))

    def test_extract_usa_spending_award_id_from_grant_url(self):
        client = DOGEQuery()
        valid_url1 = "https://usaspending.gov/award/ASST_NON_23K75IL000001_1605"
        self.assertEqual(client._extract_usa_spending_award_id_from_grant_url(valid_url1), "ASST_NON_23K75IL000001_1605")

        valid_url2 = "https://usaspending.gov/award/CONT_IDV_GS35F0209V_4732/" # With trailing slash
        self.assertEqual(client._extract_usa_spending_award_id_from_grant_url(valid_url2), "CONT_IDV_GS35F0209V_4732")

        url_not_usaspending = "https://example.com/award/ASST_NON_23K75IL000001_1605"
        self.assertIsNone(client._extract_usa_spending_award_id_from_grant_url(url_not_usaspending))

        url_wrong_path_structure1 = "https://usaspending.gov/notaward/ASST_NON_23K75IL000001_1605"
        self.assertIsNone(client._extract_usa_spending_award_id_from_grant_url(url_wrong_path_structure1))
        
        url_wrong_path_structure2 = "https://usaspending.gov/award/" # No ID part
        self.assertIsNone(client._extract_usa_spending_award_id_from_grant_url(url_wrong_path_structure2))
        
        url_too_short = "https://usaspending.gov/award"
        self.assertIsNone(client._extract_usa_spending_award_id_from_grant_url(url_too_short))

        self.assertIsNone(client._extract_usa_spending_award_id_from_grant_url(None))
        self.assertIsNone(client._extract_usa_spending_award_id_from_grant_url(""))
        self.assertIsNone(client._extract_usa_spending_award_id_from_grant_url("http://just.a.string"))

    def test_standardize_doge_item_contract(self):
        client = DOGEQuery()
        # Override final_columns for this test to match what _standardize_doge_item expects for contracts
        client.final_columns = list(DOGEQuery.DOGE_FINAL_COLUMNS) 
        if "Award ID" not in client.final_columns: client.final_columns.insert(0, "Award ID")


        contract_item_data = {
            "fpds_link": "https://www.fpds.gov/common/jsp/LaunchJSP.jsp?PIID=CONTRACT001",
            "solicitation_id": "SOL001",
            "agency": "NASA",
            "vendor": "Test Vendor Inc.",
            "status": "Active",
            "estimated_total_savings_value": 12345.67,
            "date_added": "2023-01-15",
            "description": "Original contract description.",
            "period_of_performance_start_date": "2023-02-01",
            "period_of_performance_current_end_date": "2024-01-31", # Matches key in standardize
            "last_modified_date": "2023-03-01",
            "value": 100000.00
        }
        
        # Mock the URL extractor
        with patch.object(client, '_extract_award_id_from_contract_url', return_value="CONTRACT001") as mock_extract:
            standardized = client._standardize_doge_item(contract_item_data, item_type="Contract")
            mock_extract.assert_called_once_with("https://www.fpds.gov/common/jsp/LaunchJSP.jsp?PIID=CONTRACT001")

        self.assertIsNotNone(standardized)
        self.assertEqual(standardized["Award ID"], "CONTRACT001")
        self.assertEqual(standardized["source_url"], "https://www.fpds.gov/common/jsp/LaunchJSP.jsp?PIID=CONTRACT001")
        self.assertEqual(standardized["solicitation_id"], "SOL001")
        self.assertEqual(standardized["agency"], "NASA")
        self.assertEqual(standardized["recipient"], "Test Vendor Inc.")
        
        expected_desc = "Status: Active | Est. Savings: $12,345.67 | Date: 2023-01-15 | Original contract description."
        self.assertEqual(standardized["description"], expected_desc)
        
        self.assertEqual(standardized["start_date"], "2023-02-01")
        self.assertEqual(standardized["end_date"], "2024-01-31") # Check key mapping
        self.assertEqual(standardized["last_modified_date"], "2023-03-01")
        self.assertEqual(standardized["award_value"], 100000.00)
        self.assertEqual(standardized["savings"], 12345.67)

        # Test with missing optional fields
        minimal_contract_item = {"fpds_link": "http://piid=MIN002", "vendor": "Min Vendor"}
        with patch.object(client, '_extract_award_id_from_contract_url', return_value="MIN002"):
            standardized_min = client._standardize_doge_item(minimal_contract_item, item_type="Contract")
        
        self.assertEqual(standardized_min["Award ID"], "MIN002")
        self.assertEqual(standardized_min["recipient"], "Min Vendor")
        expected_min_desc = "Status: N/A | Est. Savings: N/A | Date: N/A" # No original description
        self.assertEqual(standardized_min["description"], expected_min_desc)
        self.assertIsNone(standardized_min["solicitation_id"]) # Should be None if not present


    def test_standardize_doge_item_grant(self):
        client = DOGEQuery()
        client.final_columns = list(DOGEQuery.DOGE_FINAL_COLUMNS)
        if "Award ID" not in client.final_columns: client.final_columns.insert(0, "Award ID")

        grant_item_data = {
            "usaspending_permalink": "https://usaspending.gov/award/GRANT001_USA",
            "solicitation_number": "SOL_GRANT_001", # Matches key in standardize
            "agency": "NASA HQ",
            "grantee": "Research Foundation XYZ", # Matches key in standardize
            "status": "Awarded",
            "title": "Space Research Grant",
            "date_added": "2022-11-01",
            "start_date": "2023-01-01",
            "end_date": "2025-12-31",
            "last_modified_date": "2022-12-01",
            "total_funding_amount": 500000.00 # Matches key in standardize
        }

        with patch.object(client, '_extract_usa_spending_award_id_from_grant_url', return_value="GRANT001_USA") as mock_extract:
            standardized = client._standardize_doge_item(grant_item_data, item_type="Grant")
            mock_extract.assert_called_once_with("https://usaspending.gov/award/GRANT001_USA")
            
        self.assertIsNotNone(standardized)
        self.assertEqual(standardized["Award ID"], "GRANT001_USA")
        self.assertEqual(standardized["source_url"], "https://usaspending.gov/award/GRANT001_USA")
        self.assertEqual(standardized["solicitation_id"], "SOL_GRANT_001")
        self.assertEqual(standardized["agency"], "NASA HQ")
        self.assertEqual(standardized["recipient"], "Research Foundation XYZ")

        expected_desc = "Status: Awarded | Title: Space Research Grant | Date Added: 2022-11-01"
        self.assertEqual(standardized["description"], expected_desc)

        self.assertEqual(standardized["start_date"], "2023-01-01")
        self.assertEqual(standardized["end_date"], "2025-12-31")
        self.assertEqual(standardized["last_modified_date"], "2022-12-01")
        self.assertEqual(standardized["award_value"], 500000.00)
        self.assertIsNone(standardized["savings"]) # Should be None for grants

    @patch.object(DOGEQuery, '_log')
    def test_standardize_doge_item_unknown_type(self, mock_log):
        client = DOGEQuery()
        unknown_item_data = {"id": 1, "name": "Unknown Item"}
        standardized = client._standardize_doge_item(unknown_item_data, item_type="OtherType")
        
        self.assertIsNone(standardized)
        mock_log.assert_called_once_with("Unknown item type for standardization: OtherType")

    @patch('requests.get')
    @patch.object(DOGEQuery, '_is_nasa_agency')
    @patch.object(DOGEQuery, '_standardize_doge_item')
    @patch.object(DOGEQuery, '_log') # To verify logging calls
    def test_fetch_and_process_endpoint_single_page_nasa_items(
            self, mock_log, mock_standardize, mock_is_nasa, mock_requests_get):
        client = DOGEQuery(verbose=True) # Enable verbose for _log calls
        
        # Mock API response for a single page with 2 NASA items and 1 non-NASA
        mock_api_item_nasa1 = {"id": 1, "agency": "NASA"}
        mock_api_item_nasa2 = {"id": 2, "agency": "National Aeronautics and Space Administration"}
        mock_api_item_non_nasa = {"id": 3, "agency": "DOD"}
        
        mock_response = MagicMock()
        mock_response.status_code = 200
        # Assume API returns 'count' for total items and 'results' for items on page
        # and 'limit' is implicitly known or passed in params.
        # For a single page, 'count' might be <= 'limit'.
        mock_response.json.return_value = {
            "results": [mock_api_item_nasa1, mock_api_item_non_nasa, mock_api_item_nasa2],
            "count": 3, 
            # No next page link or total_pages, implying single page or last page
        }
        mock_requests_get.return_value = mock_response

        # Mock _is_nasa_agency behavior
        mock_is_nasa.side_effect = [
            (True, "nasa"),             # For item1
            (False, "dod"),             # For item2 (non-NASA)
            (True, "national aeronautics and space administration")  # For item3
        ]
        
        # Mock _standardize_doge_item behavior
        # Ensure the mocked standardized items have all columns expected by client.final_columns
        base_standardized = {col: None for col in client.final_columns}
        standardized_item1 = {**base_standardized, "Award ID": "N1", "agency": "NASA", "description": "Std Desc 1"}
        standardized_item2 = {**base_standardized, "Award ID": "N2", "agency": "NASA", "description": "Std Desc 2"}
        mock_standardize.side_effect = [standardized_item1, standardized_item2]

        df = client._fetch_and_process_endpoint("http://fake.doge/api/contracts", "Contract", params={"limit":10})

        # Assertions
        self.assertEqual(mock_requests_get.call_count, 1)
        mock_requests_get.assert_called_once_with("http://fake.doge/api/contracts", params={"page": 1, "limit": 10}, timeout=10)
        
        self.assertEqual(mock_is_nasa.call_count, 3)
        mock_is_nasa.assert_any_call(mock_api_item_nasa1)
        mock_is_nasa.assert_any_call(mock_api_item_non_nasa)
        mock_is_nasa.assert_any_call(mock_api_item_nasa2)

        self.assertEqual(mock_standardize.call_count, 2) # Only for NASA items
        mock_standardize.assert_any_call(mock_api_item_nasa1, item_type="Contract")
        mock_standardize.assert_any_call(mock_api_item_nasa2, item_type="Contract")
        
        self.assertEqual(len(df), 2)
        self.assertTrue(all(col in df.columns for col in client.final_columns))
        self.assertEqual(df.iloc[0]["Award ID"], "N1")
        self.assertEqual(df.iloc[1]["Award ID"], "N2")

        mock_log.assert_any_call("Skipping non-NASA item from agency: dod")


    @patch('requests.get')
    @patch.object(DOGEQuery, '_log')
    def test_fetch_and_process_endpoint_multiple_pages(self, mock_log, mock_requests_get):
        client = DOGEQuery(verbose=True)
        
        # Page 1 response
        resp1_data = {
            "results": [{"id": 1, "agency": "NASA"}], 
            "count": 2, # Total 2 items, limit 1 per page means 2 pages
            # Assuming limit is passed in params or a default is used by the method
        }
        # Page 2 response
        resp2_data = {
            "results": [{"id": 2, "agency": "NASA"}], 
            "count": 2,
        }
        # Page 3 (empty, to stop pagination if count was miscalculated or not trusted)
        resp3_data = {"results": [], "count": 2}


        mock_response_page1 = MagicMock(status_code=200)
        mock_response_page1.json.return_value = resp1_data
        mock_response_page2 = MagicMock(status_code=200)
        mock_response_page2.json.return_value = resp2_data
        mock_response_page3 = MagicMock(status_code=200) # For safety, if it tries page 3
        mock_response_page3.json.return_value = resp3_data


        mock_requests_get.side_effect = [mock_response_page1, mock_response_page2, mock_response_page3]

        # Mock _is_nasa_agency and _standardize_doge_item
        with patch.object(client, '_is_nasa_agency', return_value=(True, "nasa")) as mock_is_nasa, \
             patch.object(client, '_standardize_doge_item', side_effect=lambda item, **kw: item) as mock_standardize:

            df = client._fetch_and_process_endpoint("http://fake.doge/api/data", "Data", params={"limit": 1})
            
            self.assertEqual(mock_requests_get.call_count, 2) # Page 1, Page 2
            mock_requests_get.assert_any_call("http://fake.doge/api/data", params={"page": 1, "limit": 1}, timeout=10)
            mock_requests_get.assert_any_call("http://fake.doge/api/data", params={"page": 2, "limit": 1}, timeout=10)
            
            self.assertEqual(mock_is_nasa.call_count, 2) # One for each item
            self.assertEqual(mock_standardize.call_count, 2)
            self.assertEqual(len(df), 2)
            mock_log.assert_any_call("Fetching Data page 1 from http://fake.doge/api/data with params {'limit': 1, 'page': 1}")
            mock_log.assert_any_call("Fetching Data page 2 from http://fake.doge/api/data with params {'limit': 1, 'page': 2}")


    @patch('requests.get')
    @patch.object(DOGEQuery, '_log')
    def test_fetch_and_process_endpoint_api_error(self, mock_log, mock_requests_get):
        client = DOGEQuery(verbose=True)
        mock_requests_get.side_effect = requests.exceptions.Timeout("API Timed out")
        
        df = client._fetch_and_process_endpoint("http://fake.doge/api/fail", "FailType")
        self.assertTrue(df.empty)
        mock_log.assert_any_call("Timeout fetching FailType from http://fake.doge/api/fail page 1")

    @patch('requests.get')
    @patch.object(DOGEQuery, '_log')
    def test_fetch_and_process_endpoint_empty_results(self, mock_log, mock_requests_get):
        client = DOGEQuery(verbose=True)
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {"results": [], "count": 0}
        mock_requests_get.return_value = mock_response
        
        df = client._fetch_and_process_endpoint("http://fake.doge/api/empty", "EmptyType")
        self.assertTrue(df.empty)
        mock_log.assert_any_call("No EmptyType items found at http://fake.doge/api/empty")

    @patch('requests.get')
    @patch.object(DOGEQuery, '_log')
    def test_fetch_and_process_endpoint_json_decode_error(self, mock_log, mock_requests_get):
        client = DOGEQuery(verbose=True)
        mock_response = MagicMock(status_code=200, text="Invalid JSON")
        mock_response.json.side_effect = ValueError("JSONDecodeError simulation") # ValueError is base for JSONDecodeError
        mock_requests_get.return_value = mock_response

        df = client._fetch_and_process_endpoint("http://fake.doge/api/badjson", "BadJSON")
        self.assertTrue(df.empty)
        mock_log.assert_any_call("JSONDecodeError fetching BadJSON. Response: Invalid JSON")

    @patch.object(DOGEQuery, '_fetch_and_process_endpoint')
    @patch.object(DOGEQuery, 'export_to_csv') # Mocking the instance method directly
    @patch.object(DOGEQuery, '_log')
    def test_search_success_with_data_and_export(self, mock_log, mock_export_csv, mock_fetch_process):
        client = DOGEQuery(verbose=True)
        
        # Mock data for contracts and grants
        mock_contracts_data = [{"Award ID": "C001", "recipient": "Contractor A", "description": "Desc C1"}]
        mock_grants_data = [{"Award ID": "G001", "recipient": "Grantee X", "description": "Desc G1"}]
        
        df_contracts = pd.DataFrame(mock_contracts_data)
        df_grants = pd.DataFrame(mock_grants_data)

        # Configure _fetch_and_process_endpoint mock
        # It will be called twice: once for contracts, once for grants
        mock_fetch_process.side_effect = [df_contracts, df_grants]

        search_term = "NASA Research"
        result_df = client.search(search_term, export=True)

        # Verify _fetch_and_process_endpoint calls
        expected_contracts_url = f"{client.doge_api_base_url}{client.CONTRACTS_ENDPOINT}"
        expected_grants_url = f"{client.doge_api_base_url}{client.GRANTS_ENDPOINT}"
        expected_common_params = {"q": search_term, "limit": 50}
        
        calls = [
            call(expected_contracts_url, item_type="Contract", params=expected_common_params.copy()),
            call(expected_grants_url, item_type="Grant", params=expected_common_params.copy())
        ]
        mock_fetch_process.assert_has_calls(calls, any_order=False) # Order matters here

        # Verify DataFrame concatenation and content
        self.assertEqual(len(result_df), 2) # 1 contract + 1 grant
        self.assertIn("C001", result_df["Award ID"].values)
        self.assertIn("G001", result_df["Award ID"].values)
        
        # Verify final columns are present (even if data for some is None)
        self.assertTrue(all(col in result_df.columns for col in client.final_columns))

        # Verify export_to_csv call
        safe_search_term = "".join(c if c.isalnum() else "_" for c in search_term)
        # The filename for export_to_csv in the search method is "DOGE_search_{safe_search_term}_{date}.csv"
        # and the search_term argument to export_to_csv is "DOGE_{safe_search_term}"
        mock_export_csv.assert_called_once()
        # Check the first argument (DataFrame) by properties if direct comparison is tricky
        pd.testing.assert_frame_equal(mock_export_csv.call_args[0][0], result_df)
        self.assertEqual(mock_export_csv.call_args[0][1], f"DOGE_{safe_search_term}")

        mock_log.assert_any_call(f"Starting DOGE search for term: '{search_term}'")
        mock_log.assert_any_call(f"Fetched {len(df_contracts)} contract(s).")
        mock_log.assert_any_call(f"Fetched {len(df_grants)} grant(s).")
        mock_log.assert_any_call(f"Exported combined results for '{search_term}'.")

    @patch.object(DOGEQuery, '_fetch_and_process_endpoint')
    @patch.object(DOGEQuery, 'export_to_csv')
    def test_search_no_results(self, mock_export_csv, mock_fetch_process):
        client = DOGEQuery()
        
        # Simulate _fetch_and_process_endpoint returning empty DataFrames
        empty_df = pd.DataFrame(columns=client.final_columns)
        mock_fetch_process.return_value = empty_df # Both calls will return this

        search_term = "obscure term"
        result_df = client.search(search_term, export=False)

        self.assertTrue(result_df.empty)
        self.assertEqual(list(result_df.columns), client.final_columns) # Should have correct columns
        mock_export_csv.assert_not_called() # Export should not be called

    @patch.object(DOGEQuery, '_fetch_and_process_endpoint')
    @patch.object(DOGEQuery, 'export_to_csv')
    def test_search_only_contracts_found(self, mock_export_csv, mock_fetch_process):
        client = DOGEQuery()
        mock_contracts_data = [{"Award ID": "C001", "recipient": "Contractor A"}]
        df_contracts = pd.DataFrame(mock_contracts_data)
        empty_df_grants = pd.DataFrame(columns=client.final_columns)

        mock_fetch_process.side_effect = [df_contracts, empty_df_grants]
        
        search_term = "contracts only"
        result_df = client.search(search_term, export=False)

        self.assertEqual(len(result_df), 1)
        self.assertEqual(result_df.iloc[0]["Award ID"], "C001")
        mock_export_csv.assert_not_called()

if __name__ == '__main__':
    unittest.main()
