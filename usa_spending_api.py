import requests
import json
from typing import Any, Dict, Optional, List, Union # Added Union
from datetime import datetime # Added for potential date parsing if needed later
from utils import smart_sentence_case, contracts_titlecase
import time

# Configuration
AWARD_LOOKUP_BASE_URL = "https://api.usaspending.gov/api/v2/awards/"
AWARD_SEARCH_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
TRANSACTIONS_URL = "https://api.usaspending.gov/api/v2/transactions/"
DEFAULT_TIMEOUT = 30 # Seconds for API requests

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
    def city(self) -> Optional[str]:
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
    def state(self) -> Optional[str]:
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
        name = self._data.get("recipient_name") or self._data.get("Recipient Name")
        if name:
            name = contracts_titlecase(name)
        return name

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
        return self._data.get("generated_unique_award_id") or self._data.get("generated_internal_id")

    @property
    def description(self) -> str:
        """Returns the award description."""
        # Lookup key: 'description'
        # Search key: 'Description'
        desc = str(self._data.get("description") or self._data.get("Description") or "")
        return smart_sentence_case(desc)

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
    def potential_value(self) -> float:
        """
        Returns the potential total value of the award.
        """
        value = self._data.get("Award Amount") or self._data.get("Loan Amount") or self.total_obligations
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
    def usa_spending_url(self) -> str:
        """
        Returns the URL for the award details page on USAspending.gov.
        Constructs the URL based on the award internal ID.
        """
        return f"https://www.usaspending.gov/award/{self.generated_unique_award_id}/"

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

    Provides methods to fetch award details via lookup (/awards/{id}) and
    search (/search/spending_by_award) endpoints, as well as related
    transaction data (/transactions/). It uses a central helper method
    for making API requests and handling common errors.
    """
    # API Results Limit per request   
    RESULTS_LIMIT = 100

    
    # --- Award Type Code Constants (Grouped by API constraints) ---
    # These lists define valid groups of award_type_codes for API filtering.
    # The API generally requires filtering by only one group per search request.
    CONTRACT_CODES: List[str] = ["A", "B", "C", "D"] # BPA Call, PO, Delivery Order, Definitive Contract
    IDV_CODES: List[str] = ["IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C", "IDV_C", "IDV_D", "IDV_E"] # GWAC, IDC, FSS, BOA, BPA IDV
    GRANT_CODES: List[str] = ["02", "03", "04", "05"] # Block, Formula, Project Grant, Coop Agreement
    LOAN_CODES: List[str] = ["07", "08"] # Direct Loan, Guaranteed/Insured Loan
    DIRECT_PAYMENT_CODES: List[str] = ["06", "10"] # Direct Payment for Specified Use, Unrestricted Use
    INSURANCE_OTHER_FA_CODES: List[str] = ["09", "11", "-1"] # Insurance, Other Financial Assistance, Not Specified

    # --- Base Requestable Fields (Common across most award search types) ---
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

    # --- Group-Specific Requestable Fields Lists ---
    # Combines base fields with fields specific to each award group,
    # ensuring only valid fields are requested for each search type.
    _CONTRACT_FIELDS = ["Start Date", "End Date", "Award Amount", "Total Outlays", "Contract Award Type"]
    CONTRACT_SEARCH_FIELDS: List[str] = sorted(list(set(_BASE_SEARCH_FIELDS + _CONTRACT_FIELDS)))

    _IDV_FIELDS = ["Start Date", "Award Amount", "Total Outlays", "Contract Award Type", "Last Date to Order"]
    IDV_SEARCH_FIELDS: List[str] = sorted(list(set(_BASE_SEARCH_FIELDS + _IDV_FIELDS)))

    # Non-Loan Assistance fields apply to Grants, Direct Payments, Insurance/Other FA
    _NON_LOAN_ASSIST_FIELDS = ["Start Date", "End Date", "Award Amount", "Total Outlays", "Award Type", "SAI Number", "CFDA Number"]
    GRANT_SEARCH_FIELDS: List[str] = sorted(list(set(_BASE_SEARCH_FIELDS + _NON_LOAN_ASSIST_FIELDS)))
    DIRECT_PAYMENT_SEARCH_FIELDS: List[str] = sorted(list(set(_BASE_SEARCH_FIELDS + _NON_LOAN_ASSIST_FIELDS)))
    INSURANCE_OTHER_FA_SEARCH_FIELDS: List[str] = sorted(list(set(_BASE_SEARCH_FIELDS + _NON_LOAN_ASSIST_FIELDS)))

    # Loans have unique fields related to loan values
    _LOAN_FIELDS = ["Issued Date", "Loan Value", "Subsidy Cost", "SAI Number", "CFDA Number"]
    LOAN_SEARCH_FIELDS: List[str] = sorted(list(set(_BASE_SEARCH_FIELDS + _LOAN_FIELDS)))

    # --- Default Sort Fields per Group ---
    # Defines the default field used for sorting search results.
    DEFAULT_SORT_FIELD = "Award Amount" # Used for most types
    LOAN_SORT_FIELD = "Loan Value"      # Specific sort field required for loans

    def __init__(self, award_lookup_base_url: str = AWARD_LOOKUP_BASE_URL, award_search_url: str = AWARD_SEARCH_URL, transactions_url: str = TRANSACTIONS_URL):
        """
        Initializes the USASpendingClient.

        Args:
            award_lookup_base_url: Base URL for the award lookup endpoint.
            award_search_url: URL for the award search endpoint.
            transactions_url: URL for the transactions endpoint.
        """
        self.award_lookup_base_url = award_lookup_base_url
        self.award_search_url = award_search_url
        self.transactions_url = transactions_url
        self.default_headers = {"Content-Type": "application/json"}
        # Use a requests.Session object to persist headers and potentially cookies,
        # and enable connection pooling for performance.
        self._session = requests.Session()
        self._session.headers.update(self.default_headers)

    # --- Central API Request Helper ---
    def _make_api_request(self,
                          method: str,
                          url: str,
                          params: Optional[Dict[str, Any]] = None,
                          json_payload: Optional[Dict[str, Any]] = None,
                          caller_info: str = "Unknown") -> Optional[Dict[str, Any]]:
        """
        Internal helper method to make API requests and handle common errors.

        This centralizes request logic (GET/POST), timeout handling,
        response status checking, JSON decoding, and error logging.

        Args:
            method: HTTP method ('GET', 'POST', etc.).
            url: The target API endpoint URL.
            params: Dictionary of query parameters for GET requests.
            json_payload: Dictionary payload for POST/PUT requests.
            caller_info: String identifying the calling method for logging purposes.

        Returns:
            Parsed JSON dictionary on success (status 2xx, valid JSON, no API error msg),
            None on any failure (network error, timeout, HTTP error, JSON decode error,
            API error message within JSON response).
        """
        try:
            # Make the request using the session object
            response = self._session.request(
                method=method,
                url=url,
                params=params,
                json=json_payload,
                timeout=DEFAULT_TIMEOUT
            )

            # Check content type for potential non-JSON error pages
            content_type = response.headers.get('Content-Type', '')
            is_json = 'application/json' in content_type

            # Check for successful HTTP status code (2xx) and JSON content
            if response.ok and is_json:
                try:
                    data = response.json()
                    # Check for API-level errors embedded in the JSON response body
                    # (sometimes API returns 200 OK but includes an error message)
                    if "message" in data or "detail" in data:
                        error_detail = data.get("message", data.get("detail", "Unknown API error in response body"))
                        print(f"API Error ({caller_info}, Status {response.status_code}): {error_detail}")
                        return None # Treat as failure
                    return data # Successful response with valid JSON
                except json.JSONDecodeError as e:
                    # Handle cases where response claims to be JSON but isn't valid
                    print(f"JSON Decode Error ({caller_info}, Status {response.status_code}): {e}")
                    print(f"Response text: {response.text[:500]}...")
                    return None
            # Handle unsuccessful HTTP status codes (4xx, 5xx) or non-JSON responses
            else:
                error_snippet = response.text[:500] if not is_json else "Check logs for JSON error details."
                print(f"HTTP Error ({caller_info}): Status {response.status_code}, Content-Type: {content_type}")
                print(f"Response text snippet: {error_snippet}...")
                # Consider raising response.raise_for_status() here if specific error handling is needed
                return None

        except requests.exceptions.Timeout:
            # Handle request timeout
            print(f"Request Timeout ({caller_info}): Request to {url} exceeded {DEFAULT_TIMEOUT} seconds.")
            return None
        except requests.exceptions.RequestException as e:
            # Handle other network/request errors (connection, redirects, etc.)
            print(f"Network/Request Error ({caller_info}): {e}")
            # Log response details if available in the exception
            if hasattr(e, 'response') and e.response is not None:
                 print(f"  Response Status: {e.response.status_code}")
                 print(f"  Response Text: {e.response.text[:500]}...")
            return None
        except Exception as e:
            # Catch any other unexpected errors during the request
            print(f"An unexpected error occurred during API request ({caller_info}): {e}")
            return None

    # --- Refactored Public Methods ---

    def get_transactions(self, award_id: str, page: int = 1, limit: int = 100) -> Optional[Dict[str, Any]]:
        """
        Fetches a single page of transactions for a given award ID.
        Uses the central _make_api_request helper for the API call.

        Args:
            award_id: The award ID (preferably generated_unique_award_id or generated_internal_id).
            page: The page number to retrieve.
            limit: The number of results per page (max 5000 according to docs).

        Returns:
            The parsed JSON response dictionary containing 'results' and 'page_metadata',
            or None if an error occurs during the API request.
        """
        if not award_id: print("Error: get_transactions requires an award_id."); return None
        # Prepare payload for the POST request
        payload = {"award_id": award_id, "page": page, "limit": limit, "sort": "action_date", "order": "desc"}
        caller_info = f"get_transactions(award={award_id}, page={page})"

        # Call the helper method
        return self._make_api_request(
            method="POST",
            url=self.transactions_url,
            json_payload=payload,
            caller_info=caller_info
        )

    def award_search(self,
                     keywords: List[str],
                     award_type_codes: List[str],
                     fields: List[str],
                     sort: str,
                     limit: int = 100,
                     page: int = 1) -> List[Award]:
        """
        Performs a search for awards, returning a list of Award objects.
        Uses the central _make_api_request helper for the API call.
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
        # Input validation
        if not keywords: print("Warning: award_search called with empty keywords list."); return []
        if not award_type_codes: print("Error: award_search requires award_type_codes."); return []
        if not fields: print("Error: award_search requires fields list."); return []
        if not sort: print("Error: award_search requires sort field."); return []

        # Define default time period filter
        start_date = "2007-10-01"; end_date = "2025-09-30" # Covers most data

        # Construct the payload for the POST request
        payload = {
            "filters": {"keywords": keywords, "time_period": [{"start_date": start_date, "end_date": end_date}], "award_type_codes": award_type_codes},
            "fields": fields, "page": page, "limit": limit, "sort": sort, "order": "desc", "subawards": False
        }
        caller_info = f"award_search(types={award_type_codes}, sort={sort}, page={page})"

        # Call the helper method
        data = self._make_api_request(
            method="POST",
            url=self.award_search_url,
            json_payload=payload,
            caller_info=caller_info
        )

        # Process the results if the call was successful
        if data:
            results = data.get("results", [])
            # Convert results into Award objects, passing the client instance for lazy loading
            return [Award(result_data, client=self) for result_data in results]
        else:
            # Return empty list if the API request failed
            return []

    def award_lookup(self, usa_spending_award_id: str) -> Optional[Award]:
        """
        Fetches award details for a specific USAspending award ID.
        Uses the central _make_api_request helper for the API call.

        Args:
            usa_spending_award_id: The unique award identifier (e.g., generated_unique_award_id).

        Returns:
            An Award object containing the fetched data, or None if an error occurs.
        """
        if not usa_spending_award_id: print("Error: usa_spending_award_id cannot be empty."); return None
        # Construct the specific endpoint URL for the lookup
        endpoint = f"{self.award_lookup_base_url}{usa_spending_award_id}"
        caller_info = f"award_lookup(id={usa_spending_award_id})"

        # Call the helper method using GET
        data = self._make_api_request(
            method="GET",
            url=endpoint,
            caller_info=caller_info
            # No params or payload needed for this GET request
        )

        # Process the result if the call was successful
        if data:
            # Create Award object, passing the client instance for lazy loading
            return Award(data, client=self)
        else:
            # Return None if the API request failed
            return None

    # --- Helper Search Methods (Call refactored award_search) ---
    # These provide convenient shortcuts for searching specific award groups.
    # They now simply pass the correct parameters (codes, fields, sort) to award_search.

    def contract_search(self, keywords: List[str], limit: int = 100) -> List[Award]:
        """Searches specifically for Contracts based on keywords."""
        return self.award_search(keywords=keywords, award_type_codes=self.CONTRACT_CODES, fields=self.CONTRACT_SEARCH_FIELDS, sort=self.DEFAULT_SORT_FIELD, limit=limit)

    def idv_search(self, keywords: List[str], limit: int = 100) -> List[Award]:
        """Searches specifically for Indefinite Delivery Vehicles (IDVs) based on keywords."""
        return self.award_search(keywords=keywords, award_type_codes=self.IDV_CODES, fields=self.IDV_SEARCH_FIELDS, sort=self.DEFAULT_SORT_FIELD, limit=limit)

    def grant_search(self, keywords: List[str], limit: int = 100) -> List[Award]:
        """Searches specifically for Grants (types 02-05) based on keywords."""
        return self.award_search(keywords=keywords, award_type_codes=self.GRANT_CODES, fields=self.GRANT_SEARCH_FIELDS, sort=self.DEFAULT_SORT_FIELD, limit=limit)

    def loan_search(self, keywords: List[str], limit: int = 100) -> List[Award]:
        """Searches specifically for Loans (types 07-08) based on keywords."""
        return self.award_search(keywords=keywords, award_type_codes=self.LOAN_CODES, fields=self.LOAN_SEARCH_FIELDS, sort=self.LOAN_SORT_FIELD, limit=limit)

    def direct_payment_search(self, keywords: List[str], limit: int = 100) -> List[Award]:
        """Searches specifically for Direct Payments (types 06, 10) based on keywords."""
        return self.award_search(keywords=keywords, award_type_codes=self.DIRECT_PAYMENT_CODES, fields=self.DIRECT_PAYMENT_SEARCH_FIELDS, sort=self.DEFAULT_SORT_FIELD, limit=limit)

    def insurance_other_fa_search(self, keywords: List[str], limit: int = 100) -> List[Award]:
        """Searches specifically for Insurance and Other Financial Assistance (types 09, 11, -1)."""
        return self.award_search(keywords=keywords, award_type_codes=self.INSURANCE_OTHER_FA_CODES, fields=self.INSURANCE_OTHER_FA_SEARCH_FIELDS, sort=self.DEFAULT_SORT_FIELD, limit=limit)

    # --- Combined Search (Calls refactored award_search multiple times) ---
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
        # Use a dictionary to store results, keyed by award ID to handle duplicates
        combined_results_dict: Dict[str, Award] = {}

        # Define the parameters for each search task
        search_tasks = [
            {"name": "Contracts", "codes": self.CONTRACT_CODES, "fields": self.CONTRACT_SEARCH_FIELDS, "sort": self.DEFAULT_SORT_FIELD},
            {"name": "IDVs", "codes": self.IDV_CODES, "fields": self.IDV_SEARCH_FIELDS, "sort": self.DEFAULT_SORT_FIELD},
            {"name": "Grants", "codes": self.GRANT_CODES, "fields": self.GRANT_SEARCH_FIELDS, "sort": self.DEFAULT_SORT_FIELD},
            {"name": "Loans", "codes": self.LOAN_CODES, "fields": self.LOAN_SEARCH_FIELDS, "sort": self.LOAN_SORT_FIELD},
            {"name": "Direct Payments (06,10)", "codes": self.DIRECT_PAYMENT_CODES, "fields": self.DIRECT_PAYMENT_SEARCH_FIELDS, "sort": self.DEFAULT_SORT_FIELD},
            {"name": "Insurance/Other FA (09,11,-1)", "codes": self.INSURANCE_OTHER_FA_CODES, "fields": self.INSURANCE_OTHER_FA_SEARCH_FIELDS, "sort": self.DEFAULT_SORT_FIELD},
        ]

        # Execute each search task
        for task in search_tasks:
            print(f" -> Searching {task['name']}...")
            # Call the refactored award_search method
            results = self.award_search(
                keywords=keywords,
                award_type_codes=task["codes"],
                fields=task["fields"],
                sort=task["sort"],
                limit=limit
            )
            print(f"    Found {len(results)} {task['name'].lower()} results.")
            # Add unique results to the combined dictionary
            for award in results:
                award_id = award.prime_award_id # Use property to get consistent ID
                if award_id and award_id not in combined_results_dict:
                    combined_results_dict[award_id] = award

        # Convert the dictionary values (unique Award objects) back to a list
        final_results = list(combined_results_dict.values())
        print(f"Total unique awards found across all types: {len(final_results)}")
        return final_results

