"""Structured names for the signal that caused an award to enter the tracker."""

import sources

EXTERNAL_CLAIM = "external_claim"
DESCRIPTION_KEYWORD = "description_keyword"
POP_END_DATE_CHANGE = "pop_end_date_change"
TERMINATION_ACTION_CODE = "termination_action_code"
TERMINATION_LANGUAGE = "termination_language"
OBLIGATION_CLAWBACK = "obligation_clawback"

# Historical snapshots predate structured detection metadata. These values are
# deliberately explicit about what can and cannot be reconstructed from them.
LEGACY_FPDS_KEYWORD = "legacy_fpds_keyword"
LEGACY_LOCAL_MIRROR_SIGNAL = "legacy_local_mirror_signal"
LEGACY_USASPENDING_SIGNAL = "legacy_usaspending_signal"
LEGACY_SOURCE_SIGNAL = "legacy_source_signal"

DETECTION_METHODS = (
    EXTERNAL_CLAIM,
    DESCRIPTION_KEYWORD,
    POP_END_DATE_CHANGE,
    TERMINATION_ACTION_CODE,
    TERMINATION_LANGUAGE,
    OBLIGATION_CLAWBACK,
    LEGACY_FPDS_KEYWORD,
    LEGACY_LOCAL_MIRROR_SIGNAL,
    LEGACY_USASPENDING_SIGNAL,
    LEGACY_SOURCE_SIGNAL,
)

_LOCAL_NET_METHODS = {
    "action_code": TERMINATION_ACTION_CODE,
    "description_regex": TERMINATION_LANGUAGE,
    "clawback": OBLIGATION_CLAWBACK,
    "end_date_truncation": POP_END_DATE_CHANGE,
}

# Direct termination evidence outranks inference when more than one Local
# Mirror net finds the same award. Clawback is a stronger cancellation signal
# than a schedule change, so it wins between the two inference-only methods.
_METHOD_PRIORITY = (
    TERMINATION_ACTION_CODE,
    TERMINATION_LANGUAGE,
    OBLIGATION_CLAWBACK,
    POP_END_DATE_CHANGE,
)

# What a snapshot's source alone implies, for rows archived before the
# structured method was recorded. The two sources that ran several nets get an
# explicitly legacy answer rather than an invented one.
_SOURCE_FALLBACKS = {
    sources.DOGE: EXTERNAL_CLAIM,
    sources.NPDV: DESCRIPTION_KEYWORD,
    sources.NASA_GRANTS: POP_END_DATE_CHANGE,
    sources.FPDS: LEGACY_FPDS_KEYWORD,
    sources.LOCAL_MIRROR: LEGACY_LOCAL_MIRROR_SIGNAL,
    sources.USASPENDING_TERMINATIONS: LEGACY_USASPENDING_SIGNAL,
}


def primary_local_method(rows) -> str:
    """Return the primary public method for a group of Local Mirror net rows."""
    methods = {
        _LOCAL_NET_METHODS.get(str(row.get("detection_method") or ""), "")
        for row in rows
    }
    for method in _METHOD_PRIORITY:
        if method in methods:
            return method
    raise ValueError("Local Mirror award has no recognized detection method")


def infer_snapshot_method(row: dict) -> str:
    """Backfill the method for snapshots written before the structured field.

    Exact human-readable Detection text wins. Source-level fallbacks are used
    only where older snapshots discarded the deciding signal entirely.
    """
    existing = str(row.get("Primary Detection Method") or "").strip()
    if existing:
        return existing

    detection = str(row.get("Detection") or "").casefold()
    if "terminate-for-convenience action" in detection or (
        "legal-contract-cancellation action" in detection
    ):
        return TERMINATION_ACTION_CODE
    if "termination-language transaction" in detection:
        return TERMINATION_LANGUAGE
    if "clawback" in detection:
        return OBLIGATION_CLAWBACK
    if "end date shortened" in detection or "end date truncated" in detection:
        return POP_END_DATE_CHANGE

    source = str(row.get("Source") or "").strip()
    if not source:
        # Ledger rows accumulate Sources while snapshots carry one Source, so
        # which key is present is what tells the two apart. This is only a
        # last-resort fallback: normal rebuilds infer from each snapshot before
        # the sources are collapsed into ledger history.
        flagged = sources.sources_of(row)
        source = flagged[-1] if flagged else ""
    return _SOURCE_FALLBACKS.get(source, LEGACY_SOURCE_SIGNAL)
