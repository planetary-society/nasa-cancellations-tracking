"""The shared brain: tracking window, termination vocabulary, Txn dataclass, acceptance rules.

Two detection doors feed this module: the local Postgres mirror (SQL) and the
USAspending API (ORM keyword/code sweeps). Both normalise whatever their
upstream hands over into a `Txn` and then call `accept_award()`. That is the
only place a termination-for-convenience verdict is reached.

The SQL WHERE clause and the ORM filters are coarse PREFILTERS, never deciders:
each must return a SUPERSET of what the Python predicates accept (the mirror's
text arm even keeps `CAUSE_TEXT` in the net, which `is_cause` then drops here).
A prefilter that is narrower than the Python verdict silently loses awards, and
no test downstream would notice - so the containment runs the other way, and is
test-enforced.

This module imports nothing from the project and nothing heavier than the
standard library.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

# ---------------------------------------------------------------------------
# The tracking window
# ---------------------------------------------------------------------------

# The window opens at the second-term inauguration. In the mirror's SQL this
# bound does double duty: without an action_date bound Postgres cannot use the
# index on transaction_search and every net degrades into a seq scan.
WINDOW_START = date(2025, 1, 20)
WINDOW_START_ISO = WINDOW_START.isoformat()


# ---------------------------------------------------------------------------
# Scope: how each door names NASA
# ---------------------------------------------------------------------------

# The asymmetry is deliberate. The mirror has the raw column and filters on the
# awarding agency's numeric id; the public API offers no such filter and takes
# the toptier agency NAME instead. Both are visible in the output's `sources`
# column, so a divergence between the two nets is observable rather than silent.
NASA_AGENCY_ID = 862
NASA_TOPTIER = "National Aeronautics and Space Administration"


def as_date(value) -> date | None:
    """Parse a date, datetime, or date-ish string, or return None.

    Sources hand dates over in whatever form their upstream produced: psycopg
    returns `date`, the USAspending API returns ISO strings that sometimes
    carry a time component.

    This genuinely parses rather than comparing ISO text, because a string
    comparison silently accepts garbage: 'not-a-date' happens to be ten
    characters and sorts after '2025-01-20', so a text-only gate would have
    called it in-window.
    """
    if value is None:
        return None
    # datetime is a subclass of date, so this order matters.
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    # '2025-09-02T00:00:00' and '2025-09-02 00:00:00' both truncate correctly.
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def in_window(value) -> bool:
    """True when an action falls on or after the window start.

    A missing or unparseable date is NOT in the window: the gate's job is to
    keep pre-window actions out, and an unknown date is not evidence of an
    in-window one.
    """
    parsed = as_date(value)
    return parsed is not None and parsed >= WINDOW_START


# ---------------------------------------------------------------------------
# Action codes
# ---------------------------------------------------------------------------

# FPDS reason-for-modification codes: "F" = TERMINATE FOR CONVENIENCE (COMPLETE
# OR PARTIAL), "N" = LEGAL CONTRACT CANCELLATION. Codes "E"/"X" (terminate for
# cause / for default) are excluded by methodology - contractor failure, not a
# policy cancellation. FABS (grants) has no equivalent field at all, so grant
# terminations are language-only.
CANCELLATION_FOR_CONVENIENCE_ACTION_CODE = "F"
TERMINATION_ACTION_CODES: tuple[str, ...] = (CANCELLATION_FOR_CONVENIENCE_ACTION_CODE, "N")

# Only "F" is trusted on its own. NASA applies "N" to routine administrative
# actions too - 80JSC026F0015 ("implement the ax-5 mission specific option") and
# 80JSC026P0010 (a freeze dryer purchase) both carry it - so an N-coded
# transaction counts only when its description also asserts a termination
# (decided 2026-08-20; replaces the old mirror-dependent POP-shortening gate).
STANDALONE_TERMINATION_CODES: tuple[str, ...] = (CANCELLATION_FOR_CONVENIENCE_ACTION_CODE,)

# The award types whose transactions carry FPDS action codes.
FPDS_AWARD_TYPES = ("contract", "idv")


# ---------------------------------------------------------------------------
# Award identity and type
# ---------------------------------------------------------------------------

_FALLBACK_KEY_PREFIX = "PIID:"


def award_key(generated_id, native_id) -> str:
    """The key both doors group an award's transactions under.

    The generated award id when the row carries one. Some IDV transactions
    carry none at all, so the PIID/FAIN is the fallback - namespaced, because a
    bare PIID dropped into the generated id's key space could collide with an
    unrelated award.
    """
    return generated_id or f"{_FALLBACK_KEY_PREFIX}{native_id}"


def is_fallback_key(key: str) -> bool:
    """True when this key is a namespaced PIID rather than a generated award id."""
    return key.startswith(_FALLBACK_KEY_PREFIX)


def award_type(*, type_code="", generated_id="", is_fpds=False) -> str:
    """The award's contract | idv | grant type, from whichever inputs a door has.

    IDV_* type codes are indefinite-delivery vehicles, and so are awards whose
    generated id starts CONT_IDV_ - queries that project one, the other, or both
    all reach the same answer here. A-D are definitive contracts; everything
    else is assistance, which this project calls a grant.

    is_fpds is the backstop for a missing code: FABS transactions never carry a
    contract type, and the award type is what gates the FPDS action code below.
    """
    if str(type_code or "").strip().upper().startswith("IDV_"):
        return "idv"
    if str(generated_id or "").strip().upper().startswith("CONT_IDV_"):
        return "idv"
    return "contract" if is_fpds else "grant"


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# Asserts a termination/stop-work happened. Narrow on purpose: every
# alternative carries a qualifier, so a bare "terminated" does not match.
TERM_TEXT = re.compile(
    # NASA misspells "convenience", and not rarely: across 7,518 archived source
    # descriptions the corpus holds `terminate for convience` (345 rows),
    # `forconvenience` (36, no separator at all), `termination for connivence`
    # (8) and `termination for convicne` (1). `[\s-]*` admits the run-together
    # form; `con[vn]\w*` admits every observed spelling and cannot reach
    # "cause" or "default", which CAUSE_TEXT owns.
    #
    # No bare `terminat\w*` alternative is listed, and the evidence is two
    # awards that a broad net published as cancellations before this vocabulary
    # existed: 80LARC26F7025 (advisory work on SLS flight-termination-system
    # core battery qualification failures) and 80NSSC26P0092 (a purchase of a
    # flight termination receiver/decoder). "Flight termination system" is the
    # range-safety device that destroys a launch vehicle in flight - hardware,
    # not a contract action.
    r"terminat(?:e|ed|ion)[\s-]*for[\s-]*con[vn]\w*|\bt4c\b|stop[\s-]?work|"
    r"termination\s+settlement|notice\s+of\s+termination|"
    # Both orders. "notice of termination" alone missed 80JSC022CA012,
    # "TERMINATION NOTICE ISSUED: IN SPACE PRODUCTION APPLICATIONS".
    r"terminat(?:ion|ed)\s+notice|"
    # The prose an N-coded cancellation carries. Detection is now this
    # vocabulary's only job, so it belongs in the main pattern.
    r"legal\s+contract\s+cancellation",
    re.IGNORECASE,
)

# Termination for cause or default: contractor failure, excluded by
# methodology (commit 08a52cf) rather than counted as a policy cancellation.
CAUSE_TEXT = re.compile(r"terminat\w*\s+for\s+(?:cause|default)", re.IGNORECASE)

# A court vacated the termination - a legal fact that outranks later activity.
VACATUR_TEXT = re.compile(r"\bvacat\w*", re.IGNORECASE)

# "set aside" is a vacatur only in context: on its own it is a procurement
# category ("100% small business set aside"), so it needs a termination
# subject alongside it - the same guard the reversal vocabulary uses.
SET_ASIDE_TEXT = re.compile(r"\bset\s+aside\b", re.IGNORECASE)

# The two halves of a reversal. Both must match the same text: a reversal word
# has to co-occur with a termination subject, because "cancellation of the
# stop-work order" reverses a termination while "contract cancellation" IS one.
# Bare "cancel" is deliberately absent from the reversal vocabulary for that
# reason; the one cancel-shaped alternative below is self-guarded - it requires
# the "of ... <subject>" construction within two words, so "cancellation of
# partial stop work notice" (80JSC024F0024/80JSC024F0026, which annul a
# stop-work) reverses while "contract cancellation" stays a termination.
# Apply these per description, never to a concatenation of several -
# joining an award's descriptions would let a reversal word in one entry pair
# with a termination subject in an unrelated one.
REVERSAL_TEXT = re.compile(
    r"rescind\w*|rescission|reinstat\w*|resum\w+\s+of\s+work|"
    r"cancell?\w*\s+of\s+(?:\w+\s+){0,2}(?:stop[\s-]?work|terminat\w*|suspension)",
    re.IGNORECASE,
)
REVERSAL_SUBJECT = re.compile(r"stop[\s-]?work|terminat\w*|suspension", re.IGNORECASE)


def is_termination(text) -> bool:
    """True when this text asserts a termination or stop-work action."""
    return bool(TERM_TEXT.search(text or ""))


def is_cause(text) -> bool:
    """True when this text describes a termination for cause or default."""
    return bool(CAUSE_TEXT.search(text or ""))


def is_reversal(text) -> bool:
    """True when this text rescinds or reinstates a termination.

    Requires a reversal word AND a termination subject in the same text, so
    "rescind the small business set-aside designation" is not a reversal.
    """
    text = text or ""
    return bool(REVERSAL_TEXT.search(text) and REVERSAL_SUBJECT.search(text))


def is_vacatur(text) -> bool:
    """True when this text says a termination was vacated or set aside.

    A "vacat*" word stands on its own; "set aside" additionally requires a
    termination subject, so a small-business set-aside designation is not read
    as a court vacating a termination.
    """
    text = text or ""
    if VACATUR_TEXT.search(text):
        return True
    return bool(SET_ASIDE_TEXT.search(text) and REVERSAL_SUBJECT.search(text))


# ---------------------------------------------------------------------------
# Rendering the vocabulary for the two doors
# ---------------------------------------------------------------------------

_LOOKAROUND = re.compile(r"\(\?[=!<]")


def pg_regex(*patterns: re.Pattern) -> str:
    """Render Python vocabulary patterns as one Postgres ARE alternation.

    Postgres AREs already understand (?:...), \\s and \\w, so the only
    translation needed is the word boundary: \\b means backspace there, and
    \\y is the boundary. Lookarounds have no ARE equivalent at all, so a
    pattern carrying one is a build error rather than a silently different net.
    """
    for pattern in patterns:
        if _LOOKAROUND.search(pattern.pattern):
            raise ValueError(f"lookaround has no Postgres ARE equivalent: {pattern.pattern!r}")
    return "|".join(pattern.pattern.replace(r"\b", r"\y") for pattern in patterns)


# The historical statistics need the same positive and exclusion predicates as
# the Python classifier, while the candidate query deliberately keeps cause in
# its wider prefilter so `is_cause` remains the final judge there.
TERMINATION_KEYWORD_SQL = pg_regex(TERM_TEXT)
CAUSE_TEXT_SQL = pg_regex(CAUSE_TEXT)
TERMINATION_TEXT_SQL = pg_regex(TERM_TEXT, CAUSE_TEXT)

# Wire strings sent verbatim to the USAspending API as `filters.keywords`; they
# cannot become regexes (`con[vn]\w*` has no finite expansion and the wire
# format has no alternation), so this list enumerates TERM_TEXT's fixed phrases
# and only the regex-only forms - the `con[vn]\w*` misspelling family, the
# hyphen/space variants the pattern folds together - stay unenumerated.
#
# The API OR-combines every term into ONE filter, so the whole list costs a
# single paginated sweep however long it is. That is why it is no longer
# pruned: the old list was trimmed under a per-keyword-request cost that no
# longer exists, and each phrase it left out was a phrase the mirror door could
# find and the daily API door could not. A term that widens the result set only
# gives the judge more rows to reject, which is the cheap direction to be wrong
# in. Empirically checked against the live API on 2026-07-30: "stop-work"
# returned a byte-identical result set to "stop work" (the API normalises the
# hyphen). Every keyword must satisfy is_termination() - asserted in tests.
API_KEYWORDS: tuple[str, ...] = (
    "terminate for convenience",
    "terminate-for-convenience",
    "termination for convenience",
    "stop work",
    "terminate for convience",
    "termination for connivence",
    "termination for convicne",
    # The observed run-together spelling. It keeps its "terminate" prefix
    # because a bare "forconvenience" is not a termination on its own, and
    # every wire keyword has to satisfy is_termination().
    "terminate forconvenience",
    "t4c",
    "termination settlement",
    "notice of termination",
    "termination notice",
    "legal contract cancellation",
)


# ---------------------------------------------------------------------------
# The normalised transaction, and the verdict
# ---------------------------------------------------------------------------

# Wide enough for any modification number NASA issues; the padding only has to
# make same-length comparisons of one award's mods correct.
_MOD_DIGITS = 8


def mod_sort_key(mod) -> str:
    """A modification number that sorts in issue order as plain text.

    Mod numbers break ties within one action_date, which is exactly where a
    termination and its rescission collide - so getting the order wrong reverses
    the verdict. Compared as raw text, mod "10" sorts BEFORE mod "9", and the
    padded FPDS forms ("P00010") and unpadded FABS ones ("9") sit in the same
    column. Zero-padding every digit run orders both, and leaves the surrounding
    letters ("P00010", "A-2") comparing as before.
    """
    return re.sub(r"\d+", lambda match: match.group().zfill(_MOD_DIGITS), str(mod or ""))


@dataclass(frozen=True, slots=True)
class Location:
    """One place, as USAspending reports it on the award-level record.

    Used for both the recipient's location and the place of performance. A POP
    never carries street address lines - USAspending reports no street address
    for places of performance, only city/state/zip - so a POP's address fields
    are always "".
    """

    address1: str = ""
    address2: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    # Congressional district code ("04"). The mirror serves the CURRENT
    # (post-redistricting) code with the as-reported one as fallback; the
    # public API exposes only the as-reported code, so that is what the API
    # door can supply - the mirror-wins merge upgrades shared rows.
    district: str = ""


# Immutable, so one instance is a safe default for every Txn.
EMPTY_LOCATION = Location()


@dataclass(frozen=True, slots=True)
class Txn:
    """One award transaction, normalised out of either door."""

    award_key: str
    award_id: str
    generated_award_id: str
    award_type: str  # contract | idv | grant
    recipient_name: str = ""
    action_date: date | None = None
    action_type: str = ""  # FPDS action code; "" for FABS (grants)
    modification_number: str = ""
    description: str = ""
    # The AWARD's current summary and locations on USAspending, not this
    # transaction's own fields. Enrichment only: no predicate below reads
    # them, so a door may leave them empty.
    award_description: str = ""
    recipient_location: Location = EMPTY_LOCATION
    pop_location: Location = EMPTY_LOCATION
    # The explicit USAspending award type code ("A", "IDV_C", "02", ...) behind
    # the contract|idv|grant grouping above. The mirror reads it off every
    # transaction row; the API door reads it off the award detail record.
    award_type_code: str = ""
    total_obligated: Decimal | None = None
    total_potential_value: Decimal | None = None
    amount: Decimal | None = None
    source: str = ""  # mirror | api
    sort_key: str = ""  # tie-break within one action_date


def has_termination_code(txn: Txn) -> bool:
    """True when this transaction carries an FPDS termination reason code.

    Grants are excluded by award type, not by the code: FABS has no
    reason-for-modification field, so an "F" on a grant row means nothing.
    """
    if txn.award_type not in FPDS_AWARD_TYPES:
        return False
    return txn.action_type.upper() in TERMINATION_ACTION_CODES


def is_explicit_termination(txn: Txn) -> bool:
    """True when this transaction explicitly terminates the award for convenience.

    Explicit means an FPDS "F" action code or termination language - never an
    inference from the shape of the data. An "N" code alone is not enough (see
    STANDALONE_TERMINATION_CODES): it only ever confirms language, so the N arm
    is subsumed by the language test.
    """
    if not in_window(txn.action_date):
        return False
    if is_cause(txn.description):
        return False
    if has_termination_code(txn) and txn.action_type.upper() in STANDALONE_TERMINATION_CODES:
        return True
    return is_termination(txn.description)


def detected_by(txn: Txn) -> str:
    """Which door's evidence this transaction carries: action_code | description | both."""
    by_code = has_termination_code(txn)
    by_text = is_termination(txn.description)
    if by_code and by_text:
        return "both"
    return "action_code" if by_code else "description"


