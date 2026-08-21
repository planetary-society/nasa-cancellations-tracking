"""USASpending API door: keyword sweep over contracts/idvs/grants plus a bounded code sweep.

Two sweeps feed one judge. Neither subsumes the other: a formal F/N modification
often carries only a project name in its description, while stop-work language
usually arrives on a mod with no distinguishing action code at all. Both
normalise their rows into `criteria.Txn` and hand them to `criteria.accept_award`,
which is the only place a verdict is reached.

Nothing here writes files or prints; `cli.py` owns both.
"""

import contextlib
from datetime import date, timedelta

from usaspending import USASpendingClient
from usaspending.models import get_award_group

from .criteria import (
    API_KEYWORDS,
    NASA_TOPTIER,
    WINDOW_START,
    Txn,
    accept_award,
    as_date,
    award_key,
    group_by_award,
    has_termination_code,
    mod_sort_key,
)

PAGE_SIZE = 100


class IncompleteSweepError(RuntimeError):
    """A paginated sweep returned fewer rows than the server says exist."""


def _all_rows(query, expected: int | None = None):
    """Every row of a query, sorted for stable pagination and count-checked.

    Both guards exist because a multi-hundred-page crawl once silently lost
    rows: without a sort, the backend's page boundaries drift between requests,
    and F-coded transactions fell through the cracks (found in a 120-day sweep,
    missing from the same sweep at 600 days). The sort pins the page order; the
    count check turns any remaining loss into a failed run instead of a
    silently short publication.

    `expected` lets a caller that has already paid for the server's count hand
    it over rather than asking twice; a count is order-independent, so the one
    taken before the sort was applied is the same number.
    """
    # The sort key must be HIGH-CARDINALITY: rows sharing a sort value form one
    # tie block whose internal order the backend's replicas do not agree on, so
    # a tie block spanning several pages can drop a row at a page boundary
    # while the total count stays right. Sorted by action_date, a single day is
    # one ~700-row block and an F-coded row went reproducibly missing; sorted
    # by award id, ties are one award's handful of mods and the same crawl came
    # back complete and stable.
    ordered = query.order_by("award_id", "asc")
    rows = ordered.all()
    if expected is None:
        expected = ordered.count()
    if len(rows) != expected:
        raise IncompleteSweepError(f"sweep returned {len(rows)} rows, server reports {expected}")
    return rows


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def _procurement(client, start: str, end: str):
    """NASA contract + IDV transactions in [start, end].

    TransactionsSearch accepts mixed award categories, so awards, delivery
    orders and their IDV/BPA vehicles come back in one result set.
    """
    return (
        client.transactions.search()
        .contracts()
        .idvs()
        .agency(NASA_TOPTIER)
        .time_period(start, end)
        .page_size(PAGE_SIZE)
    )


def _assistance(client, start: str, end: str):
    """NASA grant transactions in [start, end]."""
    return (
        client.transactions.search()
        .grants()
        .agency(NASA_TOPTIER)
        .time_period(start, end)
        .page_size(PAGE_SIZE)
    )


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _award_type(row, default: str) -> str:
    """contract | idv | grant for one row.

    Global search rows report the award type as a description string ("Delivery
    Order", "BPA Blanket Purchase Agreement"); award-scoped rows report neither,
    hence the per-sweep default.
    """
    return get_award_group(row.type or "") or get_award_group(row.award_type or "") or default


