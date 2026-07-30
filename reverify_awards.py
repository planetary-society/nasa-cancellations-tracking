#!/usr/bin/env python3
"""
Re-verify ledger awards against their USAspending transaction history.

Why this exists: the daily snapshots cannot answer "is this award still
cancelled". When a closeout modification replaces the termination language, the
award falls out of the snapshot on that very day - so the closeout text is
never recorded anywhere, and the last thing we ever observed is the
termination. Of the 120 awards no longer in the daily snapshot, exactly zero
show a closeout as their final observed description. That blind spot is
structural, and only the transaction history resolves it.

This module walks each award's transactions and decides whether a termination
still stands, was superseded by a closeout (still terminated), or was reversed.

Two invariants:

  1. verification/dropped_award_status.csv is HUMAN-OWNED. Nothing here ever
     opens it for writing. Human verdicts win every precedence contest; this
     pass only ever writes verification/auto_verification.csv.
  2. An award carrying an external claim is never pruned. Automation may refine
     how a claimed award is described, never remove it - the claim is the fact
     being tracked, whether or not the award was really terminated.

A lookup failure is never a verdict: it is recorded as `unresolved` and retried
next run. Empty transactions on an HTTP 200 is also `unresolved`, never
"no termination" - that fail-open shape is what silently lost 21 awards when
FPDS was retired.

Usage:
    python reverify_awards.py --dry-run            # show selection, no calls
    python reverify_awards.py --max-requests 30    # bounded first run
    python reverify_awards.py --award-id 80HQTR22F0076
    python reverify_awards.py                      # scheduled weekly sweep
"""

import argparse
import csv
import os
import re
import shutil
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime

import build_master_ledger
from build_master_ledger import (
    AUTO_VERIFICATION_PATH,
    LEDGER_PATH,
    VERIFICATION_PATH,
    load_auto_verification,
)
from contract_query import load_snapshot
from termination_vocabulary import (
    CLOSEOUT_TEXT,
    is_cause,
    is_descope,
    is_reversal,
    is_termination,
    is_vacatur,
)

RUN_LOG_PATH = os.path.join("verification", "reverify_runs.csv")

AUTO_COLUMNS = [
    "Award ID",
    "Generated Award ID",
    "Auto Status",
    "Confidence",
    "Verified Date",
    "Evidence",
    "Signals",
    "Termination Mod",
    "Termination Date",
    "Latest Mod",
    "Latest Mod Date",
    "Post-Term Obligations",
    "Transaction Count",
    "Disagrees With Human",
    "Last Attempt Date",
    "Last Success Date",
    "Attempt Count",
    "Last Error",
]

RUN_LOG_COLUMNS = [
    "Run Date",
    "Selected",
    "Changed",
    "Unresolved",
    "Disagreements",
    "Duration Seconds",
    "Exit Status",
]

# Above this share of failed lookups the run is treated as an outage: the
# output file is left untouched rather than half-refreshed.
MAX_UNRESOLVED_SHARE = 0.25

# USAspending caps a transactions page at 5000; hitting it means we silently
# truncated the history and must not judge on a partial record.
PAGE_SIZE = 5000

# --- Action type vocabularies -------------------------------------------
# Selected by generated-id prefix. This is NOT optional: `D` means "change
# order" for contracts but "adjustment to completed project" (a closeout) for
# assistance. Reading one map for the other flips still_terminated <-> closed_out.
# Labels are FPDS's own action_type_description, confirmed against live data on
# 2026-07-30. Codes absent from this map route an award to needs_manual_review
# rather than defaulting, so the map is kept complete for every code actually
# observed in NASA contract transactions.
CONTRACT_ACTIONS = {
    "A": "funding",  # ADDITIONAL WORK (NEW AGREEMENT, JUSTIFICATION REQUIRED)
    "B": "funding",  # SUPPLEMENTAL AGREEMENT FOR WORK WITHIN SCOPE
    "C": "funding",  # FUNDING ONLY ACTION
    "D": "funding",  # CHANGE ORDER
    "G": "funding",  # EXERCISE AN OPTION
    "E": "termination_cause",  # TERMINATE FOR DEFAULT (none observed to date)
    "X": "termination_cause",  # TERMINATE FOR CAUSE
    "F": "termination_convenience",  # TERMINATE FOR CONVENIENCE (COMPLETE OR PARTIAL)
    "K": "closeout",  # CLOSE OUT
    "M": "administrative",  # OTHER ADMINISTRATIVE ACTION
    "J": "administrative",  # NOVATION AGREEMENT
    "P": "administrative",  # REREPRESENTATION OF NON-NOVATED MERGER/ACQUISITION
    "V": "administrative",  # UNIQUE ENTITY ID OR LEGAL BUSINESS NAME CHANGE
    "W": "administrative",  # ENTITY ADDRESS CHANGE
    "Y": "administrative",  # ADD SUBCONTRACTING PLAN
    # LEGAL CONTRACT CANCELLATION. Treated as administrative, not a policy
    # cancellation: both instances observed are routine procurement unwinds (a
    # $78k BPA-call deobligation and an $18k software renewal), not programme
    # terminations. Revisit if a large one appears.
    "N": "administrative",
}

