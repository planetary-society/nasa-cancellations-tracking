import requests
import csv
import io # To handle the text stream like a file
from urllib.parse import quote # For URL encoding the sheet name
from typing import List, Optional

class GoogleSheet:
    """
    Manages access to data within a specific Google Sheet using direct CSV download.

    This class uses the Google Visualization API URL format to download
    sheet data directly as CSV, then parses it using Python's csv module.
    This method works for publicly accessible sheets ('Anyone with the link can view')
    without requiring external libraries like gspread or complex authentication setup.
    """
    # Base URL template for Google Visualization API CSV export
    GVIZ_CSV_URL_TEMPLATE = "https://docs.google.com/spreadsheets/d/{key}/gviz/tq?tqx=out:csv&sheet={sheet_name}"

    def __init__(self, sheet_url: str):
        """
        Initializes the GoogleSheet client.

        Args:
            sheet_url: The full URL of the public Google Sheet.

        Raises:
            ValueError: If the Sheet ID cannot be extracted from the URL.
        """
        self.sheet_url = sheet_url
        self.sheet_id = self._extract_sheet_id(sheet_url)
        if not self.sheet_id:
            raise ValueError(f"Could not extract Sheet ID from URL: {sheet_url}")

    def _extract_sheet_id(self, url: str) -> Optional[str]:
        """Helper to attempt extracting the sheet ID from the URL."""
        try:
            # Example URL: https://docs.google.com/spreadsheets/d/SHEET_ID/edit...
            parts = url.split('/d/')
            if len(parts) > 1:
                return parts[1].split('/')[0]
        except Exception:
            pass
        return None

    def get_award_ids(self, sheet_name: str = "Contracts/Grants", column_name: str = "Award ID") -> Optional[List[str]]:
        """
        Retrieves all values from a specific column in a specific worksheet
        by downloading and parsing Google Visualization API CSV data.

        Args:
            sheet_name: The name of the worksheet (tab) to access (e.g., "Contracts/Grants").
            column_name: The exact header name of the column to retrieve (e.g., "Award ID").

        Returns:
            A list of strings containing the values from the specified column,
            excluding the header. Returns None if unable to fetch or parse data,
            or if the sheet/column is not found.
        """

        # URL-encode the sheet name to handle spaces and special characters
        encoded_sheet_name = quote(sheet_name)
        download_url = self.GVIZ_CSV_URL_TEMPLATE.format(
            key=self.sheet_id,
            sheet_name=encoded_sheet_name
        )

        try:
            response = requests.get(download_url)
            response.raise_for_status() # Check for HTTP errors (4xx or 5xx)

            # Requests usually detects encoding, but ensure UTF-8 for broader compatibility
            response.encoding = response.apparent_encoding or 'utf-8'
            csv_text = response.text

            # Use io.StringIO to treat the string as a file for the csv reader
            csv_file = io.StringIO(csv_text)
            reader = csv.reader(csv_file)

            # Read the header row
            try:
                header = next(reader)
            except StopIteration:
                print(f"Error: CSV data for sheet '{sheet_name}' appears to be empty or has no header.")
                return None # Empty sheet

            # Find the column index from the header
            try:
                col_index = header.index(column_name)
            except ValueError:
                print(f"Error: Column header '{column_name}' not found in worksheet '{sheet_name}'.")
                print(f"Available headers: {header}")
                return None

            # Extract data from the target column in remaining rows
            award_ids = []
            for row in reader:
                # Ensure row has enough columns before accessing the index
                if len(row) > col_index:
                    if row[col_index]:
                        award_ids.append(row[col_index])
                else:
                    # Handle potentially ragged CSV rows (though less common from Sheets export)
                    print(f"Warning: Row {reader.line_num} is shorter than expected, skipping.")


            print(f"Successfully extracted {len(award_ids)} values from column '{column_name}'.")
            return award_ids

        except requests.exceptions.RequestException as e:
            print(f"Error: Network or HTTP error during download: {e}")
            return None
        except csv.Error as e:
             # Handle potential errors during CSV parsing (e.g., malformed CSV)
            print(f"Error: Failed to parse CSV data: {e}")
            return None
        except Exception as e:
            print(f"Error: An unexpected error occurred retrieving data: {e}")
            return None
