"""Command-line entry point: api | mirror | doge | merge | daily subcommands.

Each subcommand is one door plus the deterministic merge that publishes:

    api    fetch -> parts/api_terminations.csv    -> merge   (daily, in CI)
    mirror fetch -> parts/mirror_terminations.csv -> merge   (local, ~monthly)
           (also writes pop_changes.csv and the fiscal-year counts)
    doge   fetch -> doge_claims.csv                          (independent)
    merge  parts + human overrides -> terminations.csv       (no network)
    daily  doge, then api (which ends in merge)              (what CI runs)

`daily` never touches the mirror: CI has no route to the mirror host, and the
committed mirror part is what keeps mirror-only awards from vanishing on a CI
run. Paths are fixed constants relative to the repo root - the pipeline has one
output layout and nothing here is configurable.

stdout carries one summary line per step and nothing else; notices, warnings
and the mirror's staleness line go to stderr.
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from usaspending import USASpendingClient

from nasatrack import api, doge, mirror, terminations
from nasatrack.schema import DogeClaimRow, TerminationRow, read_csv, write_csv

OUTPUT_DIR = Path("output")
PARTS_DIR = OUTPUT_DIR / "parts"

TERMINATIONS_CSV = OUTPUT_DIR / "terminations.csv"
DOGE_CSV = OUTPUT_DIR / "doge_claims.csv"
POP_CHANGES_CSV = OUTPUT_DIR / "pop_changes.csv"
FISCAL_YEAR_CSV = OUTPUT_DIR / "cancellations_for_convenience_awards_by_fiscal_year.csv"
API_PART = PARTS_DIR / "api_terminations.csv"
MIRROR_PART = PARTS_DIR / "mirror_terminations.csv"
MIRROR_RUN = PARTS_DIR / "mirror_run.json"

# Human-owned, read-only to this program.
OVERRIDES_CSV = Path("verification") / "dropped_award_status.csv"


# ---------------------------------------------------------------------------
# The merge step, which the fetch subcommands end in
# ---------------------------------------------------------------------------


def _mirror_staleness(mirror_run) -> str:
    """How old the committed mirror part is, for the run log."""
    mirror_run = Path(mirror_run)
    if not mirror_run.exists():
        return "no mirror part yet"
    run = json.loads(mirror_run.read_text(encoding="utf-8"))
    return f"mirror part from {run.get('ran_at', 'unknown')}, {run.get('rows', 0)} rows"


def merge_step(
    *,
    api_part=API_PART,
    mirror_part=MIRROR_PART,
    mirror_run=MIRROR_RUN,
    overrides=OVERRIDES_CSV,
    output=TERMINATIONS_CSV,
) -> None:
    """Publish terminations.csv from the two parts and the human overrides."""
    api_rows = read_csv(api_part, TerminationRow)
    mirror_rows = read_csv(mirror_part, TerminationRow)
    rows, warnings = terminations.apply_overrides(
        terminations.merge(api_rows, mirror_rows), terminations.load_overrides(overrides)
    )
    # The same tripwire the fetch doors carry: the published file is the point
    # of this program, and every part it is built from only ever gains rows.
    # Zero is a broken read of the parts, not an empty week.
    if not rows:
        raise SystemExit(
            "merge produced 0 rows - refusing to publish an empty terminations.csv; "
            "the part files or the override file look wrong"
        )
    write_csv(output, rows)

    for warning in warnings:
        print(warning, file=sys.stderr)
    print(_mirror_staleness(mirror_run), file=sys.stderr)
    both = sum(1 for row in rows if terminations.source_count(row) > 1)
    print(
        f"{Path(output).name}: {len(rows)} rows "
        f"(api {len(api_rows)}, mirror {len(mirror_rows)}, both {both})"
    )


def _write(path, rows, *, order=False) -> int:
    """Write one output file and log it. Returns the row count.

    Every non-merge write goes through here, so the run log's one-line-per-file
    format lives in exactly one place. `order` applies the published termination
    sort, which the part files need and the other outputs arrive already sorted.
    """
    rows = terminations.order(rows) if order else list(rows)
    write_csv(path, rows)
    print(f"{Path(path).name}: {len(rows)} rows")
    return len(rows)


def _part_rows(txns):
    """One door's accepted terminations as published rows."""
    return [terminations.txn_to_row(txn) for txn in txns]


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def run_api(args, client=None) -> None:
    """USAspending API door, then publish.

    An empty result is treated as a broken run, not as news: around 170 accepted
    awards stand in the window and the door only ever gains rows, so zero means
    the sweeps stopped matching - a renamed field or a filter that silently
    stopped filtering. The old pipeline's FPDS source died in exactly this
    shape, quietly publishing nothing.
    """
    txns = api.fetch_terminations(client, lookback_days=args.lookback_days)
    if not txns:
        raise SystemExit(
            "API door returned 0 awards - refusing to publish an empty part; "
            "a field rename or filter break upstream looks exactly like this"
        )
    _write(API_PART, _part_rows(txns), order=True)
    merge_step()


