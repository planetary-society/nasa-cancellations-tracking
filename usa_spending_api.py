import requests
import json
from typing import Any, Dict, Optional, List, Union # Added Union
from datetime import datetime # Added for potential date parsing if needed later
import time

# Configuration
AWARD_LOOKUP_BASE_URL = "https://api.usaspending.gov/api/v2/awards/"
AWARD_SEARCH_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
TRANSACTIONS_URL = "https://api.usaspending.gov/api/v2/transactions/"

# --- Helper Classes for Standardization ---

class Location:
    """
    Represents a location (e.g., Place of Performance, Recipient Location).

    Standardizes access to location details which might come from the nested
    'place_of_performance' or 'recipient.location' objects in the lookup response
    or from individual flat fields (like 'Place of Performance State Code')
    in the search response.
    """
    def __init__(self, data: Dict[str, Any]):
        """
        Initializes the Location object.

        Args:
            data: A dictionary containing location data, potentially from
                  either the lookup or search API structure.
        """
        self._data = data if isinstance(data, dict) else {}

    @property
    def city_name(self) -> Optional[str]:
        """Returns the city name (primarily available from lookup)."""
        # Lookup key: 'city_name'
        # Search results generally don't include city name directly.
        return self._data.get("city_name")

    @property
    def state_code(self) -> Optional[str]:
        """Returns the state code (e.g., 'CA')."""
        # Lookup key: 'state_code'
        # Search key: 'Place of Performance State Code'
        return self._data.get("state_code") or self._data.get("Place of Performance State Code")

    @property
    def state_name(self) -> Optional[str]:
        """Returns the full state name (primarily available from lookup)."""
        # Lookup key: 'state_name'
        return self._data.get("state_name")

    @property
    def country_code(self) -> Optional[str]:
        """Returns the 3-letter country code (e.g., 'USA')."""
        # Lookup key: 'location_country_code'
        # Search key: 'Place of Performance Country Code'
        return self._data.get("location_country_code") or self._data.get("Place of Performance Country Code")

    @property
    def country_name(self) -> Optional[str]:
        """Returns the full country name (primarily available from lookup)."""
        # Lookup key: 'country_name'
        return self._data.get("country_name")

    @property
    def zip5(self) -> Optional[str]:
        """Returns the 5-digit ZIP code."""
        # Lookup key: 'zip5'
        # Search key: 'Place of Performance Zip5'
        zip_val = self._data.get("zip5") or self._data.get("Place of Performance Zip5")
        # Ensure it's returned as a string if not None
        return str(zip_val) if zip_val is not None else None

    @property
    def county_name(self) -> Optional[str]:
        """Returns the county name (primarily available from lookup)."""
        # Lookup key: 'county_name'
        return self._data.get("county_name")

    @property
    def county_code(self) -> Optional[str]:
        """Returns the county code (primarily available from lookup)."""
        # Lookup key: 'county_code'
        return self._data.get("county_code")

    @property
    def address_line1(self) -> Optional[str]:
        """Returns address line 1 (primarily available from lookup)."""
        # Lookup key: 'address_line1'
        return self._data.get("address_line1")

    # Add other location fields as needed (address_line2, zip4, congressional_code etc.)
    # by adding more properties accessing self._data.get(...)

    def __repr__(self) -> str:
        """Provides a concise string representation of the location."""
        city = self.city_name or "?"
        state = self.state_code or "?"
        country = self.country_code or "?"
        return f"<Location city={city}, state={state}, country={country}>"

    @property
    def raw_data(self) -> Dict[str, Any]:
        """Returns the raw dictionary data used to initialize this location."""
        return self._data

