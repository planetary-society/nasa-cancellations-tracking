"""The termination vocabulary matches what NASA actually writes, and nothing else."""

import pytest

from nasatrack.criteria import API_KEYWORDS, is_cause, is_termination

# Spellings observed in the archived corpus of source descriptions.
OBSERVED_MISSPELLINGS = [
    "TERMINATE FOR CONVIENCE OF THE GOVERNMENT",
    "terminate forconvenience",
    "termination for connivence",
    "termination for convicne",
]

MATCHES = [
    "termination for convenience",
    "T4C effective immediately",
    "issue stop-work order",
    "stop work order issued",
    "NOTICE OF TERMINATION",
    "TERMINATION NOTICE ISSUED: IN SPACE PRODUCTION APPLICATIONS",
    "termination settlement proposal",
    "LEGAL CONTRACT CANCELLATION",
]

# "Flight termination system" is the range-safety device that destroys a launch
# vehicle in flight - hardware, not a contract action. Awards 80LARC26F7025 and
# 80NSSC26P0092 were published as cancellations by a broad net that matched
# bare "termination"; this vocabulary lists no such alternative.
NON_MATCHES = [
    "terminated",
    "flight termination system core battery qualification",
    "purchase of a flight termination receiver/decoder",
    "",
]


@pytest.mark.parametrize("text", OBSERVED_MISSPELLINGS + MATCHES)
def test_termination_language_matches(text):
    assert is_termination(text)


@pytest.mark.parametrize("text", NON_MATCHES)
def test_non_termination_language_does_not_match(text):
    assert not is_termination(text)


def test_none_is_not_a_termination():
    assert not is_termination(None)


# Termination for cause or default is contractor failure, excluded by
# methodology rather than counted as a policy cancellation. The verdict side of
# this lives in test_accept_award.test_cause_is_rejected_even_with_an_f_code.
@pytest.mark.parametrize(
    "text",
    [
        "terminated for cause",
        "TERMINATION FOR DEFAULT",
        "terminate for cause per FAR 49.402",
    ],
)
def test_cause_language_is_recognised(text):
    assert is_cause(text)


def test_convenience_language_is_not_cause():
    assert not is_cause("terminate for convenience of the government")


@pytest.mark.parametrize("keyword", API_KEYWORDS)
def test_every_api_keyword_satisfies_is_termination(keyword):
    """The API can never fetch a phrase the classifier would then reject."""
    assert is_termination(keyword)
