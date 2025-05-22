import unittest
from unittest.mock import MagicMock, patch
import pandas as pd # Though not explicitly listed, Award might use it.
from datetime import date # For PeriodOfPerformance

# Assume the data classes are in 'usa_spending_api.py'
# from usa_spending_api import Location, PeriodOfPerformance, Recipient, Transaction, Award, USASpendingClient
# For now, let's define minimal placeholders to get started if direct import is an issue
# These will be replaced by actual imports once the real file structure is clear or if they are simple enough.

class Location:
    def __init__(self, data, api_type='lookup'): # api_type can be 'lookup' or 'search'
        self.raw_data = data
        self._api_type = api_type

        # Initialize all common attributes to None
        self.address_line1 = None
        self.address_line2 = None
        self.city_name = None
        self.state_code = None
        self.zip_code = None
        self.country_code = None

        if api_type == 'lookup':
            self.address_line1 = data.get('address_line1')
            self.address_line2 = data.get('address_line2')
            self.city_name = data.get('city_name')
            self.state_code = data.get('state_code')
            self.zip_code = data.get('zip_code')
            self.country_code = data.get('country_code')
        else: # search
            self.address_line1 = data.get('pop_address_line1') or data.get('recipient_address_line1')
            self.address_line2 = data.get('pop_address_line2') or data.get('recipient_address_line2')
            self.city_name = data.get('pop_city_name') or data.get('recipient_city_name')
            self.state_code = data.get('pop_state_code') or data.get('recipient_state_code')
            self.zip_code = data.get('pop_zip_code') or data.get('recipient_zip_code')
            self.country_code = data.get('pop_country_code') or data.get('recipient_country_code')

    @property
    def formatted_address(self) -> str:
        parts = [self.address_line1, self.address_line2, self.city_name, self.state_code, self.zip_code]
        return ", ".join(filter(None, parts))

    def __repr__(self):
        return f"<Location {self.formatted_address}>"

class PeriodOfPerformance:
    def __init__(self, data):
        self.raw_data = data
        self.start_date = data.get('period_of_performance_start_date')
        self.end_date = data.get('period_of_performance_end_date')
        self.potential_end_date = data.get('period_of_performance_potential_end_date')

    def __repr__(self):
        return f"<PeriodOfPerformance {self.start_date} to {self.end_date}>"

class Recipient:
    def __init__(self, data, api_type='lookup'):
        self.raw_data = data
        self._api_type = api_type
        self._location_obj = None # Initialize _location_obj
        if api_type == 'lookup':
            self.name = data.get('recipient_name')
            self.uei = data.get('recipient_uei')
            self.parent_uei = data.get('parent_uei')
            self.location_data = data.get('location') # Nested Location data
            self._location_obj = None
        else: # search
            self.name = data.get('recipient_name')
            self.uei = data.get('recipient_uei')
            # Location fields are flat in search results for recipient
            # self.location_data handled by Award class by passing relevant flat fields

    @property
    def location(self):
        if self._api_type == 'lookup' and self.location_data and not self._location_obj:
            self._location_obj = Location(self.location_data, api_type='lookup')
        # For 'search' type, location is typically constructed by the Award class
        return self._location_obj


    def __repr__(self):
        return f"<Recipient {self.name} ({self.uei})>"

class Transaction:
    def __init__(self, data):
        self.raw_data = data
        self.id = data.get('id')
        self.type = data.get('type')
        self.description = data.get('description')
        self.action_date = data.get('action_date')
        self.federal_action_obligation = data.get('federal_action_obligation')

    def __repr__(self):
        return f"<Transaction {self.id}>"

