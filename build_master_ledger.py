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
    currently_flagged - still returned by a source in the most recent daily
                      snapshot
    source_retired  - dropped because the FPDS ezsearch source was retired;
                      award was still terminated as of last observation
    reinstated      - stop-work/termination rescinded (award active again)
    vacated         - termination vacated/set aside by court order
    excluded_by_design - removed by a deliberate methodology change, e.g. a
                      termination for cause, or an award terminated before the
                      2025-01-20 tracking window. Note that rows from the
                      reverted 2026-01-08 grants experiment are dropped at
                      ingest instead, so they never reach the ledger at all -
                      as are, since 2026-07-31, all pre-window actions
                      (tracking_window.py). This status now covers only the
                      awards admitted before that gate existed; nothing new
                      should arrive here for a window reason.
    unflagged_pending_review - no source flags it any more, for a reason
                      nobody has established yet; requires
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
from datetime import date

import award_transaction_facts
import csv_aliases
import initial_end_dates
import sources
from contract_query import load_snapshot
from detection_methods import DETECTION_METHODS, infer_snapshot_method
from termination_vocabulary import is_cause, is_reversal, is_vacatur
from utils import canonical_usaspending_url, read_rows

CONSOLIDATED_DIR = "consolidated"
LEDGER_PATH = os.path.join(CONSOLIDATED_DIR, "master_ledger.csv")

# Human-curated, evidence-backed statuses. Owned by a person; no automation in
# this repo ever writes to it, and it wins every precedence contest.
VERIFICATION_PATH = os.path.join("verification", "dropped_award_status.csv")
# Declared so the three readers assert the header instead of discovering a
# hand-edit mistake as a KeyError from somewhere deep in the build. Verified
# Date here is the date a *person* recorded the verdict, unrelated to
# auto_verification.csv's column of the same name.
VERIFICATION_COLUMNS = ["Award ID", "Tracking Status", "Verified Date", "Evidence"]

# Machine-written screening verdicts from reverify_awards.py.
AUTO_VERIFICATION_PATH = os.path.join("verification", "auto_verification.csv")

# Machine-written, write-once transaction provenance for original end dates.
INITIAL_END_DATES_PATH = os.path.join("verification", "initial_reported_end_dates.csv")
INITIAL_END_DATE_COLUMNS = [
    "Award ID",
    "Generated Award ID",
    "Award Category",
    "Initial Reported End Date",
    "Source Transaction ID",
    "Source Action Date",
    "Source Modification Number",
    "Source Basis",
    "Lookup Status",
    "Last Checked Date",
]
# Imported, not restated: the producers live in initial_end_dates and the
# mirror provider, so a status added there must not be able to pass this
# validator by accident or fail it by omission.
INITIAL_END_DATE_STATUSES = initial_end_dates.INITIAL_END_DATE_STATUSES

# Auto verdicts allowed to set a ledger Status, and then only at high
# confidence. Everything else is recorded in the Automated Verdict column only.
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

# Copied onto every ledger row from auto_verification.csv, whatever the award's
# status - that is how a false positive still in the daily snapshot stays
# visible. Mirrors LEDGER_OVERLAY_COLUMNS, which does the same job for the
# transaction-facts sidecar.
AUTO_OVERLAY_COLUMNS = (
    "Automated Verdict",
    "Automated Verdict Date",
    "Peak Cumulative Obligation",
)

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
EXPERIMENT_SOURCE = sources.NASA_GRANTS

# The transaction-derived provenance group: what the award's own USAspending
# transaction history says was done to it, and when. Declared once and splatted
# into every list that carries it - snapshot order, ledger order, and the
# refreshed set - because three hand-maintained copies of the same ten names
# drift, and a name present in two of the three is silently dropped at the
# snapshot-to-ledger boundary.
TRANSACTION_HISTORY_COLUMNS = award_transaction_facts.TRANSACTION_HISTORY_COLUMNS