ASSISTANCE_ACTIONS = {
    "A": "funding",
    "B": "funding",
    "C": "funding",
    "D": "closeout",
    "E": "funding",
}


def _is_reversal(txn):
    return is_reversal(_text(txn))


def _is_closeout(txn, kind):
    """A closeout action code must be corroborated by its text.

    The code alone is not enough: a settlement deobligation can carry a
    closeout code while the award is simply still terminated (80HQTR24F0072),
    and "closed out" vs "still terminated" must not hinge on an
    uncorroborated code.
    """
    return kind == "closeout" and bool(CLOSEOUT_TEXT.search(_text(txn)))


@dataclass(frozen=True)
class Verdict:
    """One award's screening result.

    `signals` is the structured trace behind the call - kept as a dict so the
    writer can read fields directly instead of parsing them back out of a
    rendered string. It is flattened to text only at the CSV boundary.
    """

    status: str
    confidence: str
    evidence: str
    signals: dict = field(default_factory=dict)


def render_signals(signals):
    """Flatten the signals dict to the compact trace stored in the CSV."""
    return ";".join(f"{k}={v}" for k, v in signals.items())


def generated_id(ledger_row):
    """Pull the USAspending generated award id out of the ledger URL column."""
    m = re.search(r"/award/([^/]+)/?", ledger_row.get("URL") or "")
    return m.group(1) if m else ""


def _text(txn):
    return str(getattr(txn, "award_description", "") or "")


def _obligation(txn):
    """federal_action_obligation as float, or None when genuinely unknown.

    Never use the ORM's `.amt`: it normalizes 0.00 to None, so a $0
    administrative termination mod would read as amount-less.
    """
    value = getattr(txn, "federal_action_obligation", None)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sort_key(txn):
    return (
        str(getattr(txn, "action_date", "") or ""),
        str(getattr(txn, "modification_number", "") or ""),
    )


def _kind(txn, is_contract):
    """Map a transaction's action_type to a category, or None if unrecognised."""
    code = str(getattr(txn, "action_type", "") or "").strip().upper()
    if not code:
        return None
    return (CONTRACT_ACTIONS if is_contract else ASSISTANCE_ACTIONS).get(code)


def _describe(txn):
    mod = getattr(txn, "modification_number", "") or "?"
    when = getattr(txn, "action_date", "") or "?"
    code = getattr(txn, "action_type", "") or "?"
    return f"{code} {mod} {when}"