class Award:
    def __init__(self, data, client=None, api_type='search'): # Default to search for Award init
        self.raw_data = data
        self._client = client
        self._api_type = api_type # 'search' (flat) or 'lookup' (potentially nested)

        # Direct properties
        self.prime_award_id = data.get('piid') or data.get('fain') # From search
        if not self.prime_award_id and 'contract_award_unique_key' in data : # from /api/v2/awards/
             self.prime_award_id = data.get('contract_data',{}).get('piid')

        self.generated_unique_award_id = data.get('generated_unique_award_id')
        self.description = data.get('description')
        self.total_obligations = data.get('total_obligations') or data.get('total_obligation_amount') # search vs lookup
        self.total_outlay = data.get('total_outlays') # search
        self.potential_value = data.get('total_potential_value') # search, might be base_and_all_options_value in lookup

        self._period_of_performance = None
        self._place_of_performance = None
        self._recipient = None
        self._transactions = None # List of Transaction objects

        if api_type == 'lookup': # Nested data from /api/v2/awards/{id}
            if 'period_of_performance' in data:
                self._period_of_performance = PeriodOfPerformance(data['period_of_performance'])
            if 'place_of_performance' in data:
                self._place_of_performance = Location(data['place_of_performance'], api_type='lookup')
            if 'recipient' in data:
                self._recipient = Recipient(data['recipient'], api_type='lookup')
        else: # Flat data from /api/v2/search/spending_by_award/
             # PoP and Recipient need to be constructible from flat fields
             pass # Lazy loading will handle these or direct construction if all fields present

    @property
    def usa_spending_url(self) -> str:
        if self.generated_unique_award_id:
            return f"https://www.usaspending.gov/award/{self.generated_unique_award_id}/"
        return ""
        
    @property
    def period_of_performance(self):
        if self._period_of_performance is None and self._api_type == 'search':
            # Construct from flat fields if available
            if 'period_of_performance_start_date' in self.raw_data:
                 self._period_of_performance = PeriodOfPerformance(self.raw_data)
        # No client-based lazy loading for PoP in this example, assuming it's always in award details
        return self._period_of_performance

    @property
    def place_of_performance(self):
        if self._place_of_performance is None:
            # 1. Check if already present in raw_data (e.g. from previous fetch or initial lookup data)
            if 'place_of_performance' in self.raw_data and isinstance(self.raw_data['place_of_performance'], dict):
                self._place_of_performance = Location(self.raw_data['place_of_performance'], api_type='lookup')
            # 2. Try to construct from flat 'search' data if applicable
            elif self._api_type == 'search' and ('pop_city_name' in self.raw_data or 'pop_country_code' in self.raw_data):
                # Construct from flat pop_* fields
                flat_pop_data = {key: val for key, val in self.raw_data.items() if key.startswith('pop_')}
                mapped_data = {
                    'pop_address_line1': flat_pop_data.get('pop_address_line1'),
                    'pop_city_name': flat_pop_data.get('pop_city_name'),
                    'pop_state_code': flat_pop_data.get('pop_state_code'),
                    'pop_zip_code': flat_pop_data.get('pop_zip_code'),
                    'pop_country_code': flat_pop_data.get('pop_country_code')
                }
                self._place_of_performance = Location(mapped_data, api_type='search')
            # 3. Lazy load if client available and not constructed by other means
            elif self._client and self.generated_unique_award_id:
                detailed_data = self._client.get_raw_award_details(self.generated_unique_award_id)
                if detailed_data: # Check if data was actually returned
                    self.raw_data.update(detailed_data) # Merge details first
                    if 'place_of_performance' in detailed_data and isinstance(detailed_data['place_of_performance'], dict):
                        self._place_of_performance = Location(detailed_data['place_of_performance'], api_type='lookup')
        return self._place_of_performance

    @property
    def recipient(self):
        if self._recipient is None:
            # 1. Check if already present in raw_data
            if 'recipient' in self.raw_data and isinstance(self.raw_data['recipient'], dict):
                self._recipient = Recipient(self.raw_data['recipient'], api_type='lookup')
            # 2. Try to construct from flat 'search' data
            elif self._api_type == 'search' and 'recipient_name' in self.raw_data:
                flat_recipient_data = {key: val for key, val in self.raw_data.items() if key.startswith('recipient_')}
                mapped_data = {
                    'recipient_name': flat_recipient_data.get('recipient_name'),
                    'recipient_uei': flat_recipient_data.get('recipient_uei'),
                    'recipient_address_line1': flat_recipient_data.get('recipient_address_line1'),
                    'recipient_city_name': flat_recipient_data.get('recipient_city_name'),
                    'recipient_state_code': flat_recipient_data.get('recipient_state_code'),
                    'recipient_zip_code': flat_recipient_data.get('recipient_zip_code'),
                    'recipient_country_code': flat_recipient_data.get('recipient_country_code'),
                }
                self._recipient = Recipient(mapped_data, api_type='search')
                if mapped_data.get('recipient_city_name') or mapped_data.get('recipient_country_code'):
                    self._recipient._location_obj = Location(mapped_data, api_type='search')
            # 3. Lazy load
            elif self._client and self.generated_unique_award_id:
                detailed_data = self._client.get_raw_award_details(self.generated_unique_award_id)
                if detailed_data: # Check if data was actually returned
                    self.raw_data.update(detailed_data) # Merge details first
                    if 'recipient' in detailed_data and isinstance(detailed_data['recipient'], dict):
                        self._recipient = Recipient(detailed_data['recipient'], api_type='lookup')
        return self._recipient

    @property
    def transactions(self):
        if self._transactions is None: # Only proceed if not already populated
            if 'transactions' in self.raw_data and isinstance(self.raw_data['transactions'], list):
                 # Prioritize pre-loaded raw data
                self._transactions = [Transaction(data) for data in self.raw_data['transactions']]
            elif self._client and self.generated_unique_award_id:
                # Lazy load if client available and no pre-loaded data
                # Note: This assumes get_transactions returns a list of dicts
                # and each dict can initialize a Transaction object.
                transaction_data_list = self._client.get_transactions(self.generated_unique_award_id)
                self._transactions = [Transaction(data) for data in transaction_data_list] if transaction_data_list else []
            else:
                # No client and no pre-loaded data
                self._transactions = []
        return self._transactions

    def get(self, key, default=None):
        return self.raw_data.get(key, default)

    def __repr__(self):
        return f"<Award {self.prime_award_id or self.generated_unique_award_id}>"


# Mock USASpendingClient for Award tests
class USASpendingClient:
    def __init__(self, api_key="TEST_CLIENT_KEY"):
        self.api_key = api_key

    def get_raw_award_details(self, award_id):
        # To be mocked in tests
        pass

    def get_transactions(self, award_id):
        # To be mocked in tests
        pass


class TestLocation(unittest.TestCase):
    def setUp(self):
        self.lookup_data = {
            "address_line1": "123 Main St",
            "address_line2": "Suite 100",
            "address_line3": "Building A", # Ignored by current Location class
            "city_name": "Anytown",
            "state_code": "CA",
            "zip_code": "90210",
            "country_code": "USA"
        }
        self.search_data_pop = { # Place of Performance context
            "pop_address_line1": "456 Oak Ave",
            "pop_city_name": "Otherville",
            "pop_state_code": "NY",
            "pop_zip_code": "10001",
            "pop_country_code": "USA"
        }
        self.search_data_recipient = { # Recipient address context
            "recipient_address_line1": "789 Pine Ln",
            "recipient_city_name": "Smalltown",
            "recipient_state_code": "TX",
            "recipient_zip_code": "75001",
            "recipient_country_code": "USA"
        }

    def test_init_lookup(self):
        location = Location(self.lookup_data, api_type='lookup')
        self.assertEqual(location.address_line1, "123 Main St")
        self.assertEqual(location.address_line2, "Suite 100")
        self.assertEqual(location.city_name, "Anytown")
        self.assertEqual(location.state_code, "CA")
        self.assertEqual(location.zip_code, "90210")
        self.assertEqual(location.country_code, "USA")
        self.assertEqual(location.raw_data, self.lookup_data)
        self.assertEqual(location._api_type, 'lookup')

    def test_init_search_pop(self):
        location = Location(self.search_data_pop, api_type='search')
        self.assertEqual(location.address_line1, "456 Oak Ave")
        self.assertIsNone(location.address_line2) # Not present in this mock
        self.assertEqual(location.city_name, "Otherville")
        self.assertEqual(location.state_code, "NY")
        self.assertEqual(location.zip_code, "10001")
        self.assertEqual(location.country_code, "USA")
        self.assertEqual(location.raw_data, self.search_data_pop)
        self.assertEqual(location._api_type, 'search')

    def test_init_search_recipient(self):
        location = Location(self.search_data_recipient, api_type='search')
        self.assertEqual(location.address_line1, "789 Pine Ln")
        self.assertEqual(location.city_name, "Smalltown")
        self.assertEqual(location.state_code, "TX")
        self.assertEqual(location.zip_code, "75001")
        self.assertEqual(location.country_code, "USA")
        self.assertEqual(location.raw_data, self.search_data_recipient)

    def test_formatted_address_lookup(self):
        location = Location(self.lookup_data, api_type='lookup')
        expected = "123 Main St, Suite 100, Anytown, CA, 90210"
        self.assertEqual(location.formatted_address, expected)

    def test_formatted_address_search_pop(self):
        location = Location(self.search_data_pop, api_type='search')
        expected = "456 Oak Ave, Otherville, NY, 10001"
        self.assertEqual(location.formatted_address, expected)
        
    def test_formatted_address_minimal(self):
        minimal_data = {"city_name": "JustCity", "country_code": "CAN"}
        location = Location(minimal_data, api_type='lookup')
        self.assertEqual(location.formatted_address, "JustCity") # Only city, as state/zip missing for USA format
        
        minimal_data_us = {"city_name": "JustCity", "state_code": "NV", "country_code": "USA"}
        location_us = Location(minimal_data_us, api_type='lookup')
        self.assertEqual(location_us.formatted_address, "JustCity, NV")


    def test_repr(self):
        location = Location(self.lookup_data, api_type='lookup')
        self.assertTrue(isinstance(repr(location), str))
        self.assertIn("123 Main St", repr(location))
        self.assertIn("<Location", repr(location))

