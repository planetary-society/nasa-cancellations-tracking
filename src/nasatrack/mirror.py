"""Local Postgres USASpending mirror door: termination-candidate and POP-change queries.

The mirror is the full `data_store_api` dump behind USAspending.gov, living on
the operator's LAN: `rpt.transaction_search` alone carries ~236M rows, and NASA
is `awarding_agency_id = 862`. It buys two things the public API cannot give:
a WHERE clause on the FPDS action code, and a regex over transaction
descriptions. It lags the live API by 2-6 weeks, so it is the depth door, never
the recency one - hence the part-file merge in terminations.py.

Local-only by construction: CI has no route to the host and no credentials, so
every entry point here raises `LocalMirrorUnavailableError` rather than
pretending. Both queries bind every condition as a parameter; the vocabulary is
never interpolated into the SQL text.
"""

import os
from contextlib import contextmanager
from datetime import date
from decimal import Decimal

from dotenv import load_dotenv

from nasatrack import criteria
from nasatrack.criteria import Txn
from nasatrack.schema import CancellationAwardsByFiscalYearRow, PopChangeRow

# Primary form: one full postgresql:// string.
DSN_ENV_VAR = "DATABASE_URI"

# Fallback form, kept because the operator's .env predates DATABASE_URI. The
# host variable is DB_HOST; it used to be DB_URI (a bare hostname sitting next
# to DATABASE_URI, which holds a real URI). The legacy spelling is still read so
# an unmigrated .env keeps working - .env is gitignored, so nobody but the local
# operator can migrate it.
HOST_ENV_VAR = "DB_HOST"
LEGACY_HOST_ENV_VAR = "DB_URI"
# Names the component form in the "unavailable" message; the lookups themselves
# are spelled out in `is_configured` and `_dsn`.
COMPONENT_ENV_VARS = ("DB_USER", "DB_PASS", HOST_ENV_VAR, "DB_PORT", "DB_NAME")

# Keeps a sleeping or unplugged mirror host from hanging the run before any
# statement_timeout gets a chance.
CONNECT_TIMEOUT_S = 10

# Measured on this mirror. Failing loud means failing, not hanging.
TERMINATIONS_TIMEOUT_S = 120
POP_CHANGES_TIMEOUT_S = 600
CANCELLATION_ACTION_COUNTS_TIMEOUT_S = 120


class LocalMirrorUnavailableError(RuntimeError):
    """The optional local mirror could not be reached for this run."""


def _host() -> str:
    """The mirror host, from either the current or the legacy variable."""
    return os.environ.get(HOST_ENV_VAR) or os.environ.get(LEGACY_HOST_ENV_VAR) or ""


def is_configured() -> bool:
    """True when this machine holds credentials for the mirror.

    Every entry point into this module reaches the environment through here, so
    loading .env at this point (rather than on import) keeps the side effect off
    a CI run that never touches the mirror. `load_dotenv` is idempotent.
    """
    # The DB credentials live in .env, never in the repo.
    load_dotenv()
    if os.environ.get(DSN_ENV_VAR):
        return True
    return bool(
        os.environ.get("DB_USER")
        and os.environ.get("DB_PASS")
        and _host()
        and os.environ.get("DB_PORT")
        and os.environ.get("DB_NAME")
    )


def _dsn() -> str:
    """The connection string, from either supported .env shape."""
    dsn = os.environ.get(DSN_ENV_VAR)
    if dsn:
        return dsn
    user = os.environ.get("DB_USER", "")
    password = os.environ.get("DB_PASS", "")
    port = os.environ.get("DB_PORT", "")
    name = os.environ.get("DB_NAME", "")
    return f"postgresql://{user}:{password}@{_host()}:{port}/{name}"


