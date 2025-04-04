import requests
import json
from typing import Any, Dict, Optional, List

# Configuration
AWARD_LOOKUP_BASE_URL = "https://api.usaspending.gov/api/v2/awards/"
AWARD_SEARCH_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"

# (Award class definition remains the same as previously provided)
class Award:
    """
    Represents a USAspending award, holding the data returned by the API.

    This class provides access to the award data retrieved from the
    USAspending API. It stores the raw JSON data and allows access
    to its fields.
    """
    def __init__(self, data: Dict[str, Any]):
        """
        Initializes the Award object.

        Args:
            data: A dictionary containing the award data from the API JSON response.
        """
        self._data = data

    @property
    def prime_award_id(self) -> str:
        """Returns the prime award ID (piid or fain) from the award data."""
        return self._data.get("Award ID") or self._data.get("piid") or self._data.get("fain") or ""

    def __getattr__(self, name: str) -> Any:
        """Allows accessing dictionary keys as attributes."""
        if name in self._data:
            return self._data[name]
        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")

    def get(self, key: str, default: Optional[Any] = None) -> Any:
        """Provides dictionary-like get access."""
        return self._data.get(key, default)

    def __repr__(self) -> str:
        """Provides a string representation of the Award object."""
        display_id = self.get('Award ID') or self.get('generated_unique_award_id', 'N/A')
        category = self.get('category') or self.get('Award Type', 'N/A')
        return f"<Award id='{display_id}' category='{category}'>"

    @property
    def raw_data(self) -> Dict[str, Any]:
        """Returns the raw dictionary data."""
        return self._data


