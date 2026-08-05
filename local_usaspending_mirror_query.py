#!/usr/bin/env python3
"""
Direct SQL detection against a full local mirror of the USAspending database.

The mirror is the complete `data_store_api` Postgres dump behind
USAspending.gov: `rpt.transaction_search` alone carries ~236M transaction
rows, and NASA is `awarding_agency_id = 862`. Having the raw tables locally
buys four detection nets the public API cannot run, or cannot run well:

  1. action codes - the API has no action-type *filter*, only a sort, so
     usaspending_terminations_query has to binary-search its way to each code
     block. Here it is a WHERE clause, and it can include IDV vehicle
     transactions the award-level API view splits awkwardly.
  2. description regex - the API takes plain keywords, never regexes, so the
     narrow guarded vocabulary can only be applied after download.
  3. end-date truncation - needs the entire modification history of every
     award back to 2007 to know what the period of performance used to be.
     That is one CTE here and tens of thousands of API calls there.
  4. pure clawbacks - needs a transaction-to-award join with award totals in
     the same query to compute the deobligated fraction server-side.

This source does NOT replace the API source. The mirror is a periodic dump
and lags the live API by roughly 2-6 weeks, so a termination filed last week
is simply absent from it. usaspending_terminations_query stays in the
pipeline as the recency net; this one is the depth net.

Local-only by construction: the mirror lives on the operator's LAN and is
unreachable from GitHub Actions. On CI (and on any machine without the DB
credentials) search.py skips this optional source. Successful live queries are
still exported for auditability, but an old export is never presented as a
current database result.
"""

import os
import sys
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import date
from typing import NamedTuple

import pandas as pd
from dotenv import load_dotenv

import award_period_change_facts
from award_period import SHORTENING_MIN_DAYS
from contract_query import FINAL_COLUMNS, ContractQuery
from detection_methods import primary_local_method
from initial_end_dates import (
    InitialEndDateResult,
    InitialEndDateTarget,
    select_initial_reported_end_date,
)
from termination_vocabulary import (
    CAUSE_TEXT,
    TERM_TEXT,
    is_cause,
    is_reversal,
    is_vacatur,
)
from tracking_window import TRACKING_WINDOW_START, to_iso

# The sibling API source is the home for the detection constants both sources
# share - the action codes, their phrasing, and the clawback thresholds. They
# describe the same methodology applied to the same data through two different
# doors, so they must not be allowed to drift. No import cycle: the sibling
# knows nothing about this module.
from usaspending_terminations_query import (
    ACTION_CODE_KINDS,
    CLAWBACK_AMOUNT_THRESHOLD,
    CLAWBACK_FRACTION_THRESHOLD,
    TERMINATION_ACTION_CODES,
)

# The DB credentials live in .env, never in the repo.
load_dotenv()

# Primary form: one full postgresql:// string.
DSN_ENV_VAR = "DATABASE_URI"

# Fallback form, kept because the operator's .env predates DATABASE_URI.
# NOTE the naming quirk: DB_URI holds the *host*, not a URI. Renaming it would
# break the existing .env, so the quirk is documented rather than fixed.
COMPONENT_ENV_VARS = ("DB_USER", "DB_PASS", "DB_URI", "DB_PORT", "DB_NAME")

EXPORT_BASE = "usaspending_database_direct_query"

# Keeps a sleeping or unplugged mirror host from hanging the run before any
# statement_timeout gets a chance.
CONNECT_TIMEOUT_S = 10


class LocalMirrorUnavailableError(RuntimeError):
    """The optional local mirror could not be reached for this run."""


def _pg_pattern(*regexes) -> str:
    """Render Python vocabulary patterns in Postgres's ARE dialect.

    Postgres AREs already understand (?:...), \\s and \\w, so the only
    translation needed is the word boundary: \\b means backspace there, and
    \\y is the boundary.
    """
    return "|".join(regex.pattern.replace(r"\b", r"\y") for regex in regexes)


