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
from decimal import Decimal

from dotenv import load_dotenv

from nasatrack import criteria
from nasatrack.criteria import Txn
from nasatrack.schema import PopChangeRow

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
# award's whole history.
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
       COALESCE(aws.description, '') AS award_description,
       COALESCE(aws.recipient_location_address_line1, '') AS recipient_address1,
       COALESCE(aws.recipient_location_address_line2, '') AS recipient_address2,
       COALESCE(aws.recipient_location_city_name, '') AS recipient_city,
       COALESCE(aws.recipient_location_state_code, '') AS recipient_state,
       COALESCE(aws.recipient_location_zip5, '') AS recipient_zip,
       COALESCE(aws.pop_city_name, '') AS pop_city,
       COALESCE(aws.pop_state_code, '') AS pop_state,
       COALESCE(aws.pop_zip5, '') AS pop_zip
FROM nasa
JOIN candidates USING (award_id)
LEFT JOIN rpt.award_search aws ON aws.award_id = nasa.award_id
ORDER BY nasa.award_id, nasa.action_date, nasa.sort_key
"""


def _recipient_location(row) -> criteria.Location:
    """The award's recipient location columns as a `Location`."""
    return criteria.Location(
        address1=row["recipient_address1"] or "",
        address2=row["recipient_address2"] or "",
        city=row["recipient_city"] or "",
        state=row["recipient_state"] or "",
        zip=row["recipient_zip"] or "",
    )


def _pop_location(row) -> criteria.Location:
    """The award's place-of-performance columns as a `Location` (no street address exists)."""
    return criteria.Location(
        city=row["pop_city"] or "",
        state=row["pop_state"] or "",
        zip=row["pop_zip"] or "",
    )


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
        action_type=str(row["action_type"] or "").strip().upper(),
        modification_number=row["modification_number"] or "",
        description=row["description"] or "",
        award_description=row["award_description"] or "",
        recipient_location=_recipient_location(row),
        pop_location=_pop_location(row),
        amount=None if amount is None else Decimal(str(amount)),
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
SELECT native_award_id,
       generated_unique_award_id,
       award_type_code,
       is_fpds,
       recipient_name,
       -- The award-level summary and locations, joined exactly as the
       -- terminations query joins them: LEFT, so a missing rollup row is a
       -- blank cell, not a vanished lead.
       COALESCE(aws.description, '') AS award_description,
       COALESCE(aws.recipient_location_address_line1, '') AS recipient_address1,
       COALESCE(aws.recipient_location_address_line2, '') AS recipient_address2,
       COALESCE(aws.recipient_location_city_name, '') AS recipient_city,
       COALESCE(aws.recipient_location_state_code, '') AS recipient_state,
       COALESCE(aws.recipient_location_zip5, '') AS recipient_zip,
       COALESCE(aws.pop_city_name, '') AS pop_city,
       COALESCE(aws.pop_state_code, '') AS pop_state,
       COALESCE(aws.pop_zip5, '') AS pop_zip,
       ends[1] AS original_end_date,
       max_end_date,
       ends[array_upper(ends, 1)] AS current_end_date,
       (max_end_date - ends[array_upper(ends, 1)]) AS days_shortened,
       last_action_date,
       transaction_count
FROM agg
LEFT JOIN rpt.award_search aws ON aws.award_id = agg.award_id
WHERE max_end_date - ends[array_upper(ends, 1)] > %(min_days)s
  -- The award has to have been pulled back TO a date inside the window; an
  -- award that finished in 2019 is not a lead.
  AND ends[array_upper(ends, 1)] >= %(window_start)s
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
                award_description=row["award_description"] or "",
                recipient_address1=row["recipient_address1"] or "",
                recipient_address2=row["recipient_address2"] or "",
                recipient_city=row["recipient_city"] or "",
                recipient_state=row["recipient_state"] or "",
                recipient_zip=row["recipient_zip"] or "",
                pop_city=row["pop_city"] or "",
                pop_state=row["pop_state"] or "",
                pop_zip=row["pop_zip"] or "",
                original_end_date=criteria.as_date(row["original_end_date"]),
                max_end_date=criteria.as_date(row["max_end_date"]),
                current_end_date=criteria.as_date(row["current_end_date"]),
                days_shortened=int(row["days_shortened"]),
                last_action_date=criteria.as_date(row["last_action_date"]),
                transaction_count=int(row["transaction_count"]),
            )
            for row in cur.fetchall()
        ]
