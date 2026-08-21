# NASA Cancellations Tracking

Tracks NASA contract and grant **terminations for convenience** since 2025-01-20, plus the
[DOGE](https://doge.gov/savings) claims about NASA awards and suspicious period-of-performance
pull-backs.

## Outputs

Each run derives three CSVs in `output/` — no accumulated state; `git diff` is the change log.

| File                      | Contents                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `output/terminations.csv` | One row per terminated award: identity, the terminating transaction (date, F/N code, mod, amount, description), the award's current USASpending summary (`award_description`), explicit type code, recipient/place-of-performance locations and congressional districts, total obligated and potential value, how it was detected (`action_code` / `description` / `both`), which door(s) found it (`api` / `mirror`), and any human override status. |
| `output/doge_claims.csv`  | Every NASA claim on DOGE's wall of receipts, beside what USASpending factually says about the same award: found or not, explicit termination transaction or not, latest action, current obligation and end date, type code, recipient/POP locations and districts, potential value. No verdicts.                                                                                                                                                      |
| `output/pop_changes.csv`  | A lead sheet, not a termination list: awards whose period of performance was pulled back more than 90 days — original, longest, and current end dates per award, plus the award's current USASpending summary, type code, locations/districts, and amounts. Mirror-only.                                                                                                                                                                              |

## The two doors

Both doors normalize into one `Txn` shape and run the **same** Python acceptance logic
(`src/nasatrack/criteria.py`); their queries are only prefilters.

- **USASpending API** (`nasatrack api`) — runs daily in CI. A keyword sweep over
  contracts, IDVs, and grants, plus an F/N action-code sweep over a rolling window
  (`--lookback-days`, default 120).
- **Local USASpending mirror** (`nasatrack mirror`) — a full Postgres copy of the
  USASpending dump on the operator's LAN, run ~monthly. Covers the whole window with a
  WHERE clause on action codes and a description regex, and produces `pop_changes.csv`.

Each door writes a part file under `output/parts/`; `nasatrack merge` unions them by award,
so mirror-only awards never vanish between local runs. CI commits only its own part.

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