# A LOCAL addition to the shared vocabulary, deliberately not promoted into
# termination_vocabulary: this is the prose an N-coded cancellation carries,
# i.e. detection language for this net. TERM_TEXT is a classification
# predicate applied to snapshot history, and widening it would change how
# already-collected awards are judged.
LEGAL_CANCELLATION_TEXT = r"legal\s+contract\s+cancellation"

# Built from the shared predicates rather than transliterated by hand, so the
# vocabulary genuinely has one home: widening TERM_TEXT widens this net too.
TERMINATION_TEXT_SQL = (
    f"({_pg_pattern(TERM_TEXT, CAUSE_TEXT)}|{LEGAL_CANCELLATION_TEXT})"
)

# The clawback fraction is needed twice - selected for the status phrase and
# tested in the WHERE clause - and the two must be the same expression.
CLAWBACK_FRACTION_SQL = (
    "(-ts.federal_action_obligation) "
    "/ NULLIF(aw.total_obligation - ts.federal_action_obligation, 0)"
)


# ---------------------------------------------------------------------------
# The four detection nets
#
# Every SELECT yields the same nine common columns - award_id_native,
# generated_unique_award_id, is_fpds, modification_number, action_date,
# transaction_description, federal_action_obligation, recipient_name,
# detection_method - plus whatever extra columns its own status phrase needs.
# Identical keys across the nets are what keeps _combine() a plain merge
# instead of four special cases.
#
# These constants are query text only; each net's statement timeout is a
# separate field on its NETS entry below.
# ---------------------------------------------------------------------------

Q1_ACTION_CODES = f"""
-- FPDS reason-for-modification codes.
--   F = terminate for convenience (complete or partial)
--   N = legal contract cancellation
-- Default (code E) and cause (code X) are excluded: both mean the contractor
-- failed, not that the government cancelled a policy commitment. That is the
-- same line the shared is_cause predicate draws on description text.
-- Closeout (code K) is excluded too, but for a different reason: a closeout
-- mod corroborates a termination someone else already detected, and on its own
-- it fires on every award that ever ended normally.
--
-- is_fpds = TRUE deliberately keeps IDV vehicle transactions in scope. A
-- F/N-coded sweep over the vehicles found 30 terminated IDVs that the
-- award-level API source never surfaced.
SELECT COALESCE(ts.piid, ts.fain, ts.uri) AS award_id_native,
       ts.generated_unique_award_id,
       ts.is_fpds,
       ts.modification_number,
       ts.action_date,
       ts.action_type,
       ts.action_type_description,
       ts.transaction_description,
       ts.federal_action_obligation,
       ts.recipient_name,
       'action_code' AS detection_method
FROM rpt.transaction_search ts
WHERE ts.is_fpds = TRUE
  AND ts.awarding_agency_id = 862
  AND ts.action_date >= '{TRACKING_WINDOW_START}'
  AND ts.action_type IN ({", ".join(f"'{code}'" for code in TERMINATION_ACTION_CODES)})
ORDER BY ts.action_date
"""

Q2_DESCRIPTION_REGEX = f"""
-- Description-text termination language across FPDS and FABS (no is_fpds
-- filter - grants express terminations in prose too).
--
-- The cause/default alternative is deliberately still IN the regex: matching
-- it here and dropping it in Python via termination_vocabulary.is_cause keeps
-- one home for the vocabulary. If the SQL excluded cause itself, the
-- definition of "for cause" would live in two places and drift.
SELECT COALESCE(ts.piid, ts.fain, ts.uri) AS award_id_native,
       ts.generated_unique_award_id,
       ts.is_fpds,
       ts.modification_number,
       ts.action_date,
       ts.transaction_description,
       ts.federal_action_obligation,
       ts.recipient_name,
       'description_regex' AS detection_method
FROM rpt.transaction_search ts
WHERE ts.awarding_agency_id = 862
  AND ts.action_date >= '{TRACKING_WINDOW_START}'
  AND ts.transaction_description ~* '{TERMINATION_TEXT_SQL}'
ORDER BY ts.action_date
"""

