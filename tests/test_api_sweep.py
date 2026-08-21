"""The API door's two sweeps, their bounds, and the batched reversal follow-up.

Everything here runs against a fake ORM client. There is only one query shape -
`client.transactions.search()` - and three uses of it: the keyword sweep (one
request per category, the whole vocabulary OR-combined), the code sweep (no
keywords, filtered in Python), and the follow-up (`award_ids`, no keywords).

The fake honours keyword matching (naive substring, as the API does), the
`time_period` bounds on every query, and the backend's ES result window
(`es_window`) - the last two are what the chunking tests exercise.
"""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from nasatrack.api import PAGE_SIZE, IncompleteSweepError, fetch_terminations
from nasatrack.criteria import API_KEYWORDS, NASA_TOPTIER, WINDOW_START, WINDOW_START_ISO

TODAY = date(2026, 8, 20)


def row(
    award_id,
    *,
    action_date,
    action_type="",
    description="",
    mod="",
    award_type="Definitive Contract",
    generated=None,
):
    """One ORM-shaped transaction row."""
    return SimpleNamespace(
        award_identifier=award_id,
        generated_unique_award_id=f"CONT_AWD_{award_id}" if generated is None else generated,
        type=None,  # global search rows carry the description, not the code
        award_type=award_type,
        recipient_name="Acme Aerospace",
        action_date=date.fromisoformat(action_date),
        action_type=action_type,
        modification_number=mod,
        transaction_description=description,
        federal_action_obligation=None,
    )


class FakeSearch:
    """Records the filters one request applied, then serves rows from the client."""

    def __init__(self, client):
        self.client = client
        self.categories = []
        self.terms = None
        self.ids = None
        self.agency_name = None
        self.start = None
        self.end = None
        self.size = None
        self.ordering = None

    def contracts(self):
        self.categories.append("contracts")
        return self

    def idvs(self):
        self.categories.append("idvs")
        return self

    def grants(self):
        self.categories.append("grants")
        return self

    def agency(self, name):
        self.agency_name = name
        return self

    def time_period(self, start, end):
        self.start, self.end = start, end
        return self

    def page_size(self, size):
        self.size = size
        return self

    def keywords(self, *terms):
        self.terms = terms
        return self

    def award_ids(self, *ids):
        self.ids = ids
        return self

    def order_by(self, field, direction):
        self.ordering = (field, direction)
        return self

    def count(self):
        # The completeness guard compares all() against this; losing rows
        # between the two is simulated via client.count_overshoot.
        return len(self._rows()) + self.client.count_overshoot

    @property
    def is_assistance(self):
        return self.categories == ["grants"]

    def all(self):
        self.client.searches.append(self)
        rows = self._rows()
        # The real endpoint's ES result window: a crawl silently stops here.
        return rows[: self.client.es_window] if self.client.es_window else rows

    def _rows(self):
        if self.ids is not None:
            return self._follow_up()
        pool = self.client.grant_rows if self.is_assistance else self.client.contract_rows
        if self.start is not None:
            pool = [r for r in pool if self.start <= r.action_date.isoformat() <= self.end]
        if self.terms is None:
            return list(pool)  # the code sweep reads everything and filters in Python
        return [
            r
            for r in pool
            if any(term.lower() in r.transaction_description.lower() for term in self.terms)
        ]

    def _follow_up(self):
        broken = sorted(set(self.ids) & self.client.follow_up_failures)
        if broken:
            raise RuntimeError(f"award history unavailable for {broken[0]}")
        history = self.client.grant_history if self.is_assistance else self.client.history
        return [
            r
            for award_id in self.ids
            for r in history.get(award_id, [])
            if r.action_date.isoformat() >= self.start
        ]


class FakeTransactions:
    def __init__(self, client):
        self.client = client

    def search(self):
        return FakeSearch(self.client)


class FakeClient:
    def __init__(
        self,
        *,
        contract_rows=(),
        grant_rows=(),
        history=None,
        grant_history=None,
        follow_up_failures=(),
        count_overshoot=0,
        es_window=0,
    ):
        self.contract_rows = list(contract_rows)
        self.grant_rows = list(grant_rows)
        # Keyed on the NATIVE award id, which is what `award_ids` filters on.
        self.history = history or {}
        self.grant_history = grant_history or {}
        self.follow_up_failures = set(follow_up_failures)
        self.count_overshoot = count_overshoot
        self.es_window = es_window  # 0 = uncapped
        self.searches = []
        self.transactions = FakeTransactions(self)


