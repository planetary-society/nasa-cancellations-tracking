#!/usr/bin/env python3
"""
The one definition of "does this text assert a termination, a reversal of one,
or a termination for cause".

Two modules answer that question about the same awards: build_master_ledger's
classify(), inferring a status from the description history in the daily
snapshots, and reverify_awards, judging USAspending transaction descriptions
weekly. They used to answer it differently - classify() with bare substring
tests, reverify with guarded regexes - so the same text could yield different
statuses depending on which module saw it. These patterns are the guarded
versions, and both modules now share them.

Two guards are load-bearing and easy to lose in a rewrite:

  1. A reversal word must co-occur with a termination subject in the SAME text.
     "cancellation of the stop-work order" reverses a termination;
     "contract cancellation" is a termination. Bare "cancel" is deliberately
     absent from the reversal vocabulary for that reason.

  2. Callers must apply these per description, never to a concatenation of
     several. classify() holds a list of every description ever observed for an
     award; testing the joined string would let a reversal word in one entry
     pair with a termination subject in an unrelated one.

Deliberately NOT here, because they are query strings rather than predicates:

  * usaspending_terminations_query.SEARCH_KEYWORDS - sent verbatim to the
    USAspending API as `filters.keywords`. They cannot become regexes; the
    "stop work"/"stop-work" pair exists because the wire format has no
    alternation.
  * npdv_query.NPDVQuery.DEFAULT_SEARCH_PHRASES - a deliberately broad
    detection net (bare "termination", "effectuate"). TERM_TEXT is narrow by
    design; swapping one for the other would change what enters the snapshot.
"""

import re

# Asserts a termination/stop-work happened. Narrow on purpose: every
# alternative carries a qualifier, so a bare "terminated" does not match.
TERM_TEXT = re.compile(
    r"terminat(?:e|ed|ion)\s+for\s+convenience|\bt4c\b|stop[\s-]?work|"
    r"termination\s+settlement|notice\s+of\s+termination",
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

# Partial de-scope rather than full termination.
DESCOPE_TEXT = re.compile(
    r"de[\s-]?scope|partial\s+termination|reduce\s+scope", re.IGNORECASE
)

# Administrative closeout of an already-terminated award. Used to corroborate a
# closeout action code, never on its own.
CLOSEOUT_TEXT = re.compile(
    r"adjustment\s+to\s+completed\s+project|close[\s-]?out", re.IGNORECASE
)

# The two halves of a reversal. Both must match the same text - see guard 1.
REVERSAL_TEXT = re.compile(
    r"rescind\w*|rescission|reinstat\w*|resum\w+\s+of\s+work", re.IGNORECASE
)
REVERSAL_SUBJECT = re.compile(r"stop[\s-]?work|terminat\w*|suspension", re.IGNORECASE)


def is_reversal(text):
    """True when this text rescinds or reinstates a termination.

    Requires a reversal word AND a termination subject in the same text, so
    "rescind the small business set-aside designation" is not a reversal.
    """
    text = text or ""
    return bool(REVERSAL_TEXT.search(text) and REVERSAL_SUBJECT.search(text))


def is_cause(text):
    """True when this text describes a termination for cause or default."""
    return bool(CAUSE_TEXT.search(text or ""))


def is_vacatur(text):
    """True when this text says a termination was vacated or set aside.

    A "vacat*" word stands on its own; "set aside" additionally requires a
    termination subject, so a small-business set-aside designation is not read
    as a court vacating a termination.
    """
    text = text or ""
    if VACATUR_TEXT.search(text):
        return True
    return bool(SET_ASIDE_TEXT.search(text) and REVERSAL_SUBJECT.search(text))


def is_termination(text):
    """True when this text asserts a termination or stop-work action."""
    return bool(TERM_TEXT.search(text or ""))


def is_descope(text):
    """True when this text describes a partial de-scope, not a full termination."""
    return bool(DESCOPE_TEXT.search(text or ""))
