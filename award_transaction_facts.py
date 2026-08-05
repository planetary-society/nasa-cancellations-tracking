#!/usr/bin/env python3
"""Persistent facts derived from complete USAspending transaction histories.

Daily snapshot membership is provisional until validation succeeds.  These
facts are not: once a complete award history has been fetched successfully,
its first/latest actions and formal termination/closeout provenance can safely
be retained without accepting a candidate snapshot.  This module owns that
shared methodology and the atomic sidecar used by both search.py and
reverify_awards.py.
"""

import os
import sys
from dataclasses import dataclass
from datetime import date


from utils import natural_modification_key, read_rows, write_sidecar_csv

PAGE_SIZE = 5000
SIDECAR_PATH = os.path.join("verification", "award_transaction_facts.csv")

# Fields copied into the daily snapshot and master ledger.  Latest
# Modification Number already predates this group in those schemas, but it is
# transaction-derived too and therefore belongs in the independent sidecar.
TRANSACTION_HISTORY_COLUMNS = (
    "First Action Type",
    "First Action Type Description",
    "First Action Date",
    "Latest Action Type",
    "Latest Action Type Description",
    "Latest Action Date",
    "Termination Modification Number",
    "Termination Action Date",
    "Closeout Modification Number",
    "Closeout Action Date",
)
LEDGER_OVERLAY_COLUMNS = ("Latest Modification Number", *TRANSACTION_HISTORY_COLUMNS)
SIDECAR_COLUMNS = (
    "Award ID",
    "Generated Award ID",
    "Award Category",
    "Transaction Count",
    *LEDGER_OVERLAY_COLUMNS,
    "Last Checked Date",
)
DATE_COLUMNS = (
    "First Action Date",
    "Latest Action Date",
    "Termination Action Date",
    "Closeout Action Date",
    "Last Checked Date",
)

# Selected by generated-id prefix.  This distinction is mandatory: `D` means
# change order for contracts but adjustment to completed project (closeout)
# for assistance.
CONTRACT_ACTIONS = {
    "A": "funding",
    "B": "funding",
    "C": "funding",
    "D": "funding",
    "G": "funding",
    "E": "termination_cause",
    "X": "termination_cause",
    "F": "termination_convenience",
    "K": "closeout",
    "M": "administrative",
    "J": "administrative",
    "P": "administrative",
    "V": "administrative",
    "W": "administrative",
    "Y": "administrative",
    # Legal contract cancellation is intentionally not treated as a programme
    # termination; observed NASA uses are routine procurement unwinds.
    "N": "administrative",
}

ASSISTANCE_ACTIONS = {
    "A": "funding",
    "B": "funding",
    "C": "funding",
    "D": "closeout",
    "E": "funding",
}


def uses_contract_action_codes(generated_award_id, category: str = "") -> bool:
    """Return whether FPDS rather than assistance action codes apply."""
    generated_id = str(generated_award_id or "")
    return generated_id.startswith("CONT_") or category in {"contract", "idv"}


def award_category(generated_award_id: str, category: str = "") -> str:
    """Normalize an ORM category or infer it from a generated award id."""
    if category in {"contract", "idv", "assistance"}:
        return category
    if category in {"grant", "direct_payment", "loan", "other"}:
        return "assistance"
    generated_id = str(generated_award_id or "")
    if generated_id.startswith("CONT_IDV_"):
        return "idv"
    if generated_id.startswith("CONT_AWD_"):
        return "contract"
    if generated_id.startswith("ASST_"):
        return "assistance"
    return ""


def transaction_sort_key(txn):
    return (
        str(getattr(txn, "action_date", "") or ""),
        natural_modification_key(getattr(txn, "modification_number", "")),
    )


def action_kind(txn, is_contract):
    """Map a transaction action code to its semantic category."""
    code = str(getattr(txn, "action_type", "") or "").strip().upper()
    if not code:
        return None
    return (CONTRACT_ACTIONS if is_contract else ASSISTANCE_ACTIONS).get(code)


@dataclass(frozen=True)
class TransactionHistoryFacts:
    first: object | None
    latest: object | None
    termination: object | None
    closeout: object | None


def transaction_history_facts(txns, *, is_contract):
    """Return ordered endpoints and most recent formal event transactions."""
    ordered = sorted(txns, key=transaction_sort_key)
    if not ordered:
        return TransactionHistoryFacts(None, None, None, None)

    termination = None
    closeout = None
    for txn in ordered:
        kind = action_kind(txn, is_contract)
        if kind in {"termination_convenience", "termination_cause"}:
            termination = txn
        elif kind == "closeout":
            closeout = txn
    return TransactionHistoryFacts(ordered[0], ordered[-1], termination, closeout)