def keys(results):
    return sorted(txn.award_key for txn in results)


def requests(client):
    """The client's requests, split into (keyword sweeps, code sweeps, follow-ups)."""
    keyword = [s for s in client.searches if s.terms is not None]
    code = [s for s in client.searches if s.terms is None and s.ids is None]
    follow_up = [s for s in client.searches if s.ids is not None]
    return keyword, code, follow_up


# ---------------------------------------------------------------------------
# Both sweeps feed the judge
# ---------------------------------------------------------------------------


def test_action_code_only_award_is_returned():
    # A formal F mod whose description names only the project: invisible to
    # every keyword, found solely by the code sweep.
    client = FakeClient(
        contract_rows=[
            row(
                "80NSSC25C0001", action_date="2026-07-01", action_type="F", description="Mars relay"
            )
        ]
    )
    results = fetch_terminations(client, today=TODAY)
    assert keys(results) == ["CONT_AWD_80NSSC25C0001"]
    assert results[0].source == "api"
    assert results[0].award_type == "contract"


def test_language_only_grant_is_returned():
    # FABS carries no action code, so a grant termination is language-only -
    # the coverage the grants arm of the keyword sweep exists to provide.
    client = FakeClient(
        grant_rows=[
            row(
                "80NSSC25K0030",
                action_date="2026-07-01",
                description="Termination for convenience agreement",
                award_type="Cooperative Agreement",
                generated="ASST_NON_80NSSC25K0030",
            )
        ]
    )
    results = fetch_terminations(client, today=TODAY)
    assert keys(results) == ["ASST_NON_80NSSC25K0030"]
    assert results[0].award_type == "grant"


def test_union_dedupes_by_award_key():
    # One award carrying both signals, so the code sweep and the keyword sweep
    # each collect the same row. One award out.
    client = FakeClient(
        contract_rows=[
            row(
                "80NSSC25C0002",
                action_date="2026-07-01",
                action_type="F",
                description="Stop work order issued",
            )
        ]
    )
    results = fetch_terminations(client, today=TODAY)
    assert keys(results) == ["CONT_AWD_80NSSC25C0002"]


def test_award_without_generated_id_uses_namespaced_key_and_is_still_followed_up():
    # `award_ids` filters on the native id, so an award whose rows carry no
    # generated id at all - which the per-award endpoint could never reach -
    # gets a follow-up like any other.
    client = FakeClient(
        contract_rows=[
            row("80LARC25F0003", action_date="2026-07-01", action_type="F", generated="")
        ]
    )
    results = fetch_terminations(client, today=TODAY)
    assert keys(results) == ["PIID:80LARC25F0003"]
    _, _, follow_up = requests(client)
    assert {s.ids for s in follow_up} == {("80LARC25F0003",)}


def test_pre_window_transaction_never_surfaces():
    client = FakeClient(
        contract_rows=[
            row("80NSSC24C0009", action_date="2024-11-01", action_type="F", description="Stop work")
        ]
    )
    assert fetch_terminations(client, today=TODAY) == []


# ---------------------------------------------------------------------------
# The reversal follow-up
# ---------------------------------------------------------------------------


def test_later_rescission_in_the_follow_up_drops_the_award():
    # The rescission matches no keyword and carries no termination code, so only
    # the award's own history can see it.
    terminated = row(
        "80NSSC25C0004",
        action_date="2026-06-01",
        action_type="F",
        description="Mars relay",
        mod="3",
    )
    client = FakeClient(
        contract_rows=[terminated],
        history={
            "80NSSC25C0004": [
                terminated,
                row(
                    "80NSSC25C0004",
                    action_date="2026-06-20",
                    description="Rescission of the termination notice",
                    mod="4",
                ),
            ]
        },
    )
    assert fetch_terminations(client, today=TODAY) == []


