# NASA Cancellations Tracking

Tracks NASA contract and grant **terminations for convenience** since 2025-01-20, plus the
[DOGE](https://doge.gov/savings) claims about NASA awards and suspicious period-of-performance
pull-backs.

## Outputs

Each run derives CSVs in `output/` — no accumulated state; `git diff` is the change log.

| File                                                             | Contents                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| ---------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `output/terminations.csv`                                        | One row per terminated award: identity, the terminating transaction (date, F/N code, mod, amount, description), the award's current USASpending summary (`award_description`), explicit type code, recipient/place-of-performance locations and congressional districts, total obligated and potential value, how it was detected (`action_code` / `description` / `both`), which door(s) found it (`api` / `mirror`), and any human override status. |
| `output/descoped.csv`                                            | Same schema as `terminations.csv`, for awards that were pulled back rather than ended: NASA de-scoped part of the work and the award lives on. A row lands here when a human marked it `descoped` in `verification/dropped_award_status.csv`, or when its detection basis is de-scope language and its transaction carries no standalone FPDS `F` code.                                                                                              |
| `output/doge_claims.csv`                                         | Every NASA claim on DOGE's wall of receipts, beside what USASpending factually says about the same award: found or not, explicit termination transaction or not, latest action, current obligation and end date, type code, recipient/POP locations and districts, potential value, and whether a later transaction vacated the termination. No verdicts.                                                                                             |
| `output/pop_changes.csv`                                         | A lead sheet, not a termination list: awards whose period of performance was pulled back more than 90 days — original, longest, and current end dates per award, plus the award's current USASpending summary, type code, locations/districts, and amounts. Mirror-only.                                                                                                                                                                              |
| `output/cancellations_for_convenience_awards_by_fiscal_year.csv` | Distinct NASA awards carrying cancellation signals per fiscal year since FY2010: awards with an FPDS `F` action-code match, awards with a keyword match from the tracker's shared termination-for-convenience vocabulary across FPDS and FABS, and their deduplicated union. Years with no matches are zero-filled. Mirror-only, refreshed by `nasatrack mirror`; the current fiscal year reflects the mirror's partial snapshot.                     |

The fiscal-year counts are signal-bearing award counts, not adjudicated cancellation events:
unlike `terminations.csv`, they do not clear later reversals/vacaturs or apply human overrides,
so their union should not be expected to equal the row count in `output/terminations.csv`. They
ask whether an award carried any qualifying signal during each full fiscal year; FY2025 therefore
includes October 1, 2024 through January 19, 2025, before the main tracker's January 20 start,
and the same award can appear in more than one fiscal year if it receives another signal later.
`terminations.csv` instead publishes one current operative termination per award: a later
reversal or vacatur clears the earlier signal, a later re-termination replaces the earlier
transaction as the award's anchor, and human exclusions are applied. It also merges the newer API
part with the monthly mirror part, whereas the fiscal-year counts use only the local mirror. Use
the fiscal-year counts to measure the broad incidence of cancellation signals and
`terminations.csv` for the current adjudicated list; align action dates, fiscal years, and source
snapshot before reconciling them.

## The two doors

Both doors normalize into one `Txn` shape and run the **same** Python acceptance logic
(`src/nasatrack/criteria.py`); their queries are only prefilters.

- **USASpending API** (`nasatrack api`) — runs daily in CI. A keyword sweep over
  contracts, IDVs, and grants, plus an F/N action-code sweep over a rolling window
  (`--lookback-days`, default 120).
- **Local USASpending mirror** (`nasatrack mirror`) — a full Postgres copy of the
  USASpending dump on the operator's LAN, run ~monthly. Covers the whole window with a
  WHERE clause on action codes and a description regex, and produces `pop_changes.csv`
  and the fiscal-year cancellation counts.

Each door writes a part file under `output/parts/`; `nasatrack merge` unions them by award,
so mirror-only awards never vanish between local runs. CI commits only its own part. The merge
routes an award to `descoped.csv` instead of `terminations.csv` when a human marked it
`descoped` or its detection basis is de-scope language without a standalone FPDS `F` code.

## Methodology (short form)

An award counts as terminated for convenience only on **explicit** evidence: an FPDS `F`
action code, or explicit terminate-for-convenience language in a transaction description
(including NASA's observed misspellings). An `N` ("legal contract cancellation") code counts
only when the description also carries termination language — NASA applies `N` to routine
admin actions. Terminations for cause/default are excluded. An award whose later
transactions rescind, reinstate, or vacate the termination is dropped. Human adjudications
in `verification/dropped_award_status.csv` (read-only, human-owned) can exclude or annotate
rows, never add them.

## Running

```sh
uv sync --group dev     # or: just sync
just --list             # lint / test / api / mirror / doge / merge / all
uv run nasatrack daily  # what CI runs: doge + api + merge
```

The mirror needs DB credentials in `.env`; it exits cleanly with a notice when unreachable.

## Frozen archive

`consolidated/`, `data/`, and the machine-written files in `verification/` are the previous
system's history (April 2025 – August 2026). They are kept for reference and citation but are
no longer read or written by any code.
