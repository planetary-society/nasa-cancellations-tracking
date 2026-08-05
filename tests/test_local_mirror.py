"""The local USAspending mirror source.

Offline by construction: psycopg is never imported, no socket is ever opened,
and the SQL constants are treated as text. What these tests protect is the
part that decides what enters the snapshot - the gating that keeps CI on
replay, and _combine()'s merge of the detection nets - plus the measured
tuning constants the SQL is built from, which are cheap to "clean up" and
expensive to rediscover.

The developer's machine has a real .env and load_dotenv() runs at module
import, so every gating test must clear all six variables explicitly rather
than assume a clean environment.
"""

import pandas as pd
import pytest

import local_usaspending_mirror_query as lm
import search
import sources
import termination_vocabulary
import usaspending_terminations_query as utq
from contract_query import FINAL_COLUMNS
from local_usaspending_mirror_query import LocalUSASpendingMirrorQuery as Q

ENV_VARS = [lm.DSN_ENV_VAR, *lm.COMPONENT_ENV_VARS]

COMPONENT_VALUES = {
    "DB_USER": "readonly_user",
    "DB_PASS": "readonly",
    "DB_URI": "192.168.0.223",
    "DB_PORT": "5432",
    "DB_NAME": "data_store_api",
}


@pytest.fixture
def no_db_env(monkeypatch):
    """An environment with no mirror credentials at all.

    load_dotenv() already populated os.environ from the real .env at import
    time, so "unset" has to be asserted, not assumed.
    """
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def row(method="description_regex", **overrides):
    """A transaction row shaped as the nets return it, already net-labelled.

    The SQL always emits detection_method, so production rows arrive tagged;
    fixtures do the same rather than relying on argument position.
    """
    base = {
        "detection_method": method,
        "award_id_native": "80NSSC25K0001",
        "generated_unique_award_id": "CONT_AWD_80NSSC25K0001_8000",
        "is_fpds": True,
        "modification_number": "P00001",
        "action_date": "2025-06-01",
        "transaction_description": "PROJECT WORK",
        "federal_action_obligation": -100.0,
        "recipient_name": "ACME AEROSPACE",
    }
    base.update(overrides)
    return base


# --- gating ----------------------------------------------------------------


def test_no_credentials_means_not_configured(no_db_env):
    assert Q.is_configured() is False


def test_no_credentials_and_no_export_means_unavailable(no_db_env, workdir):
    """search.py drops the source in this state; nothing else would rescue it."""
    assert Q.is_configured() is False


def test_full_dsn_alone_configures_the_source(no_db_env, monkeypatch):
    monkeypatch.setenv(lm.DSN_ENV_VAR, "postgresql://u:p@host:5432/db")
    assert Q.is_configured() is True


def test_all_component_vars_configure_the_source(no_db_env, monkeypatch):
    for var, value in COMPONENT_VALUES.items():
        monkeypatch.setenv(var, value)
    assert Q.is_configured() is True


@pytest.mark.parametrize("missing", lm.COMPONENT_ENV_VARS)
def test_a_partial_component_set_does_not_configure_the_source(
    no_db_env, monkeypatch, missing
):
    """A half-filled .env must fail the gate, not build a broken DSN and then
    fail at connect time inside the daily job."""
    for var, value in COMPONENT_VALUES.items():
        if var != missing:
            monkeypatch.setenv(var, value)
    assert Q.is_configured() is False


def test_dsn_prefers_the_full_uri_verbatim(no_db_env, monkeypatch):
    monkeypatch.setenv(lm.DSN_ENV_VAR, "postgresql://u:p@host:6543/db?opt=1")
    for var, value in COMPONENT_VALUES.items():
        monkeypatch.setenv(var, value)

    assert Q._dsn() == "postgresql://u:p@host:6543/db?opt=1"


def test_dsn_assembles_components_with_db_uri_as_the_host(no_db_env, monkeypatch):
    """DB_URI holds the host, not a URI - a pre-existing .env naming quirk that
    a reader would otherwise "fix" into an unusable DSN."""
    for var, value in COMPONENT_VALUES.items():
        monkeypatch.setenv(var, value)

    assert Q._dsn() == (
        "postgresql://readonly_user:readonly@192.168.0.223:5432/data_store_api"
    )


