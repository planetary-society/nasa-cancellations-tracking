import unittest
from unittest.mock import patch, MagicMock
import pandas as pd
import os
from datetime import datetime
from abc import ABC, abstractmethod

# Assuming ContractQuery is in a module named 'contract_query_base' or similar
# Adjust the import path as necessary if ContractQuery is located elsewhere.
# For now, let's define a minimal ContractQuery ABC here if it's not easily importable
# or to make the test self-contained for this step.

class ContractQuery(ABC):
    BASE_URL = ""
    FINAL_COLUMNS = ["col1", "col2"] # Example default

    def __init__(self, api_key: str = "DEMO_KEY"):
        self.api_key = api_key
        # Ensure a copy is made, not a reference
        self.final_columns = list(self.FINAL_COLUMNS)

    @abstractmethod
    def search(self, search_term: str, limit: int = 10) -> pd.DataFrame:
        pass

    def export_to_csv(self, df: pd.DataFrame, search_term: str) -> str:
        today_date = datetime.now().strftime("%Y-%m-%d")
        safe_search_term = "".join(c if c.isalnum() else "_" for c in search_term)
        filename = f"data/contracts_{safe_search_term}_{today_date}.csv"

        if not os.path.exists('data'):
            os.makedirs('data')

        df.to_csv(filename, index=False, encoding='utf-8')
        print(f"Data exported to {filename}")
        return filename

# Concrete subclass for testing
class MockContractQuery(ContractQuery):
    def search(self, search_term: str, limit: int = 10) -> pd.DataFrame:
        # Return a dummy DataFrame for testing purposes
        return pd.DataFrame({'col1': [1, 2], 'col2': ['a', 'b']})

class TestContractQuery(unittest.TestCase):
    def test_init_final_columns(self):
        query_instance = MockContractQuery(api_key="test_key")
        self.assertEqual(query_instance.api_key, "test_key")
        # Check if final_columns is a copy, not the same object as class attribute
        self.assertEqual(query_instance.final_columns, ContractQuery.FINAL_COLUMNS)
        self.assertIsNot(query_instance.final_columns, ContractQuery.FINAL_COLUMNS)

        # Test with modified class FINAL_COLUMNS to ensure __init__ uses the class attr
        original_final_columns = ContractQuery.FINAL_COLUMNS
        ContractQuery.FINAL_COLUMNS = ["new_col1", "new_col2"]
        query_instance_new = MockContractQuery(api_key="test_key_new")
        self.assertEqual(query_instance_new.final_columns, ["new_col1", "new_col2"])
        # Restore original for other tests
        ContractQuery.FINAL_COLUMNS = original_final_columns

    @patch('os.makedirs')
    @patch('pandas.DataFrame.to_csv')
    def test_export_to_csv(self, mock_to_csv, mock_makedirs):
        query_instance = MockContractQuery()
        dummy_df = pd.DataFrame({'col1': [1], 'col2': ['a']})
        search_term = "test search"

        # --- Scenario 1: 'data' directory does NOT exist ---
        with patch('os.path.exists', return_value=False):
            returned_filename = query_instance.export_to_csv(dummy_df, search_term)

            # Check if os.makedirs was called
            mock_makedirs.assert_called_once_with('data')

            # Check filename format
            today_date = datetime.now().strftime("%Y-%m-%d")
            safe_search_term = "test_search" # "test search" -> "test_search"
            expected_filename = f"data/contracts_{safe_search_term}_{today_date}.csv"
            self.assertEqual(returned_filename, expected_filename)

            # Check if to_csv was called correctly
            mock_to_csv.assert_called_once_with(expected_filename, index=False, encoding='utf-8')

        # Reset mocks for the next scenario
        mock_makedirs.reset_mock()
        mock_to_csv.reset_mock()

        # --- Scenario 2: 'data' directory DOES exist ---
        with patch('os.path.exists', return_value=True):
            returned_filename_exists = query_instance.export_to_csv(dummy_df, "another term")
            
            # Check if os.makedirs was NOT called
            mock_makedirs.assert_not_called()

            # Check filename format for the new search term
            today_date_exists = datetime.now().strftime("%Y-%m-%d")
            safe_search_term_exists = "another_term"
            expected_filename_exists = f"data/contracts_{safe_search_term_exists}_{today_date_exists}.csv"
            self.assertEqual(returned_filename_exists, expected_filename_exists)

            # Check if to_csv was called correctly
            mock_to_csv.assert_called_once_with(expected_filename_exists, index=False, encoding='utf-8')

if __name__ == '__main__':
    unittest.main()
