from __future__ import annotations

import pytest

from pdfzi.core.errors import PageRangeError
from pdfzi.core.pageranges import PageRange, page_numbers, parse_page_ranges


def test_spec_example():
    r = parse_page_ranges("1-3,7,10-", 12)
    assert r == [PageRange(1, 3), PageRange(7, 7), PageRange(10, 12)]
    assert page_numbers(r) == [1, 2, 3, 7, 10, 11, 12]


def test_whitespace_order_and_duplicates_preserved():
    r = parse_page_ranges(" 5 , 1 - 2 ,5", 6)
    assert page_numbers(r) == [5, 1, 2, 5]


def test_open_range_on_last_page():
    assert page_numbers(parse_page_ranges("4-", 4)) == [4]


def test_range_lengths():
    assert len(PageRange(2, 5)) == 4


@pytest.mark.parametrize(
    ("spec", "fragment"),
    [
        ("", "No pages"),
        ("  ", "No pages"),
        ("1,,2", "Empty item"),
        ("a", "Cannot understand"),
        ("-3", "Cannot understand"),
        ("1-2-3", "Cannot understand"),
        ("0", "start at 1"),
        ("5-2", "backwards"),
        ("13", "out of range"),
        ("10-13", "out of range"),
    ],
)
def test_invalid_specs_give_friendly_errors(spec, fragment):
    with pytest.raises(PageRangeError, match=fragment):
        parse_page_ranges(spec, 12)


def test_out_of_range_message_names_page_and_size():
    with pytest.raises(PageRangeError, match=r"Page 13 .* 12 pages"):
        parse_page_ranges("13", 12)


def test_empty_document():
    with pytest.raises(PageRangeError, match="no pages"):
        parse_page_ranges("1", 0)