class PeriodOfPerformance:
    """
    Represents the period of performance dates.

    Standardizes access to dates which might come from the nested
    'period_of_performance' object in the lookup response or individual
    date fields (like 'Start Date', 'End Date') in the search response.
    """
    def __init__(self, data: Dict[str, Any]):
        """
        Initializes the PeriodOfPerformance object.

        Args:
            data: A dictionary containing period of performance data, potentially
                  from either the lookup or search API structure.
        """
        self._data = data if isinstance(data, dict) else {}

    @property
    def start_date(self) -> Optional[str]:
        """Returns the performance start date (YYYY-MM-DD)."""
        # Lookup key: 'start_date' (within period_of_performance object)
        # Search keys: 'Start Date' or 'Period of Performance Start Date'
        return self._data.get("start_date") or self._data.get("Start Date") or self._data.get("Period of Performance Start Date")

    @property
    def end_date(self) -> Optional[str]:
        """Returns the performance end date (YYYY-MM-DD)."""
        # Lookup key: 'end_date' (within period_of_performance object)
        # Search keys: 'End Date' or 'Period of Performance Current End Date'
        return self._data.get("end_date") or self._data.get("End Date") or self._data.get("Period of Performance Current End Date")

    @property
    def last_modified_date(self) -> Optional[str]:
        """Returns the last modified date (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)."""
        # Lookup key: 'last_modified_date' (within period_of_performance object)
        # Search key: 'Last Modified Date'
        return self._data.get("last_modified_date") or self._data.get("Last Modified Date")

    # Add potential_end_date if needed from lookup response via self._data.get('potential_end_date')

    def __repr__(self) -> str:
        """Provides a concise string representation of the period."""
        start = self.start_date or "?"
        end = self.end_date or "?"
        return f"<PeriodOfPerformance start={start}, end={end}>"

    @property
    def raw_data(self) -> Dict[str, Any]:
        """Returns the raw dictionary data used to initialize this period."""
        return self._data

class Recipient:
    """
    Represents the recipient details.

    Standardizes access to recipient info which might come from the nested
    'recipient' object in the lookup response or individual flat fields
    (like 'Recipient Name', 'Recipient DUNS Number') in the search response.
    """
    def __init__(self, data: Dict[str, Any]):
        """
        Initializes the Recipient object.

        Args:
            data: A dictionary containing recipient data, potentially
                  from either the lookup or search API structure.
        """
        self._data = data if isinstance(data, dict) else {}
        # Location is nested only in the lookup response's recipient object
        self._location_data = self._data.get("location")

    @property
    def name(self) -> Optional[str]:
        """Returns the recipient's name."""
        # Lookup key: 'recipient_name'
        # Search key: 'Recipient Name'
        return self._data.get("recipient_name") or self._data.get("Recipient Name")

    @property
    def duns(self) -> Optional[str]:
        """Returns the recipient's DUNS number."""
        # Lookup key: 'recipient_unique_id'
        # Search key: 'Recipient DUNS Number'
        return self._data.get("recipient_unique_id") or self._data.get("Recipient DUNS Number")

    @property
    def uei(self) -> Optional[str]:
        """Returns the recipient's UEI (primarily available from lookup)."""
        # Lookup key: 'recipient_uei'
        return self._data.get("recipient_uei")

    @property
    def recipient_hash(self) -> Optional[str]:
        """
        Returns the recipient hash (from lookup) or recipient_id (from search).
        The recipient_id from search includes the hash and level (e.g., R or P).
        """
        # Lookup key: 'recipient_hash'
        # Search key: 'recipient_id'
        return self._data.get("recipient_hash") or self._data.get("recipient_id")

    @property
    def parent_name(self) -> Optional[str]:
        """Returns the parent recipient's name (primarily available from lookup)."""
        # Lookup key: 'parent_recipient_name'
        return self._data.get("parent_recipient_name")

    @property
    def parent_duns(self) -> Optional[str]:
        """Returns the parent recipient's DUNS number (primarily available from lookup)."""
        # Lookup key: 'parent_recipient_unique_id'
        return self._data.get("parent_recipient_unique_id")

    @property
    def parent_uei(self) -> Optional[str]:
        """Returns the parent recipient's UEI (primarily available from lookup)."""
        # Lookup key: 'parent_recipient_uei'
        return self._data.get("parent_recipient_uei")

    @property
    def location(self) -> Optional[Location]:
        """
        Returns the recipient's location details as a Location object.
        Note: Location details for recipients are primarily available from the lookup endpoint.
        """
        if self._location_data and isinstance(self._location_data, dict):
            return Location(self._location_data)
        return None # Search results don't nest recipient location

    @property
    def business_categories(self) -> List[str]:
        """Returns a list of business categories (primarily available from lookup)."""
        # Lookup key: 'business_categories'
        return self._data.get("business_categories", [])

    def __repr__(self) -> str:
        """Provides a concise string representation of the recipient."""
        name = self.name or "?"
        duns = self.duns or "?"
        return f"<Recipient name='{name}', duns={duns}>"

    @property
    def raw_data(self) -> Dict[str, Any]:
        """Returns the raw dictionary data used to initialize this recipient."""
        return self._data