def transaction_value(transaction, field: str) -> str:
    if transaction is None:
        return ""
    return str(getattr(transaction, field, "") or "")


def fetch_transaction_query(query):
    """Exhaust an award-history ORM query without its global result cap."""
    return (
        query.order_by("action_date", "asc")
        .page_size(PAGE_SIZE)
        .limit(sys.maxsize)
        .all()
    )


def fetch_transactions(client, generated_award_id: str):
    """Fetch one complete paginated award history by generated award id."""
    return fetch_transaction_query(client.transactions.award_id(generated_award_id))


def history_columns(facts: TransactionHistoryFacts) -> dict:
    """Map one award's history facts onto LEDGER_OVERLAY_COLUMNS.

    The single producer of these values. The sidecar row and the daily
    snapshot row both splat this, so a column added to
    TRANSACTION_HISTORY_COLUMNS is filled in both or in neither - it cannot
    land in the ledger overlay and arrive blank in the snapshot.
    """
    return {
        "Latest Modification Number": transaction_value(
            facts.latest, "modification_number"
        ),
        "First Action Type": transaction_value(facts.first, "action_type"),
        "First Action Type Description": transaction_value(
            facts.first, "action_type_description"
        ),
        "First Action Date": transaction_value(facts.first, "action_date"),
        "Latest Action Type": transaction_value(facts.latest, "action_type"),
        "Latest Action Type Description": transaction_value(
            facts.latest, "action_type_description"
        ),
        "Latest Action Date": transaction_value(facts.latest, "action_date"),
        "Termination Modification Number": transaction_value(
            facts.termination, "modification_number"
        ),
        "Termination Action Date": transaction_value(facts.termination, "action_date"),
        "Closeout Modification Number": transaction_value(
            facts.closeout, "modification_number"
        ),
        "Closeout Action Date": transaction_value(facts.closeout, "action_date"),
    }


def build_fact_row(
    award_id: str,
    generated_award_id: str,
    category: str,
    transactions,
    *,
    checked: str,
    facts: TransactionHistoryFacts | None = None,
) -> dict:
    """Build one persisted summary; empty histories are lookup failures.

    `facts` lets a caller that has already derived them for the same history
    hand them over rather than paying for a second sort and scan; it must have
    been derived from `transactions` with the same action-code vocabulary.
    """
    transactions = list(transactions)
    if not transactions:
        raise ValueError(f"empty transaction history for {award_id!r}")
    normalized_category = award_category(generated_award_id, category)
    if facts is None:
        facts = transaction_history_facts(
            transactions,
            is_contract=uses_contract_action_codes(
                generated_award_id, normalized_category
            ),
        )
    return {
        "Award ID": str(award_id or ""),
        "Generated Award ID": str(generated_award_id or ""),
        "Award Category": normalized_category,
        "Transaction Count": str(len(transactions)),
        **history_columns(facts),
        "Last Checked Date": checked,
    }


def load_facts(path: str = SIDECAR_PATH) -> dict[str, dict]:
    """Load and strictly validate the machine-owned sidecar."""
    if not os.path.exists(path):
        return {}
    rows = {}
    for row in read_rows(path, columns=SIDECAR_COLUMNS):
        aid = (row.get("Award ID") or "").strip()
        if not aid:
            raise RuntimeError(f"{path} contains a blank Award ID")
        if aid in rows:
            raise RuntimeError(f"{path} contains duplicate Award ID {aid!r}")
        try:
            count = int(row.get("Transaction Count") or "")
        except ValueError as exc:
            raise RuntimeError(
                f"{path} has invalid Transaction Count for {aid}"
            ) from exc
        if count < 1:
            raise RuntimeError(f"{path} has empty transaction history for {aid}")
        for column in DATE_COLUMNS:
            value = (row.get(column) or "").strip()
            if not value:
                continue
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise RuntimeError(
                    f"{path} has invalid {column} {value!r} for {aid}"
                ) from exc
        rows[aid] = {column: row.get(column, "") for column in SIDECAR_COLUMNS}
    return rows


def write_facts(rows: dict[str, dict], path: str = SIDECAR_PATH) -> None:
    """Atomically rewrite the complete sidecar in deterministic order."""
    write_sidecar_csv(path, SIDECAR_COLUMNS, rows)