class TestPeriodOfPerformance(unittest.TestCase):
    def setUp(self):
        self.full_data = {
            "period_of_performance_start_date": "2020-01-01",
            "period_of_performance_end_date": "2021-12-31",
            "period_of_performance_potential_end_date": "2022-12-31"
        }
        self.minimal_data = {
            "period_of_performance_start_date": "2023-03-15"
        }
        self.empty_data = {}

    def test_init_full_data(self):
        pop = PeriodOfPerformance(self.full_data)
        self.assertEqual(pop.start_date, "2020-01-01")
        self.assertEqual(pop.end_date, "2021-12-31")
        self.assertEqual(pop.potential_end_date, "2022-12-31")
        self.assertEqual(pop.raw_data, self.full_data)

    def test_init_minimal_data(self):
        pop = PeriodOfPerformance(self.minimal_data)
        self.assertEqual(pop.start_date, "2023-03-15")
        self.assertIsNone(pop.end_date)
        self.assertIsNone(pop.potential_end_date)
        self.assertEqual(pop.raw_data, self.minimal_data)

    def test_init_empty_data(self):
        pop = PeriodOfPerformance(self.empty_data)
        self.assertIsNone(pop.start_date)
        self.assertIsNone(pop.end_date)
        self.assertIsNone(pop.potential_end_date)
        self.assertEqual(pop.raw_data, self.empty_data)

    def test_repr(self):
        pop_full = PeriodOfPerformance(self.full_data)
        self.assertEqual(repr(pop_full), "<PeriodOfPerformance 2020-01-01 to 2021-12-31>")
        
        pop_minimal = PeriodOfPerformance(self.minimal_data)
        self.assertEqual(repr(pop_minimal), "<PeriodOfPerformance 2023-03-15 to None>")
        
        pop_empty = PeriodOfPerformance(self.empty_data)
        self.assertEqual(repr(pop_empty), "<PeriodOfPerformance None to None>")

class TestRecipient(unittest.TestCase):
    def setUp(self):
        self.lookup_data = {
            "recipient_name": "GLOBAL CORP INC.",
            "recipient_uei": "UEI123LOOKUP",
            "parent_uei": "PARENTUEI456",
            "location": { # Nested location data
                "address_line1": "777 Corp Blvd",
                "city_name": "Metropolis",
                "state_code": "IL",
                "zip_code": "60601",
                "country_code": "USA"
            }
        }
        # Search data for recipient is typically flat within the Award object's raw data
        # The Recipient class, when initialized with api_type='search',
        # expects flat data for its direct attributes. Location is handled by Award.
        self.search_data_flat_for_recipient_init = {
            "recipient_name": "LOCAL BUSINESS LLC",
            "recipient_uei": "UEI789SEARCH",
            # No parent_uei in typical search results directly for recipient object construction
            # Location fields for 'search' type are typically handled by the Award class
            # when constructing the Recipient's Location object from flat recipient_address_line1 etc.
        }

    def test_init_lookup(self):
        recipient = Recipient(self.lookup_data, api_type='lookup')
        self.assertEqual(recipient.name, "GLOBAL CORP INC.")
        self.assertEqual(recipient.uei, "UEI123LOOKUP")
        self.assertEqual(recipient.parent_uei, "PARENTUEI456")
        self.assertEqual(recipient.raw_data, self.lookup_data)
        self.assertEqual(recipient._api_type, 'lookup')
        self.assertIsNotNone(recipient.location_data) # Internal check
        
        # Test the location property
        location_obj = recipient.location
        self.assertIsInstance(location_obj, Location)
        self.assertEqual(location_obj.address_line1, "777 Corp Blvd")
        self.assertEqual(location_obj.city_name, "Metropolis")

    def test_init_search(self):
        # When api_type is 'search', Recipient is simpler, location is typically
        # constructed and assigned by the Award class.
        recipient = Recipient(self.search_data_flat_for_recipient_init, api_type='search')
        self.assertEqual(recipient.name, "LOCAL BUSINESS LLC")
        self.assertEqual(recipient.uei, "UEI789SEARCH")
        self.assertIsNone(getattr(recipient, 'parent_uei', None)) # Not in this mock
        self.assertIsNone(recipient.location) # Location object not directly built here for 'search' type
        self.assertEqual(recipient.raw_data, self.search_data_flat_for_recipient_init)
        self.assertEqual(recipient._api_type, 'search')

    def test_repr_lookup(self):
        recipient = Recipient(self.lookup_data, api_type='lookup')
        expected_repr = "<Recipient GLOBAL CORP INC. (UEI123LOOKUP)>"
        self.assertEqual(repr(recipient), expected_repr)

    def test_repr_search(self):
        recipient = Recipient(self.search_data_flat_for_recipient_init, api_type='search')
        expected_repr = "<Recipient LOCAL BUSINESS LLC (UEI789SEARCH)>"
        self.assertEqual(repr(recipient), expected_repr)

    def test_location_property_no_data_lookup(self):
        minimal_lookup = {"recipient_name": "Minimal Co"}
        recipient = Recipient(minimal_lookup, api_type='lookup')
        self.assertIsNone(recipient.location_data)
        self.assertIsNone(recipient.location) # Should be None if no location_data

