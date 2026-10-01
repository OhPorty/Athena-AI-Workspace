import main


def test_identical_queries_score_one():
    assert main._search_query_similarity("dropdown collapses randomly", "dropdown collapses randomly") == 1.0


def test_reworded_same_topic_query_scores_high():
    # Modeled on the real incident: near-identical rewordings of the same
    # concept set, which is exactly what evaded the old web_search-only check.
    score = main._search_query_similarity(
        "tool output rendering dropdown collapse overflow height",
        "CSS file tool output dropdown collapse max-height overflow height styling",
    )
    assert score >= 0.7


def test_unrelated_queries_score_low():
    score = main._search_query_similarity(
        "tool output dropdown collapse overflow height", "database connection pool timeout retry logic"
    )
    assert score < 0.7


def test_empty_query_scores_zero():
    assert main._search_query_similarity("", "dropdown collapse") == 0.0
    assert main._search_query_similarity("the a of", "dropdown collapse") == 0.0  # all stopwords


def test_search_codebase_included_in_similarity_checked_tools():
    assert "search_codebase" in main._SIMILARITY_CHECKED_TOOL_NAMES
    assert "web_search" in main._SIMILARITY_CHECKED_TOOL_NAMES
