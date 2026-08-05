"""A congressional district is published as a state-district pair or not at all.

USAspending publishes recipient locations carrying a congressional code and no
state, and the ORM's Location.district filters the missing piece out before
joining - so award 80AFRC19F0092 published a bare "45" in a column of pairs.
132 archived snapshots still carry that value and are never rewritten, so the
rule has to hold on read as well as on write.
"""

import build_master_ledger as bml
from utils import as_congressional_district, congressional_district


class FakeLocation:
    """Duck-typed stand-in for the ORM's Location."""

    def __init__(self, state_code=None, congressional_code=None):
        self.state_code = state_code
        self.congressional_code = congressional_code


def test_a_congressional_code_without_a_state_is_not_published():
    """The live shape of 80AFRC19F0092's recipient location."""
    assert congressional_district(FakeLocation(congressional_code="45")) == ""


def test_a_state_without_a_congressional_code_is_not_published():
    """The mirror case: the same join degrades to a bare state."""
    assert congressional_district(FakeLocation(state_code="CA")) == ""


def test_a_complete_pair_is_published():
    assert congressional_district(FakeLocation("CA", "45")) == "CA-45"
    assert congressional_district(FakeLocation("ak", "00")) == "AK-00"


def test_a_single_digit_code_is_padded_rather_than_rejected():
    """USAspending pads today; rejecting an unpadded code would silently drop
    a district we do have both halves of."""
    assert congressional_district(FakeLocation("CA", "5")) == "CA-05"


def test_a_missing_location_is_blank_not_an_error():
    """Foreign recipients legitimately have no district, and the ORM returns
    None for a location it has no data for."""
    assert congressional_district(None) == ""
    assert congressional_district(FakeLocation()) == ""


def test_a_stored_bare_code_is_dropped_on_read():
    """What the 132 archived snapshots deliver."""
    assert as_congressional_district("45") == ""
    assert as_congressional_district("CA") == ""
    assert as_congressional_district("") == ""
    assert as_congressional_district(None) == ""


def test_a_stored_well_formed_pair_survives_read_unchanged():
    assert as_congressional_district("VA-11") == "VA-11"
    assert as_congressional_district("DC-98") == "DC-98"


def test_the_ledger_normalizes_the_column_on_ingest():
    """The registry is what applies the read rule; an empty one would mean the
    archived value flows straight through to the published ledger."""
    assert "Recipient Congressional District" in bml.COLUMN_NORMALIZERS
    normalize = bml.COLUMN_NORMALIZERS["Recipient Congressional District"]
    assert normalize("45") == ""
    assert normalize("VA-11") == "VA-11"
