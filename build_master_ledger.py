#!/usr/bin/env python3
"""
Build the append-only master ledger of NASA award cancellations from the
full history of daily consolidated snapshots.

Rationale: each daily snapshot in consolidated/ reflects only what upstream
queries return *that day*. Awards silently vanish when (a) a source breaks
(FPDS ezsearch retired 2026-02-25), (b) a closeout modification supersedes
the termination language the NPDV query keys on, or (c) a termination is
rescinded/vacated. The ledger unions every award ever observed, preserving
first/last-seen dates and classifying why entries left the daily snapshots.

Usage:
    python build_master_ledger.py            # full rebuild from all snapshots
    python build_master_ledger.py --update   # incremental: merge latest snapshot only

Output: consolidated/master_ledger.csv

Status values (this list is the canonical definition; docs should point here
rather than restate it).

Assigned by classify():
    listed          - present in the most recent daily snapshot
    source_retired  - dropped because the FPDS ezsearch source was retired;
                      award was still terminated as of last observation
    reinstated      - stop-work/termination rescinded (award active again)
    vacated         - termination vacated/set aside by court order
    excluded_by_design - removed by a deliberate methodology change, e.g. a
                      termination for cause, or an award terminated before the
                      2025-01-20 tracking window. Note that rows from the
                      reverted 2026-01-08 grants experiment are dropped at
                      ingest instead, so they never reach the ledger at all.
    dropped_pending_review - disappeared for an unverified reason; requires
                      manual or API verification (see verification/)

Supplied by verification/dropped_award_status.csv, which overrides classify()
and carries the evidence for each call:
    still_terminated - termination stands; the source query stopped matching
                      because a later mod replaced the termination language
    closed_out      - a closeout/deobligation mod superseded the termination
                      mod; award remains terminated
    descoped        - partial de-scope or stop-work short of full termination
    continued       - award resumed/received new obligations after the flag
    needs_manual_review - insufficient evidence to classify
"""

import argparse
import csv
import glob
import os
import re
from collections import Counter

from contract_query import load_snapshot
from termination_vocabulary import is_cause, is_reversal, is_vacatur
from utils import canonical_usaspending_url

CONSOLIDATED_DIR = "consolidated"
LEDGER_PATH = os.path.join(CONSOLIDATED_DIR, "master_ledger.csv")

# Human-curated, evidence-backed statuses. Owned by a person; no automation in
# this repo ever writes to it, and it wins every precedence contest.
VERIFICATION_PATH = os.path.join("verification", "dropped_award_status.csv")

# Machine-written screening verdicts from reverify_awards.py.
AUTO_VERIFICATION_PATH = os.path.join("verification", "auto_verification.csv")

# Auto verdicts allowed to set a ledger Status, and then only at high
# confidence. Everything else is recorded in the Auto Status column only.
AUTO_APPLICABLE = {
    "closed_out",
    "reinstated",
    "vacated",
    "descoped",
    "continued",
}

# Verdicts deliberately withheld from the ledger, and why. Every status
# reverify_awards can emit must appear in exactly one of these two sets - see
# tests/test_verification_precedence.py - so adding a verdict forces a decision
# rather than silently defaulting to unusable.
AUTO_NOT_APPLICABLE = {
    # Lookup failures. A failure is never a verdict.
    "unresolved": "lookup failed; retried next run",
    # These rest on the ABSENCE of contrary evidence rather than on positive
    # evidence, so they are only ever emitted at low confidence and must not
    # promote an award's status. Re-verification's value is in contradicting a
    # termination, not in affirming one.
    "still_terminated": "absence of contrary evidence, not positive evidence",
    "naturally_expired": "absence of a termination signal",
    "no_termination_signal": "absence of a termination signal",
    "needs_manual_review": "ambiguous by construction",
    # A methodology decision (commit 08a52cf), not a data observation: whether
    # termination-for-cause counts as a cancellation is a human call, so the
    # machine records it without acting on it.
    "excluded_by_design": "methodology decision reserved to a person",
}