class TestTransaction(unittest.TestCase):
    def setUp(self):
        self.transaction_data_full = {
            "id": 12345,
            "type": "A",
            "type_description": "Contract Action", # Not directly used by current Transaction class
            "description": "Sample transaction description.",
            "action_date": "2022-05-15",
            "federal_action_obligation": 150000.75
        }
        self.transaction_data_minimal = {
            "id": 67890,
            "action_date": "2023-01-20",
            "federal_action_obligation": 5000.00
        }
        self.transaction_data_empty = {}

    def test_init_full_data(self):
        transaction = Transaction(self.transaction_data_full)
        self.assertEqual(transaction.id, 12345)
        self.assertEqual(transaction.type, "A")
        self.assertEqual(transaction.description, "Sample transaction description.")
        self.assertEqual(transaction.action_date, "2022-05-15")
        self.assertEqual(transaction.federal_action_obligation, 150000.75)
        self.assertEqual(transaction.raw_data, self.transaction_data_full)

    def test_init_minimal_data(self):
        transaction = Transaction(self.transaction_data_minimal)
        self.assertEqual(transaction.id, 67890)
        self.assertIsNone(transaction.type)
        self.assertIsNone(transaction.description)
        self.assertEqual(transaction.action_date, "2023-01-20")
        self.assertEqual(transaction.federal_action_obligation, 5000.00)
        self.assertEqual(transaction.raw_data, self.transaction_data_minimal)

    def test_init_empty_data(self):
        transaction = Transaction(self.transaction_data_empty)
        self.assertIsNone(transaction.id)
        self.assertIsNone(transaction.type)
        self.assertIsNone(transaction.description)
        self.assertIsNone(transaction.action_date)
        self.assertIsNone(transaction.federal_action_obligation)
        self.assertEqual(transaction.raw_data, self.transaction_data_empty)
        
    def test_repr(self):
        transaction_full = Transaction(self.transaction_data_full)
        self.assertEqual(repr(transaction_full), "<Transaction 12345>")

        transaction_minimal = Transaction(self.transaction_data_minimal)
        self.assertEqual(repr(transaction_minimal), "<Transaction 67890>")

        transaction_empty = Transaction(self.transaction_data_empty)
        self.assertEqual(repr(transaction_empty), "<Transaction None>")