@contextmanager
def _cursor(statement_timeout_s: int):
    """Yield a dict cursor on the mirror, or raise LocalMirrorUnavailableError.

    Missing credentials, an unreachable host and a failed authentication all
    mean the same thing for this optional LAN-only door: unavailable this run.
    ONLY the connect call is read that way. Everything after it - SQL errors,
    programming errors, and a `statement_timeout` firing - stays fail-loud,
    which is why the query phase sits outside the try: `psycopg.errors.
    QueryCanceled` subclasses OperationalError, so a timed-out query caught
    alongside the connect would report itself as "mirror not available" and exit
    the run 0, publishing yesterday's part as if today's had succeeded.

    The timeout goes as its own execute: psycopg3 uses the extended protocol,
    which accepts exactly one statement per call, so the guard cannot ride along
    in the query string. The interval is our own int constant, never input.
    """
    if not is_configured():
        raise LocalMirrorUnavailableError(
            f"no database credentials ({DSN_ENV_VAR} or {'/'.join(COMPONENT_ENV_VARS)})"
        )

    # Imported here, not at module scope, so a CI run that never touches the
    # mirror does not need the driver.
    import psycopg
    from psycopg.rows import dict_row

    try:
        conn = psycopg.connect(_dsn(), connect_timeout=CONNECT_TIMEOUT_S)
    except psycopg.OperationalError as exc:
        raise LocalMirrorUnavailableError("local USAspending mirror is not accessible") from exc

    with conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"SET statement_timeout = '{statement_timeout_s}s'")
        yield cur


# ---------------------------------------------------------------------------
# Query 1: termination candidates, with each candidate award's full in-window
# history alongside them.
# ---------------------------------------------------------------------------

# The `nasa` CTE's action_date bound does double duty: without it Postgres
# cannot use the index on transaction_search and the query degrades into a seq
# scan over ~236M rows.
#
# `candidates` is a coarse prefilter, deliberately wider than the Python
# verdict: the cause/default alternative stays in the pattern so `is_cause`
# owns that definition in one place, and the text arm carries NO is_fpds
# condition because grants terminate in prose and have no action-code field at
# all.
#
# The rejoin is what makes reversal filtering possible in one round trip: an
# award flagged by any single transaction comes back with every in-window
# transaction it has, so `accept_award` can see the rescission that followed
# the stop-work order.
#
# `award_search` is the award-level rollup table (PK award_id, same id space as
# transaction_search.award_id); its `description` is the award's current
# summary on USAspending, which the per-transaction descriptions never carry.
# LEFT JOIN, because a missing rollup row must cost a blank cell, not the
# award's whole history. Both queries interpolate this same projection, so the
# two published CSVs cannot drift in what an award's enrichment columns mean -
# the district COALESCE prefers the CURRENT (post-redistricting) code with the
# as-reported one as fallback, which is all the public API has.
AWARD_ROLLUP_COLUMNS_SQL = """COALESCE(aws.description, '') AS award_description,
       COALESCE(aws.recipient_location_address_line1, '') AS recipient_address1,
       COALESCE(aws.recipient_location_address_line2, '') AS recipient_address2,
       COALESCE(aws.recipient_location_city_name, '') AS recipient_city,
       COALESCE(aws.recipient_location_state_code, '') AS recipient_state,
       COALESCE(aws.recipient_location_zip5, '') AS recipient_zip,
       COALESCE(aws.recipient_location_congressional_code_current,
                aws.recipient_location_congressional_code, '') AS recipient_district,
       COALESCE(aws.pop_city_name, '') AS pop_city,
       COALESCE(aws.pop_state_code, '') AS pop_state,
       COALESCE(aws.pop_zip5, '') AS pop_zip,
       COALESCE(aws.pop_congressional_code_current,
                aws.pop_congressional_code, '') AS pop_district,
       aws.total_obligation AS total_obligated,
       aws.base_and_all_options_value AS total_potential_value"""

SQL_TERMINATION_CANDIDATES = f"""
WITH nasa AS (
    SELECT ts.award_id,
           COALESCE(ts.piid, ts.fain, ts.uri) AS native_award_id,
           ts.generated_unique_award_id,
           ts.transaction_unique_id AS sort_key,
           ts.is_fpds,
           ts.type AS award_type_code,
           ts.action_date,
           COALESCE(ts.action_type, '') AS action_type,
           COALESCE(ts.modification_number, '') AS modification_number,
           COALESCE(ts.transaction_description, '') AS description,
           ts.federal_action_obligation AS amount,
           ts.recipient_name
    FROM rpt.transaction_search ts
    WHERE ts.awarding_agency_id = {criteria.NASA_AGENCY_ID}
      AND ts.action_date >= %(window_start)s
),
candidates AS (
    SELECT DISTINCT award_id
    FROM nasa
    WHERE (is_fpds AND action_type = ANY(%(codes)s))
       OR description ~* %(pattern)s
)
SELECT nasa.*,
       {AWARD_ROLLUP_COLUMNS_SQL}
FROM nasa
JOIN candidates USING (award_id)
LEFT JOIN rpt.award_search aws ON aws.award_id = nasa.award_id
ORDER BY nasa.award_id, nasa.action_date, nasa.sort_key
"""