# One process has one run date. Interpolating the Python clock (rather than
# CURRENT_DATE from the mirror) keeps the query, sidecar validation, output
# filename, and the orchestrator's idea of "today" on the same timezone.
PERIOD_CHANGE_RUN_DATE = date.today().isoformat()

# Natural enough for USAspending modification identifiers, whose observed
# shapes contain a text prefix and one numeric run (P0002, P00010, 2, 10).
# transaction_unique_id is still the final deterministic tie-breaker.
_NATURAL_MOD_ORDER_SQL = """
lower(regexp_replace(COALESCE(modification_number, ''), '[0-9]+', '', 'g')),
NULLIF(regexp_replace(COALESCE(modification_number, ''), '[^0-9]', '', 'g'), '')::numeric NULLS FIRST,
lower(COALESCE(modification_number, '')),
transaction_id
""".strip()
_NATURAL_MOD_ORDER_DESC_SQL = """
lower(regexp_replace(COALESCE(modification_number, ''), '[0-9]+', '', 'g')) DESC,
NULLIF(regexp_replace(COALESCE(modification_number, ''), '[^0-9]', '', 'g'), '')::numeric DESC NULLS LAST,
lower(COALESCE(modification_number, '')) DESC,
transaction_id DESC
""".strip()

Q3_END_DATE_TRUNCATION = f"""
-- Largest consecutive backwards period-of-performance change per award,
-- FPDS + FABS. This is deliberately a historical event detector: a later
-- continuation or extension does not erase a suspicious shortening action.
-- Null end dates are removed before LAG, so a transaction that omits the field
-- does not break the chain between the surrounding dated records.
WITH nasa_txn AS (
  SELECT ts.award_id,
         COALESCE(ts.piid, ts.fain, ts.uri) AS award_id_native,
         ts.generated_unique_award_id,
         ts.transaction_unique_id AS transaction_id,
         ts.is_fpds,
         ts.modification_number,
         ts.action_date,
         ts.transaction_description,
         ts.federal_action_obligation,
         ts.recipient_name,
         COALESCE(
             NULLIF(TRIM(ts.ordering_period_end_date), '')::date,
             ts.period_of_performance_current_end_date
         ) AS end_date
  FROM rpt.transaction_search ts
  WHERE ts.awarding_agency_id = 862
    AND ts.action_date >= '2007-10-01'
    AND ts.award_id IN (
      SELECT t2.award_id
      FROM rpt.transaction_search t2
      WHERE t2.awarding_agency_id = 862
        AND t2.action_date > '{TRACKING_WINDOW_START}'
    )
),
dated AS (
  SELECT *,
         LAG(end_date) OVER (
           PARTITION BY award_id
           ORDER BY action_date, {_NATURAL_MOD_ORDER_SQL}
         ) AS previous_end_date
  FROM nasa_txn
  WHERE end_date IS NOT NULL
),
candidates AS (
  SELECT *,
         previous_end_date - end_date AS days_truncated
  FROM dated
  WHERE previous_end_date IS NOT NULL
    -- Strict action boundary: inauguration day itself does not qualify for
    -- this heuristic, per the method decision recorded on 2026-08-02.
    AND action_date > '{TRACKING_WINDOW_START}'
    -- The shortened-to date must describe an effect within the policy window
    -- that has happened by this run, not a future re-baseline.
    AND end_date BETWEEN '{TRACKING_WINDOW_START}' AND '{PERIOD_CHANGE_RUN_DATE}'
    AND previous_end_date - end_date > {SHORTENING_MIN_DAYS}
    -- Zero-dollar administrative changes are informative here; positive
    -- funding actions are not suspicious shortening events.
    AND federal_action_obligation <= 0
),
largest AS (
  SELECT *,
         ROW_NUMBER() OVER (
           PARTITION BY award_id
           ORDER BY days_truncated DESC,
                    action_date DESC,
                    {_NATURAL_MOD_ORDER_DESC_SQL}
         ) AS shortening_rank
  FROM candidates
)
SELECT award_id_native,
       generated_unique_award_id,
       transaction_id,
       is_fpds,
       modification_number,
       action_date,
       transaction_description,
       federal_action_obligation,
       recipient_name,
       previous_end_date,
       end_date,
       days_truncated,
       'end_date_truncation' AS detection_method
FROM largest
WHERE shortening_rank = 1
ORDER BY days_truncated DESC
"""

