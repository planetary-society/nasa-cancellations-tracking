from nasa_grants_query import TERMINATION_SEARCH_KEYWORDS
from npdv_query import NPDVQuery
from usaspending_terminations_query import SEARCH_KEYWORDS


def test_hyphenated_termination_keyword_is_used_by_all_keyword_sources():
    keyword = "terminate-for-convenience"

    assert keyword in SEARCH_KEYWORDS
    assert keyword in NPDVQuery.DEFAULT_SEARCH_PHRASES
    assert keyword in TERMINATION_SEARCH_KEYWORDS
