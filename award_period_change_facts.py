"""Persisted mirror facts for suspicious transaction-level end-date changes."""

import csv
import os
import tempfile
from datetime import date
from decimal import Decimal, InvalidOperation

from award_period import SHORTENING_MIN_DAYS, significant_shortening
from contract_query import read_rows
from tracking_window import TRACKING_WINDOW_START_DATE, as_date
from utils import natural_modification_key

FACTS_PATH = os.path.join("verification", "award_period_change_facts.csv")
FACT_COLUMNS = [
    "Award ID",
    "Generated Award ID",
    "Previous End Date",
    "Resulting End Date",
    "Shortening Days",
    "Modification Number",
    "Action Date",
    "Source Transaction ID",
    "Federal Action Obligation",
    "Last Checked Date",
]


def _transaction_sort_key(row: dict) -> tuple:
    action = as_date(row.get("action_date"))
    return (
        action or date.min,
        natural_modification_key(row.get("modification_number")),
        _clean(row.get("transaction_id")),
    )


def select_largest_change(
    rows,
    *,
    run_date,
    min_days: int = SHORTENING_MIN_DAYS,
) -> dict | None:
    """Pure reference implementation of the mirror's consecutive-date rule.

    Production applies the same predicates in PostgreSQL to avoid transferring
    hundreds of millions of rows. Keeping the reference calculation here
    makes the methodology executable in unit tests and historical audits.
    """
    run = as_date(run_date)
    if run is None:
        raise ValueError(f"invalid run_date {run_date!r}")
    dated = [row for row in rows if as_date(row.get("end_date")) is not None]
    dated.sort(key=_transaction_sort_key)
    candidates = []
    for previous, current in zip(dated, dated[1:]):
        previous_end = as_date(previous.get("end_date"))
        resulting_end = as_date(current.get("end_date"))
        action = as_date(current.get("action_date"))
        days = (previous_end - resulting_end).days
        try:
            obligation = Decimal(_clean(current.get("federal_action_obligation")))
        except InvalidOperation:
            continue
        if not significant_shortening(days, min_days=min_days):
            continue
        if action is None or action <= TRACKING_WINDOW_START_DATE:
            continue
        if not (TRACKING_WINDOW_START_DATE <= resulting_end <= run):
            continue
        if obligation > 0:
            continue
        candidate = dict(current)
        candidate.update(
            previous_end_date=previous_end.isoformat(),
            end_date=resulting_end.isoformat(),
            days_truncated=days,
        )
        candidates.append(candidate)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda row: (
            row["days_truncated"],
            as_date(row.get("action_date")) or date.min,
            natural_modification_key(row.get("modification_number")),
            _clean(row.get("transaction_id")),
        ),
    )


def _clean(value) -> str:
    # Zero is meaningful for the <=0 obligation gate; ``value or ''`` would
    # silently turn a qualifying zero-dollar change into a missing value.
    return "" if value is None else str(value).strip()


def _iso_date(value, field: str, award_id: str) -> str:
    parsed = as_date(value)
    if parsed is None:
        raise RuntimeError(f"{FACTS_PATH} has invalid {field} {value!r} for {award_id}")
    return parsed.isoformat()