def test_follow_up_reaches_an_anchor_keyed_by_its_piid_fallback():
    # The anchor's own sweep row carries no generated award id, so it groups
    # under the `PIID:` fallback key - but its later transactions DO carry one,
    # and a follow-up row filed under its own key would land in a group of its
    # own, leaving the anchor re-judged against unchanged evidence. Follow-up
    # rows are filed under the anchor's key for exactly this case: the awards
    # with inconsistent generated ids are the ones the follow-up exists for.
    terminated = row("80LARC25F0020", action_date="2026-06-01", action_type="F", generated="")
    client = FakeClient(
        contract_rows=[terminated],
        history={
            "80LARC25F0020": [
                terminated,
                row(
                    "80LARC25F0020",
                    action_date="2026-06-20",
                    description="Rescission of the termination notice",
                    mod="4",
                    generated="CONT_IDV_80LARC25F0020",
                ),
            ]
        },
    )
    assert fetch_terminations(client, today=TODAY) == []


def test_history_before_the_earliest_anchor_is_not_fetched():
    # A rescission that PRECEDES every anchor reversed an earlier termination,
    # not this one, so the follow-up window opens at the earliest anchor's date.
    client = FakeClient(
        contract_rows=[row("80NSSC25C0005", action_date="2026-06-01", action_type="F")],
        history={
            "80NSSC25C0005": [
                row(
                    "80NSSC25C0005",
                    action_date="2026-02-01",
                    description="Rescission of the stop work order",
                )
            ]
        },
    )
    results = fetch_terminations(client, today=TODAY)
    assert keys(results) == ["CONT_AWD_80NSSC25C0005"]
    _, _, follow_up = requests(client)
    assert {s.start for s in follow_up} == {"2026-06-01"}


def test_follow_up_window_opens_at_the_earliest_anchor():
    # One window covers every anchor, so it has to start at the earliest of them.
    early = row("80NSSC25C0007", action_date="2025-03-04", action_type="F")
    late = row("80NSSC25C0008", action_date="2026-06-01", action_type="F")
    client = FakeClient(contract_rows=[early, late])
    # The lookback must reach the early anchor for the code sweep to see it.
    fetch_terminations(client, lookback_days=600, today=TODAY)
    _, _, follow_up = requests(client)
    assert {s.start for s in follow_up} == {"2025-03-04"}
    assert {s.end for s in follow_up} == {TODAY.isoformat()}


def test_follow_up_is_two_requests_however_many_awards_are_anchored():
    # The whole anchor set rides in one procurement request and one assistance
    # request, rather than one paginated history fetch per award.
    client = FakeClient(
        contract_rows=[
            row(f"80NSSC25C00{n}", action_date="2026-06-01", action_type="F") for n in range(10, 15)
        ]
    )
    results = fetch_terminations(client, today=TODAY)
    assert len(results) == 5
    _, _, follow_up = requests(client)
    assert len(follow_up) == 2
    assert [s.categories for s in follow_up] == [["contracts", "idvs"], ["grants"]]
    expected = tuple(f"80NSSC25C00{n}" for n in range(10, 15))
    assert {s.ids for s in follow_up} == {expected}


def test_same_day_mod_10_rescinds_mod_9():
    # Mod numbers break ties inside one action_date, and FABS-style unpadded
    # numbers sort as text: '10' < '9' would read the rescission as coming
    # FIRST and publish the award as terminated. api.orm_txn pads them.
    client = FakeClient(
        contract_rows=[
            row(
                "80NSSC25C0030",
                action_date="2026-06-01",
                action_type="F",
                description="Stop work order issued",
                mod="9",
            ),
            row(
                "80NSSC25C0030",
                action_date="2026-06-01",
                description="Rescission of the stop work order",
                mod="10",
            ),
        ]
    )
    assert fetch_terminations(client, today=TODAY) == []


def test_no_anchor_means_no_follow_up_at_all():
    client = FakeClient(
        contract_rows=[row("80NSSC25C0016", action_date="2026-06-01", description="Option year 3")]
    )
    assert fetch_terminations(client, today=TODAY) == []
    _, _, follow_up = requests(client)
    assert follow_up == []


