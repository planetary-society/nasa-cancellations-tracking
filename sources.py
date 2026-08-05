"""The detectors that can flag an award, and how a ledger row records them.

A leaf module on purpose: search.py maps these names to query classes,
build_master_ledger.py accumulates them, and detection_methods.py reads them
back, so the names themselves cannot live in any one of those without the other
two importing it for a string.

The names are written into every daily snapshot and into the ledger, so
renaming one is a data migration rather than an edit. Production code spells
them only here; test fixtures still pin the literals deliberately, since a test
that imports the constant it is meant to be checking asserts nothing.
"""

# Source labels, as written to a snapshot's Source column and accumulated into
# the ledger's Sources column.
DOGE = "DOGE"
NPDV = "NPDV"
NASA_GRANTS = "NASAGrants"
LOCAL_MIRROR = "LocalUSASpendingMirror"
USASPENDING_TERMINATIONS = "USAspendingTerminations"

# Retired 2026-02-25: fpds.gov/ezsearch was shut down and now redirects to
# SAM.gov. No new row carries this label, but awards found by it are still in
# the ledger, and classify() keys the source_retired status on it.
FPDS = "FPDS"

# Sources whose absence from a snapshot means the run itself was broken, not
# that nothing was found. NPDV is the whole-corpus source: it reports on every
# NASA contract, so a snapshot with no NPDV row is a failed fetch, and on those
# days ~18 of its awards are misattributed to NASAGrants. Named here rather
# than left as a bare literal in is_degraded() because it is the reason every
# synthetic snapshot in the test suite has to carry an NPDV row.
REQUIRED_SOURCES = frozenset({NPDV})

# The ledger column that accumulates every source that ever flagged an award.
# One snapshot row is owned by one source and carries the singular "Source";
# this is the plural union, joined by _SEPARATOR.
SOURCES_COLUMN = "Flagged By"
_SEPARATOR = "; "


def sources_of(rec) -> list[str]:
    """Every source that has flagged this award, in first-observed order."""
    joined = str(rec.get(SOURCES_COLUMN) or "")
    return [name.strip() for name in joined.split(_SEPARATOR) if name.strip()]


def has_source(rec, name: str) -> bool:
    """Whether `name` flagged this award.

    Not a substring test. `"FPDS" in rec["Flagged By"]` was true for any label
    containing those four letters, which is the kind of thing that stays
    correct until a source is renamed.
    """
    return name in sources_of(rec)


def add_source(rec, name: str) -> None:
    """Record that `name` flagged this award, if it is not already recorded."""
    if not name:
        return
    flagged = sources_of(rec)
    if name not in flagged:
        rec[SOURCES_COLUMN] = _SEPARATOR.join([*flagged, name])
