"""Tests for the custom template filters."""

import pytest
from searchengine.query import ParsedQuery, SearchMode
from searchengine.template_filter import _highlight


@pytest.mark.parametrize(
    ("query", "before", "after"),
    [
        (
            ParsedQuery(["world"], SearchMode.WEB, 1, "", None),
            "Hello World!",
            "Hello <b>World</b>!",
        ),
        (
            ParsedQuery(["middle"], SearchMode.WEB, 1, "", None),
            "inthemiddleofaword!",
            "inthe<b>middle</b>ofaword!",
        ),
        (
            ParsedQuery(["mult"], SearchMode.WEB, 1, "", None),
            "a mult b mult c mult d",
            "a <b>mult</b> b <b>mult</b> c <b>mult</b> d",
        ),
    ],
)
def test_highlight(query: ParsedQuery, before: str, after: str):
    """Test highlighting of query parts."""
    assert _highlight(before, query) == after
