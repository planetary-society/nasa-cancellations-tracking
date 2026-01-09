# NASA Contract/Grant Change Monitoring Tool

## Overview

This Python project is designed to monitor potential cancellations, terminations, or significant changes (like end date changes paired with funding reductions) in NASA contracts and grants. It achieves this by querying multiple data sources, identifying potential changes based on specific criteria within each source, and then consolidating and enriching these findings with detailed, standardized information from the official USAspending.gov API.

The primary goal is to provide a consolidated list of NASA awards that warrant further investigation due to signals suggesting they might have been terminated, cancelled, or significantly modified.

## Data Sources

The tool currently integrates with the following data sources:

1.  **DOGE API (`doge_search.py`):** Queries `https://api.doge.gov/savings/contracts` and `https://api.doge.gov/savings/grants` specifically for entries where the agency is identified as NASA. It extracts Award IDs (PIID/FAIN) from linked URLs.
2.  **NPDV CSV (`npdv_query.py`):** Downloads and processes contract data from NASA's Procurement Data View from the [The Planetary Society's NASA Contracts repo]. It identifies potential terminations by searching for keywords like "termination" or "stop work" *only within the description of the latest modification* found for each unique Award ID.
3.  **NASA Grants API (`nasa_grants_query.py`):** Queries NASA's Grant Search Form. It specifically looks for grants awarded since Jan 21, 2025, whose status indicates a cancellation, termination, or a sudden decrease in the period of performance end date.
4.  **FPDS (`fpds_query.py`):** Queries the Federal Procurement Data System (FPDS) for NASA contracts with "Terminate for Convenience" modifications since Jan 20, 2025. Fetches detailed descriptions from HTML detail pages.
5.  **USAspending.gov API:** Uses the [`usaspending-orm`](https://pypi.org/project/usaspending-orm/) package to retrieve comprehensive details (recipient, funding, dates, location, etc.) for the unique Award IDs flagged by the other sources.

## Core Workflow (`search.py`)

1.  **Initialize Sources:** Create instances of `DOGEQuery`, `NPDVQuery`, `NASAGrantsQuery`, and `FPDSQuery`.
2.  **Source Search:** Execute the `search()` method on each query instance. Each module applies its specific logic to find potential cancellations/changes and returns a DataFrame containing relevant Award IDs and source-specific details (like the description indicating the change).
3.  **Aggregate Award IDs:** Collect all unique Award IDs found across the different sources. A hardcoded list (`ignore_award_ids`) is used to exclude specific known IDs.
4.  **Query USAspending:** Use the `USASpendingClient` to perform an `all_award_search` on the aggregated list of unique Award IDs. This fetches detailed, standardized `Award` objects.
5.  **Merge & Enrich:** Match the detailed `Award` objects obtained from USAspending back to the Award IDs found by the initial source queries.
6.  **Consolidate Results:** Create a final list containing the source that flagged the award, along with the detailed information retrieved from USAspending (recipient, dates, values, URL, etc.). The description field prioritizes the description found in the original source module (which triggered the flag) over the general USAspending description.
7.  **Export Report:** Save the consolidated, sorted list of potentially changed awards to a CSV file in the `consolidated/` directory.

## Basic Usage

### Running the Full Consolidation

The primary way to use the tool is to run the main orchestration script:

```bash
python search.py