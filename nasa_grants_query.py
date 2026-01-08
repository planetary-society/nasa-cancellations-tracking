#!/usr/bin/env python3

import pandas as pd
import requests
import sys
from typing import List, Dict, Any, Optional
from datetime import date, timedelta
from contract_query import ContractQuery, FINAL_COLUMNS
from utils import smart_sentence_case, contracts_titlecase

class NASAGrantsQuery(ContractQuery):
    """
    Implementation of ContractQuery to fetch grant data from the NASA Grants
    Search API (https://www3.nasa.gov/api/2/grants/_search).
    """
    API_URL = "https://www3.nasa.gov/api/2/grants/_search"
    # Fields requested from the NASA API
    SOURCE_FIELDS = (
        "grant_number",         # Used for Award ID
        "institution_name",     # Used for recipient
        "proposal_title",       # Used for description
        "award_date",           # Informational, not in FINAL_COLUMNS directly
        "pop_start_date",       # Informational, not in FINAL_COLUMNS directly
        "pop_end_date",         # Used for filtering
        "case_state",           # Used for status
        "purchase_request_number", "pgrp_center",
        "principal_investigator", "technical_representative",
        "pr_task", "program_title"
    )
    # Maximum results per API call page
    MAX_RESULTS_PER_PAGE = 10000 # Based on example, seems high but we'll use it

    def __init__(self, final_columns: List[str] = FINAL_COLUMNS):
        """
        Initializes the NasaGrantsQuery.

        Args:
            final_columns: The desired list of column names for the output DataFrame.
                           Defaults to the globally defined FINAL_COLUMNS.
        """
        super().__init__(final_columns)

    def _format_date(self, date_obj: date) -> str:
        """Formats a date object into YYYY-MM-DD string."""
        return date_obj.strftime('%Y-%m-%d')


    def search(self):
        date_changes = self.search_nasa_grants(
            start_date=date(2025, 1, 21),
            end_date=date.today()
        )
        # Remove entries unless they have "change pop end date" or "terminated" in the status
        date_changes = date_changes[
            date_changes['status'].str.contains("change pop end date|terminat|convenience|effectuate", case=False, na=False)
        ]
        # Now filter out only the rows with "decrease" in the status
        date_changes = date_changes[
            date_changes['status'].str.contains("decrease", case=False, na=False)
        ]
        
        # Find cancelled grants but don't return them in output for now
        cancelled = self.search_nasa_grants(
            start_date=date(2025, 1, 21),
            end_date=date(2030, 1, 19),
            status="Cancelled"
        )
        
        return date_changes

    def search_nasa_grants(self,
               start_date: date = date(2025, 1, 21),
               end_date: Optional[date] = None,
               status: Optional[str] = None,
              ) -> pd.DataFrame:
        """
        Searches the NASA Grants API for grants with a 'Period of Performance End Date'
        within the specified date range.

        Args:
            start_date: The start date for the 'pop_end_date' query range (inclusive).
                        Defaults to January 21, 2025.
            end_date: The end date for the 'pop_end_date' query range (inclusive).
                      Defaults to the current date if not provided.
            status: Optional filter for the case state of the grants. If not provided,
                   all grants are included. Can be set to a specific case state:
                   "Cancelled","Awarded","Pending","Work in Progress"

        Returns:
            A Pandas DataFrame containing the grant data, structured according
            to self.final_columns. Returns an empty DataFrame if an error occurs
            or no grants are found.

        Raises:
            requests.exceptions.RequestException: If the API request fails.
            ValueError: If the API response is not valid JSON or is missing expected structure.
        """
        # Set end_date to today if it's None
        if end_date is None:
            end_date = date.today()

        # Validate that start_date is not after end_date
        if start_date > end_date:
             print(f"Warning: Start date ({start_date}) is after end date ({end_date}). Swapping dates for query.", file=sys.stderr)
             start_date, end_date = end_date, start_date # Swap them for the query logic


        start_date_str = self._format_date(start_date)
        end_date_str = self._format_date(end_date)

        print(f"Querying NASA Grants API for pop_end_date between {start_date_str} and {end_date_str}", file=sys.stderr)

        if status and (status in ["Cancelled","Awarded","Pending","Work in Progress"]):
            case_state = status
        else:
            case_state = "*"
        
        params = {
            "sort": "award_date:desc",
            "from": "0",
            "size": str(self.MAX_RESULTS_PER_PAGE),
            "_source_include": ",".join(self.SOURCE_FIELDS),
            "q": f"pop_end_date:[{start_date_str} TO {end_date_str}] AND pgrp_center:* AND case_state:{case_state}",
        }

        try:
            response = requests.get(
                self.API_URL,
                params=params,
                headers={"Content-Type": "charset=UTF-8"}
            )
            response.raise_for_status()
            response.encoding = 'utf-8'
            data = response.json()

        except requests.exceptions.RequestException as e:
            print(f"Error during NASA Grants API request: {e}", file=sys.stderr)
            return pd.DataFrame(columns=self.final_columns)
        except ValueError as e:
            print(f"Error decoding NASA Grants API response: {e}", file=sys.stderr)
            print(f"Response text: {response.text[:500]}...", file=sys.stderr)
            return pd.DataFrame(columns=self.final_columns)

        hits = data.get("hits", {}).get("hits", [])
        print(f"Found {len(hits)} grants matching the criteria in NASA API.", file=sys.stderr)
        
        # Dump hits to CSV for recordkeping
        raw_df = pd.json_normalize(hits)
        # Sort by _source.pop_end_date asc
        raw_df.sort_values(by=['_source.pop_end_date'], ascending=True, inplace=True)
        
        if status == "Cancelled":
            self.export_to_csv(raw_df, "nasa_grants_cancelled_query")
        else:
            # filter raw_df by keywords in the pr_task field
            keywords = ["change pop end date", "terminat", "convenience"]
            raw_df = raw_df[raw_df['_source.pr_task'].str.contains('|'.join(keywords), case=False, na=False)]
            # Now filter out only the rows with "decrease" in the pr_task field
            raw_df = raw_df[raw_df['_source.pr_task'].str.contains("decrease", case=False, na=False)]
            
            self.export_to_csv(raw_df, "nasa_grants_date_changes_query")
        
        processed_data: List[Dict[str, Any]] = []
        for hit in hits:
            source = hit.get("_source", {})
            if not source:
                continue

            grant_number = source.get("grant_number", "").strip()
            grant_parts = grant_number.split(maxsplit=1)
            award_id = grant_parts[0] if grant_parts else grant_number

            recipient_name = contracts_titlecase(source.get("institution_name", ""))
            status_text = source.get("case_state", "") + " - " + source.get("pr_task", "")
            description_text = smart_sentence_case(source.get("proposal_title", ""))

            description_text = status_text + ". " + description_text

            record: Dict[str, Any] = {
                'Award ID': award_id,
                'source_type': 'NASA Grant',
                'recipient': recipient_name,
                'value': None,
                'savings': None,
                'status': status_text,
                'source_url': None,
                'description': description_text,
                'agency': 'NASA'
            }
            processed_data.append(record)

        df = pd.DataFrame(processed_data)

        if df.empty:
             return pd.DataFrame(columns=self.final_columns)
        else:
             # Ensure final columns using reindex (handles missing cols & order)
             # This is more robust than just assigning columns if processed_data was empty
             return df.reindex(columns=self.final_columns)


# --- Example Usage (Optional) ---
if __name__ == '__main__':
    print("Running NASA Grants Query Example...")

    # Define a date range for the search (e.g., grants ending in the last 30 days)
    today = date.today()
    thirty_days_ago = today - timedelta(days=30)

    # Create an instance of the query class
    nasa_query = NASAGrantsQuery()

    try:
        # Perform the search
        results_df = nasa_query.search()

        # Display results
        if not results_df.empty:
            print("\nNASA Grants Search Results:")
            print(results_df.to_string())
            print(f"\nTotal records retrieved: {len(results_df)}")
            # print("\nColumn Data Types:")
            # print(results_df.dtypes)
        else:
            print("\nNo grants found matching the criteria or an error occurred.")

    except Exception as e:
        # Catch any unexpected errors during the search process
        print(f"\nAn unexpected error occurred: {e}", file=sys.stderr)