# For an award carrying an external claim, an auto verdict may only move it
# within this set. A claim is the fact being tracked, so automation may refine
# how a claimed award is described but may never prune it from the ledger.
AUTO_APPLICABLE_CLAIMED = {"closed_out"}

FPDS_LAST_GOOD_DATE = "2026-02-24"  # last snapshot before ezsearch retirement

# The 2026-01-08 "Cancelled grants" experiment (commit 8a7533b) briefly treated
# a NASA grant-status of "Cancelled" as a cancellation. It was reverted the
# next day (da9d34a) because the flag proved unreliable. Its rows are ignored
# at ingest rather than recorded and reclassified: they were never observations
# of a real cancellation, so admitting them would mean carrying 66 phantom
# awards in an artifact whose whole purpose is to be citable.
#
# Only that source on that date is skipped. The 23 awards NASAGrants flagged
# that day which also appear on other dates keep their genuine observations.
EXPERIMENT_DATE = "2026-01-08"
EXPERIMENT_SOURCE = "NASAGrants"

LEDGER_COLUMNS = [
    "Award ID",
    "Recipient",
    "District",
    "Sources",
    "First Seen",
    "Last Seen",
    "Status",
    "Status Detail",
    "Latest Modification Number",
    "Latest Modification Date",
    "Start Date",
    "End Date",
    "Award Amount",
    "Total Outlays",
    "Description",
    "Business Categories",
    "URL",
    "Claiming Source",
    "Claimed Status",
    "Claimed Savings",
    "Claim Date",
    "Claim Revisions",
    "First Award Amount",
    "Transaction Baseline Amount",
    "First End Date",
    "Amount Trend",
    "End Date Trend",
    "Claim Divergence",
    "Auto Status",
    "Auto Verified Date",
]

# Earliest observed value of a field that REFRESHED_COLUMNS keeps current.
# Storing both ends lets the trend columns be derived on the incremental path,
# which only ever sees one snapshot and so cannot replay history.
# Maps the write-once column -> the refreshed column it snapshots.
FIRST_VALUE_COLUMNS = {
    "First Award Amount": "Award Amount",
    "First End Date": "End Date",
}

# Fraction an award's value must move before it counts as grown/shrunk.
TREND_THRESHOLD = 0.05

# Write-once fields. Unlike REFRESHED_COLUMNS these are never updated after
# first capture: a claim is a historical assertion, and on days when a
# different source wins an award's row the claim fields arrive blank. Letting
# them refresh would erase the claim from the published ledger.
STICKY_COLUMNS = (
    "Claiming Source",
    "Claimed Status",
    "Claimed Savings",
    "Claim Date",
)

# Claim fields a source can meaningfully restate (the claimant itself cannot).
REVISABLE_COLUMNS = ("Claimed Status", "Claimed Savings", "Claim Date")

# Fields refreshed from the newest observation of an award. A blank value never
# clobbers a populated one (the USAspending API stopped returning
# Latest Modification Date on 2026-04-08).
REFRESHED_COLUMNS = (
    "Recipient",
    "District",
    "Latest Modification Number",
    "Latest Modification Date",
    "Start Date",
    "End Date",
    "Award Amount",
    "Total Outlays",
    "Description",
    "Business Categories",
    "URL",
)


def snapshot_files():
    """All daily snapshot CSVs, sorted chronologically, keyed by ISO date."""
    files = []
    for path in glob.glob(os.path.join(CONSOLIDATED_DIR, "*.csv")):
        name = os.path.basename(path)
        if name == os.path.basename(LEDGER_PATH):
            continue
        m = re.search(r"(\d{4})-?(\d{2})-?(\d{2})", name)
        if not m:
            continue
        files.append(("%s-%s-%s" % m.groups(), path))
    return sorted(files)


def normalize_savings(value):
    """Canonical 2-decimal string, so "82838" and "82838.00" compare equal.

    DOGE's serialization changed on 2025-05-23 (integers gained ".00");
    without normalizing, that reads as 33 awards restating their savings.
    """
    number = _amount(value)
    if number is not None:
        return f"{number:.2f}"
    return _strip_money(value)