Q4_PURE_CLAWBACKS = f"""
-- Grants killed by money removal alone: no termination language, no date
-- truncation, no action code that means anything. Q1-Q3 cannot see these by
-- construction, which is why this net exists at all.
--
-- Canonical catch: 80NSSC25K0030 (Brown University), $448K awarded 2025-07 and
-- 100% clawed back 2026-01 under an ordinary action type D.
--
-- The inside-PoP gate is load-bearing, not cosmetic. Measured 2026-07-30, 109
-- transactions cleared the amount and fraction thresholds but 91 of them were
-- routine post-expiry underruns - a finished grant handing back what it did
-- not spend. Without `action_date < period_of_performance_current_end_date`
-- those 91 would flood the snapshot with non-cancellations.
--
-- The denominator is the PRE-clawback total: total_obligation already has the
-- deobligation applied, so subtracting the (negative) amount adds it back.
SELECT COALESCE(ts.piid, ts.fain, ts.uri) AS award_id_native,
       ts.generated_unique_award_id,
       ts.is_fpds,
       ts.modification_number,
       ts.action_date,
       ts.transaction_description,
       ts.federal_action_obligation,
       ts.recipient_name,
       {CLAWBACK_FRACTION_SQL} AS clawback_fraction,
       'clawback' AS detection_method
FROM rpt.transaction_search ts
JOIN rpt.award_search aw ON aw.award_id = ts.award_id
WHERE ts.is_fpds = FALSE
  AND ts.awarding_agency_id = 862
  AND ts.action_date >= '{TRACKING_WINDOW_START}'
  AND ts.federal_action_obligation <= {CLAWBACK_AMOUNT_THRESHOLD}
  AND {CLAWBACK_FRACTION_SQL} >= {CLAWBACK_FRACTION_THRESHOLD}
  AND ts.action_date > aw.period_of_performance_start_date
  AND ts.action_date < aw.period_of_performance_current_end_date
ORDER BY ts.action_date
"""


# Initial Reported End Date provider. See initial_end_dates for why the public
# API cannot supply this column and the download path that used to was removed.
#
# Reaches back to 2007-10-01 rather than the tracking window on purpose: the
# whole point is the end date the award STARTED with, which for a long-running
# award predates the window by years.
#
# That makes the award-id predicate load-bearing in a way it is not for the
# four detection nets. Every other query in this module gets its index access
# from `action_date >= TRACKING_WINDOW_START`; this one has no such bound, so
# if the award filter is not sargable nothing keeps Postgres off a scan of the
# 236M-row table. Hence generated_unique_award_id rather than
# COALESCE(piid, fain, uri): a bare equality on one stored column, versus an
# expression no index can serve. Every target reaching this query has a
# generated id by construction - search.py only sends targets whose category is
# non-empty, and initial_end_date_category derives category from that id's
# prefix.
#
# No ORDER BY: select_initial_reported_end_date re-sorts in Python, and its
# natural mod-number order ('2' before '10') differs from SQL's text order
# anyway, so sorting here would be a Sort node whose result is discarded.
INITIAL_END_DATE_START = "2007-10-01"
INITIAL_END_DATE_TIMEOUT_S = 600

