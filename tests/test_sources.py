"""How a ledger row records which detectors flagged an award.

The Sources cell is a joined string, so every read of it used to re-derive the
format at the call site. These are the two operations that were being
re-derived, plus the one that was being got wrong.
"""

import build_master_ledger
import sources


def test_sources_of_splits_the_joined_cell():
    assert sources.sources_of({"Flagged By": "NPDV; DOGE"}) == ["NPDV", "DOGE"]


def test_sources_of_handles_absent_empty_and_single():
    assert sources.sources_of({}) == []
    assert sources.sources_of({"Flagged By": ""}) == []
    assert sources.sources_of({"Flagged By": None}) == []
    assert sources.sources_of({"Flagged By": "DOGE"}) == ["DOGE"]


def test_has_source_does_not_match_a_label_that_merely_contains_another():
    """classify() used `"FPDS" in rec["Flagged By"]`, a substring test.

    Nothing is misclassified by it today, because no current label contains
    another's name - but that is a property of the six labels, not of the
    check, and the point of naming them in one place is to be able to change
    them.
    """
    rec = {"Flagged By": f"Legacy{sources.FPDS}Mirror"}

    assert not sources.has_source(rec, sources.FPDS)


def test_has_source_matches_whole_labels():
    rec = {"Flagged By": "NPDV; FPDS; USAspendingTerminations"}

    assert sources.has_source(rec, sources.FPDS)
    assert sources.has_source(rec, sources.USASPENDING_TERMINATIONS)
    assert not sources.has_source(rec, sources.DOGE)


def test_add_source_appends_and_stays_idempotent():
    rec = {"Flagged By": "NPDV"}

    sources.add_source(rec, sources.DOGE)
    assert rec["Flagged By"] == "NPDV; DOGE"

    sources.add_source(rec, sources.DOGE)
    assert rec["Flagged By"] == "NPDV; DOGE"


def test_add_source_seeds_an_empty_cell_without_a_leading_separator():
    rec = {"Flagged By": ""}

    sources.add_source(rec, sources.NPDV)

    assert rec["Flagged By"] == "NPDV"


def test_add_source_ignores_a_blank_name():
    """A snapshot row with no Source must not append an empty segment."""
    rec = {"Flagged By": "NPDV"}

    sources.add_source(rec, "")

    assert rec["Flagged By"] == "NPDV"


def test_a_snapshot_without_a_required_source_is_degraded():
    """The NPDV row every synthetic snapshot carries is not decoration.

    is_degraded() discards a whole snapshot when a whole-corpus source is
    absent, on the grounds that the fetch failed rather than that nothing was
    found. Naming the set is what keeps that invariant findable.
    """
    healthy = {"A-1": {"Source": sources.NPDV}, "A-2": {"Source": sources.DOGE}}
    broken = {"A-2": {"Source": sources.DOGE}}

    assert not build_master_ledger.is_degraded(healthy)
    assert build_master_ledger.is_degraded(broken)
    assert not build_master_ledger.is_degraded({})