def _location(row, prefix: str) -> criteria.Location:
    """The award's `recipient` or `pop` columns as a `Location`.

    A POP has no address columns (USAspending reports no street address for a
    place of performance), which the `.get` reads render as "".
    """
    return criteria.Location(
        address1=row.get(f"{prefix}_address1") or "",
        address2=row.get(f"{prefix}_address2") or "",
        city=row[f"{prefix}_city"] or "",
        state=row[f"{prefix}_state"] or "",
        zip=row[f"{prefix}_zip"] or "",
        district=row[f"{prefix}_district"] or "",
    )


def _decimal(value) -> Decimal | None:
    """A numeric column as a plain Decimal, or None when the column is NULL."""
    return None if value is None else Decimal(str(value))


def txn_from_row(row) -> Txn:
    """One mirror row as a `Txn`, the form both doors' verdicts are reached on."""
    native_id = str(row["native_award_id"] or "").strip()
    generated_id = str(row["generated_unique_award_id"] or "").strip()
    amount = row["amount"]
    return Txn(
        award_key=criteria.award_key(generated_id, native_id),
        award_id=native_id,
        generated_award_id=generated_id,
        award_type=criteria.award_type(
            type_code=row["award_type_code"],
            generated_id=generated_id,
            is_fpds=row["is_fpds"],
        ),
        recipient_name=row["recipient_name"] or "",
        action_date=criteria.as_date(row["action_date"]),
        action_type=criteria.code(row["action_type"]),
        modification_number=row["modification_number"] or "",
        description=row["description"] or "",
        award_description=row["award_description"] or "",
        recipient_location=_location(row, "recipient"),
        pop_location=_location(row, "pop"),
        award_type_code=criteria.code(row["award_type_code"]),
        total_obligated=_decimal(row["total_obligated"]),
        total_potential_value=_decimal(row["total_potential_value"]),
        amount=_decimal(amount),
        source="mirror",
        sort_key=str(row["sort_key"] or ""),
    )


def fetch_termination_txns() -> list[Txn]:
    """Every in-window transaction of every candidate award, oldest first.

    Candidates only - this is the SQL prefilter's output, not a verdict. The
    caller runs `criteria.accept_award` over each award's transactions.
    """
    with _cursor(TERMINATIONS_TIMEOUT_S) as cur:
        cur.execute(
            SQL_TERMINATION_CANDIDATES,
            {
                "window_start": criteria.WINDOW_START_ISO,
                "codes": list(criteria.TERMINATION_ACTION_CODES),
                "pattern": criteria.TERMINATION_TEXT_SQL,
            },
        )
        return [txn_from_row(row) for row in cur.fetchall()]


def fetch_terminated_awards() -> list[Txn]:
    """The operative termination of every award this door accepts, one per award."""
    by_award = criteria.group_by_award(fetch_termination_txns())
    accepted = (criteria.accept_award(txns) for txns in by_award.values())
    return [txn for txn in accepted if txn is not None]


# ---------------------------------------------------------------------------
# Query 2: period-of-performance pull-backs. A lead sheet, never a verdict, and
# never merged into terminations.csv.
# ---------------------------------------------------------------------------