LEDGER_COLUMNS = [
    "Award ID",
    "Recipient Name",
    "Recipient Congressional District",
    sources.SOURCES_COLUMN,
    "First Flagged Date",
    "Last Flagged Date",
    "Tracking Status",
    "Tracking Status Detail",
    # Paired with Latest Action Date below, which is that modification's
    # action_date. There was once a separate Latest Modification Date column
    # sourced from award.period_of_performance.last_modified_date; it was
    # dropped in favour of this pairing because USAspending defines that field
    # as when the award *record* was last updated, not when its latest
    # transaction occurred. The two routinely diverged by months, so the column
    # read as a transaction date while carrying a database timestamp.
    "Latest Modification Number",
    *TRANSACTION_HISTORY_COLUMNS,
    "Start Date",
    "Current End Date",
    "Initial Reported End Date",
    "Current Obligated Amount",
    "Total Outlays",
    "Award or Action Description",
    "Primary Detection Method",
    "Detection Evidence",
    "Recipient Business Categories",
    "USAspending URL",
    "Claimed By",
    "DOGE Claimed Status",
    "DOGE Claimed Savings",
    "DOGE Claim Date",
    "DOGE Claim Revisions",
    "Obligated Amount When First Flagged",
    "Peak Cumulative Obligation",
    "End Date When First Flagged",
    "Amount Trend",
    "End Date Trend",
    "DOGE Claim vs Outcome",
    "Automated Verdict",
    "Automated Verdict Date",
]

# Earliest observed value of a field that REFRESHED_COLUMNS keeps current.
# Storing both ends lets the trend columns be derived on the incremental path,
# which only ever sees one snapshot and so cannot replay history.
# Maps the write-once column -> the refreshed column it snapshots.
FIRST_VALUE_COLUMNS = {
    "Obligated Amount When First Flagged": "Current Obligated Amount",
    "End Date When First Flagged": "Current End Date",
    # The durable sidecar is authoritative, but carrying the value in snapshots
    # also keeps an accepted daily export self-describing.
    "Initial Reported End Date": "Initial Reported End Date",
}

# Fraction an award's value must move before it counts as grown/shrunk.
TREND_THRESHOLD = 0.05

# Write-once fields. Unlike REFRESHED_COLUMNS these are never updated after
# first capture: a claim is a historical assertion, and on days when a
# different source wins an award's row the claim fields arrive blank. Letting
# them refresh would erase the claim from the published ledger.
STICKY_COLUMNS = (
    "Claimed By",
    "DOGE Claimed Status",
    "DOGE Claimed Savings",
    "DOGE Claim Date",
)

# Claim fields a source can meaningfully restate (the claimant itself cannot).
# Derived, so that a fifth claim column becomes revisable by adding it once
# rather than by remembering to add it twice.
CLAIMANT_COLUMN = "Claimed By"
REVISABLE_COLUMNS = tuple(c for c in STICKY_COLUMNS if c != CLAIMANT_COLUMN)

# Fields refreshed from the newest observation of an award. A blank value never
# clobbers a populated one, so a field the API drops for a while keeps its last
# known value rather than being erased.
#
# Detection Evidence is refreshed rather than write-once: it describes the award's most
# recent detected action, so a later mod supersedes the earlier evidence. Every
# snapshot written before 2026-07-30 lacks the column entirely, which reads as
# blank here and therefore cannot erase a value a newer snapshot supplies.
REFRESHED_COLUMNS = (
    "Recipient Name",
    "Recipient Congressional District",
    "Latest Modification Number",
    *TRANSACTION_HISTORY_COLUMNS,
    "Start Date",
    "Current End Date",
    "Current Obligated Amount",
    "Total Outlays",
    "Award or Action Description",
    "Primary Detection Method",
    "Detection Evidence",
    "Recipient Business Categories",
    "USAspending URL",
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
# DOGE Claimed Status / Savings / Claim Date columns, so removing it loses
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
    claim in the Award or Action Description string, in one of two stable shapes:

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
        "Claimed By": sources.DOGE,
        "DOGE Claimed Status": status.group(1).strip() if status else "",
        "DOGE Claimed Savings": normalize_savings(savings.group(1)) if savings else "",
        "DOGE Claim Date": claim_date.group(1).strip() if claim_date else "",
    }