class Transaction:
    """
    Represents a single transaction associated with an award.
    Provides access to transaction details.
    """
    def __init__(self, data: Dict[str, Any]):
        """
        Initializes the Transaction object.

        Args:
            data: A dictionary containing transaction data from the API response.
        """
        self._data = data if isinstance(data, dict) else {}

    @property
    def id(self) -> Optional[str]:
        """Returns the internal transaction ID."""
        return self._data.get("id")

    @property
    def type(self) -> Optional[str]:
        """Returns the award type code for this transaction."""
        return self._data.get("type")

    @property
    def type_description(self) -> Optional[str]:
        """Returns the description of the award type."""
        return self._data.get("type_description")

    @property
    def action_date(self) -> Optional[str]:
        """Returns the transaction action date (YYYY-MM-DD)."""
        return self._data.get("action_date")

    @property
    def action_type(self) -> Optional[str]:
        """Returns the transaction action type code (e.g., 'A' for New)."""
        return self._data.get("action_type")

    @property
    def action_type_description(self) -> Optional[str]:
        """Returns the description of the action type."""
        return self._data.get("action_type_description")

    @property
    def modification_number(self) -> Optional[str]:
        """Returns the modification number for the transaction."""
        return self._data.get("modification_number")

    @property
    def description(self) -> Optional[str]:
        """Returns the transaction description."""
        return self._data.get("description")

    @property
    def federal_action_obligation(self) -> Optional[float]:
        """Returns the obligation amount (non-loan assistance/contracts)."""
        value = self._data.get("federal_action_obligation")
        try:
            return float(value) if value is not None else None
        except (ValueError, TypeError):
            return None # Or 0.0? Returning None seems more accurate

    @property
    def face_value_loan_guarantee(self) -> Optional[float]:
        """Returns the face value (loans only)."""
        value = self._data.get("face_value_loan_guarantee")
        try:
            return float(value) if value is not None else None
        except (ValueError, TypeError):
            return None

    @property
    def original_loan_subsidy_cost(self) -> Optional[float]:
        """Returns the original loan subsidy cost (loans only)."""
        value = self._data.get("original_loan_subsidy_cost")
        try:
            return float(value) if value is not None else None
        except (ValueError, TypeError):
            return None

    @property
    def cfda_number(self) -> Optional[str]:
        """Returns the CFDA number (assistance only)."""
        return self._data.get("cfda_number")

    def get(self, key: str, default: Optional[Any] = None) -> Any:
        """Provides dictionary-like get access to the raw data."""
        return self._data.get(key, default)

    @property
    def raw_data(self) -> Dict[str, Any]:
        """Returns the raw underlying dictionary data for this transaction."""
        return self._data

    def __repr__(self) -> str:
        """Provides a concise string representation of the transaction."""
        mod = self.modification_number or "Base"
        date = self.action_date or "?"
        amount = self.federal_action_obligation
        if amount is None: # Check loan fields if obligation is None
            amount = self.face_value_loan_guarantee
        amount_str = f"{amount:,.2f}" if amount is not None else "?"

        return f"<Transaction mod={mod} date={date} amount={amount_str}>"

