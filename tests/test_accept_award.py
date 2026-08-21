"""accept_award scans an award's history and returns its operative termination."""

from datetime import date

from nasatrack.criteria import (
    Txn,
    accept_award,
    detected_by,
    is_explicit_termination,
    mod_sort_key,
)


def txn(day, *, action_type="", description="", sort_key="", award_type="contract"):
    return Txn(
        award_key="80NSSC25C0001",
        award_id="80NSSC25C0001",
        generated_award_id="CONT_AWD_80NSSC25C0001",
        award_type=award_type,
        recipient_name="ACME AEROSPACE",
        action_date=date.fromisoformat(day),
        action_type=action_type,
        description=description,
        source="mirror",
        sort_key=sort_key,
    )


def test_action_code_alone_is_accepted():
    row = txn("2025-06-01", action_type="F", description="ADMINISTRATIVE MODIFICATION")
    assert accept_award([row]) is row
    assert detected_by(row) == "action_code"


def test_description_alone_is_accepted():
    row = txn("2025-06-01", description="TERMINATION FOR CONVENIENCE OF THE GOVERNMENT")
    assert accept_award([row]) is row
    assert detected_by(row) == "description"


def test_grant_action_code_is_ignored_language_is_not():
    # FABS has no reason-for-modification field; grants terminate in prose.
    coded = txn("2025-06-01", action_type="F", award_type="grant", description="AMENDMENT")
    assert not is_explicit_termination(coded)
    worded = txn(
        "2025-06-01", award_type="grant", description="TERMINATION FOR CONVENIENCE AGREEMENT"
    )
    assert accept_award([worded]) is worded


def test_code_and_language_together():
    row = txn("2025-06-01", action_type="N", description="LEGAL CONTRACT CANCELLATION")
    assert detected_by(row) == "both"
    assert accept_award([row]) is row


def test_a_bare_n_code_is_not_a_termination():
    # NASA applies N to routine admin actions (80JSC026F0015, 80JSC026P0010),
    # so N only ever confirms language; F stands alone.
    bare_n = txn("2025-06-01", action_type="N", description="EXERCISE OPTION PERIOD 2")
    assert not is_explicit_termination(bare_n)
    assert accept_award([bare_n]) is None
    bare_f = txn("2025-06-01", action_type="F", description="MODIFICATION")
    assert accept_award([bare_f]) is bare_f


def test_cause_is_rejected_even_with_an_f_code():
    row = txn("2025-06-01", action_type="F", description="TERMINATED FOR CAUSE")
    assert accept_award([row]) is None


def test_terminate_then_rescind_drops_out():
    rows = [
        txn("2025-06-01", action_type="F", description="STOP WORK ORDER"),
        txn("2025-07-01", description="RESCISSION OF THE STOP WORK ORDER"),
    ]
    assert accept_award(rows) is None


def test_terminate_rescind_terminate_keeps_the_second_termination():
    first = txn("2025-06-01", action_type="F", description="STOP WORK ORDER")
    rescind = txn("2025-07-01", description="RESCISSION OF THE STOP WORK ORDER")
    second = txn("2025-09-15", action_type="F", description="TERMINATION FOR CONVENIENCE")
    assert accept_award([second, rescind, first]) is second


def test_repeated_terminations_without_a_reversal_report_the_first():
    # The date of record is when the termination was ISSUED. 80GSFC23CA001's
    # mod 00009 carried the F code on 2025-05-01; a year of settlement mods
    # followed, and reporting the last of them pushed the published date to
    # 2026-07-30. The anchor is the first explicit termination still standing.
    issued = txn("2025-05-01", action_type="F", description="TERMINATE FOR CONVENIENCE")
    settled = txn("2026-07-30", action_type="F", description="PARTIAL TERMINATION SETTLEMENT")
    assert accept_award([settled, issued]) is issued


def test_vacatur_clears_the_termination():
    rows = [
        txn("2025-06-01", action_type="F", description="TERMINATE FOR CONVENIENCE"),
        txn("2025-08-01", description="TERMINATION VACATED BY COURT ORDER"),
    ]
    assert accept_award(rows) is None


def test_pre_window_termination_is_not_accepted():
    row = txn("2025-01-19", action_type="F", description="TERMINATE FOR CONVENIENCE")
    assert accept_award([row]) is None


def test_reversal_before_the_termination_does_not_clear_it():
    rows = [
        txn("2025-03-01", description="RESCISSION OF THE STOP WORK ORDER"),
        txn("2025-06-01", action_type="F", description="ADMIN MOD"),
    ]
    assert accept_award(rows) is rows[1]


def test_same_day_actions_order_by_sort_key():
    terminate = txn("2025-06-01", action_type="F", description="STOP WORK", sort_key="0001")
    rescind = txn("2025-06-01", description="RESCIND THE STOP WORK ORDER", sort_key="0002")
    assert accept_award([rescind, terminate]) is None
    # Same two actions, opposite sort keys: the termination now lands last.
    terminate_last = txn("2025-06-01", action_type="F", description="STOP WORK", sort_key="0003")
    assert accept_award([terminate_last, rescind]) is terminate_last


def test_mod_numbers_sort_in_issue_order_not_alphabetically():
    # Raw text puts mod 10 before mod 9, which reverses the verdict when a
    # termination and its rescission share an action_date.
    assert mod_sort_key("9") < mod_sort_key("10")
    assert mod_sort_key("P00009") < mod_sort_key("P00010")
    assert mod_sort_key("2") < mod_sort_key("10") < mod_sort_key("11")
    assert mod_sort_key(None) == ""


def test_same_day_mods_9_and_10_order_by_number():
    # Padded through mod_sort_key, so the rescission lands after the
    # termination it undoes. Unpadded, the award would publish as terminated.
    terminate = txn(
        "2025-06-01", action_type="F", description="STOP WORK", sort_key=mod_sort_key("9")
    )
    rescind = txn(
        "2025-06-01", description="RESCIND THE STOP WORK ORDER", sort_key=mod_sort_key("10")
    )
    assert accept_award([rescind, terminate]) is None


def test_no_termination_at_all():
    assert accept_award([txn("2025-06-01", description="ADMINISTRATIVE MODIFICATION")]) is None
    assert accept_award([]) is None
