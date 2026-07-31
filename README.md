# NASA Contract/Grant Change Monitoring Tool

## Overview

This Python project is designed to monitor potential cancellations, terminations, or significant changes (like end date changes paired with funding reductions) in NASA contracts and grants. It achieves this by querying multiple data sources, identifying potential changes based on specific criteria within each source, and then consolidating and enriching these findings with detailed, standardized information from the official USAspending.gov API.

The primary goal is to provide a consolidated list of NASA awards that warrant further investigation due to signals suggesting they might have been terminated, cancelled, or significantly modified.

## Data Sources

The tool currently integrates with the following data sources:

1.  **DOGE API (`doge_search.py`):** Queries `https://api.doge.gov/savings/contracts` and `https://api.doge.gov/savings/grants` specifically for entries where the agency is identified as NASA. It extracts Award IDs (PIID/FAIN) from linked URLs.
2.  **NPDV CSV (`npdv_query.py`):** Downloads and processes contract data from NASA's Procurement Data View from the [The Planetary Society's NASA Contracts repo]. It identifies potential terminations by searching for keywords like "termination" or "stop work" _only within the description of the latest modification_ found for each unique Award ID.
3.  **NASA Grants API (`nasa_grants_query.py`):** Queries NASA's Grant Search Form. It specifically looks for grants awarded since Jan 21, 2025, whose status indicates a cancellation, termination, or a sudden decrease in the period of performance end date.
4.  **USAspending Terminations (`usaspending_terminations_query.py`):** Uses [`usaspending-orm`](https://pypi.org/project/usaspending-orm/)'s `client.transactions.search()` in two passes: one finds NASA contract transactions containing termination/stop-work language since Jan 20, 2025, while the other recovers formal terminate-for-convenience action-code `F` transactions whose descriptions contain no termination language. Matching at the _transaction_ level keeps awards flagged after closeout modifications replace the latest-mod description. This replaces the retired FPDS source (`fpds_query.py`, kept for provenance): fpds.gov/ezsearch was shut down around Feb 25, 2026 and now redirects to SAM.gov.
5.  **Local USAspending Mirror (`local_usaspending_mirror_query.py`):** Runs direct SQL against a full local Postgres copy of the USAspending database (credentials via `.env`, loaded with `python-dotenv`). Four detection nets, all bounded to actions on or after Jan 20, 2025: FPDS termination action codes `F`/`N` (including IDV vehicle transactions), termination-language regex over FPDS **and** FABS transaction descriptions (built from the shared `termination_vocabulary` patterns), period-of-performance end dates pulled in ≥180 days by a deobligating mod, and pure grant clawbacks (≥$10K, ≥25% of the pre-clawback total, inside the period of performance). **Local-only:** on machines without the DB credentials — including GitHub Actions — it replays its most recent committed `data/usaspending_database_direct_query_*.csv` export instead, and is skipped entirely only if no export exists yet. The mirror lags the live API by roughly 2–6 weeks, so the USAspending Terminations API source above is not redundant with it: the API source is the recency net, the mirror is the depth net.
6.  **USAspending.gov enrichment:** Uses `usaspending-orm` to retrieve comprehensive award details (recipient, funding, dates, location, etc.) for the unique Award IDs flagged by the other sources.

## Core Workflow (`search.py`)

1.  **Initialize Sources:** Create instances of `DOGEQuery`, `NPDVQuery`, `NASAGrantsQuery`, `USASpendingTerminationsQuery`, and — when DB credentials or a prior export exist — `LocalUSASpendingMirrorQuery`.
2.  **Source Search:** Execute the `search()` method on each query instance. Each module applies its specific logic to find potential cancellations/changes and returns a DataFrame containing relevant Award IDs and source-specific details (like the description indicating the change).
3.  **Aggregate Award IDs:** Collect all unique Award IDs found across the different sources. A hardcoded list (`ignore_award_ids`) is used to exclude specific known IDs.
4.  **Query USAspending:** Use `client.awards.search().award_ids(...)` on the aggregated Award IDs. This fetches detailed, standardized `Award` objects.
5.  **Merge & Enrich:** Match the detailed `Award` objects obtained from USAspending back to the Award IDs found by the initial source queries.
6.  **Consolidate Results:** Create a final list containing the source that flagged the award, along with the detailed information retrieved from USAspending (recipient, dates, values, URL, etc.). The description field prioritizes the description found in the original source module (which triggered the flag) over the general USAspending description.
7.  **Export Report:** Save the consolidated, sorted list of potentially changed awards to a CSV file in the `consolidated/` directory.
8.  **Validate & Ledger:** The new snapshot must pass `validate_snapshot.py` (no source may silently return zero rows; net shrinkage > 3 rows quarantines the file; every disappeared award is logged to `verification/disappearance_log.csv`). Accepted snapshots are merged into the append-only **master ledger** (`consolidated/master_ledger.csv`), which unions every award ever observed and reclassifies rather than deletes (the status vocabulary is defined in the `build_master_ledger.py` docstring). Public reporting should cite the ledger, not the daily snapshot. See `PLAN.md` for the 2026-07 audit that motivated this design and `verification/dropped_award_status.csv` for evidence behind each classification.

## Claims vs. Outcomes

The tracker records two different kinds of fact, and the ledger keeps them apart.

A **claim** is an external assertion that an award was cancelled — currently only DOGE makes them. The claim is the thing being tracked, so it is retained permanently and is never pruned, whether or not the award turned out to be genuinely terminated. Claims are captured in `Claiming Source`, `Claimed Status`, `Claimed Savings`, and `Claim Date`. These are **write-once**: a later snapshot in which a different source flags the award cannot erase them. When a source restates a claim, the change is appended to `Claim Revisions` rather than overwriting the original assertion.

An **outcome** is what the award data actually shows happened since: `Amount Trend`, `End Date Trend`, and `Claim Divergence` (`claimed_but_grew`, `claimed_but_extended`, `claimed_and_shrank`, `consistent`). These are derived on every build from the first and latest values. When an award was first observed on a zero-dollar action, `Transaction Baseline Amount` supplies the maximum cumulative obligation from its complete USAspending transaction history without overwriting `First Award Amount`; an incomplete or nonnumeric history is marked `unknown`. A computed zero also leaves the trend `unknown` because percentage change from zero is undefined. `Initial Reported End Date` comes from the base transaction in USAspending's downloadable history, or the earliest nonblank transaction when the base row has no date; `End Date Trend` prefers it and falls back to the tracker-era `First End Date`. For IDV vehicles, both the initial and current dates prefer USAspending's `Last Date to Order` when the generic period end is absent. `claimed_but_grew` is the notable case — an award whose claimed savings is contradicted by the award subsequently growing.

Divergence is a comparison, not a judgement. A claimed award that grew is still reported.

**Detection** is the third kind of fact: why *this tracker* flagged the award, in the flagging source's own words — `Terminate-for-convenience action P00180 on 2026-05-06`, `End date truncated 893 days by mod P00001 on 2026-01-20`, `Clawback of 100% ($448,257) on 2026-01-14`, or several joined by `; ` when more than one net fired. Unlike a claim it is **refreshed**, not write-once: it describes the most recent detected action, so a later modification supersedes the earlier evidence, and a blank never clobbers a populated value. Sources that match on description text alone (NPDV) leave it empty, and snapshots archived before the column existed carry no detection at all, so it only fills in from the run that introduced it onward.

## Re-verification (`reverify_awards.py`)

The daily snapshots cannot establish whether an award is _still_ cancelled: when a closeout modification replaces the termination language, the award leaves the snapshot that same day, so the closeout is never recorded anywhere. Only the USAspending transaction history resolves this.

`reverify_awards.py` walks each award's transactions and decides whether a termination stands, was superseded by a closeout, or was reversed. The same complete history supplies `Transaction Baseline Amount` for zero-dollar first observations. It runs weekly via `.github/workflows/reverify.yml`.

Two invariants:

- `verification/dropped_award_status.csv` is **human-owned**. No automation writes it, and human verdicts win every precedence contest. Machine verdicts go to `verification/auto_verification.csv`, prefixed `[auto]` wherever they reach the ledger.
- A lookup failure is never a verdict. Failures are recorded as `unresolved` and retried; if more than 25% of a run fails, the output file is left untouched rather than half-refreshed.

Only high-confidence verdicts can set a ledger `Status`; verdicts resting on the _absence_ of evidence never can. The `Disagrees With Human` column surfaces conflicts for review without ever applying them.

```bash
python reverify_awards.py --dry-run          # show selection, make no calls
python reverify_awards.py --max-requests 30  # bounded run
python reverify_awards.py --award-id 80HQTR22F0076
```

## Basic Usage

### Running the Full Consolidation

The primary way to use the tool is to run the main orchestration script:

```bash
python search.py
```