def run_mirror(args) -> None:
    """Local Postgres mirror door, then publish. A missing mirror is not a failure.

    The mirror is LAN-only and optional, so a machine without it exits clean:
    the committed part file stays exactly as it was.
    """
    if not mirror.is_configured():
        print("mirror not configured on this machine; skipping", file=sys.stderr)
        return
    # Every query the published merge depends on runs before ANY file is
    # written. The POP query takes minutes and can time out; writing the part
    # first would leave a rewritten part file, a fresh sidecar and a
    # terminations.csv that was never re-merged - the published file out of
    # sync with the part it claims to come from. A failure here leaves the
    # tree exactly as it was.
    try:
        terminated = _part_rows(mirror.fetch_terminated_awards())
        pop_changes = mirror.fetch_pop_changes()
    except mirror.LocalMirrorUnavailableError as exc:
        print(f"mirror unavailable, skipping: {exc}", file=sys.stderr)
        return
    count = _write(MIRROR_PART, terminated, order=True)
    MIRROR_RUN.write_text(
        json.dumps({"ran_at": datetime.now(UTC).isoformat(), "rows": count}) + "\n",
        encoding="utf-8",
    )
    _write(POP_CHANGES_CSV, pop_changes)
    merge_step()
    # The fiscal-year counts are mirror-only and never merged, so they sit
    # outside the sync block above: fetched last, a failure here cannot throw
    # away the POP query's expensive result. No empty tripwire - the query's
    # generate_series spine cannot return zero rows.
    _write(FISCAL_YEAR_CSV, mirror.fetch_cancellations_for_convenience_awards_by_fy())


def run_doge(args, client=None) -> None:
    """DOGE's wall of receipts plus factual USASpending status. Never merged.

    The file this writes is also the cache it reads: rows already enriched and
    still fresh are carried over rather than re-fetched.

    An empty result refuses to publish, like the API door's: DOGE's wall only
    grows, ~112 NASA claims stand on it, and an empty NASA set means the feed's
    shape or its agency spelling changed under us. Writing it would blank the
    committed file - and with it the cache the next run reads.
    """
    existing = read_csv(DOGE_CSV, DogeClaimRow)
    rows = doge.enrich(
        doge.fetch_claims(), client, refresh_days=args.refresh_days, existing=existing
    )
    if not rows:
        raise SystemExit(
            "DOGE returned 0 NASA claims - refusing to blank doge_claims.csv; "
            "a feed schema or agency-name change upstream looks exactly like this"
        )
    _write(DOGE_CSV, rows)


def run_merge(args) -> None:
    """Republish from the committed parts alone - no fetch, no network."""
    merge_step()


def run_daily(args) -> None:
    """What CI runs: DOGE, then the API door, which ends in the merge.

    Both steps talk to the same host, so they share one client - and with it one
    connection pool, one rate limiter and one retry budget for the whole run.
    """
    with USASpendingClient() as client:
        run_doge(args, client)
        run_api(args, client)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nasatrack", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    # Shared option groups: `daily` takes both, and each default is written once
    # rather than repeated per subcommand where the two could drift apart.
    lookback = argparse.ArgumentParser(add_help=False)
    lookback.add_argument("--lookback-days", type=int, default=120)
    refresh = argparse.ArgumentParser(add_help=False)
    refresh.add_argument("--refresh-days", type=int, default=14)

    sub.add_parser("api", parents=[lookback], help="USAspending API door, then merge").set_defaults(
        func=run_api
    )

    sub.add_parser(
        "mirror", help="local mirror door + pop changes + fiscal-year counts, then merge"
    ).set_defaults(func=run_mirror)

    sub.add_parser(
        "doge", parents=[refresh], help="DOGE claims with USASpending status"
    ).set_defaults(func=run_doge)

    sub.add_parser("merge", help="republish terminations.csv from the parts").set_defaults(
        func=run_merge
    )

    sub.add_parser(
        "daily", parents=[lookback, refresh], help="doge, then api (which ends in merge)"
    ).set_defaults(func=run_daily)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0
