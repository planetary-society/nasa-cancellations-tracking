"""How a ledger row records which detectors flagged an award.

The Sources cell is a joined string, so every read of it used to re-derive the
format at the call site. These are the two operations that were being
re-derived, plus the one that was being got wrong.
"""

import build_master_ledger
import sources


def test_sources_of_splits_the_joined_cell():
    assert sources.sources_of({"Sources": "NPDV; DOGE"}) == ["NPDV", "DOGE"]


def test_sources_of_handles_absent_empty_and_single():
    assert sources.sources_of({}) == []
    assert sources.sources_of({"Sources": ""}) == []
    assert sources.sources_of({"Sources": None}) == []
    assert sources.sources_of({"Sources": "DOGE"}) == ["DOGE"]


def test_has_source_is_not_a_substring_test():
    """`"FPDS" in rec["Sources"]` was true for any label containing "FPDS".

    Nothing was misclassified by it, because no current label contains
    another's name - but that is a property of today's six labels, not of the
    check, and the whole point of naming them in one place is to be able to
    change them.
    """
    rec = {"Sources": "LegacyFPDSMirror"}

    assert "FPDS" in rec["Sources"]  # what the old check asked
    assert not sources.has_source(rec, sources.FPDS)  # what it meant to ask


def test_has_source_matches_whole_labels():
    rec = {"Sources": "NPDV; FPDS; USAspendingTerminations"}

    assert sources.has_source(rec, sources.FPDS)
    assert sources.has_source(rec, sources.USASPENDING_TERMINATIONS)
    assert not sources.has_source(rec, sources.DOGE)


def test_add_source_appends_and_stays_idempotent():
    rec = {"Sources": "NPDV"}

    sources.add_source(rec, sources.DOGE)
    assert rec["Sources"] == "NPDV; DOGE"

    sources.add_source(rec, sources.DOGE)
    assert rec["Sources"] == "NPDV; DOGE"


def test_add_source_seeds_an_empty_cell_without_a_leading_separator():
    rec = {"Sources": ""}

    sources.add_source(rec, sources.NPDV)

    assert rec["Sources"] == "NPDV"


def test_add_source_ignores_a_blank_name():
    """A snapshot row with no Source must not append an empty segment."""
    rec = {"Sources": "NPDV"}

    sources.add_source(rec, "")

    assert rec["Sources"] == "NPDV"


def test_every_query_source_has_a_detection_fallback():
    """A new source must declare what its legacy snapshots imply.

    detection_methods back-fills Primary Detection Method from the source alone
    for rows archived before the structured field existed. A source missing
    from that table silently degrades to LEGACY_SOURCE_SIGNAL.
    """
    import detection_methods
    import search

    assert set(search.SOURCES) <= set(detection_methods._SOURCE_FALLBACKS)


def test_experiment_source_is_a_real_source():
    assert build_master_ledger.EXPERIMENT_SOURCE in sources.ALL_SOURCES