def orm_txn(
    row,
    *,
    award_key: str,
    award_id: str,
    generated_award_id: str,
    award_type: str,
) -> Txn:
    """One ORM transaction as a `Txn`, with award identity supplied by the caller.

    Every ORM row in this project - both sweeps here and the DOGE enrichment -
    is mapped here, so the ORM's attribute names are read in exactly one place.
    """
    return Txn(
        award_key=award_key,
        award_id=award_id,
        generated_award_id=generated_award_id,
        award_type=award_type,
        recipient_name=row.recipient_name or "",
        action_date=as_date(row.action_date),
        # Normalised exactly as mirror.txn_from_row normalises it, so Txn's
        # action_type has one contract and terminations.csv cannot publish two
        # spellings of the same code.
        action_type=str(row.action_type or "").strip().upper(),
        modification_number=row.modification_number or "",
        description=row.transaction_description or "",
        amount=row.federal_action_obligation,
        source="api",
        # Mod numbers order the transactions of one award within a single
        # action_date, which is where reversals and re-terminations collide.
        # Padded, because the sort is textual and mod 10 follows mod 9.
        sort_key=mod_sort_key(row.modification_number),
    )


def _from_search(row, default_award_type: str) -> Txn:
    """One global-search row as a `Txn`, taking its award identity from the row."""
    award_id = (row.award_identifier or "").strip()
    generated_award_id = (row.generated_unique_award_id or "").strip()
    return orm_txn(
        row,
        award_key=award_key(generated_award_id, award_id),
        award_id=award_id,
        generated_award_id=generated_award_id,
        award_type=_award_type(row, default_award_type),
    )


# ---------------------------------------------------------------------------
# The two sweeps
# ---------------------------------------------------------------------------


# The transaction search sits on an Elasticsearch index whose result window
# caps a crawl at 10,000 rows - page 101 at size 100 simply does not exist, and
# a 578-day sweep once came back 10,000 of 36,720. Chunks are split while the
# server-reported count exceeds this, held under the hard cap so a row landing
# mid-crawl cannot push a chunk over it.
ES_RESULT_WINDOW_SAFE = 9_500


def _chunked_rows(make_query, start: date, end: date) -> list:
    """Every row of [start, end], bisecting until each chunk fits the ES window."""
    query = make_query(start.isoformat(), end.isoformat())
    expected = query.count()
    if expected <= ES_RESULT_WINDOW_SAFE:
        return _all_rows(query, expected)
    if start == end:
        raise IncompleteSweepError(f"{start}: {expected} rows on one day exceeds the ES window")
    mid = start + (end - start) / 2
    return _chunked_rows(make_query, start, mid) + _chunked_rows(
        make_query, mid + timedelta(days=1), end
    )


def _keyword_sweep(client, start: date, end: date) -> list[Txn]:
    """Every transaction matching any API keyword, over the full window.

    The whole vocabulary goes into ONE `keywords()` call per category: the API
    OR-combines the terms server-side, so the sweep costs one paginated request
    per category rather than one per keyword.

    It goes through the chunker like the code sweep does. The window is anchored
    at the administration's start and grows by a day every day, so a keyword
    result set that fits the ES window today will not fit it forever - and an
    unchunked sweep would then fail the job permanently.

    Grants are NEW coverage on this door. They replace the two dropped sources
    (NASA Grants and NPDV): FABS has no termination action code, so an
    assistance termination is only ever visible as language, and the
    grant/coop-agreement awards NPDV used to catch carry explicit "TERMINATION
    FOR CONVENIENCE AGREEMENT" text this sweep now sees.
    """
    rows = [
        _from_search(row, "contract")
        for row in _chunked_rows(
            lambda s, e: _procurement(client, s, e).keywords(*API_KEYWORDS), start, end
        )
    ]
    rows += [
        _from_search(row, "grant")
        for row in _chunked_rows(
            lambda s, e: _assistance(client, s, e).keywords(*API_KEYWORDS), start, end
        )
    ]
    return rows


def _code_sweep(client, start: date, end: date) -> list[Txn]:
    """Transactions carrying an F/N action code, over a bounded recent window.

    USAspending has no server-side filter for the reason-for-modification code,
    so every NASA procurement transaction in the window is fetched - in
    ES-window-sized time chunks - and filtered here. That is why the window is
    bounded by `--lookback-days` rather than running back to the window start;
    the mirror door covers the full window on its ~monthly runs.

    Contracts and IDVs only: FABS records carry no action code at all, so a
    grant pass here would fetch every NASA grant transaction to match nothing.
    """
    return [
        txn
        for txn in (
            _from_search(row, "contract")
            for row in _chunked_rows(lambda s, e: _procurement(client, s, e), start, end)
        )
        if has_termination_code(txn)
    ]