INITIAL_END_DATE_SQL = f"""
SELECT ts.generated_unique_award_id,
       ts.transaction_unique_id AS transaction_id,
       ts.action_date,
       ts.modification_number,
       -- An IDV reports an ordering-period boundary rather than a period of
       -- performance; the same preference _award_end_date applies to enriched
       -- awards in search.py. Applied to every row: ordering_period_end_date is
       -- an FPDS IDV field and null elsewhere, so the COALESCE is a no-op for
       -- non-IDVs.
       --
       -- NOT `last_date_to_order`: that is the API's DISPLAY name for this
       -- value (search.py reads it off award.raw) and exists in no table. The
       -- column is `ordering_period_end_date`, and in transaction_search it is
       -- TEXT while period_of_performance_current_end_date is DATE - so both
       -- arms are rendered as text rather than cast here, because COALESCE
       -- cannot match the two types and select_initial_reported_end_date
       -- already normalises whatever text arrives (this module is transport;
       -- initial_end_dates owns the parsing and its failure policy).
       COALESCE(
           NULLIF(TRIM(ts.ordering_period_end_date), ''),
           ts.period_of_performance_current_end_date::text
       ) AS end_date
FROM rpt.transaction_search ts
WHERE ts.awarding_agency_id = 862
  AND ts.action_date >= '{INITIAL_END_DATE_START}'
  AND ts.generated_unique_award_id = ANY(%(generated_award_ids)s)
"""


class Net(NamedTuple):
    """One detection net: its label in `detection_method`, timeout, SQL, basis.

    The timeouts are measured on this mirror. Fail-loud means failing, not
    hanging: an unresponsive mirror must abort the run rather than block the
    daily job forever.

    `basis` is the net's answer to contract_query.DETECTION_BASES - whether it
    saw a termination or deduced one. It is a required field so a new net
    cannot be registered without classifying it; the tracking-window effect
    gate depends on that answer.
    """

    name: str
    timeout_s: int
    sql: str
    basis: str


# Order matters twice: it is the order the nets run in, and the order their
# phrases appear in an award's `status`.
NETS = (
    Net("action_code", 300, Q1_ACTION_CODES, "evidence"),
    Net("description_regex", 300, Q2_DESCRIPTION_REGEX, "evidence"),
    Net("end_date_truncation", 600, Q3_END_DATE_TRUNCATION, "inference"),
    Net("clawback", 300, Q4_PURE_CLAWBACKS, "inference"),
)
_NETS_BY_NAME = {net.name: net for net in NETS}
_NET_ORDER = {net.name: rank for rank, net in enumerate(NETS)}


def _is_fpds(val) -> bool:
    """True when this row's is_fpds flag means "contract".

    The flag arrives as a real boolean from psycopg but as text from a replayed
    CSV export, where it can be 'True', 't', 'False' or 'f' depending on who
    wrote it - and bool('f') is True, which would silently label every grant a
    contract.
    """
    if isinstance(val, str):
        return val.strip().lower() in {"true", "t"}
    return bool(val)


def _action_date_key(row: dict) -> str:
    """Sortable action date, whether it arrived as a date or as CSV text.

    ISO strings and date.isoformat() sort identically, so normalising to text
    avoids comparing a datetime.date against a str mid-sort.
    """
    return str(row.get("action_date") or "")


def _detection_basis(award_rows) -> str:
    """ "evidence" if any net saw a real termination action, else "inference".

    Strongest claim wins: an award found by both a truncation and a real
    termination action has evidence behind it, so the effect gate must not
    apply and evict a genuine cancellation. See
    contract_query.DETECTION_BASES.
    """
    return (
        "evidence"
        if any(
            _NETS_BY_NAME[row["detection_method"]].basis == "evidence"
            for row in award_rows
        )
        else "inference"
    )