def claim_fields(row):
    """Claim fields for a snapshot row, falling back to the description prose."""
    if row.get("Claimed By") or row.get("DOGE Claimed Status"):
        fields = {col: row.get(col, "") for col in STICKY_COLUMNS}
        fields["DOGE Claimed Savings"] = normalize_savings(
            fields.get("DOGE Claimed Savings")
        )
        return fields
    return parse_claim_from_description(row.get("Award or Action Description", ""))


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
        _amount(rec.get("Obligated Amount When First Flagged")),
        _amount(rec.get("Current Obligated Amount")),
    )
    # A missing or zero first observation falls back to the transaction-derived
    # baseline; a baseline that is itself missing or zero lands in the "unknown"
    # branch below, where percentage change from zero is undefined anyway.
    first = observed_first or _amount(rec.get("Peak Cumulative Obligation"))
    if first is None or latest is None or first == 0:
        rec["Amount Trend"] = "unknown"
    elif latest > first * (1 + TREND_THRESHOLD):
        rec["Amount Trend"] = "grew"
    elif latest < first * (1 - TREND_THRESHOLD):
        rec["Amount Trend"] = "shrank"
    else:
        rec["Amount Trend"] = "flat"

    # Prefer the transaction-derived date from the award's base action. The
    # legacy End Date When First Flagged remains the fallback for awards whose available
    # USAspending history carries no end date.
    tracker_era_end = rec.get("End Date When First Flagged", "")
    first_end = rec.get("Initial Reported End Date") or tracker_era_end
    latest_end = rec.get("Current End Date", "")
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
    if not rec.get("Claimed By"):
        rec["DOGE Claim vs Outcome"] = ""
    elif rec["Amount Trend"] == "grew":
        rec["DOGE Claim vs Outcome"] = "claimed_but_grew"
    elif rec["End Date Trend"] == "extended":
        rec["DOGE Claim vs Outcome"] = "claimed_but_extended"
    elif rec["Amount Trend"] == "shrank":
        rec["DOGE Claim vs Outcome"] = "claimed_and_shrank"
    elif rec["Amount Trend"] == "unknown":
        rec["DOGE Claim vs Outcome"] = "unknown"
    else:
        rec["DOGE Claim vs Outcome"] = "consistent"


def parse_claim_revisions(text):
    """Newest value of each field recorded in a DOGE Claim Revisions string.

    Only needed to reseed state on the incremental path, where the ledger is
    read back from disk and the in-memory history of a build is gone. Entries
    are "YYYY-MM-DD Column Name=value", joined by "; ".

    The entry names a *column*, so a stored value written before a column was
    renamed still says the old name - and csv_aliases only translates headers,
    not the insides of cells. Translating here too is what stops a rename from
    silently dropping an award's revision history and re-appending revisions
    that were already recorded. A full rebuild regenerates these values, so
    this matters on the incremental path, which is exactly where the in-memory
    history is gone.
    """
    latest = {}
    for entry in (text or "").split("; "):
        _, _, remainder = entry.partition(" ")
        col, sep, value = remainder.partition("=")
        col = csv_aliases.LEDGER.get(col, col)
        if sep and col in REVISABLE_COLUMNS:
            latest[col] = value
    return latest


def record_claim(rec, claim, date_str, latest):
    """Capture a claim write-once, logging any later restatement.

    The four claim columns hold the *original* assertion and are never
    overwritten. When a source restates a claim (26 awards did, mostly a
    revised status), the change is appended to DOGE Claim Revisions as
    "YYYY-MM-DD field=new value" so both the original and the revision
    survive in one artifact.

    `latest` is this award's most recently seen claim values, carried by the
    caller across snapshots. Tracking it explicitly avoids re-parsing the
    DOGE Claim Revisions string we just wrote - a value containing a semicolon
    would truncate on the way back out.
    """
    if not rec.get("Claimed By"):
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
        existing = rec.get("DOGE Claim Revisions", "")
        rec["DOGE Claim Revisions"] = "; ".join(filter(None, [existing, *changes]))


def is_reverted_experiment(date_str, row):
    """True for rows produced by the reverted 2026-01-08 grants experiment."""
    return date_str == EXPERIMENT_DATE and row.get("Source") == EXPERIMENT_SOURCE