# --- historical exports are not replayed ----------------------------------


@pytest.fixture
def prior_export(workdir, write_csv):
    """A committed export standing in for the last successful live run."""
    rows = [
        {
            "Award ID": "80NSSC25K0030",
            "source_type": "Grant",
            "recipient": "BROWN UNIVERSITY",
            "value": "-448257.0",
            "savings": "",
            "status": "Clawback of 100% ($448,257) on 2026-01-20",
            "source_url": "https://www.usaspending.gov/award/"
            "ASST_NON_80NSSC25K0030_080/",
            "description": "ADJUSTMENT TO COMPLETED PROJECT",
            "agency": "NASA",
            "claim_date": "",
        },
        {
            "Award ID": "80MSFC22CA005",
            "source_type": "Contract",
            "recipient": "LOCKHEED MARTIN",
            "value": "-1000.0",
            "savings": "",
            "status": "Terminate-for-convenience action P00032 on 2025-09-30",
            "source_url": "https://www.usaspending.gov/award/"
            "CONT_AWD_80MSFC22CA005_8000/",
            "description": "MARS ASCENT VEHICLE INTEGRATED SYSTEM (MAVIS)",
            "agency": "NASA",
            "claim_date": "",
        },
    ]
    write_csv(
        "data/usaspending_database_direct_query_2026-07-28.csv",
        FINAL_COLUMNS,
        rows,
    )
    return rows


def test_a_prior_export_does_not_make_the_live_source_available(
    no_db_env, prior_export
):
    """An export can predate the current source contract and is never proof
    that the database can answer today's query."""
    assert Q.is_configured() is False


def test_direct_search_without_credentials_reports_unavailable(no_db_env, prior_export):
    """The exact regression: a legacy export must not enter ingest and fail a
    newer source-frame contract when no database is configured."""
    with pytest.raises(lm.LocalMirrorUnavailableError, match="no database"):
        Q().search()


def test_database_connectivity_errors_are_reported_as_unavailable(
    no_db_env, monkeypatch
):
    import psycopg

    monkeypatch.setenv(lm.DSN_ENV_VAR, "postgresql://u:p@offline:5432/db")

    def offline(*args, **kwargs):
        raise psycopg.OperationalError("host is down")

    monkeypatch.setattr(psycopg, "connect", offline)

    with pytest.raises(lm.LocalMirrorUnavailableError, match="not accessible"):
        Q()._query_mirror()


# --- _combine --------------------------------------------------------------


def test_most_recent_transaction_wins_the_description():
    """Two mods on one award: the later one describes its current state."""
    old = row(
        modification_number="P00001",
        action_date="2025-01-21",
        transaction_description="STOP WORK ORDER",
    )
    new = row(
        modification_number="P00009",
        action_date="2026-03-04",
        transaction_description="TERMINATION SETTLEMENT",
    )

    df = lm._combine([old, new])

    assert len(df) == 1
    assert df.iloc[0]["description"] == "TERMINATION SETTLEMENT"


def test_corroborating_nets_are_joined_in_one_status():
    """An award caught by both the action-code and truncation nets should say
    so; collapsing to the winning row's phrase alone would hide the second
    line of evidence."""
    coded = row(
        "action_code",
        action_type="F",
        modification_number="P00003",
        action_date="2025-05-01",
    )
    truncated = row(
        "end_date_truncation",
        modification_number="P00003",
        action_date="2025-05-01",
        days_truncated=912,
        previous_end_date="2028-01-01",
        end_date="2025-07-04",
    )

    df = lm._combine([coded, truncated])

    assert len(df) == 1
    assert df.iloc[0]["status"] == (
        "Terminate-for-convenience action P00003 on 2025-05-01; "
        "End date shortened 912 days from 2028-01-01 to 2025-07-04 "
        "by mod P00003 on 2025-05-01"
    )


def test_phrases_appear_in_net_order_and_never_repeat():
    """Status reads in net order even when the later transaction belongs to an
    earlier net, and a transaction returned twice says it once."""
    hit = row(action_date="2025-02-01", modification_number="P00001")
    coded = row(
        "action_code",
        action_type="N",
        action_date="2026-01-01",
        modification_number="P00007",
    )

    df = lm._combine([hit, coded, dict(hit)])

    assert df.iloc[0]["status"] == (
        "Legal-contract-cancellation action P00007 on 2026-01-01; "
        "Termination-language transaction P00001 on 2025-02-01"
    )