# The claim preamble doge_search prepends to an award's own description.
# Stripped from the ledger only because its content is fully preserved in the
# Claimed Status / Claimed Savings / Claim Date columns, so removing it loses
# nothing. The archived snapshots keep the original prose either way.
#
# Other sources' preambles are deliberately left alone. nasa_grants_query's
# "<case_state> - <pr_task>." carries the *reason* a grant was flagged
# ("Administrative - Change Pop End Date, Administrative - Decrease"), and no
# column holds that - stripping it would delete evidence rather than tidy it.
#
# Both patterns are anchored on the exact wording DOGE emits. A looser
# "<words> - <text>. " rule looks tempting and is wrong: it eats real content,
# e.g. NNM16AA08C's "STOP WORK NOTICE ISSUED WITH NOTIFICATION OF INTENT TO
# DESCOPE - LUCY IS A PLANNED NASA SPACE PROBE..." would lose the termination
# evidence and keep the trailing sentence.
CLAIM_PREFIXES = (
    # doge_search, contracts
    re.compile(
        r"^Status:\s*[^.]*\.\s*Reported savings:\s*\$[\d,.]+\.\s*"
        r"DOGE Action Date:\s*[\d/\-]+\.\s*"
    ),
    # doge_search, grants (no Status: segment)
    re.compile(r"^DOGE Action Date:\s*[\d/\-]+\.\s*Reported savings:\s*\$[\d,.]+\.\s*"),
)


def strip_claim_prefix(desc):
    """Drop DOGE's claim preamble, leaving the award's own description.

    Idempotent: re-running over an already-stripped value is a no-op, which
    matters because the incremental path reads the ledger back and rewrites it.
    """
    text = desc or ""
    for pattern in CLAIM_PREFIXES:
        stripped = pattern.sub("", text, count=1)
        if stripped != text:
            return stripped.strip()
    return text


def parse_claim_from_description(desc):
    """Recover DOGE claim fields from the pre-2026-07 description prose.

    Snapshots written before claim fields became real columns embedded the
    claim in the Description string, in one of two stable shapes:

        contracts: "Status: {s}. Reported savings: ${n}. DOGE Action Date: {d}. ..."
        grants:    "DOGE Action Date: {d}. Reported savings: ${n}. ..."

    Returns a dict of claim fields, or {} when the text isn't a DOGE claim.
    Used only as a fallback for rows that predate the columns, so it
    self-limits to historical snapshots.
    """
    if not desc or "DOGE Action Date:" not in desc:
        return {}
    status = re.match(r"Status:\s*([^.]*)\.", desc)
    savings = re.search(r"Reported savings:\s*\$([\d,]+(?:\.\d+)?)", desc)
    claim_date = re.search(r"DOGE Action Date:\s*([\d/\-]+)", desc)
    return {
        "Claiming Source": "DOGE",
        "Claimed Status": status.group(1).strip() if status else "",
        "Claimed Savings": normalize_savings(savings.group(1)) if savings else "",
        "Claim Date": claim_date.group(1).strip() if claim_date else "",
    }


def claim_fields(row):
    """Claim fields for a snapshot row, falling back to the description prose."""
    if row.get("Claiming Source") or row.get("Claimed Status"):
        fields = {col: row.get(col, "") for col in STICKY_COLUMNS}
        fields["Claimed Savings"] = normalize_savings(fields.get("Claimed Savings"))
        return fields
    return parse_claim_from_description(row.get("Description", ""))


def _strip_money(value):
    """The currency decoration off a money field, leaving the bare number text.

    The single place that knows what a money string looks like; _amount parses
    on top of it and normalize_savings falls back to it.
    """
    return str(value or "").replace("$", "").replace(",", "").strip()