def classify_transactions(txns, *, is_contract, ledger_row):
    """Decide an award's current state from its ordered transaction history.

    Pure: no network, no clock beyond `today` for expiry, no file access. The
    risky logic lives here precisely so it can be tested offline.
    """
    if not txns:
        return Verdict(
            "unresolved",
            "none",
            "No transactions returned for this award; cannot judge.",
            {"reason": "no_transactions"},
        )
    if len(txns) >= PAGE_SIZE:
        return Verdict(
            "unresolved",
            "none",
            f"Transaction history hit the {PAGE_SIZE}-row page cap; "
            f"record may be truncated.",
            {"reason": "pagination_overflow"},
        )

    txns = sorted(txns, key=_sort_key)
    kinds = [_kind(t, is_contract) for t in txns]

    unknown = [
        str(getattr(t, "action_type", "") or "")
        for t, k in zip(txns, kinds)
        if k is None and str(getattr(t, "action_type", "") or "").strip()
    ]

    # Termination for cause is excluded by methodology, not a policy cancellation.
    for txn, kind in zip(txns, kinds):
        if kind == "termination_cause" or is_cause(_text(txn)):
            return Verdict(
                "excluded_by_design",
                "high",
                f"Termination for cause/default ({_describe(txn)}) per USAspending "
                f"transactions; contractor failure, not a policy cancellation.",
                {"cause": _describe(txn), "n": len(txns)},
            )

    # Anchor on the LAST termination action, derived from the history itself
    # rather than from when we happened to observe it.
    # A rescission names the thing it undoes ("Rescinding stop work notice"),
    # so it reads as a termination too. Skip reversals here or they become their own
    # anchor, leaving no post-termination window and hiding the reversal.
    term_idx = None
    for i, (txn, kind) in enumerate(zip(txns, kinds)):
        if _is_reversal(txn):
            continue
        if kind == "termination_convenience" or is_termination(_text(txn)):
            term_idx = i
    if term_idx is None:
        if unknown:
            return Verdict(
                "needs_manual_review",
                "none",
                f"Unrecognised action type(s) {sorted(set(unknown))} and no "
                f"termination signal; vocabulary may have drifted.",
                {"unknown": ",".join(sorted(set(unknown))), "n": len(txns)},
            )
        end_date = str(ledger_row.get("End Date") or "")
        if end_date and end_date < date.today().isoformat():
            return Verdict(
                "naturally_expired",
                "low",
                f"No termination action in {len(txns)} transactions; "
                f"period of performance ended {end_date}.",
                {"term": "none", "end": end_date, "n": len(txns)},
            )
        return Verdict(
            "no_termination_signal",
            "low",
            f"No termination action found in {len(txns)} transactions.",
            {"term": "none", "n": len(txns)},
        )

    term_txn = txns[term_idx]
    post = txns[term_idx + 1 :]
    post_kinds = kinds[term_idx + 1 :]
    obligations = [_obligation(t) for t in post]
    positive = [
        (t, o)
        for t, o, k in zip(post, obligations, post_kinds)
        if o is not None and o >= 1 and k == "funding"
    ]
    post_total = sum(o for o in obligations if o is not None)
    unknown_money = any(o is None for o in obligations)
    base = {
        "term": _describe(term_txn),
        "term_mod": str(getattr(term_txn, "modification_number", "") or ""),
        "term_date": str(getattr(term_txn, "action_date", "") or ""),
        "post_n": len(post),
        "post_obl": f"{post_total:.2f}",
        "post_pos": len(positive),
        "n": len(txns),
    }

    # A court vacatur is a legal fact that outranks any later money movement.
    for txn in [term_txn, *post]:
        if is_vacatur(_text(txn)):
            return Verdict(
                "vacated",
                "high",
                f"Termination ({_describe(term_txn)}) vacated/set aside per "
                f"{_describe(txn)} in USAspending transactions.",
                {**base, "vacatur": _describe(txn)},
            )

    for txn in post:
        if _is_reversal(txn):
            return Verdict(
                "reinstated",
                "high",
                f"Termination ({_describe(term_txn)}) rescinded per "
                f"{_describe(txn)} in USAspending transactions; award active again.",
                {**base, "reversal": _describe(txn)},
            )

    # De-scope must beat the `continued` money test: a partially de-scoped
    # award legitimately keeps taking new obligations for the surviving work.
    if is_descope(_text(term_txn)):
        return Verdict(
            "descoped",
            "high",
            f"Partial de-scope rather than full termination ({_describe(term_txn)}) "
            f"per USAspending transactions.",
            {**base, "descope": 1},
        )

    closeout_last = bool(post) and _is_closeout(post[-1], post_kinds[-1])
    if closeout_last and not positive:
        return Verdict(
            "closed_out",
            "high",
            f"Terminated for convenience ({_describe(term_txn)}); closeout "
            f"{_describe(post[-1])} (net ${post_total:,.2f}) superseded the "
            f"termination language, so source queries no longer match; "
            f"award remains terminated.",
            {**base, "closeout": _describe(post[-1])},
        )

    if positive:
        txn, amount = positive[-1]
        return Verdict(
            "continued",
            "high",
            f"Award received new obligations after termination "
            f"({_describe(term_txn)}): +${amount:,.2f} on {_describe(txn)} "
            f"per USAspending transactions.",
            {**base, "continued": _describe(txn)},
        )

    if unknown_money or unknown:
        return Verdict(
            "needs_manual_review",
            "none",
            f"Termination ({_describe(term_txn)}) with unresolved post-termination "
            f"evidence (unknown obligation amounts or action types).",
            {**base, "ambiguous": 1},
        )

    return Verdict(
        "still_terminated",
        "low",
        f"Terminated for convenience ({_describe(term_txn)}); no rescission, "
        f"closeout, or new obligations in {len(post)} later transactions.",
        base,
    )


def load_human_verdicts():
    if not os.path.exists(VERIFICATION_PATH):
        return {}
    with open(VERIFICATION_PATH, encoding="utf-8") as fh:
        return {r["Award ID"]: r["Status"] for r in csv.DictReader(fh)}


