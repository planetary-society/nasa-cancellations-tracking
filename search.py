import pandas as pd
from datetime import datetime
from typing import List, Dict
import os
import logging
from doge_search import DOGEQuery
from npdv_query import NPDVQuery
from nasa_grants_query import NASAGrantsQuery
from usaspending import USASpendingClient, Award

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class Search():
    def __init__(self):
        self.client = USASpendingClient()
        self.sources = ["DOGE", "NPDV", "NASAGrants"]
        self.sources_cancellation_data: Dict[str,pd.DataFrame] = {} # key: source name, value: source dataframe
        self.unique_award_ids: List[str] = []
        self.unique_cancellations: Dict[str, List] = {} # key: award_id, value: List[award details]
        self.awards: List[Award] = []
        self.ignore_award_ids: List[str] = ["80LARC19F0086","80LARC25F7014","80JSC024F0024","80JSC024F0026","80LARC21F0053","80NSSC19K0714"]

    def search(self):
        
        # Query all sources and collect both their returned dataframes and a list of unique award ids
        for source in self.sources:
            # Dynamically get the query class based on the source name
            # DOGEQuery, NPPVQuery, etc
            query_class = globals()[f"{source}Query"]
            self.sources_cancellation_data[source] = query_class().search()
            award_ids = self.sources_cancellation_data[source]["Award ID"].astype(str).tolist()
            for award_id in award_ids:
                if award_id not in self.unique_award_ids:
                    self.unique_award_ids.append(award_id)

        # Remove empty strings from the list
        self.unique_award_ids = [award_id for award_id in self.unique_award_ids if award_id]

        # Remove award IDs that are in the ignore list
        self.unique_award_ids = [award_id for award_id in self.unique_award_ids if award_id not in self.ignore_award_ids]

        contracts = self.client.awards.search().with_award_ids(*self.unique_award_ids).contracts().all()
        grants = self.client.awards.search().with_award_ids(*self.unique_award_ids).grants().all()
        self.awards = contracts + grants
        
        print(f"Found {len(self.awards)} awards from USASpending API.")
        
        for source in self.sources:
            # Get the source award IDs from the source dataframe
            source_award_ids = self.sources_cancellation_data[source]["Award ID"].astype(str).tolist()
            # Add the source awards to the cancellations dictionary
            self._add_source_awards(source, source_award_ids, self.awards)
        
        headers = [
            "Source",
            "District",
            "Recipient",
            "Award ID",
            "Latest Modification Number",
            "Latest Modification Date",
            "Start Date",
            "End Date",
            "Award Amount",
            "Total Outlays",
            "Description",
            "Business Categories",
            "URL"
        ]
        
        print(f"Found {len(self.unique_cancellations)} unique cancellations.")
        
        output_data = list(self.unique_cancellations.values())
        
        df = pd.DataFrame(output_data, columns=headers)
        df.sort_values(by=["Recipient", "Latest Modification Date"], inplace=True)
        
        # Make output directory if it doesn't exist
        os.makedirs("consolidated", exist_ok=True)
        
        csv_filename = f"nasa_contract_cancellations_{datetime.now().strftime('%Y-%m-%d')}.csv"
        
        csv_filename = os.path.join("consolidated", csv_filename)
        # Save the DataFrame to a CSV file
        df.to_csv(csv_filename, index=False)
        
        print(f"CSV saved at {csv_filename}")
        
    def _add_source_awards(self, source_name: str, source_award_ids: List[str], awards: List[Award]):
        """
        Adds awards from a specific source to the cancellations dictionary.

        Args:
            source_name (str): The name of the source (e.g., "DOGE", "NPDV").
            source_award_ids (List[str]): A list of award IDs from the source.
            awards (List[Award]): A list of Award objects retrieved from the USASpending API.

        This method checks if each award ID from the source is already in the cancellations
        dictionary. If not, it finds the corresponding Award object in the awards list and
        adds its details to the cancellations dictionary.
        """
        for award_id in source_award_ids:
            # Check if the award_id is already in the cancellations dictionary
            # If it is, we skip to the next award_id
            if award_id in self.unique_cancellations:
                continue
            # Find the relevant award award_id is in the awards list
            for award in awards:
                if award.award_identifier == award_id:
                    
                    # Search the relevant source dataframe for the original description
                    # and add it to the award object
                    source_df = self.sources_cancellation_data[source_name]
                    # Get the original description from the source dataframe
                    # We use .loc to find the row where the award_id matches
                    # and then get the original description
                    original_description = source_df.loc[source_df["Award ID"] == award_id, "description"].values[0]
                    
                    self.unique_cancellations[award_id] = [
                        source_name,
                        award.recipient.location.district,
                        award.recipient.name,
                        award_id,
                        award.transactions[0].modification_number if award.transactions else "",
                        award.period_of_performance.last_modified_date,
                        award.period_of_performance.start_date,
                        award.period_of_performance.end_date,
                        award.award_amount,
                        award.total_outlay,
                        (original_description or award.description),
                        ", ".join(award.recipient.business_types),
                        award.usa_spending_url
                    ]
                    break

if __name__ == "__main__":
    search = Search()
    search.search()