def test_termination_for_cause_never_surfaces():
    """Contractor failure is out of scope by methodology (commit 08a52cf).
    Q2's regex matches cause text on purpose so the exclusion stays in one
    place - here, in Python, using the shared predicate."""
    cause = row(transaction_description="termination for cause of contractor")

    assert lm._combine([cause]).empty


def test_combine_uses_the_shared_cause_predicate():
    """Guards against this module quietly reintroducing a local copy, the same
    way test_ledger_classify pins the ledger's predicates."""
    assert lm.is_cause is termination_vocabulary.is_cause
    assert lm.is_reversal is termination_vocabulary.is_reversal
    assert lm.is_vacatur is termination_vocabulary.is_vacatur


def test_a_rescinded_award_is_not_resurfaced():
    """The regression from the mirror's first live run (2026-07-30): six
    rescinded grants flipped from `reinstated` back to `listed`, because the
    rescission's own text ("RESCINDING STOP WORK NOTICE") matches the sweep
    and the full-window query re-reports the old termination forever."""
    stop = row(
        action_date="2025-04-25",
        modification_number="P00002",
        transaction_description="STOP WORK ORDER ISSUED",
    )
    rescission = row(
        action_date="2025-06-27",
        modification_number="P00003",
        transaction_description=(
            "RESCINDING STOP WORK NOTICE. TAURUS: A BALLOON-BORNE POLARIMETER"
        ),
    )

    assert lm._combine([stop, rescission]).empty


def test_a_vacated_termination_is_not_resurfaced():
    """Same rule for the court-ordered flavor of reversal."""
    vacatur = row(
        transaction_description=(
            "The termination for convenience has been vacated and set aside "
            "pursuant to the order entered on september 3 2025"
        )
    )

    assert lm._combine([vacatur]).empty


def test_a_retermination_after_a_rescission_still_surfaces():
    """The reversal is judged on the LATEST row only: an award terminated
    again after a rescission is a live cancellation and must not be hidden by
    its older reversal."""
    rescission = row(
        action_date="2025-06-27",
        transaction_description="RESCINDING STOP WORK NOTICE. TAURUS",
    )
    reterminated = row(
        action_date="2026-02-01",
        modification_number="P00009",
        transaction_description="TERMINATE FOR CONVENIENCE - FULL",
    )

    df = lm._combine([rescission, reterminated])

    assert len(df) == 1
    assert df.iloc[0]["description"] == "TERMINATE FOR CONVENIENCE - FULL"


def test_status_phrasing_is_shared_with_the_api_source():
    """Both sources describe the same FPDS action codes; a local copy of the
    wording would let the snapshot say two different things about one code."""
    assert lm.ACTION_CODE_KINDS is utq.ACTION_CODE_KINDS


@pytest.mark.parametrize(
    ("is_fpds", "expected"),
    [
        (True, "Contract"),
        (False, "Grant"),
        ("t", "Contract"),
        ("f", "Grant"),
        ("True", "Contract"),
        ("False", "Grant"),
    ],
)
def test_source_type_reads_is_fpds_from_booleans_and_csv_text(is_fpds, expected):
    """A replayed export carries is_fpds as text, and bool('f') is True - so a
    naive cast would relabel every grant a contract."""
    df = lm._combine([row(is_fpds=is_fpds)])

    assert df.iloc[0]["source_type"] == expected


def test_clawback_status_reports_percentage_and_amount():
    """Brown (80NSSC25K0030): the canonical pure clawback, invisible to the
    other three nets - no termination text, no truncation, action type D."""
    brown = row(
        "clawback",
        award_id_native="80NSSC25K0030",
        generated_unique_award_id="ASST_NON_80NSSC25K0030_080",
        is_fpds=False,
        action_date="2026-01-20",
        transaction_description="ADJUSTMENT TO COMPLETED PROJECT",
        federal_action_obligation=-448257.0,
        recipient_name="BROWN UNIVERSITY",
        clawback_fraction=1.0,
    )

    df = lm._combine([brown])

    assert df.iloc[0]["status"] == "Clawback of 100% ($448,257) on 2026-01-20"
    assert df.iloc[0]["source_type"] == "Grant"
    assert df.iloc[0]["source_url"] == (
        "https://www.usaspending.gov/award/ASST_NON_80NSSC25K0030_080/"
    )