def _amount(value):
    """Parse a money field to a float, or None when it isn't a number."""
    text = _strip_money(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def derive_trends(rec):
    """Fill the outcome columns from the first/latest values already stored.

    Recomputed on every build rather than accumulated, so the incremental and
    full-rebuild paths always agree. Answers "what actually happened to this
    award since we started tracking it", independent of whether a source still
    flags it.
    """
    observed_first, latest = (
        _amount(rec.get("First Award Amount")),
        _amount(rec.get("Award Amount")),
    )
    # A missing or zero first observation falls back to the transaction-derived
    # baseline; a baseline that is itself missing or zero lands in the "unknown"
    # branch below, where percentage change from zero is undefined anyway.
    first = observed_first or _amount(rec.get("Transaction Baseline Amount"))
    if first is None or latest is None or first == 0:
        rec["Amount Trend"] = "unknown"
    elif latest > first * (1 + TREND_THRESHOLD):
        rec["Amount Trend"] = "grew"
    elif latest < first * (1 - TREND_THRESHOLD):
        rec["Amount Trend"] = "shrank"
    else:
        rec["Amount Trend"] = "flat"

    first_end, latest_end = rec.get("First End Date", ""), rec.get("End Date", "")
    if not first_end or not latest_end:
        rec["End Date Trend"] = "unknown"
    elif latest_end > first_end:
        rec["End Date Trend"] = "extended"
    elif latest_end < first_end:
        rec["End Date Trend"] = "truncated"
    else:
        rec["End Date Trend"] = "unchanged"

    # Claim divergence is only meaningful where somebody actually claimed a
    # cancellation. It is a comparison, not a judgement: a claimed award that
    # grew is retained and reported, never pruned.
    if not rec.get("Claiming Source"):
        rec["Claim Divergence"] = ""
    elif rec["Amount Trend"] == "grew":
        rec["Claim Divergence"] = "claimed_but_grew"
    elif rec["End Date Trend"] == "extended":
        rec["Claim Divergence"] = "claimed_but_extended"
    elif rec["Amount Trend"] == "shrank":
        rec["Claim Divergence"] = "claimed_and_shrank"
    elif rec["Amount Trend"] == "unknown":
        rec["Claim Divergence"] = "unknown"
    else:
        rec["Claim Divergence"] = "consistent"


def parse_claim_revisions(text):
    """Newest value of each field recorded in a Claim Revisions string.

    Only needed to reseed state on the incremental path, where the ledger is
    read back from disk and the in-memory history of a build is gone. Entries
    are "YYYY-MM-DD Column Name=value", joined by "; ".
    """
    latest = {}
    for entry in (text or "").split("; "):
        _, _, remainder = entry.partition(" ")
        col, sep, value = remainder.partition("=")
        if sep and col in REVISABLE_COLUMNS:
            latest[col] = value
    return latest


def record_claim(rec, claim, date_str, latest):
    """Capture a claim write-once, logging any later restatement.

    The four claim columns hold the *original* assertion and are never
    overwritten. When a source restates a claim (26 awards did, mostly a
    revised status), the change is appended to Claim Revisions as
    "YYYY-MM-DD field=new value" so both the original and the revision
    survive in one artifact.

    `latest` is this award's most recently seen claim values, carried by the
    caller across snapshots. Tracking it explicitly avoids re-parsing the
    Claim Revisions string we just wrote - a value containing a semicolon
    would truncate on the way back out.
    """
    if not rec.get("Claiming Source"):
        for col in STICKY_COLUMNS:
            rec[col] = claim.get(col, "")
        latest.update({col: claim.get(col, "") for col in REVISABLE_COLUMNS})
        return

    changes = []
    for col in REVISABLE_COLUMNS:
        new = claim.get(col, "")
        # Compare against the newest value seen so far, not the original, so a
        # field that flaps doesn't append a duplicate entry on every snapshot.
        if new and new != latest.get(col, rec.get(col, "")):
            changes.append(f"{date_str} {col}={new}")
            latest[col] = new
    if changes:
        existing = rec.get("Claim Revisions", "")
        rec["Claim Revisions"] = "; ".join(filter(None, [existing, *changes]))


def is_reverted_experiment(date_str, row):
    """True for rows produced by the reverted 2026-01-08 grants experiment."""
    return date_str == EXPERIMENT_DATE and row.get("Source") == EXPERIMENT_SOURCE


def is_degraded(rows):
    """Detect known bad-run snapshots (e.g., NPDV fetch failed entirely).

    On those days ~18 NPDV awards are misattributed to NASAGrants and ~27
    disappear; treating them as real observations corrupts source history.
    """
    sources = {r.get("Source", "") for r in rows.values()}
    return "NPDV" not in sources and len(rows) > 0


def load_auto_verification():
    """Machine screening verdicts, keyed by Award ID (raw rows)."""
    if not os.path.exists(AUTO_VERIFICATION_PATH):
        return {}
    return load_snapshot(AUTO_VERIFICATION_PATH)


def load_verification(ledger=None):
    """Verified statuses for dropped awards, as {award_id: (status, evidence)}.

    Two sources, merged so that **human curation always wins**: the auto file
    is loaded first and the hand-curated file second, overwriting it. Only
    high-confidence auto verdicts in AUTO_APPLICABLE are eligible at all, and
    an award carrying a claim is further restricted to
    AUTO_APPLICABLE_CLAIMED so automation can never prune a claimed award.
    """
    overrides = {}
    for aid, r in load_auto_verification().items():
        status = r.get("Auto Status", "")
        if r.get("Confidence") != "high" or status not in AUTO_APPLICABLE:
            continue
        claimed = (ledger or {}).get(aid, {}).get("Claiming Source")
        if claimed and status not in AUTO_APPLICABLE_CLAIMED:
            continue
        overrides[aid] = (
            status,
            f"[auto {r.get('Verified Date', '')}] {r.get('Evidence', '')}",
        )

    if os.path.exists(VERIFICATION_PATH):
        with open(VERIFICATION_PATH, encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                overrides[r["Award ID"]] = (r["Status"], r["Evidence"])
    return overrides


def classify(aid, rec, desc_history):
    """Infer a status for an award no longer present in the latest snapshot.

    Only reached when no adjudicated verdict exists - build() applies
    overrides before falling back here.
    """
    # Scan every description ever observed for this award: the upstream NPDV
    # 'latest modification' can flap between mods, so the final observation is
    # not always the most informative one.
    #
    # Tested one description at a time, never on a concatenation: is_reversal
    # requires a reversal word and a termination subject in the SAME text, and
    # joining the history first would let those pair across two unrelated
    # descriptions.
    descs = desc_history.get(aid, [])
    if any(is_vacatur(d) for d in descs):
        return (
            "vacated",
            "Termination vacated/set aside per observed description history",
        )
    if any(is_reversal(d) for d in descs):
        return (
            "reinstated",
            "Stop-work/termination rescinded per observed description history",
        )
    if any(is_cause(d) for d in descs):
        return (
            "excluded_by_design",
            "Termination for cause (contractor failure) excluded by commit 08a52cf, 2026-01-09",
        )
    # No branch for the 2026-01-08 grants experiment: those rows are dropped at
    # ingest (see EXPERIMENT_DATE), so no award can reach here because of it.
    if "FPDS" in rec["Sources"] and rec["Last Seen"] == FPDS_LAST_GOOD_DATE:
        return (
            "source_retired",
            "FPDS ezsearch retired 2026-02-25 (fpds.gov now redirects to SAM.gov); "
            "award was terminated as of last observation",
        )
    return (
        "dropped_pending_review",
        f"Disappeared after {rec['Last Seen']}; verify via USAspending transactions",
    )


def build(update_only=False):
    files = snapshot_files()
    if not files:
        raise RuntimeError("No snapshot files found in consolidated/")

    latest_date = files[-1][0]

    ledger = {}
    latest_claim = {}  # award id -> most recently seen claim values
    if update_only and os.path.exists(LEDGER_PATH):
        with open(LEDGER_PATH, encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                ledger[r["Award ID"]] = dict(r)
                # Reseed from what was already recorded, so a claim revised on
                # an earlier day is not appended again on every later run.
                latest_claim[r["Award ID"]] = parse_claim_revisions(
                    r.get("Claim Revisions", "")
                )
        files = files[-1:]

    skipped = []
    ignored_experiment = 0
    desc_history = {}
    latest = {}
    for date_str, path in files:
        snap = load_snapshot(path)
        if date_str == latest_date:
            latest = snap
        if is_degraded(snap):
            skipped.append(date_str)
            continue
        for aid, row in snap.items():
            if is_reverted_experiment(date_str, row):
                ignored_experiment += 1
                continue
            d = row.get("Description", "")
            if d:
                # dict-as-ordered-set: a full rebuild walks ~400 snapshots, and
                # a membership scan over an award's growing description list is
                # the one hot spot in that loop. classify() only iterates it.
                desc_history.setdefault(aid, {})[d] = None
            rec = ledger.get(aid)
            if rec is None:
                rec = {
                    "Award ID": aid,
                    "Sources": row.get("Source", ""),
                    "First Seen": date_str,
                    "Last Seen": date_str,
                    "Status": "",
                    "Status Detail": "",
                    # Same source of truth as the refresh branch below, so a
                    # new column can never be present on update but missing
                    # on an award's first sighting.
                    **{col: row.get(col, "") for col in REFRESHED_COLUMNS},
                    **{col: "" for col in STICKY_COLUMNS},
                    "Claim Revisions": "",
                }
                ledger[aid] = rec
            else:
                rec["Last Seen"] = max(rec["Last Seen"], date_str)
                src = row.get("Source", "")
                if src and src not in rec["Sources"].split("; "):
                    rec["Sources"] += "; " + src
                for col in REFRESHED_COLUMNS:
                    if row.get(col):
                        rec[col] = row[col]

            claim = claim_fields(row)
            if claim:
                record_claim(rec, claim, date_str, latest_claim.setdefault(aid, {}))

            # Earliest non-blank value of each tracked field, write-once.
            for target, source_col in FIRST_VALUE_COLUMNS.items():
                if row.get(source_col) and not rec.get(target):
                    rec[target] = row[source_col]

    auto = load_auto_verification()
    overrides = load_verification(ledger)

    for aid, rec in ledger.items():
        # Every award carries its machine read, including listed ones - that
        # is how a false positive still in the daily snapshot becomes visible.
        rec["Auto Status"] = auto.get(aid, {}).get("Auto Status", "")
        rec["Auto Verified Date"] = auto.get(aid, {}).get("Verified Date", "")
        rec["Transaction Baseline Amount"] = auto.get(aid, {}).get(
            "Transaction Baseline Amount", ""
        )

        if aid in latest:
            rec["Status"], rec["Status Detail"] = "listed", ""
        elif aid in overrides:
            # An adjudicated verdict always applies, even over an existing
            # one: a status assigned months ago must be able to change when a
            # termination is later vacated or rescinded.
            rec["Status"], rec["Status Detail"] = overrides[aid]
        elif rec["Status"] in ("", "listed", "dropped_pending_review"):
            rec["Status"], rec["Status Detail"] = classify(aid, rec, desc_history)

    for rec in ledger.values():
        # After the claim fields have been captured: parse_claim_from_description
        # reads the *snapshot* row, so stripping here cannot starve it, and the
        # archived snapshots keep the original prose either way.
        rec["Description"] = strip_claim_prefix(rec.get("Description"))
        rec["URL"] = canonical_usaspending_url(rec.get("URL"))
        derive_trends(rec)

    rows = sorted(
        ledger.values(), key=lambda r: (r["Recipient"].lower(), r["Award ID"])
    )
    with open(LEDGER_PATH, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LEDGER_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    counts = Counter(r["Status"] for r in rows)
    print(f"Master ledger written: {LEDGER_PATH}")
    print(f"  {len(rows)} awards total (latest snapshot {latest_date}: {len(latest)})")
    for status, n in counts.most_common():
        print(f"  {status}: {n}")
    if skipped:
        print(
            f"  Skipped {len(skipped)} degraded snapshots (NPDV source absent): {', '.join(skipped)}"
        )
    if ignored_experiment:
        print(
            f"  Ignored {ignored_experiment} rows from the reverted "
            f"{EXPERIMENT_DATE} {EXPERIMENT_SOURCE} experiment"
        )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--update",
        action="store_true",
        help="Incrementally merge only the latest snapshot. NOTE: cannot infer "
        "statuses that depend on description history (reinstated, vacated, "
        "excluded-by-cause), because it only reads one snapshot. Use a full "
        "rebuild for anything that will be published.",
    )
    args = ap.parse_args()
    build(update_only=args.update)
