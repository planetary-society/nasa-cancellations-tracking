"""Structured names for the signal that caused an award to enter the tracker."""

import sources

EXTERNAL_CLAIM = "external_claim"
DESCRIPTION_KEYWORD = "description_keyword"
POP_END_DATE_CHANGE = "pop_end_date_change"
# FPDS action code F, "terminate for convenience (complete or partial)".
# Specifically F: code N used to share this name and now has its own.
TERMINATION_ACTION_CODE = "termination_action_code"
TERMINATION_LANGUAGE = "termination_language"
OBLIGATION_CLAWBACK = "obligation_clawback"
# FPDS action code N. Named for the code's own label rather than a synonym,
# because the column is cited publicly. N voids a contract instead of stopping
# work, so on its own it says nothing about whether the award was cut short -
# search.py admits it only when the award period was actually truncated.
LEGAL_CONTRACT_CANCELLATION = "legal_contract_cancellation"

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
    LEGAL_CONTRACT_CANCELLATION,
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
# A bare N sits below termination language on purpose: it is the weakest thing
# in the direct-evidence tier, and it is the one that needs corroborating. That
# ranking also buys a property the gate relies on - an award whose primary
# method is LEGAL_CONTRACT_CANCELLATION is by construction an N-only award, so
# gating on the method never touches an award another net independently held.
_METHOD_PRIORITY = (
    TERMINATION_ACTION_CODE,
    TERMINATION_LANGUAGE,
    LEGAL_CONTRACT_CANCELLATION,
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


def _local_method(row) -> str:
    """The public method for one Local Mirror net row.

    The mirror runs ONE action-code net covering both F and N, so the net label
    alone cannot say which was found. The published method splits where the net
    does not, keyed on the row's own action_type - Q1 already selects it.
    """
    net = str(row.get("detection_method") or "")
    if net == "action_code" and str(row.get("action_type") or "").upper() == "N":
        return LEGAL_CONTRACT_CANCELLATION
    return _LOCAL_NET_METHODS.get(net, "")


def primary_local_method(rows) -> str:
    """Return the primary public method for a group of Local Mirror net rows."""
    methods = {_local_method(row) for row in rows}
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

    # Tested in _METHOD_PRIORITY order, which is load-bearing rather than
    # stylistic: the mirror joins one award's net phrases with "; ", so a row
    # found by two nets contains both strings and first-match decides. An award
    # carrying an F alongside an N must read as the F.
    detection = str(row.get("Detection Evidence") or "").casefold()
    if "terminate-for-convenience action" in detection:
        return TERMINATION_ACTION_CODE
    if "termination-language transaction" in detection:
        return TERMINATION_LANGUAGE
    if "legal-contract-cancellation action" in detection:
        return LEGAL_CONTRACT_CANCELLATION
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