# Three end dates per award answer "was this award's runway cut short?" without
# any per-transaction heuristic: the first one ever reported, the longest one
# ever reported, and the one standing now.
#
# `txns` scans NASA's whole history back to 2007-10-01, because the end date an
# award STARTED with predates the window by years for a long-running award.
# Restricting that to awards NASA touched inside the window is `agg`'s HAVING
# rather than a second pass over the table: max(action_date) is taken over ALL
# of an award's transactions, which is exactly what the separate "active"
# prepass used to compute - same semantics, one scan instead of two.
#
# `ends` is the award's reported end dates in transaction order, the undated
# ones filtered out so a transaction that simply omits the field cannot be read
# as the award's first or current end date. First element and last element are
# the original and the current, so one ordered aggregate answers both and the
# second, descending sort is gone. An award with no dated transaction at all
# gets a NULL array; the outer WHERE drops it, because NULL arithmetic is never
# greater than min_days.
#
# The end-date expression's cast is load-bearing: USAspending publishes no
# period of performance for an IDV, so the ordering-period boundary is its only
# end date - and in transaction_search that column is TEXT (blank, not null,
# when unset) while period_of_performance_current_end_date is DATE.
SQL_POP_CHANGES = f"""
WITH txns AS (
    SELECT ts.award_id,
           COALESCE(ts.piid, ts.fain, ts.uri) AS native_award_id,
           ts.generated_unique_award_id,
           ts.transaction_unique_id AS sort_key,
           ts.is_fpds,
           ts.type AS award_type_code,
           ts.action_date,
           ts.recipient_name,
           COALESCE(
               ts.period_of_performance_current_end_date,
               NULLIF(TRIM(ts.ordering_period_end_date), '')::date
           ) AS end_date
    FROM rpt.transaction_search ts
    WHERE ts.awarding_agency_id = {criteria.NASA_AGENCY_ID}
      AND ts.action_date >= '2007-10-01'
),
agg AS (
    SELECT award_id,
           max(native_award_id) AS native_award_id,
           max(generated_unique_award_id) AS generated_unique_award_id,
           max(award_type_code) AS award_type_code,
           bool_or(is_fpds) AS is_fpds,
           max(recipient_name) AS recipient_name,
           array_agg(end_date ORDER BY action_date, sort_key)
               FILTER (WHERE end_date IS NOT NULL) AS ends,
           max(end_date) AS max_end_date,
           max(action_date) AS last_action_date,
           -- Every transaction, dated end date or not: the honest history depth.
           count(*) AS transaction_count
    FROM txns
    GROUP BY award_id
    HAVING max(action_date) >= %(window_start)s
)
-- Every agg column is agg-qualified: the joined award_search table carries
-- some of the same column names (generated_unique_award_id at least), and an
-- unqualified reference is an AmbiguousColumn error.
SELECT agg.native_award_id,
       agg.generated_unique_award_id,
       agg.award_type_code,
       agg.is_fpds,
       agg.recipient_name,
       {AWARD_ROLLUP_COLUMNS_SQL},
       agg.ends[1] AS original_end_date,
       agg.max_end_date,
       agg.ends[array_upper(agg.ends, 1)] AS current_end_date,
       (agg.max_end_date - agg.ends[array_upper(agg.ends, 1)]) AS days_shortened,
       agg.last_action_date,
       agg.transaction_count
FROM agg
LEFT JOIN rpt.award_search aws ON aws.award_id = agg.award_id
WHERE agg.max_end_date - agg.ends[array_upper(agg.ends, 1)] > %(min_days)s
  -- The award has to have been pulled back TO a date inside the window; an
  -- award that finished in 2019 is not a lead.
  AND agg.ends[array_upper(agg.ends, 1)] >= %(window_start)s
ORDER BY days_shortened DESC
"""


def fetch_pop_changes() -> list[PopChangeRow]:
    """Awards whose period of performance was pulled back, longest pull-back first."""
    with _cursor(POP_CHANGES_TIMEOUT_S) as cur:
        cur.execute(
            SQL_POP_CHANGES,
            {
                "window_start": criteria.WINDOW_START_ISO,
                "min_days": criteria.SHORTENING_MIN_DAYS,
            },
        )
        return [
            PopChangeRow(
                award_id=str(row["native_award_id"] or "").strip(),
                generated_award_id=str(row["generated_unique_award_id"] or "").strip(),
                award_type=criteria.award_type(
                    type_code=row["award_type_code"],
                    generated_id=row["generated_unique_award_id"],
                    is_fpds=row["is_fpds"],
                ),
                recipient_name=row["recipient_name"] or "",
                award_type_code=criteria.code(row["award_type_code"]),
                award_description=row["award_description"] or "",
                recipient_address1=row["recipient_address1"] or "",
                recipient_address2=row["recipient_address2"] or "",
                recipient_city=row["recipient_city"] or "",
                recipient_state=row["recipient_state"] or "",
                recipient_zip=row["recipient_zip"] or "",
                recipient_district=row["recipient_district"] or "",
                pop_city=row["pop_city"] or "",
                pop_state=row["pop_state"] or "",
                pop_zip=row["pop_zip"] or "",
                pop_district=row["pop_district"] or "",
                total_obligated=_decimal(row["total_obligated"]),
                total_potential_value=_decimal(row["total_potential_value"]),
                original_end_date=criteria.as_date(row["original_end_date"]),
                max_end_date=criteria.as_date(row["max_end_date"]),
                current_end_date=criteria.as_date(row["current_end_date"]),
                days_shortened=int(row["days_shortened"]),
                last_action_date=criteria.as_date(row["last_action_date"]),
                transaction_count=int(row["transaction_count"]),
            )
            for row in cur.fetchall()
        ]


