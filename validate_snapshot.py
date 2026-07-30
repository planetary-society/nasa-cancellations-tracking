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

MAX_ROW_DROP = 3  # max net rows lost vs. previous snapshot before quarantine
DISAPPEARANCE_LOG = os.path.join("verification", "disappearance_log.csv")
QUARANTINE_DIR = os.path.join("consolidated", "quarantine")


def _source_counts(rows):
    return Counter(r.get("Source", "?") for r in rows.values())


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
            w.writerow(
                [
                    "Run Date",
                    "Award ID",
                    "Source",
                    "Recipient",
                    "Last Description",
                    "Review Status",
                ]
            )
        for aid in gone:
            r = old_rows[aid]
            w.writerow(
                [
                    run_date,
                    aid,
                    r.get("Source", ""),
                    r.get("Recipient", ""),
                    (r.get("Description", "") or "")[:300],
                    "pending",
                ]
            )
    return gone


def validate(new_csv_path, previous_csv_path):
    """Validate a candidate snapshot against the previous accepted one.

    Returns (ok: bool, messages: list[str]). On failure the candidate file is
    moved to consolidated/quarantine/ and must not be committed.
    """
    messages = []

    if previous_csv_path is None or not os.path.exists(previous_csv_path):
        return True, ["No previous snapshot; accepting first snapshot as baseline."]

    new_rows, old_rows = load_snapshot(new_csv_path), load_snapshot(previous_csv_path)
    new_counts, old_counts = _source_counts(new_rows), _source_counts(old_rows)

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
    drop = len(old_rows) - len(new_rows)
    if drop > MAX_ROW_DROP:
        ok = False
        messages.append(
            f"FAIL shrinkage: snapshot lost {drop} net rows "
            f"({len(old_rows)} -> {len(new_rows)}); limit is {MAX_ROW_DROP}. "
            f"Mass disappearances are almost always a source/methodology "
            f"failure, not real-world change."
        )

    # 3. Log every disappearance for review (runs even when checks pass:
    #    a single dropped award is legal but must leave a trail).
    if ok:
        gone = log_disappearances(
            new_rows, old_rows, datetime.now().strftime("%Y-%m-%d")
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