def build_fact_row(row: dict, *, checked: str) -> dict[str, str]:
    """Normalise one successful Q3 result into the committed sidecar schema."""
    award_id = _clean(row.get("award_id_native"))
    if not award_id:
        raise ValueError("period-change fact is missing award_id_native")
    previous = _iso_date(row.get("previous_end_date"), "previous end date", award_id)
    resulting = _iso_date(row.get("end_date"), "resulting end date", award_id)
    action = _iso_date(row.get("action_date"), "action date", award_id)
    checked_iso = _iso_date(checked, "last checked date", award_id)
    try:
        days = int(row.get("days_truncated"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"period-change result has invalid shortening days for {award_id}: "
            f"{row.get('days_truncated')!r}"
        ) from exc
    if not significant_shortening(days):
        raise RuntimeError(
            f"period-change result for {award_id} is not greater than the "
            f"configured {SHORTENING_MIN_DAYS}-day threshold"
        )
    obligation = _clean(row.get("federal_action_obligation"))
    try:
        if not obligation or Decimal(obligation) > 0:
            raise RuntimeError(
                f"period-change result for {award_id} has nonqualifying "
                f"obligation {obligation or '(none)'}"
            )
    except InvalidOperation as exc:
        raise RuntimeError(
            f"period-change result for {award_id} has invalid obligation {obligation!r}"
        ) from exc

    return {
        "Award ID": award_id,
        "Generated Award ID": _clean(row.get("generated_unique_award_id")),
        "Previous End Date": previous,
        "Resulting End Date": resulting,
        "Shortening Days": str(days),
        "Modification Number": _clean(row.get("modification_number")),
        "Action Date": action,
        "Source Transaction ID": _clean(row.get("transaction_id")),
        "Federal Action Obligation": obligation,
        "Last Checked Date": checked_iso,
    }


def _validate(row: dict) -> dict[str, str]:
    award_id = _clean(row.get("Award ID"))
    if not award_id:
        raise RuntimeError(f"{FACTS_PATH} contains a row without Award ID")
    previous = _iso_date(row.get("Previous End Date"), "Previous End Date", award_id)
    resulting = _iso_date(row.get("Resulting End Date"), "Resulting End Date", award_id)
    action = _iso_date(row.get("Action Date"), "Action Date", award_id)
    checked = _iso_date(row.get("Last Checked Date"), "Last Checked Date", award_id)
    days = (date.fromisoformat(previous) - date.fromisoformat(resulting)).days
    try:
        recorded_days = int(_clean(row.get("Shortening Days")))
    except ValueError as exc:
        raise RuntimeError(
            f"{FACTS_PATH} has invalid Shortening Days for {award_id}"
        ) from exc
    if recorded_days != days or not significant_shortening(recorded_days):
        raise RuntimeError(
            f"{FACTS_PATH} has inconsistent/nonqualifying shortening for {award_id}"
        )
    action_date = date.fromisoformat(action)
    resulting_date = date.fromisoformat(resulting)
    checked_date = date.fromisoformat(checked)
    if action_date <= TRACKING_WINDOW_START_DATE:
        raise RuntimeError(
            f"{FACTS_PATH} has out-of-window action date for {award_id}: {action}"
        )
    if not (TRACKING_WINDOW_START_DATE <= resulting_date <= checked_date):
        raise RuntimeError(
            f"{FACTS_PATH} has out-of-window resulting end date for {award_id}: "
            f"{resulting}"
        )
    obligation = _clean(row.get("Federal Action Obligation"))
    try:
        if not obligation or Decimal(obligation) > 0:
            raise RuntimeError(
                f"{FACTS_PATH} has nonqualifying obligation for {award_id}: "
                f"{obligation or '(none)'}"
            )
    except InvalidOperation as exc:
        raise RuntimeError(
            f"{FACTS_PATH} has invalid obligation for {award_id}: {obligation!r}"
        ) from exc
    return {column: _clean(row.get(column)) for column in FACT_COLUMNS}


def load_facts(path: str = FACTS_PATH) -> dict[str, dict[str, str]]:
    """Load validated qualifying facts, keyed by native award ID."""
    if not os.path.exists(path):
        return {}
    names, raw_rows = read_rows(path, encoding="utf-8-sig")
    if names != FACT_COLUMNS:
        raise RuntimeError(f"{path} has columns {names}; expected {FACT_COLUMNS}")
    facts = {}
    for raw in raw_rows:
        row = _validate(raw)
        award_id = row["Award ID"]
        if award_id in facts:
            raise RuntimeError(f"{path} contains duplicate Award ID {award_id}")
        facts[award_id] = row
    return facts


def detection_text(row: dict) -> str:
    """Human-readable evidence sentence shared by mirror and confirmation."""
    return (
        f"End date shortened {row.get('Shortening Days', '')} days from "
        f"{row.get('Previous End Date', '')} to {row.get('Resulting End Date', '')} "
        f"by mod {row.get('Modification Number', '')} on "
        f"{row.get('Action Date', '')}"
    )


def write_facts(rows: dict[str, dict] | list[dict], path: str = FACTS_PATH) -> None:
    """Atomically replace facts after a complete successful mirror query."""
    if isinstance(rows, dict):
        values = list(rows.values())
    else:
        values = list(rows)
    validated = [_validate(row) for row in values]
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            newline="",
            encoding="utf-8",
            dir=parent,
            prefix=".award-period-change-facts-",
            suffix=".csv",
            delete=False,
        ) as fh:
            tmp_path = fh.name
            writer = csv.DictWriter(fh, fieldnames=FACT_COLUMNS)
            writer.writeheader()
            for row in sorted(validated, key=lambda item: item["Award ID"]):
                writer.writerow(row)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
