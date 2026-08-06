"""One termination vocabulary, three transports that cannot share a list.

`termination_vocabulary.TERM_TEXT` is the classification predicate. The wire
formats cannot hold it: the USAspending API takes literal phrases as
`filters.keywords`, NPDV compiles its own local regex, and NASA Grants filters
workflow-task labels with `str.contains`. So the vocabulary is expressed three
times, and these tests pin the relationship that keeps the three honest.
"""

import pytest

import termination_vocabulary as tv
from nasa_grants_query import TERMINATION_SEARCH_KEYWORDS
from npdv_query import NPDVQuery
from usaspending_terminations_query import SEARCH_KEYWORDS


def test_hyphenated_termination_keyword_is_used_by_all_keyword_sources():
    keyword = "terminate-for-convenience"

    assert keyword in SEARCH_KEYWORDS
    assert keyword in NPDVQuery.DEFAULT_SEARCH_PHRASES
    assert keyword in TERMINATION_SEARCH_KEYWORDS


@pytest.mark.parametrize("keyword", SEARCH_KEYWORDS)
def test_every_api_keyword_is_a_termination_the_classifier_agrees_with(keyword):
    """The containment that holds: keywords are a subset of the predicate.

    A keyword the classifier would reject means the API spends a full
    paginated sweep fetching rows that the pipeline then throws away - and,
    worse, that two parts of the tracker disagree about what the same sentence
    says. This is what makes "one vocabulary, several transports" true rather
    than aspirational.
    """
    assert tv.is_termination(keyword)


def test_the_predicate_deliberately_covers_more_than_the_wire_can_ask_for():
    """The reverse containment does NOT hold, on purpose.

    `con[vn]\\w*` has no finite expansion and `\\b`/`[\\s-]?` have no wire
    equivalent, so the keyword list can never be derived from the regex. It is
    the subset NASA actually writes, because each entry costs a sweep. Pinned
    so the gap reads as a decision rather than an oversight.
    """
    predicate_only = [
        "t4c",
        "termination settlement",
        "notice of termination",
        "termination notice",
        "termination forconvenience",
    ]
    for phrase in predicate_only:
        assert tv.is_termination(phrase)
        assert phrase not in SEARCH_KEYWORDS


def test_nasa_grants_keywords_are_a_different_corpus_entirely():
    """Not FPDS description text: these match NASA workflow-task labels, which
    are a controlled vocabulary written by a different system. Unifying them
    with the other two would be a category error, so this pins them apart
    rather than together.
    """
    assert "change pop end date" in TERMINATION_SEARCH_KEYWORDS
    assert "change pop end date" not in SEARCH_KEYWORDS
    # And a bare stem, which no FPDS-description vocabulary would ever admit -
    # it is safe here only because the label set is controlled.
    assert "terminat" in TERMINATION_SEARCH_KEYWORDS
    assert not tv.is_termination("terminat")