def _status_phrase(row: dict) -> str:
    """One human-readable sentence describing why this net flagged this row."""
    method = row.get("detection_method")
    mod = row.get("modification_number") or ""
    when = _action_date_key(row)

    if method == "action_code":
        code = (row.get("action_type") or "").upper()
        kind = ACTION_CODE_KINDS.get(code, "Termination action")
        return f"{kind} {mod} on {when}"
    if method == "description_regex":
        return f"Termination-language transaction {mod} on {when}"
    if method == "end_date_truncation":
        days = row.get("days_truncated")
        previous = to_iso(row.get("previous_end_date"))
        resulting = to_iso(row.get("end_date"))
        return (
            f"End date shortened {days} days from {previous} to {resulting} "
            f"by mod {mod} on {when}"
        )
    if method == "clawback":
        pct = round(float(row.get("clawback_fraction") or 0) * 100)
        amount = abs(float(row.get("federal_action_obligation") or 0))
        return f"Clawback of {pct}% (${amount:,.0f}) on {when}"
    return ""


def _combine(rows) -> pd.DataFrame:
    """Merge every net's transaction rows into one row per award.

    Pure: no database, no filesystem, no clock. Everything that decides what
    ends up in the snapshot lives here, so it can be tested without a mirror.
    Each row carries its own `detection_method`, labelled by the SQL.
    """
    by_award: dict = {}
    for row in rows:
        # Termination for cause is contractor failure, not a policy
        # cancellation (commit 08a52cf). Dropped per transaction, before
        # grouping - same parity edge as the API source: an award whose
        # cause-coded mod is dropped can still surface through a later
        # innocuous mod of its own. Accepted; the alternative is letting one
        # row's text veto evidence it never saw.
        if is_cause(row.get("transaction_description") or ""):
            continue
        aid = str(row.get("award_id_native") or "").strip()
        if not aid:
            continue
        by_award.setdefault(aid, []).append(row)

    records = []
    for aid, award_rows in by_award.items():
        # The most recent transaction describes the award's current state; the
        # status string below still reports every net that ever fired on it.
        latest = max(award_rows, key=_action_date_key)

        # An award whose LATEST flagged transaction reverses the termination is
        # not currently cancelled - but its old termination mods keep matching
        # this source's full-window sweep forever, and a rescission's own text
        # ("RESCINDING STOP WORK NOTICE") matches the sweep too. Without this
        # skip the award re-enters the snapshot daily as "listed" and the
        # ledger can never say reinstated again: measured 2026-07-30, six
        # rescinded grants flipped back to listed on the mirror's first run.
        # A later RE-termination still surfaces, because it becomes the latest
        # row. Judged on the latest row only - the shared vocabulary requires
        # the reversal and its subject in the SAME text, never a concatenation.
        latest_desc = latest.get("transaction_description") or ""
        if is_reversal(latest_desc) or is_vacatur(latest_desc):
            continue

        gid = latest.get("generated_unique_award_id") or ""

        phrases: list[str] = []
        ordered = sorted(
            award_rows,
            key=lambda r: _NET_ORDER.get(r.get("detection_method"), len(NETS)),
        )
        for row in ordered:
            phrase = _status_phrase(row)
            if phrase and phrase not in phrases:
                phrases.append(phrase)

        records.append(
            {
                "Award ID": aid,
                "source_type": "Contract"
                if _is_fpds(latest.get("is_fpds"))
                else "Grant",
                "recipient": latest.get("recipient_name") or "",
                "value": latest.get("federal_action_obligation"),
                # This source infers cancellations from transaction data; it
                # never carries an asserted savings figure or a claim date.
                "savings": None,
                "status": "; ".join(phrases),
                "source_url": f"https://www.usaspending.gov/award/{gid}/"
                if gid
                else "",
                "description": latest.get("transaction_description") or "",
                "agency": "NASA",
                "claim_date": None,
                "action_date": to_iso(latest.get("action_date")),
                # An award this source flagged through more than one net is
                # judged on its strongest claim: if ANY net saw real
                # termination evidence, the award is evidence-based and the
                # effect gate does not apply. Only a purely inferred award -
                # truncation and/or clawback with nothing else - carries
                # "inference" and must show its effect inside the window.
                "detection_basis": _detection_basis(award_rows),
                "detection_method": primary_local_method(award_rows),
            }
        )

    return pd.DataFrame(records, columns=FINAL_COLUMNS)