class Award:
    """
    Represents a USAspending award, standardizing access to data and providing
    lazy-loading for related transactions.

    Access standardized fields via properties (e.g., award.description, award.recipient.name).
    Access transactions via award.transactions (fetches on first access).
    Access raw underlying data via award.raw_data or award.get('Original Field Name').
    """
    def __init__(self, data: Dict[str, Any], client: Optional['USASpendingClient'] = None):
        """
        Initializes the Award object.

        Args:
            data: A dictionary containing the award data from the API JSON response.
            client: An instance of USASpendingClient, required for lazy-loading related data.
        """
        self._data = data if isinstance(data, dict) else {}
        self._client = client # Store reference to client for lazy loading

        # Cache for lazy-loaded transactions
        self._transactions: Optional[List[Transaction]] = None

        # Pre-fetch potential nested objects from lookup response for efficiency
        self._pop_data = self._data.get('place_of_performance')
        self._recipient_data = self._data.get('recipient')
        self._perf_period_data = self._data.get('period_of_performance')
        
    # --- Standardized Properties ---

    @property
    def prime_award_id(self) -> str:
        """
        Returns the primary award identifier (PIID/FAIN/URI).
        Checks search result field 'Award ID' first, then lookup fields.
        """
        # Search: "Award ID"
        # Lookup: "piid" (Contracts/IDV), "fain" (Assistance), "uri" (Assistance)
        return str(self._data.get("Award ID") or \
               self._data.get("piid") or \
               self._data.get("fain") or \
               self._data.get("uri") or \
               "")

    @property
    def generated_unique_award_id(self) -> Optional[str]:
        """Returns the unique hash ID generated by USAspending (lookup endpoint only)."""
        # Lookup key: 'generated_unique_award_id'
        return self._data.get("generated_unique_award_id")

    @property
    def description(self) -> str:
        """Returns the award description."""
        # Lookup key: 'description'
        # Search key: 'Description'
        return str(self._data.get("description") or self._data.get("Description") or "")

    @property
    def total_obligations(self) -> float:
        """
        Returns the total obligated amount.
        Uses 'total_obligation' from lookup or 'Award Amount' from search.
        """
        # Lookup key: 'total_obligation'
        # Search key: 'Award Amount' (represents obligation for prime awards)
        lookup_val = self._data.get("total_obligation")
        search_val = self._data.get("Award Amount")
        value = lookup_val if lookup_val is not None else search_val
        try:
            # Ensure conversion to float, defaulting to 0.0 if None or invalid
            return float(value) if value is not None else 0.0
        except (ValueError, TypeError):
            return 0.0

    @property
    def total_outlay(self) -> float:
        """
        Returns the total outlayed amount.
        Uses 'total_account_outlay' from lookup or 'Total Outlays' from search.
        """
        # Lookup key: 'total_account_outlay'
        # Search key: 'Total Outlays'
        lookup_val = self._data.get("total_account_outlay")
        search_val = self._data.get("Total Outlays")
        value = lookup_val if lookup_val is not None else search_val
        try:
            # Ensure conversion to float, defaulting to 0.0 if None or invalid
            return float(value) if value is not None else 0.0
        except (ValueError, TypeError):
            return 0.0

    @property
    def period_of_performance(self) -> Optional[PeriodOfPerformance]:
        """
        Returns the period of performance details as a PeriodOfPerformance object.
        Constructs data from lookup ('period_of_performance' object) or search fields.
        """
        if self._perf_period_data and isinstance(self._perf_period_data, dict):
            # Data from lookup endpoint (nested object)
            return PeriodOfPerformance(self._perf_period_data)
        elif self._data.get("Start Date") or self._data.get("End Date") or self._data.get("Period of Performance Start Date"):
            # Data potentially from search endpoint (flat fields)
            search_perf_data = {
                "start_date": self._data.get("Start Date") or self._data.get("Period of Performance Start Date"),
                "end_date": self._data.get("End Date") or self._data.get("Period of Performance Current End Date"),
                "last_modified_date": self._data.get("Last Modified Date")
            }
            # Only return object if we found some relevant date info
            if any(v is not None for v in search_perf_data.values()):
                 return PeriodOfPerformance(search_perf_data)
        # Return None if no relevant data found from either source
        return None

    @property
    def place_of_performance(self) -> Optional[Location]:
        """
        Returns the primary place of performance details as a Location object.
        Constructs data from lookup ('place_of_performance' object) or search fields.
        Note: Lookup provides more detailed location info than search.
        """
        if self._pop_data and isinstance(self._pop_data, dict):
             # Data from lookup endpoint (nested object)
             return Location(self._pop_data)
        elif self._data.get("Place of Performance State Code") or self._data.get("Place of Performance Country Code"):
             # Data potentially from search endpoint (flat fields - less detail)
             search_loc_data = {
                 "state_code": self._data.get("Place of Performance State Code"),
                 "country_code": self._data.get("Place of Performance Country Code"),
                 "zip5": self._data.get("Place of Performance Zip5"),
                 # Search results lack city name, county, address etc. for PoP
             }
             # Only return object if we found some relevant location info
             if any(v is not None for v in search_loc_data.values()):
                 return Location(search_loc_data)
        # Return None if no relevant data found from either source
        return None

    @property
    def recipient(self) -> Optional[Recipient]:
        """
        Returns the recipient details as a Recipient object.
        Constructs data from lookup ('recipient' object) or search fields.
        Note: Lookup provides more detailed recipient info (parent, location, categories).
        """
        if self._recipient_data and isinstance(self._recipient_data, dict):
             # Data from lookup endpoint (nested object)
             return Recipient(self._recipient_data)
        elif self._data.get("Recipient Name") or self._data.get("Recipient DUNS Number"):
             # Data potentially from search endpoint (flat fields)
             search_recip_data = {
                 "recipient_name": self._data.get("Recipient Name"),
                 "recipient_unique_id": self._data.get("Recipient DUNS Number"), # Mapping DUNS
                 "recipient_id": self._data.get("recipient_id"), # Hash + level
                 # Search results lack parent info, business categories, recipient location etc.
             }
             # Only return object if we found some relevant recipient info
             if any(v is not None for v in search_recip_data.values()):
                 return Recipient(search_recip_data)
        # Return None if no relevant data found from either source
        return None

    @property
    def transactions(self) -> List[Transaction]:
        """
        Returns a list of Transaction objects associated with this award.

        Fetches all transactions from the API using pagination on first access
        and caches the result. Requires the Award object to have been
        initialized with a USASpendingClient instance.
        """
        # Check cache first
        if self._transactions is not None:
            return self._transactions

        # Check if client is available for fetching
        if not self._client:
            print("Warning: Cannot fetch transactions. Award object was not initialized with a client instance.")
            self._transactions = [] # Cache empty list to prevent re-attempts
            return self._transactions

        # Determine the best Award ID to use for the transaction endpoint
        # Prefer the generated ID from lookup, fall back to prime ID
        award_id_to_use = self.get('generated_internal_id') or self.generated_unique_award_id or self.prime_award_id
        if not award_id_to_use:
            print("Warning: Cannot fetch transactions. No suitable award ID found.")
            self._transactions = []
            return self._transactions

        print(f"Fetching transactions for Award ID: {award_id_to_use}...")
        all_transactions_data = []
        current_page = 1
        page_limit = 500 # Fetch in larger batches
        max_pages = 100 # Safety break to prevent potential infinite loops

        while current_page <= max_pages:
            response_data = self._client.get_transactions(
                award_id=award_id_to_use,
                page=current_page,
                limit=page_limit
            )

            if response_data is None:
                print(f"  Error fetching page {current_page}. Stopping transaction fetch.")
                # Decide whether to return partial results or empty list on error
                # Caching empty list for now to indicate failure
                self._transactions = []
                return self._transactions # Or potentially return partial results?

            results = response_data.get("results", [])
            all_transactions_data.extend(results)

            page_metadata = response_data.get("page_metadata", {})
            has_next = page_metadata.get("hasNext", False)
            next_page = page_metadata.get("next") # Use 'next' if available

            if not has_next or next_page is None:
                break # Exit loop if no more pages

            # Use the 'next' page number provided by the API
            current_page = next_page
            # Optional: Add a small delay between pages if needed
            # time.sleep(0.1)

        if current_page > max_pages:
             print(f"Warning: Reached maximum page limit ({max_pages}) while fetching transactions. Results may be incomplete.")

        # Convert raw data to Transaction objects and cache
        self._transactions = [Transaction(data) for data in all_transactions_data]
        return self._transactions

    # --- Direct Access Methods ---

    def get(self, key: str, default: Optional[Any] = None) -> Any:
        """
        Provides dictionary-like get access to the raw underlying data.
        Useful for accessing fields not covered by standardized properties
        or fields with spaces in their names (e.g., award.get('Awarding Agency')).
        """
        return self._data.get(key, default)

    @property
    def raw_data(self) -> Dict[str, Any]:
        """Returns the raw underlying dictionary data used to initialize this award."""
        return self._data

    # --- Representation ---

    def __repr__(self) -> str:
        """Provides a concise string representation of the Award object."""
        # Use standardized properties for consistent display
        display_id = self.prime_award_id or self.generated_unique_award_id or 'N/A'
        recipient_obj = self.recipient
        recipient_name = recipient_obj.name if recipient_obj else self.get('Recipient Name', 'N/A')
        return f"<Award id='{display_id}' recipient='{recipient_name}'>"

