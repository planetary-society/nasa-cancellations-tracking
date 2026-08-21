"""The two part files merge into one deduped, deterministically ordered row set."""

from datetime import date
from decimal import Decimal

from nasatrack.criteria import Txn
from nasatrack.schema import TerminationRow
from nasatrack.terminations import merge, txn_to_row


def row(award_key, *, source, day="2025-06-01", award_id=None, award_type="contract", **overrides):
    values = {
        "award_key": award_key,
        "award_id": award_id if award_id is not None else award_key,
        "generated_award_id": "" if award_key.startswith("PIID:") else award_key,
        "award_type": award_type,
        "recipient_name": "ACME AEROSPACE",
        "action_date": date.fromisoformat(day),
        "action_type": "F",
        "modification_number": "P00003",
        "transaction_amount": Decimal("-1000"),
        "transaction_description": "TERMINATE FOR CONVENIENCE",
        "detected_by": "both",
        "sources": source,
        "override_status": "",
    }
    return TerminationRow(**{**values, **overrides})


def test_txn_to_row_carries_the_door_and_its_evidence():
    txn = Txn(
        award_key="CONT_AWD_80NSSC25C0001",
        award_id="80NSSC25C0001",
        generated_award_id="CONT_AWD_80NSSC25C0001",
        award_type="contract",
        recipient_name="ACME AEROSPACE",
        action_date=date(2025, 6, 1),
        action_type="F",
        modification_number="P00003",
        description="ADMINISTRATIVE MODIFICATION",
        amount=Decimal("-1000"),
        source="mirror",
    )
    published = txn_to_row(txn)
    assert published.detected_by == "action_code"
    assert published.sources == "mirror"
    assert published.override_status == ""
    assert published.transaction_amount == Decimal("-1000")
    assert published.transaction_description == "ADMINISTRATIVE MODIFICATION"


def test_same_award_in_both_parts_merges_with_the_mirror_winning():
    key = "CONT_AWD_80NSSC25C0001"
    api_row = row(key, source="api", recipient_name="ACME (STALE)", modification_number="P00002")
    mirror_row = row(key, source="mirror", recipient_name="ACME AEROSPACE INC")

    (merged,) = merge([api_row], [mirror_row])
    assert merged.sources == "api;mirror"
    # Mirror wins every shared field: it is the authoritative published record.
    assert merged.recipient_name == "ACME AEROSPACE INC"
    assert merged.modification_number == "P00003"


def test_same_bare_id_in_two_id_spaces_stays_two_rows():
    # A PIID and a FAIN can be the same string on unrelated awards; the
    # generated id is what tells them apart.
    contract = row("CONT_AWD_80NSSC25C0001", source="api", award_id="80NSSC25C0001")
    grant = row(
        "ASST_NON_80NSSC25C0001",
        source="mirror",
        award_id="80NSSC25C0001",
        award_type="grant",
        action_type="",
    )
    merged = merge([contract], [grant])
    assert len(merged) == 2
    assert {r.sources for r in merged} == {"api", "mirror"}


def test_piid_keyed_row_collapses_into_the_generated_id_row():
    # Some IDV transactions carry no generated id, so a door falls back to the
    # namespaced PIID. The other door saw the same award with its real key.
    fallback = row("PIID:80NSSC25C0001", source="api", award_id="80NSSC25C0001", award_type="idv")
    generated = row(
        "CONT_IDV_80NSSC25C0001", source="mirror", award_id="80NSSC25C0001", award_type="idv"
    )
    (merged,) = merge([fallback], [generated])
    assert merged.award_key == "CONT_IDV_80NSSC25C0001"
    assert merged.sources == "api;mirror"


def test_collapsing_keeps_the_mirror_fields_when_the_mirror_is_the_fallback_row():
    # The collapse must not invert merge()'s field precedence. Here it is the
    # MIRROR that fell back to the PIID key, so the surviving row takes the
    # generated-id row's identity but the mirror's transaction fields.
    fallback = row(
        "PIID:80NSSC25C0001",
        source="mirror",
        award_id="80NSSC25C0001",
        award_type="idv",
        recipient_name="ACME AEROSPACE INC",
        modification_number="P00003",
    )
    generated = row(
        "CONT_IDV_80NSSC25C0001",
        source="api",
        award_id="80NSSC25C0001",
        award_type="idv",
        recipient_name="ACME (STALE)",
        modification_number="P00002",
    )
    (merged,) = merge([generated], [fallback])
    assert merged.award_key == "CONT_IDV_80NSSC25C0001"
    assert merged.generated_award_id == "CONT_IDV_80NSSC25C0001"
    assert merged.sources == "api;mirror"
    assert merged.recipient_name == "ACME AEROSPACE INC"
    assert merged.modification_number == "P00003"


def test_collapsing_keeps_the_mirror_fields_when_the_api_is_the_fallback_row():
    # The other direction: the mirror row already holds the generated key, so
    # it is the row that survives outright.
    fallback = row(
        "PIID:80NSSC25C0001",
        source="api",
        award_id="80NSSC25C0001",
        award_type="idv",
        recipient_name="ACME (STALE)",
        modification_number="P00002",
    )
    generated = row(
        "CONT_IDV_80NSSC25C0001",
        source="mirror",
        award_id="80NSSC25C0001",
        award_type="idv",
        recipient_name="ACME AEROSPACE INC",
    )
    (merged,) = merge([fallback], [generated])
    assert merged.award_key == "CONT_IDV_80NSSC25C0001"
    assert merged.sources == "api;mirror"
    assert merged.recipient_name == "ACME AEROSPACE INC"
    assert merged.modification_number == "P00003"


def test_piid_keyed_row_does_not_collapse_across_award_types():
    fallback = row("PIID:80NSSC25C0001", source="api", award_id="80NSSC25C0001", award_type="idv")
    grant = row(
        "ASST_NON_80NSSC25C0001", source="mirror", award_id="80NSSC25C0001", award_type="grant"
    )
    merged = merge([fallback], [grant])
    assert len(merged) == 2
    assert {r.award_key for r in merged} == {"PIID:80NSSC25C0001", "ASST_NON_80NSSC25C0001"}


def test_a_piid_only_award_survives_on_its_own_key():
    fallback = row(
        "PIID:80NSSC25C0001", source="mirror", award_id="80NSSC25C0001", award_type="idv"
    )
    (merged,) = merge([], [fallback])
    assert merged.award_key == "PIID:80NSSC25C0001"
    assert merged.sources == "mirror"


def test_an_empty_part_passes_the_other_through():
    api_row = row("CONT_AWD_A", source="api")
    mirror_row = row("CONT_AWD_B", source="mirror")
    assert merge([api_row], []) == [api_row]
    assert merge([], [mirror_row]) == [mirror_row]
    assert merge([], []) == []


def test_ordering_is_newest_first_then_award_key():
    rows = [
        row("CONT_AWD_B", source="api", day="2025-06-01"),
        row("CONT_AWD_A", source="api", day="2025-06-01"),
        row("CONT_AWD_C", source="api", day="2026-01-15"),
        row("CONT_AWD_D", source="api", day="2025-02-01"),
    ]
    keys = [r.award_key for r in merge(rows, [])]
    assert keys == ["CONT_AWD_C", "CONT_AWD_A", "CONT_AWD_B", "CONT_AWD_D"]
    # Same rows in any input order produce the same file.
    assert [r.award_key for r in merge(list(reversed(rows)), [])] == keys