def _net_award_counts(rows, surviving) -> dict:
    """Awards each net contributed, and how many only it found.

    One pass over the rows building award -> {methods}: the moment a net stops
    contributing awards nothing else finds, it is a candidate for deletion -
    and if one suddenly finds everything, something upstream changed shape.
    """
    methods_by_award: dict = {}
    for row in rows:
        aid = str(row.get("award_id_native") or "").strip()
        if aid in surviving:
            methods_by_award.setdefault(aid, set()).add(row.get("detection_method"))

    counts = {net.name: [0, 0] for net in NETS}
    for methods in methods_by_award.values():
        for method in methods:
            if method in counts:
                counts[method][0] += 1
                if len(methods) == 1:
                    counts[method][1] += 1
    return counts


class LocalUSASpendingMirrorQuery(ContractQuery):
    """Queries a local USAspending Postgres mirror for NASA cancellations."""

    @classmethod
    def is_configured(cls) -> bool:
        """True when this machine holds credentials for the mirror."""
        if os.environ.get(DSN_ENV_VAR):
            return True
        return all(os.environ.get(var) for var in COMPONENT_ENV_VARS)

    @staticmethod
    def _dsn() -> str:
        """The connection string, from either supported .env shape."""
        dsn = os.environ.get(DSN_ENV_VAR)
        if dsn:
            return dsn
        user, password, host, port, name = (
            os.environ.get(var, "") for var in COMPONENT_ENV_VARS
        )
        return f"postgresql://{user}:{password}@{host}:{port}/{name}"

    def _require_configured(self) -> None:
        """Raise the narrow availability error when no credentials are set.

        Historical exports are evidence from earlier runs, not a substitute
        for a current query, so their presence never satisfies this.
        """
        if not self.is_configured():
            raise LocalMirrorUnavailableError(
                f"no database credentials ({DSN_ENV_VAR} or "
                f"{'/'.join(COMPONENT_ENV_VARS)})"
            )

    @staticmethod
    def _run(cur, net: Net):
        """Run one net under its own timeout and return its rows.

        The timeout and the query go as two executes on the same cursor:
        psycopg3 sends statements through the extended protocol, which accepts
        exactly one statement per call, so the guard cannot ride along in the
        query string. The interval is our own int constant, never input.
        """
        cur.execute(f"SET statement_timeout = '{net.timeout_s}s'")
        cur.execute(net.sql)
        return cur.fetchall()

    @contextmanager
    def _cursor(self):
        """Yield a dict cursor on the mirror, or raise LocalMirrorUnavailableError.

        Both callers - the detection nets and the initial-end-date provider -
        need the same connection policy, so it is written once here.

        connect_timeout keeps a sleeping or unplugged mirror host from hanging
        the run before statement_timeout ever gets a chance. Connectivity,
        authentication, a dropped connection, and server timeouts all make this
        optional LAN-only source unavailable for the run. SQL/programming
        errors remain fail-loud: treating those as an offline database would
        hide a broken query.
        """
        # Imported here, not at module scope, so processes that skip this
        # optional source never need the driver installed.
        import psycopg
        from psycopg.rows import dict_row

        try:
            with (
                psycopg.connect(self._dsn(), connect_timeout=CONNECT_TIMEOUT_S) as conn,
                conn.cursor(row_factory=dict_row) as cur,
            ):
                yield cur
        except psycopg.OperationalError as exc:
            raise LocalMirrorUnavailableError(
                "local USAspending database is not accessible"
            ) from exc

    def _query_mirror(self) -> pd.DataFrame:
        """Run every net against the live mirror and combine them."""
        print(
            f"Querying local USAspending mirror for NASA cancellations "
            f"since {TRACKING_WINDOW_START}",
            file=sys.stderr,
        )

        with self._cursor() as cur:
            results = {net.name: self._run(cur, net) for net in NETS}

        # Stored on the instance until search() has successfully combined all
        # nets. A caller of _query_mirror() is diagnostic/read-only; only the
        # public search() method replaces the committed facts sidecar.
        self.period_change_fact_rows = {
            row["award_id_native"]: award_period_change_facts.build_fact_row(
                row, checked=PERIOD_CHANGE_RUN_DATE
            )
            for row in results["end_date_truncation"]
        }

        rows = []
        for net in NETS:
            print(
                f"  {net.name}: {len(results[net.name])} transactions",
                file=sys.stderr,
            )
            rows.extend(results[net.name])

        df = _combine(rows)

        counts = _net_award_counts(rows, set(df["Award ID"]))
        for net in NETS:
            total, exclusive = counts[net.name]
            print(
                f"  {net.name}: {total} awards ({exclusive} found by this net alone)",
                file=sys.stderr,
            )
        print(f"LocalUSASpendingMirror: {len(df)} unique awards", file=sys.stderr)
        return df

    def fetch_initial_reported_end_dates(
        self, targets: Sequence[InitialEndDateTarget]
    ) -> list[InitialEndDateResult]:
        """Resolve each target's originally-reported period-of-performance end.

        One query for every target. See initial_end_dates for why this is the
        only provider.

        Raises LocalMirrorUnavailableError when the mirror cannot be reached,
        which the caller treats as "resolve none this run" - the stored
        provenance file is write-once and committed, so nothing is lost, the
        backlog is simply picked up on the next run with mirror access.
        """
        if not targets:
            return []
        self._require_configured()

        by_gid = {target.generated_award_id: target for target in targets}
        print(
            f"Resolving {len(by_gid)} initial reported end date(s) from the "
            f"local USAspending mirror",
            file=sys.stderr,
        )

        with self._cursor() as cur:
            cur.execute(f"SET statement_timeout = '{INITIAL_END_DATE_TIMEOUT_S}s'")
            try:
                cur.execute(INITIAL_END_DATE_SQL, {"generated_award_ids": list(by_gid)})
            except Exception as exc:
                # transaction_unique_id and ordering_period_end_date are the
                # only two columns this module uses that no detection net
                # proves exist. Fail loudly and name them rather than degrade:
                # this feeds a write-once provenance file, so a silently-
                # substituted column would be recorded as `resolved` and never
                # revisited.
                raise RuntimeError(
                    f"initial-end-date query failed against "
                    f"rpt.transaction_search ({exc}). It needs "
                    f"generated_unique_award_id, transaction_unique_id, "
                    f"ordering_period_end_date and "
                    f"period_of_performance_current_end_date."
                ) from exc
            rows = cur.fetchall()

        grouped: dict[str, list[dict]] = {gid: [] for gid in by_gid}
        for row in rows:
            grouped[row["generated_unique_award_id"]].append(row)

        return [
            select_initial_reported_end_date(target, grouped[gid])
            if grouped[gid]
            # The mirror lags the live API by 2-6 weeks, so a just-flagged
            # award can be genuinely absent. Non-terminal, so the caller does
            # not persist it; one missing award must not abort the others.
            else InitialEndDateResult.unresolved(target, "not_in_mirror")
            for gid, target in by_gid.items()
        ]

    def search(self, **kwargs) -> pd.DataFrame:
        """Query the live mirror and export the successful result.

        The orchestrator normally removes this source before calling search()
        when credentials are absent. Raising the same typed availability error
        here keeps direct callers safe and gives the orchestrator one narrow
        exception to skip without weakening its fail-loud policy elsewhere.
        """
        self._require_configured()

        df = self._query_mirror()
        award_period_change_facts.write_facts(self.period_change_fact_rows)
        self.export_to_csv(df, EXPORT_BASE)
        return df


if __name__ == "__main__":
    print(LocalUSASpendingMirrorQuery().search().to_string())