# ---------------------------------------------------------------------------
# Query 3: historical NASA termination-for-convenience action counts.
# ---------------------------------------------------------------------------

# This is action grain by design: one transaction_search row is one reported
# action. The action-code signal is FPDS-only because FABS has no reason-for-
# modification field; the keyword signal includes both FPDS and FABS. Both
# signals reuse criteria.py's shared verdict vocabulary. The action-date bound
# is required for the mirror's partial indexes; fiscal_year alone is not enough.
SQL_CANCELLATIONS_FOR_CONVENIENCE_BY_FY = f"""
WITH fiscal_years AS (
    SELECT generate_series(
        %(start_fiscal_year)s,
        EXTRACT(YEAR FROM CURRENT_DATE + INTERVAL '3 months')::integer
    ) AS fy
),
signals AS (
    SELECT source.award_id,
           source.fy,
           source.is_fpds IS TRUE
               AND source.action_type = ANY(%(action_codes)s) AS by_action_code,
           source.description ~* %(keyword_pattern)s
               AND source.description !~* %(cause_pattern)s AS by_keyword
    FROM (
        SELECT ts.award_id,
               ts.fiscal_year AS fy,
               ts.is_fpds,
               ts.action_type,
               COALESCE(ts.transaction_description, '') AS description
        FROM rpt.transaction_search ts
        WHERE ts.awarding_agency_id = {criteria.NASA_AGENCY_ID}
          AND ts.action_date >= %(start_date)s
          AND ts.action_date <= CURRENT_DATE
          AND ts.fiscal_year >= %(start_fiscal_year)s
    ) AS source
),
cancellations AS (
    SELECT fy,
           count(DISTINCT award_id) FILTER (WHERE by_action_code)
               AS action_code_cancellation_awards,
           count(DISTINCT award_id) FILTER (WHERE by_keyword)
               AS keyword_cancellation_awards,
           count(DISTINCT award_id) FILTER (WHERE by_action_code OR by_keyword)
               AS action_code_or_keyword_cancellation_awards
    FROM signals
    WHERE by_action_code OR by_keyword
    GROUP BY fy
)
SELECT fiscal_years.fy,
       COALESCE(cancellations.action_code_cancellation_awards, 0)
           AS action_code_cancellation_awards,
       COALESCE(cancellations.keyword_cancellation_awards, 0)
           AS keyword_cancellation_awards,
       COALESCE(cancellations.action_code_or_keyword_cancellation_awards, 0)
           AS action_code_or_keyword_cancellation_awards
FROM fiscal_years
LEFT JOIN cancellations USING (fy)
ORDER BY fiscal_years.fy
"""


def fetch_cancellations_for_convenience_awards_by_fy(
    start_fiscal_year: int = 2010,
) -> list[CancellationAwardsByFiscalYearRow]:
    """Count distinct NASA awards carrying code, keyword, or either signal by FY."""
    start_date = date(start_fiscal_year - 1, 10, 1)
    with _cursor(CANCELLATION_ACTION_COUNTS_TIMEOUT_S) as cur:
        cur.execute(
            SQL_CANCELLATIONS_FOR_CONVENIENCE_BY_FY,
            {
                "start_date": start_date,
                "start_fiscal_year": start_fiscal_year,
                "action_codes": list(criteria.STANDALONE_TERMINATION_CODES),
                "keyword_pattern": criteria.TERMINATION_KEYWORD_SQL,
                "cause_pattern": criteria.CAUSE_TEXT_SQL,
            },
        )
        return [
            CancellationAwardsByFiscalYearRow(
                fiscal_year=int(row["fy"]),
                action_code_cancellation_awards=int(row["action_code_cancellation_awards"]),
                keyword_cancellation_awards=int(row["keyword_cancellation_awards"]),
                action_code_or_keyword_cancellation_awards=int(
                    row["action_code_or_keyword_cancellation_awards"]
                ),
            )
            for row in cur.fetchall()
        ]
