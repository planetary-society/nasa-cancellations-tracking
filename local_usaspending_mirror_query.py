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
credentials) search() replays the most recent committed export instead, which
keeps validate_snapshot's source-presence check satisfied without pretending
to have queried anything.
"""

import os
import sys
from typing import List, NamedTuple, Optional

import pandas as pd
from dotenv import load_dotenv

from contract_query import ContractQuery, FINAL_COLUMNS, find_most_recent_csv
from termination_vocabulary import (
    CAUSE_TEXT,
    TERM_TEXT,
    is_cause,
    is_reversal,
    is_vacatur,
)

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

# The tracking window opens at the second-term inauguration. An action_date
# bound is also what lets Postgres use the index on transaction_search; without
# one, every net degrades into a seq scan over ~236M rows.
TRACKING_WINDOW_START = "2025-01-20"

# How far an end date must move back before the truncation net calls it a kill.
# Measured, not chosen for roundness - see Q3's comment.
TRUNCATION_MIN_DAYS = 180


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

Q3_END_DATE_TRUNCATION = f"""
-- Period-of-performance end dates yanked backwards, FPDS + FABS. This net
-- needs no termination language at all, which is the point: a quietly
-- de-scoped award often reads as a routine administrative mod.
--
-- nasa_txn reaches back to 2007-10-01 because max_end_ever must be the
-- highest end date the award EVER carried, not the highest inside the
-- tracking window. Only the truncating mod itself has to be recent, which is
-- what the inner award_id subquery selects for.
--
-- Three gates, all measured on this mirror on 2026-07-30. The loose form of
-- this query (truncation > 30 days, no obligation test) flagged 414 awards,
-- 299 of them truncation-only, and only 108 survived the two extra gates:
--   * federal_action_obligation < 0 - a real kill takes money back; a pure
--     date change with no deobligation is almost always PoP realignment.
--   * max_end_ever - end_date >= {TRUNCATION_MIN_DAYS} - six months is the
--     floor where the signal stops being administrative noise.
-- The other ~200 were exactly that noise, and would have flooded the snapshot.
WITH nasa_txn AS (
  SELECT ts.award_id,
         COALESCE(ts.piid, ts.fain, ts.uri) AS award_id_native,
         ts.generated_unique_award_id,
         ts.is_fpds,
         ts.modification_number,
         ts.action_date,
         ts.transaction_description,
         ts.federal_action_obligation,
         ts.recipient_name,
         ts.period_of_performance_current_end_date AS end_date
  FROM rpt.transaction_search ts
  WHERE ts.awarding_agency_id = 862
    AND ts.action_date >= '2007-10-01'
    AND ts.award_id IN (
      SELECT t2.award_id
      FROM rpt.transaction_search t2
      WHERE t2.awarding_agency_id = 862
        AND t2.action_date >= '{TRACKING_WINDOW_START}'
    )
),
-- One pass: the window function computes each award's highest-ever end date
-- alongside the rows it is chosen from, so DISTINCT ON can pick the latest
-- mod from the same sort. Aggregating separately and joining back cost this
-- query - the only one that needs 600s - an extra sort and a hash join.
latest AS (
  SELECT DISTINCT ON (award_id)
         *,
         MAX(end_date) OVER (PARTITION BY award_id) AS max_end_ever
  FROM nasa_txn
  WHERE end_date IS NOT NULL
  ORDER BY award_id, action_date DESC, modification_number DESC
)
SELECT award_id_native,
       generated_unique_award_id,
       is_fpds,
       modification_number,
       action_date,
       transaction_description,
       federal_action_obligation,
       recipient_name,
       (max_end_ever - end_date) AS days_truncated,
       'end_date_truncation' AS detection_method
FROM latest
WHERE action_date >= '{TRACKING_WINDOW_START}'
  AND max_end_ever - end_date >= {TRUNCATION_MIN_DAYS}
  AND federal_action_obligation < 0
  -- The new end must land near the mod that set it, otherwise this is an
  -- award whose end date was always in the past, not one just cut short.
  AND end_date - action_date BETWEEN -365 AND 60
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


class Net(NamedTuple):
    """One detection net: its label in `detection_method`, its timeout, its SQL.

    The timeouts are measured on this mirror. Fail-loud means failing, not
    hanging: an unresponsive mirror must abort the run rather than block the
    daily job forever.
    """

    name: str
    timeout_s: int
    sql: str


# Order matters twice: it is the order the nets run in, and the order their
# phrases appear in an award's `status`.
NETS = (
    Net("action_code", 300, Q1_ACTION_CODES),
    Net("description_regex", 300, Q2_DESCRIPTION_REGEX),
    Net("end_date_truncation", 600, Q3_END_DATE_TRUNCATION),
    Net("clawback", 300, Q4_PURE_CLAWBACKS),
)
_NET_RANK = {net.name: rank for rank, net in enumerate(NETS)}


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
        return f"End date truncated {days} days by mod {mod} on {when}"
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

        phrases: List[str] = []
        ordered = sorted(
            award_rows,
            key=lambda r: _NET_RANK.get(r.get("detection_method"), len(NETS)),
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

    @classmethod
    def is_available(cls) -> bool:
        """True when the source can produce a result: live DB or prior export.

        search.py drops unavailable sources, so this is what keeps CI from
        aborting the run over a database it can never reach.
        """
        if cls.is_configured():
            return True
        return find_most_recent_csv("data", EXPORT_BASE) is not None

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

    def _query_mirror(self) -> pd.DataFrame:
        """Run every net against the live mirror and combine them."""
        # Imported here, not at module scope, so replay mode and the test suite
        # never need the driver installed.
        import psycopg
        from psycopg.rows import dict_row

        print(
            f"Querying local USAspending mirror for NASA cancellations "
            f"since {TRACKING_WINDOW_START}",
            file=sys.stderr,
        )

        # connect_timeout keeps a sleeping or unplugged mirror host from
        # hanging the run before statement_timeout ever gets a chance.
        with psycopg.connect(self._dsn(), connect_timeout=10) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                results = {net.name: self._run(cur, net) for net in NETS}

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

    def search(self, **kwargs) -> pd.DataFrame:
        """Query the mirror, or replay the last export when it is unreachable.

        The export IS the search result - _combine's output written verbatim -
        so replay reproduces the last live run exactly rather than
        approximating it. That is what lets a DB-less runner still satisfy
        validate_snapshot's source-presence check.
        """
        if self.is_configured():
            df = self._query_mirror()
            self.export_to_csv(df, EXPORT_BASE)
            return df

        latest: Optional[str] = find_most_recent_csv("data", EXPORT_BASE)
        if latest:
            print(
                f"LocalUSASpendingMirror: replaying {latest} (DB not configured)",
                file=sys.stderr,
            )
            return pd.read_csv(latest, dtype=str, keep_default_na=False)

        raise RuntimeError(
            f"LocalUSASpendingMirror has neither database credentials "
            f"({DSN_ENV_VAR} or {'/'.join(COMPONENT_ENV_VARS)}) nor a prior "
            f"data/{EXPORT_BASE}_*.csv to replay; cannot distinguish "
            f"'no cancellations' from 'source unavailable'."
        )


if __name__ == "__main__":
    print(LocalUSASpendingMirrorQuery().search().to_string())