def select_awards(ledger, previous, *, stale_days, include_excluded, only=None):
    """Choose which awards to re-check, cheapest-value-first.

    Tier 0 always runs (failures and explicit requests); the rest are skipped
    when their last successful check is younger than the tier's staleness
    window, so a weekly sweep costs ~50-80 requests in steady state.
    """
    if only:
        return [a for a in only if a in ledger], {"explicit": len(only)}

    today = date.today()

    def age_days(aid):
        stamp = previous.get(aid, {}).get("Last Success Date", "")
        try:
            return (today - datetime.strptime(stamp, "%Y-%m-%d").date()).days
        except (ValueError, TypeError):
            return 10**6

    selected, tiers = [], Counter()
    for aid, rec in ledger.items():
        status = rec.get("Status", "")
        prev = previous.get(aid, {})
        if status == "excluded_by_design" and not include_excluded:
            tiers["skipped_excluded"] += 1
            continue

        if prev.get("Auto Status") == "unresolved" or prev.get("Last Error"):
            tier, window = "0_retry", 0
        elif status in ("dropped_pending_review", "source_retired"):
            tier, window = "1_unverified", 0
        elif status in ("still_terminated", "closed_out", "descoped"):
            tier, window = "2_verified_dropped", stale_days
        elif status == "listed" and not rec.get("Claiming Source"):
            tier, window = "3_listed_inferred", stale_days
        elif status == "listed":
            tier, window = "4_listed_claimed", stale_days * 3
        else:
            tier, window = "2_verified_dropped", stale_days

        if window and age_days(aid) < window:
            tiers["skipped_fresh"] += 1
            continue
        selected.append(aid)
        tiers[tier] += 1
    return selected, dict(tiers)


def fetch_transactions(client, gid):
    return (
        client.transactions.award_id(gid)
        .order_by("action_date", "asc")
        .page_size(PAGE_SIZE)
        .all()
    )


def build_row(aid, rec, verdict, txns, previous, human, *, today, ok):
    """Assemble one auto_verification.csv row, preserving retry bookkeeping."""
    prev = previous.get(aid, {})
    row = dict(prev) if prev else {c: "" for c in AUTO_COLUMNS}
    row["Award ID"] = aid
    row["Generated Award ID"] = generated_id(rec)
    row["Last Attempt Date"] = today
    row["Auto Status"] = verdict.status
    row["Confidence"] = verdict.confidence
    row["Evidence"] = verdict.evidence
    signals = render_signals(verdict.signals)
    row["Signals"] = signals

    if not ok:
        # A failure never overwrites the previous verdict or success stamp.
        row["Attempt Count"] = str(int(prev.get("Attempt Count") or 0) + 1)
        return row

    row["Last Error"] = ""
    row["Last Success Date"] = today
    row["Attempt Count"] = "0"
    row["Transaction Count"] = str(len(txns))
    if txns:
        latest = max(txns, key=_sort_key)
        row["Latest Mod"] = str(getattr(latest, "modification_number", "") or "")
        row["Latest Mod Date"] = str(getattr(latest, "action_date", "") or "")
    row["Termination Mod"] = verdict.signals.get("term_mod", "")
    row["Termination Date"] = verdict.signals.get("term_date", "")
    row["Post-Term Obligations"] = verdict.signals.get("post_obl", "")

    human_status = human.get(aid)
    row["Disagrees With Human"] = (
        human_status if human_status and human_status != verdict.status else ""
    )
    # Verified Date marks when the VERDICT changed, not when we last looked.
    if prev.get("Auto Status") != verdict.status or prev.get("Signals") != signals:
        row["Verified Date"] = today
    else:
        row["Verified Date"] = prev.get("Verified Date", today)
    return row


def write_auto(rows):
    """Atomic full rewrite, sorted, so an interrupted run is a no-op."""
    os.makedirs(os.path.dirname(AUTO_VERIFICATION_PATH), exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, newline="", encoding="utf-8"
        ) as fh:
            tmp = fh.name
            w = csv.DictWriter(fh, fieldnames=AUTO_COLUMNS, extrasaction="ignore")
            w.writeheader()
            w.writerows(sorted(rows, key=lambda r: r["Award ID"]))
        shutil.move(tmp, AUTO_VERIFICATION_PATH)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)