def test_follow_up_failure_propagates():
    # A partial result must never publish: one broken follow-up fails the run.
    client = FakeClient(
        contract_rows=[row("80NSSC25C0006", action_date="2026-06-01", action_type="F")],
        follow_up_failures=["80NSSC25C0006"],
    )
    with pytest.raises(RuntimeError, match="award history unavailable"):
        fetch_terminations(client, today=TODAY)


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def test_keyword_sweep_is_one_request_per_category_over_the_full_window():
    client = FakeClient()
    fetch_terminations(client, today=TODAY)
    keyword, code, follow_up = requests(client)

    # The whole vocabulary is OR-combined server-side: two requests, not two
    # per keyword, and no follow-up because nothing was anchored.
    assert len(keyword) == 2
    assert follow_up == []
    assert {s.terms for s in keyword} == {tuple(API_KEYWORDS)}
    assert [s.categories for s in keyword] == [["contracts", "idvs"], ["grants"]]
    assert {s.start for s in keyword} == {WINDOW_START_ISO}
    assert {s.end for s in keyword} == {TODAY.isoformat()}

    # The code sweep is procurement-only: FABS has no action codes to match.
    assert len(code) == 1
    assert code[0].categories == ["contracts", "idvs"]

    assert {s.agency_name for s in client.searches} == {NASA_TOPTIER}
    assert {s.size for s in client.searches} == {PAGE_SIZE}


def test_code_sweep_start_is_bounded_by_lookback_days():
    client = FakeClient()
    fetch_terminations(client, lookback_days=120, today=TODAY)
    _, code, _ = requests(client)
    assert code[0].start == (TODAY - timedelta(days=120)).isoformat()
    assert code[0].end == TODAY.isoformat()


def test_code_sweep_start_never_precedes_the_window():
    today = WINDOW_START + timedelta(days=10)
    client = FakeClient()
    fetch_terminations(client, lookback_days=120, today=today)
    _, code, _ = requests(client)
    assert code[0].start == WINDOW_START_ISO


def test_every_sweep_pins_a_high_cardinality_sort_order():
    # Unstable pagination on a multi-hundred-page crawl silently lost F-coded
    # rows once; the sort pins the page boundaries, and it must be a
    # high-cardinality key - sorted by action_date, a whole day is one sort-tie
    # block whose order the backend's replicas disagree on, and a row can slip
    # through a page boundary while the total count stays right.
    client = FakeClient()
    fetch_terminations(client, today=TODAY)
    assert all(s.ordering == ("award_id", "asc") for s in client.searches)


def test_a_short_paginated_sweep_fails_the_run():
    # Fewer rows than the server reports means pages were lost - abort rather
    # than publish a silently short file.
    client = FakeClient(count_overshoot=1)
    with pytest.raises(IncompleteSweepError):
        fetch_terminations(client, today=TODAY)


def test_code_sweep_bisects_windows_that_exceed_the_es_result_window(monkeypatch):
    # The real endpoint's ES window caps any crawl at 10,000 rows; a 578-day
    # sweep once came back 10,000 of 36,720. Chunking by time must recover
    # every row, including the F mods that fell past the cap.
    import nasatrack.api as api_module

    monkeypatch.setattr(api_module, "ES_RESULT_WINDOW_SAFE", 3)
    rows = [
        row(f"80NSSC26P{i:04d}", action_date=d, action_type=t, description=f"item {i}")
        for i, (d, t) in enumerate(
            [
                ("2026-05-01", ""),
                ("2026-05-20", ""),
                ("2026-06-05", "F"),
                ("2026-06-20", ""),
                ("2026-07-10", ""),
                ("2026-07-25", ""),
                ("2026-08-10", "F"),
                ("2026-08-15", ""),
            ]
        )
    ]
    client = FakeClient(contract_rows=rows, es_window=3)
    results = fetch_terminations(client, lookback_days=120, today=TODAY)
    assert keys(results) == ["CONT_AWD_80NSSC26P0002", "CONT_AWD_80NSSC26P0006"]


def test_a_single_day_larger_than_the_es_window_fails_loudly(monkeypatch):
    import nasatrack.api as api_module

    monkeypatch.setattr(api_module, "ES_RESULT_WINDOW_SAFE", 2)
    rows = [
        row(f"80NSSC26P{i:04d}", action_date="2026-08-10", description=f"item {i}")
        for i in range(4)
    ]
    client = FakeClient(contract_rows=rows, es_window=2)
    with pytest.raises(IncompleteSweepError):
        fetch_terminations(client, lookback_days=30, today=TODAY)
