# NASA Contract/Grant Change Monitoring Tool

## Overview

This Python project is designed to monitor potential cancellations, terminations, or significant changes (like end date changes paired with funding reductions) in NASA contracts and grants. It achieves this by querying multiple data sources, identifying potential changes based on specific criteria within each source, and then consolidating and enriching these findings with detailed, standardized information from the official USAspending.gov API.

The primary goal is to provide a consolidated list of NASA awards that warrant further investigation due to signals suggesting they might have been terminated, cancelled, or significantly modified.

## Data Sources

The tool currently integrates with the following data sources:

1.  **DOGE API (`doge_search.py`):** Queries `https://api.doge.gov/savings/contracts` and `https://api.doge.gov/savings/grants` specifically for entries where the agency is identified as NASA. It extracts Award IDs (PIID/FAIN) from linked URLs.
2.  **NPDV CSV (`npdv_query.py`):** Downloads and processes contract data from NASA's Procurement Data View from the [The Planetary Society's NASA Contracts repo]. It identifies potential terminations by searching for keywords like "termination" or "stop work" _only within the description of the latest modification_ found for each unique Award ID.
3.  **NASA Grants API (`nasa_grants_query.py`):** Queries NASA's Grant Search Form for workflow labels that suggest a period-of-performance decrease. The label does not identify the prior date or the transaction that changed it, so it is only a candidate: processing keeps it when `verification/award_period_change_facts.csv` contains a qualifying transaction-level mirror fact. Unconfirmed labels are excluded and retried after a later successful mirror refresh.
4.  **USAspending Terminations (`usaspending_terminations_query.py`):** Uses [`usaspending-orm`](https://pypi.org/project/usaspending-orm/)'s `client.transactions.search()` in two passes: one finds NASA contract transactions containing termination/stop-work language since Jan 20, 2025, while the other recovers formal terminate-for-convenience action-code `F` transactions whose descriptions contain no termination language. Matching at the _transaction_ level keeps awards flagged after closeout modifications replace the latest-mod description. This replaces the retired FPDS source (`fpds_query.py`, kept for provenance): fpds.gov/ezsearch was shut down around Feb 25, 2026 and now redirects to SAM.gov.
5.  **Local USAspending Mirror (`local_usaspending_mirror_query.py`):** Runs direct SQL against a full local Postgres copy of the USAspending database (credentials via `.env`, loaded with `python-dotenv`). Four detection nets: FPDS termination action codes `F`/`N` (including IDV vehicle transactions), termination-language regex over FPDS **and** FABS transaction descriptions, suspicious period changes, and pure grant clawbacks. The period-change net orders every dated transaction, compares consecutive end dates, and records the largest backward jump per award when it is strictly greater than `AWARD_PERIOD_SHORTENING_MIN_DAYS` (default: 90), occurs after Jan 20, 2025, lands between Jan 20, 2025 and the run date, and carries a zero or negative obligation change. Later continuations do not erase that historical event. **Local-only and optional:** on machines without DB credentials — including GitHub Actions — or when the configured database cannot be reached, the source is skipped. Successful live exports are audit artifacts, not replayed as current results; successful period facts are atomically replaced in `verification/award_period_change_facts.csv`, while failures retain the prior facts.
6.  **USAspending.gov enrichment:** Uses `usaspending-orm` to retrieve comprehensive award details (recipient, funding, dates, location, etc.) for the unique Award IDs flagged by the other sources.

## Core Workflow (`search.py`)

1.  **Initialize Sources:** Create instances of `DOGEQuery`, `NPDVQuery`, `NASAGrantsQuery`, `USASpendingTerminationsQuery`, and — when live DB credentials exist — `LocalUSASpendingMirrorQuery`.
2.  **Source Search:** Execute the `search()` method on each query instance. Each module applies its specific logic to find potential cancellations/changes and returns a DataFrame containing relevant Award IDs and source-specific details (like the description indicating the change).
3.  **Aggregate Award IDs:** Collect all unique Award IDs found across the different sources. A hardcoded list (`ignore_award_ids`) is used to exclude specific known IDs.
4.  **Query USAspending:** Use `client.awards.search().award_ids(...)` on the aggregated Award IDs. This fetches detailed, standardized `Award` objects.
5.  **Merge & Enrich:** Match the detailed `Award` objects obtained from USAspending back to the Award IDs found by the initial source queries.
6.  **Consolidate Results:** Create a final list containing the source that flagged the award, along with the detailed information retrieved from USAspending (recipient, dates, values, URL, etc.). The description field prioritizes the description found in the original source module (which triggered the flag) over the general USAspending description.
7.  **Export Report:** Save the consolidated, sorted list of potentially changed awards to a CSV file in the `consolidated/` directory.
8.  **Validate & Ledger:** The new snapshot must pass `validate_snapshot.py` (no source may silently return zero rows; net shrinkage > 3 rows quarantines the file; every unexplained disappearance is logged to `verification/disappearance_log.csv`). Awards a human has explicitly marked `excluded_by_design` are omitted from the comparison baseline, allowing reviewed methodology removals without weakening the guard for any other row. Accepted snapshots are merged into the append-only **master ledger** (`consolidated/master_ledger.csv`), which unions every award ever observed and reclassifies rather than deletes (the status vocabulary is defined in the `build_master_ledger.py` docstring). Public reporting should cite the ledger, not the daily snapshot. See `PLAN.md` for the 2026-07 audit that motivated this design and `verification/dropped_award_status.csv` for evidence behind each classification.

## Initial Reported End Date

`End Date Trend` asks whether an award was **cut short**, which needs the end date the award was originally awarded with — not the first end date this tracker happened to see. An award flagged in 2026 may have been running since 2017, and comparing against our own first sighting would report every long-running award as unchanged.

Only per-transaction period-of-performance dates answer that, and **the public API publishes none on any transaction route.** Verified against the live API on 2026-07-31:

| Route                                                                      | Period-of-performance end date?                                                                              |
| -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `award.transactions` (`/api/v2/transactions/`)                             | No — returns 11 fields: action date, action type, mod number, description, obligations                       |
| `client.transactions.search()` (`/api/v2/search/spending_by_transaction/`) | No — 45 valid field names, none is a PoP end date (`Last Date to Order` is the IDV ordering-period boundary) |
| Bulk download (`spending_level=["transactions"]`)                          | Yes — but minutes of server-side work and a zip per award category                                           |

This used to drive the bulk download. **That path was removed in 2026-07: far too expensive for one column.** The mirror has the field directly in `rpt.transaction_search`, so it is now the only provider — `LocalUSASpendingMirrorQuery.fetch_initial_reported_end_dates`, one query for every pending award, reaching back to 2007-10-01 rather than the tracking window (the originally-awarded end date predates the window for any long-running award).

Two consequences, both deliberate:

- **Without mirror credentials — including on CI — no new values are resolved.** The run says so and continues. `verification/initial_reported_end_dates.csv` is committed and write-once, so every value the mirror has ever resolved stays available; the backlog is picked up on the next run with mirror access.
- **An award the mirror hasn't replicated yet is not written.** The mirror lags the live API by 2–6 weeks, so a just-flagged award can be genuinely absent. That outcome (`not_in_mirror`) is reported but never persisted — writing it to a write-once file would retire the award from lookup forever over a delay that resolves itself.

The selection rule (base transaction wins, else earliest transaction carrying any end date) lives in `initial_end_dates.py`, separate from the provider, so it is unchanged whatever reads the rows.

## The Tracking Window

Everything this tracker publishes answers one question: **was this award cancelled by the current administration?** That question has a single date bound — the second-term inauguration, **January 20, 2025** — defined once in `tracking_window.py` and imported everywhere. It used to be copied into four source modules, where it had drifted (the grants source used Jan 21, an off-by-one that dropped inauguration-day actions) and where two sources carried no bound at all.

Two gates enforce it, and the difference between them matters:

- **Action gate.** The federal action must fall on or after Jan 20, 2025. Every source declares the action date behind its detection in the shared `action_date` column, and `search.py` re-checks it at ingest. When a source genuinely cannot observe an action date (only NASAGrants, which reads NASA's grant-status system), ingest derives one from the award's latest USAspending transaction rather than waiving the gate.
- **Effect gate.** A source that _infers_ a cancellation from the shape of the data — an end date yanked backwards, money clawed back mid-performance — must also show the effect landing inside the window. An in-window mod can encode a pre-window decision: closeout paperwork routinely deobligates funds and backdates a period of performance years after the fact. Sources declare which kind of detection they made in the shared `detection_basis` column (`evidence` or `inference`); the effect gate applies only to `inference`, because for a real termination a retroactive end date is an ordinary closeout artifact.

The case that motivated the split: **80LARC17C0001** (GeoCarb, University of Oklahoma). NASA cancelled that mission in 2023. Mod P00032 on 2025-09-02 deobligated $513K and pulled the period of performance back 638 days to 2024-09-30. The action date was genuinely inside the window, so every action-date gate passed it, and a 2023 cancellation reached the published ledger. Thirteen other awards had entered the same way; all fourteen are recorded in `verification/dropped_award_status.csv` as `excluded_by_design`.

Exclusions are never silent — every row the window keeps out is printed at the end of the run with its award ID, source, and reason. A gate that quietly shrinks the snapshot is indistinguishable from a source breaking, which is the failure the fail-loud policy exists to catch.

## Claims vs. Outcomes

The tracker records two different kinds of fact, and the ledger keeps them apart.

A **claim** is an external assertion that an award was cancelled — currently only DOGE makes them. The claim is the thing being tracked, so it is retained permanently and is never pruned, whether or not the award turned out to be genuinely terminated. Claims are captured in `Claiming Source`, `Claimed Status`, `Claimed Savings`, and `Claim Date`. These are **write-once**: a later snapshot in which a different source flags the award cannot erase them. When a source restates a claim, the change is appended to `Claim Revisions` rather than overwriting the original assertion.

An **outcome** is what the award data actually shows happened since: `Amount Trend`, `End Date Trend`, and `Claim Divergence` (`claimed_but_grew`, `claimed_but_extended`, `claimed_and_shrank`, `consistent`). These are derived on every build from the first and latest values. When an award was first observed on a zero-dollar action, `Transaction Baseline Amount` supplies the maximum cumulative obligation from its complete USAspending transaction history without overwriting `First Award Amount`; an incomplete or nonnumeric history is marked `unknown`. A computed zero also leaves the trend `unknown` because percentage change from zero is undefined. `Initial Reported End Date` comes from the base transaction in the award's full history, or the earliest nonblank transaction when the base row has no date; `End Date Trend` prefers it and falls back to the tracker-era `First End Date`. It is resolved **only from the local mirror** — see [Initial Reported End Date](#initial-reported-end-date) below for why, and for what happens without mirror access. For IDV vehicles, both the initial and current dates prefer USAspending's `Last Date to Order` when the generic period end is absent. `claimed_but_grew` is the notable case — an award whose claimed savings is contradicted by the award subsequently growing.

Daily enrichment fetches each award's complete USAspending transaction history once and reuses it for every transaction-derived field. `First Action Type` and `Latest Action Type` (plus their descriptions and action dates) report the endpoints of that history. The award's latest modification is therefore reported as the pair `Latest Modification Number` and `Latest Action Date`, both read from that final transaction. An earlier `Latest Modification Date` column was removed: it was sourced from `award.period_of_performance.last_modified_date`, which USAspending defines as when the award _record_ was last updated rather than when its latest transaction occurred, and the two routinely diverged by months. `Termination Modification Number` and `Closeout Modification Number` retain the most recent formal action of each kind, together with its action date, even when a later continuation or administrative modification becomes the award's latest transaction. Contract and assistance action codes are interpreted separately because the same code can mean different things in the two systems.

Successful summaries are persisted atomically in `verification/award_transaction_facts.csv`, independently of daily snapshot acceptance. The master ledger overlays that sidecar after replaying accepted snapshots, so a quarantined candidate cannot change membership or status but cannot discard transaction enrichment either. Current awards refresh daily; ledger-only awards are backfilled once and then refreshed whenever the weekly re-verification workflow already fetches their histories. Failed or empty lookups never erase prior facts and remain eligible for retry.

Divergence is a comparison, not a judgement. A claimed award that grew is still reported.

**Detection** is the third kind of fact: why _this tracker_ flagged the award, in the flagging source's own words — `Terminate-for-convenience action P00180 on 2026-05-06`, `End date shortened 368 days from 2027-02-28 to 2026-02-25 by mod P00002 on 2026-02-25`, `Clawback of 100% ($448,257) on 2026-01-14`, or several joined by `; ` when more than one net fired. **Primary Detection Method** is its structured counterpart: `external_claim`, `description_keyword`, `pop_end_date_change`, `termination_action_code`, `termination_language`, or `obligation_clawback`. Direct termination evidence outranks inference when several nets fire. Historical snapshots are backfilled from their recorded source and detection text; where an old Local Mirror snapshot discarded the individual net, the ledger says `legacy_local_mirror_signal` rather than inventing precision. Unlike a claim, Detection and Primary Detection Method are **refreshed**, not write-once. Most nets report their latest matching action; the period-change net deliberately retains the largest qualifying historical jump even if a continuation follows. A blank never clobbers populated evidence. Sources that match on description text alone (NPDV) leave Detection empty, and snapshots archived before the column existed carry no Detection text at all.

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