def test_rows_without_an_award_id_are_skipped():
    assert lm._combine([row(award_id_native="")]).empty


def test_combine_emits_every_final_column():
    """search.py concatenates sources, so a missing column would misalign the
    whole snapshot."""
    df = lm._combine([row()])

    assert list(df.columns) == FINAL_COLUMNS
    assert df.iloc[0]["agency"] == "NASA"
    # An inference source asserts no savings figure and no claim date.
    assert pd.isna(df.iloc[0]["savings"])
    assert pd.isna(df.iloc[0]["claim_date"])


# --- the net registry ------------------------------------------------------


@pytest.mark.parametrize("net", lm.NETS, ids=lambda net: net.name)
def test_every_net_is_bounded_to_the_tracking_window(net):
    """Without an action_date bound each net seq-scans ~236M rows, and the
    window itself is the methodology: cancellations since the inauguration."""
    assert f"'{lm.TRACKING_WINDOW_START}'" in net.sql


@pytest.mark.parametrize("net", lm.NETS, ids=lambda net: net.name)
def test_every_net_carries_a_timeout_outside_its_sql(net):
    """Fail-loud means failing, not hanging: an unresponsive mirror must abort
    the daily job. The guard is a field on the net, not a statement glued to
    the front of the query text - splitting that back off was one stray
    semicolon away from silently truncating a query."""
    assert net.timeout_s > 0
    assert not net.sql.lstrip().startswith("SET")


@pytest.mark.parametrize("net", lm.NETS, ids=lambda net: net.name)
def test_net_names_match_the_detection_method_the_sql_emits(net):
    """_combine ranks an award's phrases by its net's position, keyed on the
    label the SQL wrote into the row; a mismatch would unorder the status."""
    assert f"'{net.name}' AS detection_method" in net.sql


# --- the SQL the nets are built from ---------------------------------------


def test_action_code_net_renders_the_shared_code_list():
    """Default and cause mean contractor failure, not policy cancellation, and
    closeout fires on every award that ever ended normally. Re-adding any of
    them would flood the snapshot."""
    in_list = lm.Q1_ACTION_CODES.split("action_type IN (")[1].split(")")[0]

    assert in_list == ", ".join(f"'{c}'" for c in utq.TERMINATION_ACTION_CODES)
    for excluded in ("'E'", "'X'", "'K'"):
        assert excluded not in in_list


@pytest.mark.parametrize(
    "pattern",
    [termination_vocabulary.TERM_TEXT, termination_vocabulary.CAUSE_TEXT],
    ids=["TERM_TEXT", "CAUSE_TEXT"],
)
def test_description_net_is_built_from_the_shared_vocabulary(pattern):
    """The regex is translated from termination_vocabulary, not retyped, so
    widening the shared predicate widens this net too. Only the word boundary
    differs: \\b is a backspace in Postgres AREs, \\y is the boundary."""
    assert pattern.pattern.replace(r"\b", r"\y") in lm.Q2_DESCRIPTION_REGEX


def test_description_net_keeps_its_local_n_code_language():
    """'legal contract cancellation' is detection language for this net, not a
    classification predicate - promoting it into termination_vocabulary would
    change how already-collected snapshot history is judged."""
    assert lm.LEGAL_CANCELLATION_TEXT in lm.Q2_DESCRIPTION_REGEX
    assert lm.LEGAL_CANCELLATION_TEXT not in termination_vocabulary.TERM_TEXT.pattern


def test_truncation_net_keeps_its_direction_and_nonpositive_obligation_filters():
    """Positive funding cannot qualify, while zero-dollar administrative
    shortenings remain visible."""
    assert "federal_action_obligation <= 0" in lm.Q3_END_DATE_TRUNCATION
    assert (
        f"previous_end_date - end_date > {lm.SHORTENING_MIN_DAYS}"
        in lm.Q3_END_DATE_TRUNCATION
    )