def is_degraded(rows):
    """Detect known bad-run snapshots (e.g., NPDV fetch failed entirely).

    On those days ~18 NPDV awards are misattributed to NASAGrants and ~27
    disappear; treating them as real observations corrupts source history.
    """
    observed = {r.get("Source", "") for r in rows.values()}
    return bool(rows) and not sources.REQUIRED_SOURCES <= observed


def load_auto_verification():
    """Machine screening verdicts, keyed by Award ID (raw rows)."""
    if not os.path.exists(AUTO_VERIFICATION_PATH):
        return {}
    return load_snapshot(AUTO_VERIFICATION_PATH)


def load_initial_end_dates(path=INITIAL_END_DATES_PATH):
    """Validated Initial Reported End Date provenance, keyed by Award ID."""
    if not os.path.exists(path):
        return {}

    rows = {}
    for row in read_rows(path, columns=INITIAL_END_DATE_COLUMNS):
        aid = (row.get("Award ID") or "").strip()
        if not aid:
            raise RuntimeError(f"{path} contains a blank Award ID")
        if aid in rows:
            raise RuntimeError(f"{path} contains duplicate Award ID {aid!r}")
        status = (row.get("Lookup Status") or "").strip()
        if status not in INITIAL_END_DATE_STATUSES:
            raise RuntimeError(f"{path} has invalid Lookup Status {status!r} for {aid}")
        initial = (row.get("Initial Reported End Date") or "").strip()
        if status == "resolved" and not initial:
            raise RuntimeError(f"{path} marks {aid} resolved without a date")
        if initial:
            try:
                date.fromisoformat(initial)
            except ValueError as exc:
                raise RuntimeError(
                    f"{path} has invalid date {initial!r} for {aid}"
                ) from exc
        rows[aid] = dict(row)
    return rows


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
        status = r.get("Automated Verdict", "")
        if r.get("Confidence") != "high" or status not in AUTO_APPLICABLE:
            continue
        claimed = (ledger or {}).get(aid, {}).get("Claimed By")
        if claimed and status not in AUTO_APPLICABLE_CLAIMED:
            continue
        overrides[aid] = (
            status,
            f"[auto {r.get('Automated Verdict Date', '')}] {r.get('Evidence', '')}",
        )

    if os.path.exists(VERIFICATION_PATH):
        for r in read_rows(VERIFICATION_PATH, columns=VERIFICATION_COLUMNS):
            overrides[r["Award ID"]] = (r["Tracking Status"], r["Evidence"])
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
    if (
        sources.has_source(rec, sources.FPDS)
        and rec["Last Flagged Date"] == FPDS_LAST_GOOD_DATE
    ):
        return (
            "source_retired",
            "FPDS ezsearch retired 2026-02-25 (fpds.gov now redirects to SAM.gov); "
            "award was terminated as of last observation",
        )
    return (
        "unflagged_pending_review",
        f"Disappeared after {rec['Last Flagged Date']}; verify via USAspending transactions",
    )