def append_run_log(stats):
    os.makedirs(os.path.dirname(RUN_LOG_PATH), exist_ok=True)
    exists = os.path.exists(RUN_LOG_PATH)
    with open(RUN_LOG_PATH, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=RUN_LOG_COLUMNS, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow(stats)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the selection and request count without calling the API",
    )
    ap.add_argument(
        "--max-requests",
        type=int,
        default=300,
        help="Hard cap on awards checked this run",
    )
    ap.add_argument(
        "--stale-days",
        type=int,
        default=30,
        help="Skip awards successfully checked more recently than this",
    )
    ap.add_argument(
        "--award-id",
        action="append",
        default=[],
        help="Check only these award IDs (repeatable)",
    )
    ap.add_argument(
        "--include-excluded",
        action="store_true",
        help="Also check excluded_by_design awards",
    )
    ap.add_argument(
        "--no-rebuild",
        action="store_true",
        help="Skip the master ledger rebuild at the end",
    )
    args = ap.parse_args(argv)

    started = datetime.now()
    today = date.today().isoformat()
    ledger = load_snapshot(LEDGER_PATH)
    previous = load_auto_verification()
    human = load_human_verdicts()

    selected, tiers = select_awards(
        ledger,
        previous,
        stale_days=args.stale_days,
        include_excluded=args.include_excluded,
        only=args.award_id,
    )
    if len(selected) > args.max_requests:
        print(
            f"Capping selection {len(selected)} -> {args.max_requests} "
            f"(--max-requests); the remainder is picked up next run."
        )
        selected = selected[: args.max_requests]

    print(f"Ledger {len(ledger)} awards; selected {len(selected)} to re-verify.")
    for tier, n in sorted(tiers.items()):
        print(f"  {tier}: {n}")
    if args.dry_run:
        print(f"\nDry run: {len(selected)} requests would be made. No calls issued.")
        return 0

    from usaspending import USASpendingClient
    from usaspending.exceptions import USASpendingError

    client = USASpendingClient()
    rows, unresolved, changed, disagreements = [], 0, 0, 0
    try:
        for i, aid in enumerate(selected, 1):
            rec = ledger[aid]
            gid = generated_id(rec)
            txns, ok = [], True
            if not gid:
                verdict = Verdict(
                    "unresolved",
                    "none",
                    "No generated award id in the ledger URL column.",
                    {"reason": "no_generated_id"},
                )
                ok = False
            else:
                try:
                    txns = fetch_transactions(client, gid)
                    verdict = classify_transactions(
                        txns, is_contract=gid.startswith("CONT_"), ledger_row=rec
                    )
                    ok = verdict.status != "unresolved"
                except (USASpendingError, OSError) as e:
                    verdict = Verdict(
                        "unresolved",
                        "none",
                        f"Lookup failed; verdict withheld: {e}",
                        {"reason": "fetch_error"},
                    )
                    ok = False

            row = build_row(
                aid, rec, verdict, txns, previous, human, today=today, ok=ok
            )
            if not ok:
                row["Last Error"] = verdict.evidence[:200]
                unresolved += 1
            if previous.get(aid, {}).get("Auto Status") != verdict.status:
                changed += 1
            if row.get("Disagrees With Human"):
                disagreements += 1
            rows.append(row)
            print(
                f"  [{i}/{len(selected)}] {aid}: {verdict.status} ({verdict.confidence})"
            )
    finally:
        client.close()

    # Carry forward awards not checked this run so the file stays complete.
    checked = {r["Award ID"] for r in rows}
    rows.extend(r for a, r in previous.items() if a not in checked)

    share = unresolved / len(selected) if selected else 0
    stats = {
        "Run Date": today,
        "Selected": len(selected),
        "Changed": changed,
        "Unresolved": unresolved,
        "Disagreements": disagreements,
        "Duration Seconds": int((datetime.now() - started).total_seconds()),
    }

    if share > MAX_UNRESOLVED_SHARE:
        stats["Exit Status"] = "aborted_outage"
        append_run_log(stats)
        print(
            f"\nFAIL: {unresolved}/{len(selected)} lookups unresolved "
            f"({share:.0%} > {MAX_UNRESOLVED_SHARE:.0%}). Treating as an upstream "
            f"outage and leaving {AUTO_VERIFICATION_PATH} untouched rather than "
            f"half-refreshing it."
        )
        return 1

    write_auto(rows)
    stats["Exit Status"] = "ok"
    append_run_log(stats)

    print(
        f"\nWrote {AUTO_VERIFICATION_PATH}: {len(rows)} awards "
        f"({changed} changed, {unresolved} unresolved, {disagreements} disagree with human)."
    )
    if disagreements:
        print("  Disagreements are recorded but NEVER applied - review them:")
        for r in rows:
            if r.get("Disagrees With Human"):
                print(
                    f"    {r['Award ID']}: auto={r['Auto Status']} human={r['Disagrees With Human']}"
                )

    if not args.no_rebuild:
        build_master_ledger.build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