def group_by_award(txns) -> dict[str, list[Txn]]:
    """An award's transactions gathered under its key, ready for `accept_award`.

    Both doors collect transactions award-blind - the mirror in one flat result
    set, the API across several sweeps - and both need them grouped before a
    verdict can be reached.
    """
    groups: dict[str, list[Txn]] = {}
    for txn in txns:
        groups.setdefault(txn.award_key, []).append(txn)
    return groups


def accept_award(txns: Sequence[Txn]) -> Txn | None:
    """The award's operative termination, or None if it has none.

    Scans the award's history chronologically: the FIRST explicit termination
    sets the anchor, a later reversal or vacatur clears it, and the first
    CODED termination action supersedes an earlier language-only anchor - the
    reason-for-modification code is the unambiguous signal, where stop-work
    language can precede the formal act. The date of record is when the
    termination was issued: 80GSFC23CA001 got a stop-work notice on
    2025-03-18, mod 00009's F code on 2025-05-01, and a year of settlement
    mods after - it reports 2025-05-01, and neither the earlier prose nor the
    later settlements move that. An award no code ever confirms (every grant -
    FABS has no code - and prose-only contracts) anchors on its earliest
    language. terminate->rescind still drops out entirely, and terminate->
    rescind->terminate reports the post-rescission termination.

    Reversals are tested first because a rescission names what it rescinds:
    "rescission of the stop work order" matches the termination vocabulary too,
    and reading it as a termination would make a reversal un-reversible.
    """
    anchor: Txn | None = None
    for txn in sorted(txns, key=lambda t: (t.action_date or date.min, t.sort_key)):
        if is_reversal(txn.description) or is_vacatur(txn.description):
            anchor = None
        elif is_explicit_termination(txn) and (
            anchor is None or (has_termination_code(txn) and not has_termination_code(anchor))
        ):
            anchor = txn
    return anchor


# A period-of-performance pull-back smaller than this is ordinary contract
# administration, not a lead worth reviewing.
SHORTENING_MIN_DAYS = 90
