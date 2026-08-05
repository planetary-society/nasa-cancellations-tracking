#!/usr/bin/env python3
"""
Snapshot validation guards for the daily consolidation pipeline.

Every silent data loss found in the 2025-2026 audit shared one root cause:
sources fail *open* (an empty DataFrame looks identical to "no cancellations"),
and the exporter trusts whatever it gets. These checks make failures loud.

Checks (run before a new snapshot is accepted):
  1. source_presence - every source that produced rows in the previous
     snapshot must produce rows now. Catches: FPDS ezsearch retirement
     (silent from 2026-02-25), the recurring NPDV fetch failures
     (7 degraded snapshots in 2026 alone).
  2. shrinkage - net row loss vs. the previous snapshot must not exceed
     MAX_ROW_DROP. Catches mass drops from upstream query changes.
  3. disappearance_log - any award present yesterday but missing today is
     appended to verification/disappearance_log.csv with its final row,
     so nothing ever vanishes without a review trail.

A failed check quarantines the snapshot to consolidated/quarantine/ and
returns False; the caller should exit nonzero so the GitHub Action surfaces
the failure instead of committing degraded data.
"""

import csv
import os
import shutil
from collections import Counter
from datetime import datetime

from contract_query import load_snapshot
from utils import read_rows

MAX_ROW_DROP = 3  # max net rows lost vs. previous snapshot before quarantine
DISAPPEARANCE_LOG = os.path.join("verification", "disappearance_log.csv")
# This log keeps its own vocabulary on purpose. It is append-only, so adopting
# the ledger's renamed columns would either concatenate two header generations
# into one file or mean rewriting an audit trail; and "Last Description" is a
# 300-character truncation, not the snapshot column it is taken from. Declared
# here rather than inline so the divergence reads as a decision.
DISAPPEARANCE_LOG_COLUMNS = [
    "Run Date",
    "Award ID",
    "Source",
    "Recipient",
    "Last Description",
    "Review Status",
]
QUARANTINE_DIR = os.path.join("consolidated", "quarantine")
REVIEWED_REMOVALS_PATH = os.path.join("verification", "dropped_award_status.csv")


def _source_counts(rows):
    return Counter(r.get("Source", "?") for r in rows.values())


def load_reviewed_removals(path=REVIEWED_REMOVALS_PATH):
    """Awards a human explicitly approved for methodology removal."""
    if not os.path.exists(path):
        return set()
    return {
        row["Award ID"]
        for row in read_rows(path)
        if row.get("Award ID") and row.get("Tracking Status") == "excluded_by_design"
    }


def log_disappearances(new_rows, old_rows, run_date):
    """Append awards missing from the new snapshot to the review log."""
    gone = [aid for aid in old_rows if aid not in new_rows]
    if not gone:
        return []
    os.makedirs(os.path.dirname(DISAPPEARANCE_LOG), exist_ok=True)
    exists = os.path.exists(DISAPPEARANCE_LOG)
    with open(DISAPPEARANCE_LOG, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if not exists:
            w.writerow(DISAPPEARANCE_LOG_COLUMNS)
        for aid in gone:
            r = old_rows[aid]
            w.writerow(
                [
                    run_date,
                    aid,
                    r.get("Source", ""),
                    r.get("Recipient Name", ""),
                    (r.get("Award or Action Description", "") or "")[:300],
                    "pending",
                ]
            )
    return gone


def validate(
    new_csv_path,
    previous_csv_path,
    *,
    skipped_sources=(),
    reviewed_removals=None,
):
    """Validate a candidate snapshot against the previous accepted one.

    ``skipped_sources`` names explicitly optional sources that were unavailable
    before collection began (or raised their narrow availability exception).
    Their old rows are excluded from presence, shrinkage, and disappearance
    checks; all other sources retain the normal fail-loud guarantees.

    ``reviewed_removals`` is the narrow methodology escape hatch: only awards
    a human marked ``excluded_by_design`` are removed from the comparison
    baseline. Other human statuses and machine verdicts cannot suppress the
    guard. Passing a set is useful for tests; production loads the human-owned
    verification file.

    Returns (ok: bool, messages: list[str]). On failure the candidate file is
    moved to consolidated/quarantine/ and must not be committed.
    """
    messages = []

    if previous_csv_path is None or not os.path.exists(previous_csv_path):
        return True, ["No previous snapshot; accepting first snapshot as baseline."]

    new_rows, old_rows = load_snapshot(new_csv_path), load_snapshot(previous_csv_path)
    skipped_sources = set(skipped_sources)
    reviewed_removals = (
        load_reviewed_removals()
        if reviewed_removals is None
        else set(reviewed_removals)
    )
    # Every check below compares against the previous snapshot minus the rows a
    # skipped source owned, so the exclusion is applied once, here, rather than
    # re-stated as a guard inside each check.
    comparable_old_rows = {
        aid: row
        for aid, row in old_rows.items()
        if row.get("Source") not in skipped_sources and aid not in reviewed_removals
    }
    reviewed_present = sorted(set(old_rows) & reviewed_removals - set(new_rows))
    if reviewed_present:
        messages.append(
            f"NOTE: {len(reviewed_present)} reviewed excluded_by_design removal(s) "
            f"omitted from snapshot comparison: {', '.join(reviewed_present)}"
        )
    new_counts = _source_counts(new_rows)
    old_counts = _source_counts(comparable_old_rows)

    ok = True

    # 1. Source presence: a source can't silently go from N rows to zero.
    for source, n in old_counts.items():
        if new_counts.get(source, 0) == 0:
            ok = False
            messages.append(
                f"FAIL source_presence: '{source}' produced {n} rows in previous "
                f"snapshot but 0 now. Likely upstream fetch failure or retired "
                f"endpoint - investigate before trusting this run."
            )

    # 2. Shrinkage guard.
    drop = len(comparable_old_rows) - len(new_rows)
    if drop > MAX_ROW_DROP:
        ok = False
        messages.append(
            f"FAIL shrinkage: snapshot lost {drop} net rows "
            f"({len(comparable_old_rows)} comparable -> {len(new_rows)}); "
            f"limit is {MAX_ROW_DROP}. "
            f"Mass disappearances are almost always a source/methodology "
            f"failure, not real-world change."
        )

    # 3. Log every disappearance for review (runs even when checks pass:
    #    a single dropped award is legal but must leave a trail).
    if ok:
        gone = log_disappearances(
            new_rows, comparable_old_rows, datetime.now().strftime("%Y-%m-%d")
        )
        if gone:
            messages.append(
                f"NOTE: {len(gone)} award(s) disappeared vs. previous snapshot; "
                f"logged to {DISAPPEARANCE_LOG} for review: {', '.join(gone)}"
            )

    if not ok:
        os.makedirs(QUARANTINE_DIR, exist_ok=True)
        qpath = os.path.join(QUARANTINE_DIR, os.path.basename(new_csv_path))
        shutil.move(new_csv_path, qpath)
        messages.append(
            f"Snapshot quarantined to {qpath}; previous snapshot remains authoritative."
        )

    return ok, messages