# --- USASpendingClient Class (No changes needed here for this request) ---

class USASpendingClient:
    """
    A client class to interact with the USAspending.gov API v2.
    Provides methods to fetch award details via lookup and search endpoints.
    """
    # --- Award Type Code Constants (Grouped by API constraints) ---
    CONTRACT_CODES: List[str] = ["A", "B", "C", "D"]
    IDV_CODES: List[str] = ["IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C", "IDV_C", "IDV_D", "IDV_E"]
    GRANT_CODES: List[str] = ["02", "03", "04", "05"]
    LOAN_CODES: List[str] = ["07", "08"]
    DIRECT_PAYMENT_CODES: List[str] = ["06", "10"]
    INSURANCE_OTHER_FA_CODES: List[str] = ["09", "11", "-1"]

    # --- Base Requestable Fields (Common across most types) ---
    _BASE_SEARCH_FIELDS = [
        "Award ID", "Recipient Name", "Recipient DUNS Number", "recipient_id",
        "Awarding Agency", "Awarding Agency Code", "Awarding Sub Agency", "Awarding Sub Agency Code",
        "Funding Agency", "Funding Agency Code", "Funding Sub Agency", "Funding Sub Agency Code",
        "Place of Performance City Code", "Place of Performance State Code",
        "Place of Performance Country Code", "Place of Performance Zip5",
        "Description", "Last Modified Date", "Base Obligation Date",
        "prime_award_recipient_id", "generated_internal_id", "def_codes",
        "COVID-19 Obligations", "COVID-19 Outlays",
        "Infrastructure Obligations", "Infrastructure Outlays"
    ]

    # --- Group-Specific Requestable Fields ---
    CONTRACT_SEARCH_FIELDS: List[str] = sorted(list(set(
        _BASE_SEARCH_FIELDS + ["Start Date", "End Date", "Award Amount", "Total Outlays", "Contract Award Type"]
    )))
    IDV_SEARCH_FIELDS: List[str] = sorted(list(set(
        _BASE_SEARCH_FIELDS + ["Start Date", "Award Amount", "Total Outlays", "Contract Award Type", "Last Date to Order"]
    )))
    # Non-Loan Assistance covers Grants, Direct Payments, Insurance/Other
    _NON_LOAN_ASSIST_FIELDS = ["Start Date", "End Date", "Award Amount", "Total Outlays", "Award Type", "SAI Number", "CFDA Number"]
    GRANT_SEARCH_FIELDS: List[str] = sorted(list(set(_BASE_SEARCH_FIELDS + _NON_LOAN_ASSIST_FIELDS)))
    DIRECT_PAYMENT_SEARCH_FIELDS: List[str] = sorted(list(set(_BASE_SEARCH_FIELDS + _NON_LOAN_ASSIST_FIELDS)))
    INSURANCE_OTHER_FA_SEARCH_FIELDS: List[str] = sorted(list(set(_BASE_SEARCH_FIELDS + _NON_LOAN_ASSIST_FIELDS)))
    # Loans have specific fields
    LOAN_SEARCH_FIELDS: List[str] = sorted(list(set(
        _BASE_SEARCH_FIELDS + ["Issued Date", "Loan Value", "Subsidy Cost", "SAI Number", "CFDA Number"]
    )))

    # --- Default Sort Fields per Group ---
    DEFAULT_SORT_FIELD = "Award Amount"
    LOAN_SORT_FIELD = "Loan Value" # Use Loan Value for sorting loans

    def __init__(self, award_lookup_base_url: str = AWARD_LOOKUP_BASE_URL, award_search_url: str = AWARD_SEARCH_URL, transactions_url: str = TRANSACTIONS_URL):
        """Initializes the USASpendingClient."""
        self.award_lookup_base_url = award_lookup_base_url
        self.award_search_url = award_search_url
        self.transactions_url = transactions_url # Store transactions endpoint URL
        self.default_headers = {"Content-Type": "application/json"}
        self._session = requests.Session()
        self._session.headers.update(self.default_headers)

    def get_transactions(self, award_id: str, page: int = 1, limit: int = 100) -> Optional[Dict[str, Any]]:
        """
        Fetches a single page of transactions for a given award ID.

        Args:
            award_id: The award ID (preferably generated_unique_award_id).
            page: The page number to retrieve.
            limit: The number of results per page.

        Returns:
            The parsed JSON response dictionary containing 'results' and 'page_metadata',
            or None if an error occurs.
        """
        if not award_id:
            print("Error: get_transactions requires an award_id.")
            return None

        payload = {
            "award_id": award_id,
            "page": page,
            "limit": limit,
            "sort": "action_date", # Default sort for transactions
            "order": "desc"
        }
        try:
            response = self._session.post(self.transactions_url, json=payload)
            # Check for API errors returned in JSON even with 200 status
            if response.status_code == 200 and 'application/json' in response.headers.get('Content-Type', ''):
                data = response.json()
                # Some API errors might still be in the JSON body with status 200
                if "message" in data or "detail" in data:
                     error_detail = data.get("message", data.get("detail", "Unknown API error in response body"))
                     print(f"API Error fetching transactions page {page} for {award_id} (Status {response.status_code}): {error_detail}")
                     return None # Indicate error
                return data # Return successful data
            # Handle non-200 or non-JSON responses
            else:
                print(f"Error fetching transactions page {page} for {award_id}: Status {response.status_code}")
                print(f"Response text: {response.text[:500]}...")
                # Optionally raise for non-200 errors if preferred
                # response.raise_for_status()
                return None

        except requests.exceptions.RequestException as e:
            print(f"Network/HTTP Error during transaction request: {e}")
            return None
        except json.JSONDecodeError as e:
            print(f"Error decoding JSON transaction response: {e}")
            return None
        except Exception as e:
            print(f"An unexpected error occurred fetching transactions: {e}")
            return None

    def award_search(self,
                     keywords: List[str],
                     award_type_codes: List[str],
                     fields: List[str],
                     sort: str, 
                     limit: int = 100,
                     page: int = 1) -> List[Award]:
        """
        Performs a search for awards based on keywords, type codes, specific fields, and sort order.
        **Note:** award_type_codes must only contain types from a single valid API group.

        Args:
            keywords: A list of keywords.
            award_type_codes: A list of award type codes from a single valid API group.
            fields: A list of specific fields to request for this award group.
            sort: The field to sort results by (must be valid for the award group).
            limit: The maximum number of results to return per page. Defaults to 100.
            page: The page number to retrieve. Defaults to 1.

        Returns:
            A list of Award objects matching the search criteria, or an empty list on error.
        """
        if not keywords: print("Warning: award_search called with empty keywords list."); return []
        if not award_type_codes: print("Error: award_search requires award_type_codes."); return []
        if not fields: print("Error: award_search requires fields list."); return []
        if not sort: print("Error: award_search requires sort field."); return []

        start_date = "2007-10-01"; end_date = "2025-09-30"
        payload = {
            "filters": {"keywords": keywords, "time_period": [{"start_date": start_date, "end_date": end_date}], "award_type_codes": award_type_codes},
            "fields": fields, # Use provided fields list
            "page": page, "limit": limit,
            "sort": sort, # Use provided sort field
            "order": "desc", # Keep descending order as default
            "subawards": False
        }
        try:
            response = self._session.post(self.award_search_url, json=payload)
            if 'application/json' in response.headers.get('Content-Type', ''):
                data = response.json()
                if response.status_code != 200 or "message" in data or "detail" in data:
                    error_detail = data.get("message", data.get("detail", f"Non-JSON response: {response.text[:200]}"))
                    print(f"API Error searching types {award_type_codes} (Status {response.status_code}, Sort: {sort}): {error_detail}")
                    return []
                results = data.get("results", [])
                return [Award(result_data, client=self) for result_data in results]
            else:
                print(f"Error: Unexpected content type from search: {response.headers.get('Content-Type')}")
                print(f"Response Status: {response.status_code}"); print(f"Response text: {response.text[:500]}...")
                response.raise_for_status(); return []
        except requests.exceptions.RequestException as e:
            print(f"Network/HTTP Error during API search request: {e}")
            if hasattr(e, 'response') and e.response is not None: print(f"API Response Status: {e.response.status_code}"); print(f"API Response Text: {e.response.text[:500]}...")
            return []
        except json.JSONDecodeError as e: print(f"Error decoding JSON search response: {e}"); return []
        except Exception as e: print(f"An unexpected error occurred during award search: {e}"); return []

    # --- Helper Search Methods (Pass specific fields and sort) ---

    def contract_search(self, keywords: List[str], limit: int = 100) -> List[Award]:
        """Searches specifically for Contracts based on keywords."""
        print(f"Searching contracts matching keywords: {keywords}")
        return self.award_search(keywords=keywords, award_type_codes=self.CONTRACT_CODES,
                                 fields=self.CONTRACT_SEARCH_FIELDS, sort=self.DEFAULT_SORT_FIELD, limit=limit)

    def idv_search(self, keywords: List[str], limit: int = 100) -> List[Award]:
        """Searches specifically for Indefinite Delivery Vehicles (IDVs) based on keywords."""
        print(f"Searching IDVs matching keywords: {keywords}")
        return self.award_search(keywords=keywords, award_type_codes=self.IDV_CODES,
                                 fields=self.IDV_SEARCH_FIELDS, sort=self.DEFAULT_SORT_FIELD, limit=limit)

    def grant_search(self, keywords: List[str], limit: int = 100) -> List[Award]:
        """Searches specifically for Grants (types 02-05) based on keywords."""
        print(f"Searching grants matching keywords: {keywords}")
        return self.award_search(keywords=keywords, award_type_codes=self.GRANT_CODES,
                                 fields=self.GRANT_SEARCH_FIELDS, sort=self.DEFAULT_SORT_FIELD, limit=limit)

    def loan_search(self, keywords: List[str], limit: int = 100) -> List[Award]:
        """Searches specifically for Loans (types 07-08) based on keywords."""
        print(f"Searching loans matching keywords: {keywords}")
        return self.award_search(keywords=keywords, award_type_codes=self.LOAN_CODES,
                                 fields=self.LOAN_SEARCH_FIELDS, sort=self.LOAN_SORT_FIELD, limit=limit) # Use LOAN_SORT_FIELD

    def direct_payment_search(self, keywords: List[str], limit: int = 100) -> List[Award]:
        """Searches specifically for Direct Payments (types 06, 10) based on keywords."""
        print(f"Searching direct payments (06, 10) matching keywords: {keywords}")
        return self.award_search(keywords=keywords, award_type_codes=self.DIRECT_PAYMENT_CODES,
                                 fields=self.DIRECT_PAYMENT_SEARCH_FIELDS, sort=self.DEFAULT_SORT_FIELD, limit=limit)

    def insurance_other_fa_search(self, keywords: List[str], limit: int = 100) -> List[Award]:
        """Searches specifically for Insurance and Other Financial Assistance (types 09, 11, -1)."""
        print(f"Searching insurance/other FA (09, 11, -1) matching keywords: {keywords}")
        return self.award_search(keywords=keywords, award_type_codes=self.INSURANCE_OTHER_FA_CODES,
                                 fields=self.INSURANCE_OTHER_FA_SEARCH_FIELDS, sort=self.DEFAULT_SORT_FIELD, limit=limit)

    # --- Combined Search (Multiple API Calls with specific fields/sort) ---

    def all_award_search(self, keywords: List[str], limit: int = 100) -> List[Award]:
        """
        Searches across all major award groups by making separate, optimized API calls
        for each group and combining the unique results.

        Args:
            keywords: A list of keywords to search for.
            limit: The maximum number of results to retrieve *for each award type group*.

        Returns:
            A combined list of unique Award objects found across the searches.
        """
        print(f"Searching all award types matching keywords: {keywords} (limit {limit} per type group)")
        combined_results_dict: Dict[str, Award] = {}

        # Define searches with their specific codes, fields, and sort parameters
        search_tasks = [
            {"name": "Contracts", "codes": self.CONTRACT_CODES, "fields": self.CONTRACT_SEARCH_FIELDS, "sort": self.DEFAULT_SORT_FIELD},
            {"name": "IDVs", "codes": self.IDV_CODES, "fields": self.IDV_SEARCH_FIELDS, "sort": self.DEFAULT_SORT_FIELD},
            {"name": "Grants", "codes": self.GRANT_CODES, "fields": self.GRANT_SEARCH_FIELDS, "sort": self.DEFAULT_SORT_FIELD},
            {"name": "Loans", "codes": self.LOAN_CODES, "fields": self.LOAN_SEARCH_FIELDS, "sort": self.LOAN_SORT_FIELD},
            {"name": "Direct Payments (06,10)", "codes": self.DIRECT_PAYMENT_CODES, "fields": self.DIRECT_PAYMENT_SEARCH_FIELDS, "sort": self.DEFAULT_SORT_FIELD},
            {"name": "Insurance/Other FA (09,11,-1)", "codes": self.INSURANCE_OTHER_FA_CODES, "fields": self.INSURANCE_OTHER_FA_SEARCH_FIELDS, "sort": self.DEFAULT_SORT_FIELD},
        ]

        for task in search_tasks:
            print(f" -> Searching {task['name']}...")
            results = self.award_search(
                keywords=keywords,
                award_type_codes=task["codes"],
                fields=task["fields"],
                sort=task["sort"],
                limit=limit
            )
            print(f"    Found {len(results)} {task['name'].lower()} results.")
            for award in results:
                award_id = award.prime_award_id
                if award_id and award_id not in combined_results_dict:
                    combined_results_dict[award_id] = award

        final_results = list(combined_results_dict.values())
        print(f"Total unique awards found across all types: {len(final_results)}")
        return final_results

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
                return Award(award_data, client=self)
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