def build(update_only=False):
    files = snapshot_files()
    if not files:
        raise RuntimeError("No snapshot files found in consolidated/")

    latest_date = files[-1][0]

    ledger = {}
    latest_claim = {}  # award id -> most recently seen claim values
    if update_only and os.path.exists(LEDGER_PATH):
        for r in read_rows(LEDGER_PATH):
            ledger[r["Award ID"]] = dict(r)
            # Reseed from what was already recorded, so a claim revised on
            # an earlier day is not appended again on every later run.
            latest_claim[r["Award ID"]] = parse_claim_revisions(
                r.get("DOGE Claim Revisions", "")
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
            # Snapshots written before this column existed still need a
            # structured method in the all-history ledger. Infer only what the
            # archived Source/Detection fields support; legacy Local Mirror
            # rows remain explicitly legacy rather than gaining false detail.
            row["Primary Detection Method"] = infer_snapshot_method(row)
            d = row.get("Award or Action Description", "")
            if d:
                # dict-as-ordered-set: a full rebuild walks ~400 snapshots, and
                # a membership scan over an award's growing description list is
                # the one hot spot in that loop. classify() only iterates it.
                desc_history.setdefault(aid, {})[d] = None
            rec = ledger.get(aid)
            if rec is None:
                rec = {
                    "Award ID": aid,
                    sources.SOURCES_COLUMN: row.get("Source", ""),
                    "First Flagged Date": date_str,
                    "Last Flagged Date": date_str,
                    "Tracking Status": "",
                    "Tracking Status Detail": "",
                    # Same source of truth as the refresh branch below, so a
                    # new column can never be present on update but missing
                    # on an award's first sighting.
                    **{col: row.get(col, "") for col in REFRESHED_COLUMNS},
                    **{col: "" for col in STICKY_COLUMNS},
                    "DOGE Claim Revisions": "",
                }
                ledger[aid] = rec
            else:
                rec["Last Flagged Date"] = max(rec["Last Flagged Date"], date_str)
                sources.add_source(rec, row.get("Source", ""))
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
    initial_end_dates = load_initial_end_dates()
    transaction_facts = award_transaction_facts.load_facts()
    overrides = load_verification(ledger)

    for aid, rec in ledger.items():
        if not rec.get("Primary Detection Method"):
            rec["Primary Detection Method"] = infer_snapshot_method(rec)
        # Every award carries its machine read, including listed ones - that
        # is how a false positive still in the daily snapshot becomes visible.
        verdict = auto.get(aid, {})
        for column in AUTO_OVERLAY_COLUMNS:
            rec[column] = verdict.get(column, "")
        # Resolved values are write-once on the incremental path. A blank
        # ledger field can be backfilled later, while a missing/blank sidecar
        # value can never erase one already recorded.
        if not rec.get("Initial Reported End Date"):
            rec["Initial Reported End Date"] = initial_end_dates.get(aid, {}).get(
                "Initial Reported End Date", ""
            )
        # Transaction facts are independent of daily snapshot acceptance. A
        # successful complete-history lookup is authoritative for this group,
        # including legitimate blanks where USAspending supplies no action
        # code or the history contains no formal termination/closeout.
        facts = transaction_facts.get(aid)
        if facts:
            for column in award_transaction_facts.LEDGER_OVERLAY_COLUMNS:
                rec[column] = facts.get(column, "")

        if aid in latest:
            rec["Tracking Status"], rec["Tracking Status Detail"] = (
                "currently_flagged",
                "",
            )
        elif aid in overrides:
            # An adjudicated verdict always applies, even over an existing
            # one: a status assigned months ago must be able to change when a
            # termination is later vacated or rescinded.
            rec["Tracking Status"], rec["Tracking Status Detail"] = overrides[aid]
        elif rec["Tracking Status"] in (
            "",
            "currently_flagged",
            "unflagged_pending_review",
        ):
            rec["Tracking Status"], rec["Tracking Status Detail"] = classify(
                aid, rec, desc_history
            )

    for rec in ledger.values():
        # After the claim fields have been captured: parse_claim_from_description
        # reads the *snapshot* row, so stripping here cannot starve it, and the
        # archived snapshots keep the original prose either way.
        rec["Award or Action Description"] = strip_claim_prefix(
            rec.get("Award or Action Description")
        )
        rec["USAspending URL"] = canonical_usaspending_url(rec.get("USAspending URL"))
        derive_trends(rec)

    rows = sorted(
        ledger.values(), key=lambda r: (r["Recipient Name"].lower(), r["Award ID"])
    )
    bad_methods = sorted(
        {
            rec.get("Primary Detection Method", "")
            for rec in rows
            if rec.get("Primary Detection Method", "") not in DETECTION_METHODS
        }
    )
    if bad_methods:
        raise RuntimeError(
            "Master ledger contains invalid Primary Detection Method value(s): "
            + ", ".join(repr(method) for method in bad_methods)
        )
    with open(LEDGER_PATH, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LEDGER_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    counts = Counter(r["Tracking Status"] for r in rows)
    print(f"Master ledger written: {LEDGER_PATH}")
    print(f"  {len(rows)} awards total (latest snapshot {latest_date}: {len(latest)})")
    for status, n in counts.most_common():
        print(f"  {status}: {n}")
    if skipped:
        print(
            f"  Skipped {len(skipped)} degraded snapshots (missing {sorted(sources.REQUIRED_SOURCES)}): {', '.join(skipped)}"
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