class TestAward(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock(spec=USASpendingClient)

        self.search_award_data_flat = {
            "generated_unique_award_id": "CONT_AW_123_456",
            "piid": "PIID123", # Contract ID
            "description": "Flat award description from search.",
            "total_obligations": 50000.00,
            "total_outlays": 45000.00,
            "total_potential_value": 60000.00,
            "period_of_performance_start_date": "2021-01-01",
            "period_of_performance_end_date": "2021-12-31",
            "pop_city_name": "FLATCITY",
            "pop_state_code": "FC",
            "pop_country_code": "USA",
            "recipient_name": "Flat Recipient",
            "recipient_uei": "FLATUEI123",
            "recipient_address_line1": "1 Flat Address", # For recipient's location
            "recipient_city_name": "Flat Recip City",
            "recipient_state_code": "FR",
            "recipient_zip_code": "00001"
        }

        self.lookup_award_data_nested = {
            "generated_unique_award_id": "ASST_AW_789_012",
            "fain": "FAIN789", # Assistance ID
            "description": "Nested award description from lookup.",
            "total_obligation_amount": 120000.00, # Different key name than search
            "period_of_performance": {
                "period_of_performance_start_date": "2022-03-01",
                "period_of_performance_end_date": "2023-02-28",
            },
            "place_of_performance": {
                "city_name": "NESTEDCITY",
                "state_code": "NC",
                "country_code": "USA",
                "address_line1": "2 Nested Way"
            },
            "recipient": {
                "recipient_name": "Nested Recipient Inc",
                "recipient_uei": "NESTEDUEI789",
                "location": {"city_name": "Nest Recip City", "state_code": "NR"}
            },
            "transactions": [ # Pre-loaded transactions
                 {"id": 771, "type": "A", "action_date": "2022-03-05", "federal_action_obligation": 50000},
                 {"id": 772, "type": "B", "action_date": "2022-06-10", "federal_action_obligation": 70000},
            ]
        }
        
        # For lazy loading: Award initially has minimal data, client fetches more
        self.minimal_award_data_for_lazy_load = {
            "generated_unique_award_id": "CONT_AW_LAZY_LOAD_001",
            "piid": "LAZYPIID"
        }
        self.detailed_data_for_lazy_pop_recipient = {
             "generated_unique_award_id": "CONT_AW_LAZY_LOAD_001", # Ensure ID matches
             "place_of_performance": {"city_name": "LazyLoaded PoP City", "state_code": "LZ"},
             "recipient": {"recipient_name": "Lazy Recipient", "recipient_uei": "LAZYUEI001", 
                           "location": {"city_name": "Lazy Recip City"}}
        }
        self.transaction_data_for_lazy_load = [
            {"id": 881, "type": "C", "action_date": "2023-01-01", "federal_action_obligation": 1000},
            {"id": 882, "type": "D", "action_date": "2023-02-01", "federal_action_obligation": 2000},
        ]

    def test_init_search_flat_data(self):
        award = Award(self.search_award_data_flat, client=self.mock_client, api_type='search')
        self.assertEqual(award.prime_award_id, "PIID123")
        self.assertEqual(award.generated_unique_award_id, "CONT_AW_123_456")
        self.assertEqual(award.description, "Flat award description from search.")
        self.assertEqual(award.total_obligations, 50000.00)
        self.assertEqual(award.total_outlay, 45000.00)
        self.assertEqual(award.potential_value, 60000.00)
        self.assertEqual(award.raw_data, self.search_award_data_flat)
        self.assertEqual(award._api_type, 'search')
        self.assertEqual(award._client, self.mock_client)
        
        # Test PoP, Recipient constructed from flat data
        self.assertIsInstance(award.period_of_performance, PeriodOfPerformance)
        self.assertEqual(award.period_of_performance.start_date, "2021-01-01")
        
        self.assertIsInstance(award.place_of_performance, Location)
        self.assertEqual(award.place_of_performance.city_name, "FLATCITY")
        
        self.assertIsInstance(award.recipient, Recipient)
        self.assertEqual(award.recipient.name, "Flat Recipient")
        self.assertIsInstance(award.recipient.location, Location) # Recipient's location
        self.assertEqual(award.recipient.location.city_name, "Flat Recip City")


    def test_init_lookup_nested_data(self):
        award = Award(self.lookup_award_data_nested, client=self.mock_client, api_type='lookup')
        self.assertEqual(award.prime_award_id, "FAIN789") # Assistance uses FAIN
        self.assertEqual(award.generated_unique_award_id, "ASST_AW_789_012")
        self.assertEqual(award.total_obligations, 120000.00) # Uses 'total_obligation_amount'
        self.assertEqual(award._api_type, 'lookup')

        # Test PoP, Recipient from nested data
        self.assertIsInstance(award.period_of_performance, PeriodOfPerformance)
        self.assertEqual(award.period_of_performance.start_date, "2022-03-01")
        
        self.assertIsInstance(award.place_of_performance, Location)
        self.assertEqual(award.place_of_performance.city_name, "NESTEDCITY")
        
        self.assertIsInstance(award.recipient, Recipient)
        self.assertEqual(award.recipient.name, "Nested Recipient Inc")
        self.assertIsInstance(award.recipient.location, Location)
        self.assertEqual(award.recipient.location.city_name, "Nest Recip City")
        
        # Test pre-loaded transactions
        self.assertIsInstance(award.transactions, list)
        self.assertEqual(len(award.transactions), 2)
        self.assertIsInstance(award.transactions[0], Transaction)
        self.assertEqual(award.transactions[0].id, 771)


    def test_usa_spending_url(self):
        award_search = Award(self.search_award_data_flat)
        expected_url_search = "https://www.usaspending.gov/award/CONT_AW_123_456/"
        self.assertEqual(award_search.usa_spending_url, expected_url_search)

        award_lookup = Award(self.lookup_award_data_nested)
        expected_url_lookup = "https://www.usaspending.gov/award/ASST_AW_789_012/"
        self.assertEqual(award_lookup.usa_spending_url, expected_url_lookup)
        
        award_no_id = Award({})
        self.assertEqual(award_no_id.usa_spending_url, "")

    def test_lazy_load_pop_and_recipient(self):
        self.mock_client.get_raw_award_details.return_value = self.detailed_data_for_lazy_pop_recipient
        
        award = Award(self.minimal_award_data_for_lazy_load, client=self.mock_client, api_type='search') # api_type search initially
        
        # PoP and Recipient should be None initially
        self.assertIsNone(award._place_of_performance)
        self.assertIsNone(award._recipient)
        
        # Access PoP to trigger lazy load
        pop = award.place_of_performance
        self.mock_client.get_raw_award_details.assert_called_once_with("CONT_AW_LAZY_LOAD_001")
        self.assertIsInstance(pop, Location)
        self.assertEqual(pop.city_name, "LazyLoaded PoP City")
        
        # Reset mock for next lazy load, or ensure it's not called again if data is cached
        self.mock_client.get_raw_award_details.reset_mock() # Or assert_called_once if it should only fetch once
        
        # Access Recipient to trigger lazy load (should use cached detailed_data if merged)
        recipient = award.recipient
        # If details are merged, get_raw_award_details shouldn't be called again for recipient
        # This depends on implementation: does it fetch once and populate all, or fetch for each?
        # Current placeholder Award class merges raw_data, so it should fetch once.
        self.mock_client.get_raw_award_details.assert_not_called() 
        self.assertIsInstance(recipient, Recipient)
        self.assertEqual(recipient.name, "Lazy Recipient")
        self.assertEqual(recipient.location.city_name, "Lazy Recip City")


    def test_lazy_load_transactions(self):
        self.mock_client.get_transactions.return_value = self.transaction_data_for_lazy_load
        award = Award(self.minimal_award_data_for_lazy_load, client=self.mock_client)
        
        self.assertIsNone(award._transactions) # Internal check
        
        transactions = award.transactions
        self.mock_client.get_transactions.assert_called_once_with("CONT_AW_LAZY_LOAD_001")
        self.assertIsInstance(transactions, list)
        self.assertEqual(len(transactions), 2)
        self.assertIsInstance(transactions[0], Transaction)
        self.assertEqual(transactions[0].id, 881)

    def test_get_method(self):
        award = Award(self.search_award_data_flat)
        self.assertEqual(award.get("piid"), "PIID123")
        self.assertEqual(award.get("total_obligations"), 50000.00)
        self.assertIsNone(award.get("non_existent_key"))
        self.assertEqual(award.get("non_existent_key", "default_val"), "default_val")
        
    def test_repr(self):
        award_piid = Award({"piid": "TestPIID1"})
        self.assertEqual(repr(award_piid), "<Award TestPIID1>")
        
        award_fain = Award({"fain": "TestFAIN1"})
        self.assertEqual(repr(award_fain), "<Award TestFAIN1>")

        award_gen_id = Award({"generated_unique_award_id": "TestGenID1"})
        self.assertEqual(repr(award_gen_id), "<Award TestGenID1>") # Gen ID takes precedence if no PIID/FAIN

        award_combo = Award({"piid": "PIID2", "generated_unique_award_id": "GenID2"})
        self.assertEqual(repr(award_combo), "<Award PIID2>") # PIID/FAIN preferred over GenID for repr
        
        award_none = Award({})
        self.assertEqual(repr(award_none), "<Award None>")

if __name__ == '__main__':
    unittest.main()

# Placeholder for USASpendingClient and its tests will be added below.
# Note: The actual USASpendingClient class would be in usa_spending_api.py
# For testing purposes, we define a version of it here.

import requests 
import json 

# (Existing Award, Location, etc. class definitions are assumed to be above this line)
# For brevity, they are not repeated here but were defined in previous steps.

class USASpendingClient:
    API_BASE_URL = "https://api.usaspending.gov/api/v2"
    AWARD_ENDPOINT = "/awards" 
    TRANSACTIONS_ENDPOINT = "/transactions" 
    AWARD_SEARCH_ENDPOINT = "/search/spending_by_award/" 

    DEFAULT_SEARCH_FIELDS = ["award_id", "generated_unique_award_id", "piid", "fain", "description", "total_obligations"] 
    DEFAULT_SORT_FIELD = "total_obligations"

    CONTRACT_TYPE_CODES = ["A", "B", "C", "D"]
    IDV_TYPE_CODES = ["IDV_A", "IDV_B", "IDV_C", "IDV_D", "IDV_E"] 
    GRANT_TYPE_CODES = ["02", "03", "04", "05"]
    LOAN_TYPE_CODES = ["07", "08"]
    DIRECT_PAYMENT_TYPE_CODES = ["06", "10"]
    OTHER_ASSISTANCE_TYPE_CODES = ["09", "11"]


    def __init__(self, api_key="DEMO_KEY"):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({"X-Api-Key": self.api_key, "Content-Type": "application/json"})
        self.api_base_url = self.API_BASE_URL

    def _make_api_request(self, method, endpoint, params=None, json_payload=None):
        url = f"{self.api_base_url}{endpoint}"
        try:
            # In a real scenario, timeout would be configurable.
            response = self.session.request(method, url, params=params, json=json_payload, timeout=10)
            response.raise_for_status() 
            
            if response.status_code == 200:
                try:
                    data = response.json()
                    if isinstance(data, dict) and data.get("message") and "error" in data.get("message").lower(): # Basic error check
                        print(f"API Error for {method} {url}: {data['message']}") 
                        return None 
                    return data
                except json.JSONDecodeError:
                    print(f"JSONDecodeError for {method} {url}: Could not parse response.") 
                    return None
            return response.content 
            
        except requests.exceptions.HTTPError as e:
            print(f"HTTPError for {method} {url}: {e}") 
            return None
        except requests.exceptions.Timeout as e:
            print(f"Timeout for {method} {url}: {e}") 
            return None
        except requests.exceptions.RequestException as e: # Catch other specific requests exceptions if needed
            print(f"RequestException for {method} {url}: {e}") 
            return None

    def get_raw_award_details(self, generated_award_id: str):
        endpoint = f"{self.AWARD_ENDPOINT}/{generated_award_id}/"
        return self._make_api_request(method="GET", endpoint=endpoint)

    def get_transactions(self, generated_award_id: str, page: int = 1, limit: int = 10):
        endpoint = f"{self.AWARD_ENDPOINT}/{generated_award_id}{self.TRANSACTIONS_ENDPOINT}/"
        payload = {"page": page, "limit": limit, "order": "desc"}
        return self._make_api_request(method="POST", endpoint=endpoint, json_payload=payload)

    def award_search(self, search_text: str, award_type_codes: list = None, 
                       fields: list = None, sort_by: str = None, page: int = 1, limit: int = 10, **kwargs):
        if fields is None: fields = self.DEFAULT_SEARCH_FIELDS
        if sort_by is None: sort_by = self.DEFAULT_SORT_FIELD
        
        payload = {
            "filters": {
                "keywords": [search_text] if isinstance(search_text, str) else search_text,
            },
            "fields": fields,
            "sort": sort_by,
            "page": page,
            "limit": limit,
            "order": kwargs.get("order", "desc") 
        }
        if award_type_codes: # Only add if not None or empty
             payload["filters"]["award_type_codes"] = award_type_codes
        
        response_data = self._make_api_request(method="POST", endpoint=self.AWARD_SEARCH_ENDPOINT, json_payload=payload)
        
        if response_data and "results" in response_data:
            return [Award(item, client=self, api_type='search') for item in response_data["results"]]
        return []

    def award_lookup(self, award_id: str): 
        raw_data = self.get_raw_award_details(award_id) 
        if raw_data:
            return Award(raw_data, client=self, api_type='lookup')
        return None

    def contract_search(self, search_text: str, **kwargs):
        fields = kwargs.pop("fields", self.DEFAULT_SEARCH_FIELDS + ["contract_data"])
        sort = kwargs.pop("sort_by", "total_obligations")
        return self.award_search(search_text, award_type_codes=self.CONTRACT_TYPE_CODES, 
                                 fields=fields, sort_by=sort, **kwargs)

    def idv_search(self, search_text: str, **kwargs):
        fields = kwargs.pop("fields", self.DEFAULT_SEARCH_FIELDS + ["idv_data"])
        sort = kwargs.pop("sort_by", "total_obligations")
        return self.award_search(search_text, award_type_codes=self.IDV_TYPE_CODES, 
                                 fields=fields, sort_by=sort, **kwargs)
    
    def grant_search(self, search_text: str, **kwargs):
        fields = kwargs.pop("fields", self.DEFAULT_SEARCH_FIELDS)
        sort = kwargs.pop("sort_by", "total_obligations")
        return self.award_search(search_text, award_type_codes=self.GRANT_TYPE_CODES,
                                 fields=fields, sort_by=sort, **kwargs)

    def loan_search(self, search_text: str, **kwargs):
        fields = kwargs.pop("fields", self.DEFAULT_SEARCH_FIELDS + ["loan_data"])
        sort = kwargs.pop("sort_by", "total_obligations")
        return self.award_search(search_text, award_type_codes=self.LOAN_TYPE_CODES,
                                 fields=fields, sort_by=sort, **kwargs)

    def direct_payment_search(self, search_text: str, **kwargs):
        fields = kwargs.pop("fields", self.DEFAULT_SEARCH_FIELDS)
        sort = kwargs.pop("sort_by", "total_obligations")
        return self.award_search(search_text, award_type_codes=self.DIRECT_PAYMENT_TYPE_CODES,
                                 fields=fields, sort_by=sort, **kwargs)
                                 
    def other_assistance_search(self, search_text: str, **kwargs):
        fields = kwargs.pop("fields", self.DEFAULT_SEARCH_FIELDS)
        sort = kwargs.pop("sort_by", "total_obligations")
        return self.award_search(search_text, award_type_codes=self.OTHER_ASSISTANCE_TYPE_CODES,
                                 fields=fields, sort_by=sort, **kwargs)

    def all_award_search(self, search_text: str, **kwargs):
        all_results = []
        seen_ids = set()

        helper_searches = [
            self.contract_search, self.idv_search, self.grant_search, 
            self.loan_search, self.direct_payment_search, self.other_assistance_search
        ]

        for search_method in helper_searches:
            # Pass through any additional kwargs like page, limit
            results = search_method(search_text, **kwargs) 
            for award in results:
                award_identifier = award.prime_award_id or award.generated_unique_award_id
                if award_identifier and award_identifier not in seen_ids:
                    all_results.append(award)
                    seen_ids.add(award_identifier)
                elif not award_identifier: 
                    all_results.append(award) 
        return all_results


class TestUSASpendingClient(unittest.TestCase):
    def test_init(self):
        client_default_key = USASpendingClient()
        self.assertEqual(client_default_key.api_key, "DEMO_KEY")
        self.assertEqual(client_default_key.api_base_url, USASpendingClient.API_BASE_URL)
        self.assertIsInstance(client_default_key.session, requests.Session)
        self.assertEqual(client_default_key.session.headers["X-Api-Key"], "DEMO_KEY")
        self.assertEqual(client_default_key.session.headers["Content-Type"], "application/json")

        client_custom_key = USASpendingClient(api_key="CUSTOM_KEY_123")
        self.assertEqual(client_custom_key.api_key, "CUSTOM_KEY_123")
        self.assertEqual(client_custom_key.session.headers["X-Api-Key"], "CUSTOM_KEY_123")

    @patch('requests.Session.request')
    @patch('builtins.print') # To capture print calls for logging
    def test_make_api_request_success(self, mock_print, mock_request):
        client = USASpendingClient()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"key": "value"}
        mock_request.return_value = mock_response

        # Test GET
        response_data_get = client._make_api_request("GET", "/test_endpoint", params={"p": "1"})
        mock_request.assert_called_with("GET", f"{client.api_base_url}/test_endpoint", params={"p": "1"}, json=None, timeout=10)
        self.assertEqual(response_data_get, {"key": "value"})

        # Test POST
        payload = {"data": "send"}
        response_data_post = client._make_api_request("POST", "/another_endpoint", json_payload=payload)
        mock_request.assert_called_with("POST", f"{client.api_base_url}/another_endpoint", params=None, json=payload, timeout=10)
        self.assertEqual(response_data_post, {"key": "value"})
        mock_print.assert_not_called() # No errors should be printed

    @patch('requests.Session.request')
    @patch('builtins.print')
    def test_make_api_request_http_error(self, mock_print, mock_request):
        client = USASpendingClient()
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError("404 Not Found")
        mock_request.return_value = mock_response

        response_data = client._make_api_request("GET", "/notfound")
        self.assertIsNone(response_data)
        mock_print.assert_any_call(f"HTTPError for GET {client.api_base_url}/notfound: 404 Not Found")

    @patch('requests.Session.request')
    @patch('builtins.print')
    def test_make_api_request_timeout(self, mock_print, mock_request):
        client = USASpendingClient()
        mock_request.side_effect = requests.exceptions.Timeout("Request timed out")

        response_data = client._make_api_request("GET", "/timeout_endpoint")
        self.assertIsNone(response_data)
        mock_print.assert_any_call(f"Timeout for GET {client.api_base_url}/timeout_endpoint: Request timed out")

    @patch('requests.Session.request')
    @patch('builtins.print')
    def test_make_api_request_request_exception(self, mock_print, mock_request):
        client = USASpendingClient()
        mock_request.side_effect = requests.exceptions.RequestException("Some network error")

        response_data = client._make_api_request("GET", "/network_error_endpoint")
        self.assertIsNone(response_data)
        mock_print.assert_any_call(f"RequestException for GET {client.api_base_url}/network_error_endpoint: Some network error")

    @patch('requests.Session.request')
    @patch('builtins.print')
    def test_make_api_request_json_decode_error(self, mock_print, mock_request):
        client = USASpendingClient()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.side_effect = json.JSONDecodeError("msg", "doc", 0)
        mock_request.return_value = mock_response

        response_data = client._make_api_request("GET", "/json_error_endpoint")
        self.assertIsNone(response_data)
        mock_print.assert_any_call(f"JSONDecodeError for GET {client.api_base_url}/json_error_endpoint: Could not parse response.")

    @patch('requests.Session.request')
    @patch('builtins.print')
    def test_make_api_request_embedded_api_error_message(self, mock_print, mock_request):
        client = USASpendingClient()
        mock_response = MagicMock()
        mock_response.status_code = 200
        # Simulate an API error message within a 200 OK response
        mock_response.json.return_value = {"message": "An error occurred on the server side."} 
        mock_request.return_value = mock_response

        response_data = client._make_api_request("GET", "/api_error_endpoint")
        self.assertIsNone(response_data) # Should return None on API error message
        mock_print.assert_any_call(f"API Error for GET {client.api_base_url}/api_error_endpoint: An error occurred on the server side.")

    @patch.object(USASpendingClient, '_make_api_request')
    def test_get_raw_award_details(self, mock_make_request):
        client = USASpendingClient()
        expected_response = {"id": "AWARD_001_DETAILS", "description": "Details"}
        mock_make_request.return_value = expected_response
        
        award_id = "AWARD_001"
        response = client.get_raw_award_details(award_id)
        
        expected_endpoint = f"{client.AWARD_ENDPOINT}/{award_id}/"
        mock_make_request.assert_called_once_with(method="GET", endpoint=expected_endpoint)
        self.assertEqual(response, expected_response)

    @patch.object(USASpendingClient, '_make_api_request')
    def test_get_transactions(self, mock_make_request):
        client = USASpendingClient()
        expected_response = {"page_metadata": {}, "results": [{"id": "TRANS_001"}]}
        mock_make_request.return_value = expected_response

        award_id = "AWARD_002"
        page = 2
        limit = 20
        response = client.get_transactions(award_id, page=page, limit=limit)

        expected_endpoint = f"{client.AWARD_ENDPOINT}/{award_id}{client.TRANSACTIONS_ENDPOINT}/"
        expected_payload = {"page": page, "limit": limit, "order": "desc"}
        mock_make_request.assert_called_once_with(method="POST", endpoint=expected_endpoint, json_payload=expected_payload)
        self.assertEqual(response, expected_response)

    @patch.object(USASpendingClient, '_make_api_request')
    def test_award_search(self, mock_make_request):
        client = USASpendingClient()
        mock_award_data_item = {"generated_unique_award_id": "AWARD_SEARCH_001", "piid": "PIID_S1"}
        mock_response_data = {"results": [mock_award_data_item]}
        mock_make_request.return_value = mock_response_data

        search_text = "test keyword"
        award_types = ["A", "B"]
        fields = ["piid", "description"]
        sort_by = "description"
        page = 1
        limit = 5
        
        awards = client.award_search(search_text, award_type_codes=award_types, 
                                     fields=fields, sort_by=sort_by, page=page, limit=limit)

        expected_payload = {
            "filters": {"keywords": [search_text], "award_type_codes": award_types},
            "fields": fields,
            "sort": sort_by,
            "page": page,
            "limit": limit,
            "order": "desc" 
        }
        mock_make_request.assert_called_once_with(method="POST", 
                                                  endpoint=client.AWARD_SEARCH_ENDPOINT, 
                                                  json_payload=expected_payload)
        self.assertEqual(len(awards), 1)
        self.assertIsInstance(awards[0], Award)
        self.assertEqual(awards[0].generated_unique_award_id, "AWARD_SEARCH_001")
        self.assertEqual(awards[0]._client, client) # Check client instance is passed
        self.assertEqual(awards[0]._api_type, 'search')


    @patch.object(USASpendingClient, 'get_raw_award_details') # Mocking the method it calls
    def test_award_lookup(self, mock_get_raw_details):
        client = USASpendingClient()
        mock_award_detail_data = {"generated_unique_award_id": "AWARD_LOOKUP_002", "description": "Lookup result"}
        mock_get_raw_details.return_value = mock_award_detail_data
        
        award_id_to_lookup = "AWARD_LOOKUP_002"
        award = client.award_lookup(award_id_to_lookup)
        
        mock_get_raw_details.assert_called_once_with(award_id_to_lookup)
        self.assertIsInstance(award, Award)
        self.assertEqual(award.generated_unique_award_id, "AWARD_LOOKUP_002")
        self.assertEqual(award.description, "Lookup result")
        self.assertEqual(award._client, client)
        self.assertEqual(award._api_type, 'lookup')

    @patch.object(USASpendingClient, 'get_raw_award_details')
    def test_award_lookup_not_found(self, mock_get_raw_details):
        client = USASpendingClient()
        mock_get_raw_details.return_value = None # Simulate award not found
        
        award = client.award_lookup("NON_EXISTENT_AWARD")
        self.assertIsNone(award)

    @patch.object(USASpendingClient, 'award_search')
    def test_contract_search(self, mock_award_search):
        client = USASpendingClient()
        search_text = "solar panels"
        kwargs = {"limit": 5, "page": 2}
        client.contract_search(search_text, **kwargs)
        
        expected_fields = client.DEFAULT_SEARCH_FIELDS + ["contract_data"]
        mock_award_search.assert_called_once_with(
            search_text, 
            award_type_codes=client.CONTRACT_TYPE_CODES,
            fields=expected_fields,
            sort_by="total_obligations",
            **kwargs
        )

    @patch.object(USASpendingClient, 'award_search')
    def test_idv_search(self, mock_award_search):
        client = USASpendingClient()
        search_text = "it support"
        kwargs = {"limit": 20}
        client.idv_search(search_text, **kwargs)
        
        expected_fields = client.DEFAULT_SEARCH_FIELDS + ["idv_data"]
        mock_award_search.assert_called_once_with(
            search_text,
            award_type_codes=client.IDV_TYPE_CODES,
            fields=expected_fields,
            sort_by="total_obligations",
            **kwargs
        )

    @patch.object(USASpendingClient, 'award_search')
    def test_grant_search(self, mock_award_search):
        client = USASpendingClient()
        search_text = "research grant"
        client.grant_search(search_text) # No extra kwargs
        
        mock_award_search.assert_called_once_with(
            search_text,
            award_type_codes=client.GRANT_TYPE_CODES,
            fields=client.DEFAULT_SEARCH_FIELDS,
            sort_by="total_obligations"
        )
    
    @patch.object(USASpendingClient, 'award_search')
    def test_loan_search(self, mock_award_search):
        client = USASpendingClient()
        search_text = "small business loan"
        kwargs = {"fields": ["custom_loan_field"], "sort_by": "loan_amount"}
        client.loan_search(search_text, **kwargs)
        
        # Note: The pop in the client's helper methods for fields/sort_by means
        # the **kwargs passed to award_search will not contain 'fields' or 'sort_by'
        # if they were in the original kwargs.
        mock_award_search.assert_called_once_with(
            search_text,
            award_type_codes=client.LOAN_TYPE_CODES,
            fields=["custom_loan_field"], # Overridden
            sort_by="loan_amount", # Overridden
        )

    @patch.object(USASpendingClient, 'award_search')
    def test_direct_payment_search(self, mock_award_search):
        client = USASpendingClient()
        search_text = "disaster relief"
        client.direct_payment_search(search_text, limit=50)
        
        mock_award_search.assert_called_once_with(
            search_text,
            award_type_codes=client.DIRECT_PAYMENT_TYPE_CODES,
            fields=client.DEFAULT_SEARCH_FIELDS,
            sort_by="total_obligations",
            limit=50
        )

    @patch.object(USASpendingClient, 'award_search')
    def test_other_assistance_search(self, mock_award_search):
        client = USASpendingClient()
        search_text = "community support"
        client.other_assistance_search(search_text, page=3, order="asc")
        
        mock_award_search.assert_called_once_with(
            search_text,
            award_type_codes=client.OTHER_ASSISTANCE_TYPE_CODES,
            fields=client.DEFAULT_SEARCH_FIELDS,
            sort_by="total_obligations",
            page=3,
            order="asc"
        )

    @patch.object(USASpendingClient, 'contract_search')
    @patch.object(USASpendingClient, 'idv_search')
    @patch.object(USASpendingClient, 'grant_search')
    @patch.object(USASpendingClient, 'loan_search')
    @patch.object(USASpendingClient, 'direct_payment_search')
    @patch.object(USASpendingClient, 'other_assistance_search')
    def test_all_award_search(self, mock_other_search, mock_dp_search, mock_loan_search, 
                              mock_grant_search, mock_idv_search, mock_contract_search):
        client = USASpendingClient()
        search_text = "comprehensive search"
        common_kwargs = {"limit": 10, "page": 1}

        # Mock return values for each helper method
        # Each returns a list of Award objects (or mocks that behave like them for ID checking)
        mock_contract_award1 = MagicMock(spec=Award, prime_award_id="PIID123", generated_unique_award_id="CONT_AW_1")
        mock_contract_award2 = MagicMock(spec=Award, prime_award_id="PIID456", generated_unique_award_id="CONT_AW_2")
        mock_contract_search.return_value = [mock_contract_award1, mock_contract_award2]

        mock_idv_award = MagicMock(spec=Award, prime_award_id="IDV789", generated_unique_award_id="IDV_AW_1")
        mock_idv_search.return_value = [mock_idv_award]

        # Grant award that duplicates a contract award ID (should be de-duplicated)
        mock_grant_award_dup = MagicMock(spec=Award, prime_award_id="PIID123", generated_unique_award_id="GRANT_AW_DUP") 
        mock_grant_award_new = MagicMock(spec=Award, prime_award_id="FAIN000", generated_unique_award_id="GRANT_AW_NEW")
        mock_grant_search.return_value = [mock_grant_award_dup, mock_grant_award_new]
        
        mock_loan_search.return_value = [] # No loan results
        mock_dp_search.return_value = []   # No direct payment results
        
        # Other assistance with a generated_unique_award_id but no prime_award_id (should still be included)
        mock_other_award_no_prime = MagicMock(spec=Award, prime_award_id=None, generated_unique_award_id="OTHER_GEN_ID_1")
        mock_other_search.return_value = [mock_other_award_no_prime]


        results = client.all_award_search(search_text, **common_kwargs)

        # Verify each helper method was called
        mock_contract_search.assert_called_once_with(search_text, **common_kwargs)
        mock_idv_search.assert_called_once_with(search_text, **common_kwargs)
        mock_grant_search.assert_called_once_with(search_text, **common_kwargs)
        mock_loan_search.assert_called_once_with(search_text, **common_kwargs)
        mock_dp_search.assert_called_once_with(search_text, **common_kwargs)
        mock_other_search.assert_called_once_with(search_text, **common_kwargs)

        # Verify de-duplication and content
        # Expected: PIID123 (from contract), PIID456, IDV789, FAIN000, OTHER_GEN_ID_1
        # mock_grant_award_dup (PIID123) should be excluded
        self.assertEqual(len(results), 5)
        
        returned_ids = set()
        for r in results:
            if r.prime_award_id:
                returned_ids.add(r.prime_award_id)
            else:
                returned_ids.add(r.generated_unique_award_id) # Fallback for de-duplication

        expected_ids = {"PIID123", "PIID456", "IDV789", "FAIN000", "OTHER_GEN_ID_1"}
        self.assertEqual(returned_ids, expected_ids)
        
        # Check that the specific objects are present (order might vary)
        self.assertIn(mock_contract_award1, results)
        self.assertIn(mock_contract_award2, results)
        self.assertIn(mock_idv_award, results)
        self.assertIn(mock_grant_award_new, results)
        self.assertIn(mock_other_award_no_prime, results)
        self.assertNotIn(mock_grant_award_dup, results) # Due to de-duplication
