"""The detectors that can flag an award, and how a row records which did.

A leaf module on purpose: search.py owns the mapping from these names to query
classes, build_master_ledger.py accumulates them, and detection_methods.py
reads them back, so the names themselves cannot live in any one of those
without the other two importing it for a string.

Two things live here together because they change together. The names are
written into every daily snapshot and into the ledger, so renaming one is a
data migration, not just an edit - and the only reason that migration is
tractable is that nothing outside this module spells them.
"""

# Source labels, as written to a snapshot's Source column and accumulated into
# the ledger's Sources column.
DOGE = "DOGE"
NPDV = "NPDV"
NASA_GRANTS = "NASAGrants"
LOCAL_MIRROR = "LocalUSASpendingMirror"
USASPENDING_TERMINATIONS = "USAspendingTerminations"

# Retired 2026-02-25: fpds.gov/ezsearch was shut down and now redirects to
# SAM.gov. No new row carries this label, but ~24 awards in the ledger were
# found by it, and classify() still keys the source_retired status on it.
FPDS = "FPDS"

# Every label that appears in the archived data, retired ones included - a
# rebuild replays snapshots going back to 2025-04, so FPDS rows are still read.
ALL_SOURCES = (
    DOGE,
    NPDV,
    NASA_GRANTS,
    USASPENDING_TERMINATIONS,
    LOCAL_MIRROR,
    FPDS,
)

# Sources that publish an external *claim* of a cancellation, as opposed to
# sources where we infer one from award data.
CLAIM_SOURCES = frozenset({DOGE})

# One snapshot row is owned by one source; a ledger row unions every source
# that ever flagged the award, joined by this separator.
SOURCE_COLUMN = "Source"
SOURCES_COLUMN = "Sources"
SEPARATOR = "; "


def sources_of(rec) -> list[str]:
    """Every source that has flagged this award, in first-observed order."""
    joined = str(rec.get(SOURCES_COLUMN) or "")
    return [name.strip() for name in joined.split(SEPARATOR) if name.strip()]


def has_source(rec, name: str) -> bool:
    """Whether `name` flagged this award.

    Not a substring test. `"FPDS" in rec["Sources"]` was true for any label
    containing those four letters, which is the kind of thing that stays
    correct until a source is renamed.
    """
    return name in sources_of(rec)


def add_source(rec, name: str) -> None:
    """Record that `name` flagged this award, if it is not already recorded."""
    if not name or has_source(rec, name):
        return
    existing = str(rec.get(SOURCES_COLUMN) or "")
    rec[SOURCES_COLUMN] = f"{existing}{SEPARATOR}{name}" if existing else name