def test_truncation_net_reads_history_past_the_tracking_window():
    """The previous dated transaction may predate the policy window, so the
    history CTE deliberately reaches back to USAspending's beginning."""
    assert "'2007-10-01'" in lm.Q3_END_DATE_TRUNCATION


def test_clawback_net_keeps_the_inside_pop_gate_and_shared_thresholds():
    """Measured 2026-07-30: 91 of 109 threshold-passing clawbacks were routine
    post-expiry underruns. Dropping the inside-PoP gate floods the snapshot
    with finished grants handing back unspent money."""
    assert "period_of_performance_start_date" in lm.Q4_PURE_CLAWBACKS
    assert "period_of_performance_current_end_date" in lm.Q4_PURE_CLAWBACKS
    assert str(utq.CLAWBACK_AMOUNT_THRESHOLD) in lm.Q4_PURE_CLAWBACKS
    assert str(utq.CLAWBACK_FRACTION_THRESHOLD) in lm.Q4_PURE_CLAWBACKS


def test_clawback_fraction_is_selected_and_filtered_by_one_expression():
    """The reported percentage and the >=25% gate must be the same arithmetic;
    two hand-kept copies could disagree about the denominator."""
    assert lm.Q4_PURE_CLAWBACKS.count(lm.CLAWBACK_FRACTION_SQL) == 2


# --- search.py integration -------------------------------------------------


def test_an_unconfigured_mirror_is_skipped_and_nothing_else_is(no_db_env, workdir):
    """Without credentials the source cannot produce a frame, and search.py's
    fail-loud loop would abort the whole run on it. Only the mirror may be
    skipped: a gate that swallowed anyone else would silently shrink the
    snapshot, so the expectation is derived from the registry itself and a
    future sixth source is covered automatically."""
    obj = search.Search.__new__(search.Search)
    obj.sources = {"Local USAspending Mirror": lm.LocalUSASpendingMirrorQuery}
    obj.skipped_sources = set()
    obj.sources_cancellation_data = {}
    obj.unique_award_ids = []

    obj._collect_source_data()

    assert obj.sources_cancellation_data == {}
    assert obj.skipped_sources == {"Local USAspending Mirror"}
    assert set(search.SOURCES) - {"Local USAspending Mirror"} == set(search.SOURCES) - {
        "Local USAspending Mirror"
    }


def test_a_prior_export_does_not_keep_the_mirror_in_the_run(no_db_env, prior_export):
    """An export is an artifact of an earlier run, never proof the database can
    answer today's query, so it must not rescue the source from being skipped."""
    obj = search.Search.__new__(search.Search)
    obj.sources = {"Local USAspending Mirror": lm.LocalUSASpendingMirrorQuery}
    obj.skipped_sources = set()
    obj.sources_cancellation_data = {}
    obj.unique_award_ids = []

    obj._collect_source_data()

    assert obj.skipped_sources == {"Local USAspending Mirror"}


def test_an_unreachable_configured_mirror_is_skipped_during_collection(capsys):
    class OfflineMirror:
        def search(self):
            raise lm.LocalMirrorUnavailableError("database host is down")

    obj = search.Search.__new__(search.Search)
    obj.sources = {"Local USAspending Mirror": OfflineMirror}
    obj.skipped_sources = set()
    obj.sources_cancellation_data = {}
    obj.unique_award_ids = []

    obj._collect_source_data()

    assert obj.sources_cancellation_data == {}
    assert obj.skipped_sources == {"Local USAspending Mirror"}
    assert f"Skipping {sources.LOCAL_MIRROR}" in capsys.readouterr().err


def test_mirror_is_the_last_source_in_the_registry():
    """SOURCES order is first-source-wins for an award's snapshot row, so the
    depth net must stay last and only own rows nobody else found."""
    assert list(search.SOURCES)[-1] == "Local USAspending Mirror"


def test_instantiating_search_does_not_mutate_the_registry(no_db_env, workdir):
    """self.sources is a copy: popping the mirror in place would drop it for
    every later Search in the same process, export included."""
    search.Search()

    assert "Local USAspending Mirror" in search.SOURCES