class USASpendingClient:
    """
    A client class to interact with the USAspending.gov API v2.

    Provides methods to fetch award details via lookup and search endpoints,
    respecting API limitations on grouping award type codes.
    """
    # --- Award Type Code Constants (Grouped by API constraints) ---
    CONTRACT_CODES: List[str] = ["A", "B", "C", "D"]
    IDV_CODES: List[str] = ["IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C", "IDV_C", "IDV_D", "IDV_E"]
    GRANT_CODES: List[str] = ["02", "03", "04", "05"]
    LOAN_CODES: List[str] = ["07", "08"]
    # Based on user JSON: "other_financial_assistance" group
    DIRECT_PAYMENT_CODES: List[str] = ["06", "10"]
    # Based on user JSON: "direct_payments" group (confusing name, contains Insurance/Other)
    INSURANCE_OTHER_FA_CODES: List[str] = ["09", "11", "-1"]

    # Default fields requested by the search endpoint
    DEFAULT_SEARCH_FIELDS: List[str] = [
        "Award ID", "Recipient Name", "recipient_id", "prime_award_recipient_id",
        "Award Amount", "Total Outlays", "Description", "Award Type",
        "Start Date", "End Date", "Awarding Agency", "Awarding Sub Agency",
        "def_codes", "COVID-19 Obligations", "COVID-19 Outlays",
        "Infrastructure Obligations", "Infrastructure Outlays",
        "Contract Award Type", "CFDA Number", "Loan Value", "Subsidy Cost", "Last Modified Date",
        "Period of Performance Current End Date"
    ]

    def __init__(self, award_lookup_base_url: str = AWARD_LOOKUP_BASE_URL, award_search_url: str = AWARD_SEARCH_URL):
        """
        Initializes the USASpendingClient.

        Args:
            award_lookup_base_url: The base URL for the award lookup endpoint.
            award_search_url: The URL for the award search endpoint.
        """
        self.award_lookup_base_url = award_lookup_base_url
        self.award_search_url = award_search_url
        self.default_headers = {"Content-Type": "application/json"}
        self._session = requests.Session()
        self._session.headers.update(self.default_headers)

    def award_search(self, keywords: List[str], award_type_codes: List[str], limit: int = 100, page: int = 1) -> List[Award]:
        """
        Performs a search for awards based on keywords and a *single group* of type codes
        using the spending_by_award endpoint.

        Args:
            keywords: A list of keywords (can include Award IDs, recipient names, etc.).
            award_type_codes: A list of award type codes *from a single valid API group*
                              (e.g., only contract codes, only grant codes).
            limit: The maximum number of results to return per page. Defaults to 100.
            page: The page number to retrieve. Defaults to 1.

        Returns:
            A list of Award objects matching the search criteria, or an empty list if
            an error occurs or no results are found.
        """
        if not keywords:
             print("Warning: award_search called with empty keywords list.")
             return []
        if not award_type_codes:
             print("Error: award_search requires a non-empty list of award_type_codes.")
             return []

        # Using a default time period that covers most historical data up to near future
        start_date = "2007-10-01"
        # Current date: 2025-04-04
        end_date = "2025-09-30" # End of current FY based on run date

        payload = {
            "filters": {
                "keywords": keywords,
                "time_period": [{"start_date": start_date, "end_date": end_date}],
                "award_type_codes": award_type_codes # Must be from a single group
            },
            "fields": self.DEFAULT_SEARCH_FIELDS,
            "page": page,
            "limit": limit,
            "sort": "Award Amount",
            "order": "desc",
            "subawards": False
        }

        try:
            response = self._session.post(self.award_search_url, json=payload)

            if 'application/json' in response.headers.get('Content-Type', ''):
                data = response.json()
                # Check for application-level errors even with 200 status
                # Example error: {"message":"'award_type_codes' must only contain types from one group."}
                if response.status_code != 200 or "message" in data or "detail" in data:
                    error_detail = data.get("message", data.get("detail", response.text))
                    print(f"API Error searching for types {award_type_codes} (Status {response.status_code}): {error_detail}")
                    # Don't return partial data on error
                    return []

                results = data.get("results", [])
                return [Award(result_data) for result_data in results]
            else:
                # Handle non-JSON error pages
                print(f"Error: Unexpected content type received from search: {response.headers.get('Content-Type')}")
                print(f"Response Status: {response.status_code}")
                print(f"Response text: {response.text[:500]}...")
                response.raise_for_status() # Raise HTTPError for non-JSON errors
                return [] # Should not be reached

        except requests.exceptions.RequestException as e:
            print(f"Error during API search request: {e}")
            if hasattr(e, 'response') and e.response is not None:
                 print(f"API Response Status: {e.response.status_code}")
                 print(f"API Response Text: {e.response.text[:500]}...")
            return []
        except json.JSONDecodeError as e:
            print(f"Error decoding JSON search response: {e}")
            # Log details if possible (response object might not be available here)
            return []
        except Exception as e:
            print(f"An unexpected error occurred during award search: {e}")
            return []

    # --- Helper Search Methods (Search within a single valid group) ---

    def contract_search(self, keywords: List[str], limit: int = 100) -> List[Award]:
        """Searches specifically for Contracts based on keywords."""
        print(f"Searching contracts matching keywords: {keywords}")
        return self.award_search(keywords=keywords, award_type_codes=self.CONTRACT_CODES, limit=limit)

    def idv_search(self, keywords: List[str], limit: int = 100) -> List[Award]:
        """Searches specifically for Indefinite Delivery Vehicles (IDVs) based on keywords."""
        print(f"Searching IDVs matching keywords: {keywords}")
        return self.award_search(keywords=keywords, award_type_codes=self.IDV_CODES, limit=limit)

    def grant_search(self, keywords: List[str], limit: int = 100) -> List[Award]:
        """Searches specifically for Grants (types 02-05) based on keywords."""
        print(f"Searching grants matching keywords: {keywords}")
        return self.award_search(keywords=keywords, award_type_codes=self.GRANT_CODES, limit=limit)

    def loan_search(self, keywords: List[str], limit: int = 100) -> List[Award]:
        """Searches specifically for Loans (types 07-08) based on keywords."""
        print(f"Searching loans matching keywords: {keywords}")
        return self.award_search(keywords=keywords, award_type_codes=self.LOAN_CODES, limit=limit)

    def direct_payment_search(self, keywords: List[str], limit: int = 100) -> List[Award]:
        """Searches specifically for Direct Payments (types 06, 10) based on keywords."""
        print(f"Searching direct payments (06, 10) matching keywords: {keywords}")
        return self.award_search(keywords=keywords, award_type_codes=self.DIRECT_PAYMENT_CODES, limit=limit)

    def insurance_other_fa_search(self, keywords: List[str], limit: int = 100) -> List[Award]:
        """Searches specifically for Insurance and Other Financial Assistance
           (types 09, 11, -1) based on keywords."""
        print(f"Searching insurance/other FA (09, 11, -1) matching keywords: {keywords}")
        return self.award_search(keywords=keywords, award_type_codes=self.INSURANCE_OTHER_FA_CODES, limit=limit)

    # --- Combined Search (Multiple API Calls) ---

    def all_award_search(self, keywords: List[str], limit: int = 100) -> List[Award]:
        """
        Searches across all major award groups (Contracts, IDVs, Grants, Loans,
        Direct Payments (06,10), Insurance/Other FA (09,11,-1)) for the given keywords
        by making separate API calls for each group and combining the results.

        Args:
            keywords: A list of keywords to search for.
            limit: The maximum number of results to retrieve *for each award type group*.
                   The total results may exceed this limit significantly (up to limit * 6).

        Returns:
            A combined list of unique Award objects found across the searches.
        """
        print(f"Searching all award types matching keywords: {keywords} (limit {limit} per type group)")

        # Use a dictionary to store results, keyed by 'Award ID' to handle duplicates
        combined_results_dict: Dict[str, Award] = {}
        search_groups = {
            "Contracts": self.CONTRACT_CODES,
            "IDVs": self.IDV_CODES,
            "Grants": self.GRANT_CODES,
            "Loans": self.LOAN_CODES,
            "Direct Payments (06,10)": self.DIRECT_PAYMENT_CODES,
            "Insurance/Other FA (09,11,-1)": self.INSURANCE_OTHER_FA_CODES,
        }

        for group_name, group_codes in search_groups.items():
            print(f" -> Searching {group_name}...")
            results = self.award_search(keywords=keywords, award_type_codes=group_codes, limit=limit)
            print(f"    Found {len(results)} {group_name.lower()} results.")
            for award in results:
                # Use prime_award_id which attempts to get 'Award ID' first
                award_id = award.prime_award_id
                if award_id and award_id not in combined_results_dict:
                    combined_results_dict[award_id] = award
                elif award_id and award_id in combined_results_dict:
                     # Optional: Log if a duplicate was found across groups
                     # print(f"    (Duplicate award ID {award_id} found in {group_name}, already present)")
                     pass

        # Convert the unique awards back to a list
        final_results = list(combined_results_dict.values())
        print(f"Total unique awards found across all types: {len(final_results)}")
        return final_results

    # --- Original Award Lookup Method ---
    # (award_lookup method remains the same as the previous version)
    def award_lookup(self, usa_spending_award_id: str) -> Optional[Award]:
        """
        Fetches award details for a specific USAspending award ID using the lookup endpoint.

        Args:
            usa_spending_award_id: The unique identifier for the award
                                   (e.g., 'CONT_AWD_H907_9700_SPE2DX16D1500_9700').
                                   This should typically be the generated_unique_award_id.

        Returns:
            An Award object containing the fetched data, or None if an error occurs
            or the award is not found.
        """
        if not usa_spending_award_id:
            print("Error: usa_spending_award_id cannot be empty.")
            return None

        endpoint = f"{self.award_lookup_base_url}{usa_spending_award_id}"

        try:
            response = self._session.get(endpoint)
            response.raise_for_status()

            if 'application/json' in response.headers.get('Content-Type', ''):
                award_data = response.json()
                return Award(award_data)
            else:
                print(f"Error: Unexpected content type received from lookup: {response.headers.get('Content-Type')}")
                print(f"Response text: {response.text[:500]}...")
                return None

        except requests.exceptions.RequestException as e:
            print(f"Error during API lookup request to {endpoint}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                 print(f"API Response Status: {e.response.status_code}")
                 print(f"API Response Text: {e.response.text[:500]}...")
            return None
        except json.JSONDecodeError as e:
            print(f"Error decoding JSON lookup response from {endpoint}: {e}")
            print(f"Response text: {response.text[:500]}...")
            return None
        except Exception as e:
            print(f"An unexpected error occurred during award lookup: {e}")
            return None

# --- Example Usage (Illustrative) ---
# if __name__ == "__main__":
#     client = USASpendingClient()
#
#     print("\n--- Testing Corrected All Award Search ---")
#     keywords = ["Specific Company Name"] # Replace with actual keywords
#     all_awards_corrected = client.all_award_search(keywords=keywords, limit=10) # Small limit for example
#
#     if all_awards_corrected:
#         print(f"\nFound {len(all_awards_corrected)} unique awards for '{keywords[0]}':")
#         # Further analysis or printing can be done here
#     else:
#         print(f"No awards found for keywords '{keywords}'.")