def _follow_up(client, anchors: list[Txn], end: str) -> list[Txn]:
    """The anchored awards' own later transactions, in two searches for all of them.

    The sweeps see a transaction only if it matched a keyword or carried a
    termination code, so an award can surface through its termination while the
    rescission that undid it - which may match neither - stays invisible. This
    re-reads the anchored awards' histories so `accept_award` gets to see it.

    `award_ids` is an exact-list filter keyed on the NATIVE award id (PIID or
    FAIN), which buys two things over the per-award /transactions/ endpoint: the
    whole set of anchors is covered by one procurement request and one
    assistance request instead of one request per award, and the awards whose
    rows carry no generated award id at all - some IDV transactions - now get a
    follow-up too, where the generated-id endpoint could never reach them.

    The window runs from the EARLIEST anchor's action date, so every anchor's
    own later history falls inside it.

    Failures propagate: a partial history could resurrect a reversed award, and
    a wrong publication is worse than a failed run.
    """
    ids = sorted({anchor.award_id for anchor in anchors if anchor.award_id})
    if not ids:
        return []
    earliest = min(anchor.action_date for anchor in anchors if anchor.action_date)
    start = max(WINDOW_START, earliest).isoformat()
    return [
        _from_search(row, default_type)
        for query, default_type in (
            (_procurement(client, start, end), "contract"),
            (_assistance(client, start, end), "grant"),
        )
        for row in _all_rows(query.award_ids(*ids))
    ]


# ---------------------------------------------------------------------------
# The door
# ---------------------------------------------------------------------------


def fetch_terminations(client=None, *, lookback_days: int = 120, today=None) -> list[Txn]:
    """Accepted terminations from the USAspending API, one `Txn` per award.

    Args:
        client: an ORM client, or None to build one. Passing one keeps tests offline.
        lookback_days: how far back the action-code sweep reads.
        today: the window's end, defaulting to the current date.
    """
    today = as_date(today) or date.today()
    end = today.isoformat()

    with contextlib.ExitStack() as stack:
        # The ORM client brings its own retry handler and rate limiter (3
        # retries, 10s base delay, exponential backoff) and leaves response
        # caching off by default, so a daily run always sees fresh data. No
        # backoff belongs here. A client this function opens, it closes; a
        # caller-supplied one is left alone.
        client = client or stack.enter_context(USASpendingClient())

        rows = _keyword_sweep(client, WINDOW_START, today)
        # The bounded sweep never reaches back before the window opened.
        code_start = max(WINDOW_START, today - timedelta(days=lookback_days))
        rows += _code_sweep(client, code_start, today)

        groups = group_by_award(rows)
        anchored = {
            key: anchor
            for key, group in groups.items()
            if (anchor := accept_award(group)) is not None
        }
        if not anchored:
            return []

        # Re-judge with the awards' own later history in hand; a reversal the
        # sweeps could not see drops the award here.
        #
        # A follow-up row is filed under the ANCHOR's group, not under its own
        # key. The two can differ: `award_key` falls back to `PIID:<id>` for a
        # row carrying no generated award id, and whether a given transaction of
        # one award carries one is not consistent across that award's history.
        # Keyed on itself, a rescission would land in a group of its own and the
        # re-judge below would re-read the anchor's group unchanged - silently
        # doing nothing for exactly the awards the follow-up exists to protect.
        anchor_keys = {anchor.award_id: key for key, anchor in anchored.items() if anchor.award_id}
        for txn in _follow_up(client, list(anchored.values()), end):
            groups.setdefault(anchor_keys.get(txn.award_id, txn.award_key), []).append(txn)

    accepted = (accept_award(groups[key]) for key in anchored)
    return [txn for txn in accepted if txn is not None]
