"""Tests for the keyword matcher."""

from __future__ import annotations

import pytest

from keyword_scrub.matching import Matcher


def test_any_matches_on_a_single_keyword():
    m = Matcher(["cat", "dog"], mode="any")
    r = m.search("I have a dog")
    assert r.matched
    assert r.keywords == ["dog"]


def test_any_reports_all_hits_in_input_order():
    m = Matcher(["dog", "cat"], mode="any")
    r = m.search("cat and dog")
    assert r.keywords == ["dog", "cat"]


def test_all_requires_every_keyword():
    m = Matcher(["cat", "dog"], mode="all")
    assert not m.search("only a dog here").matched
    r = m.search("a cat and a dog")
    assert r.matched
    assert set(r.keywords) == {"cat", "dog"}


def test_phrase_requires_adjacency():
    m = Matcher(["hot", "dog"], mode="phrase")
    assert m.search("a hot dog please").matched
    assert not m.search("the dog is hot").matched


def test_phrase_tolerates_extra_whitespace():
    m = Matcher(["hot", "dog"], mode="phrase")
    assert m.search("hot   dog").matched


def test_word_boundaries_prevent_substring_matches():
    m = Matcher(["cat"], mode="any")
    assert not m.search("category catastrophe").matched
    assert m.search("the cat sat").matched


def test_case_folding_default():
    m = Matcher(["Cat"], mode="any")
    assert m.search("a CAT").matched


def test_case_sensitive_mode():
    m = Matcher(["Cat"], mode="any", case_sensitive=True)
    assert not m.search("a cat").matched
    assert m.search("a Cat").matched


def test_unicode_normalization():
    # Fullwidth characters normalize to ASCII under NFKC.
    m = Matcher(["cat"], mode="any")
    assert m.search("ｃａｔ").matched  # ｃｅｔ -> cat


def test_non_word_boundary_keywords():
    m = Matcher(["c++"], mode="any")
    assert m.search("I love c++ a lot").matched
    assert m.search("c++").matched


def test_cashtag_keyword():
    m = Matcher(["$gme"], mode="any")
    assert m.search("buying $gme today").matched


def test_search_fields_reports_field():
    m = Matcher(["rocket"], mode="any")
    matched, kws, field = m.search_fields(title="rocket launch", body="nothing")
    assert matched and field == "title" and kws == ["rocket"]

    matched, kws, field = m.search_fields(title="nothing", body="a rocket")
    assert field == "body"

    matched, kws, field = m.search_fields(title="rocket", body="rocket")
    assert field == "both"

    matched, kws, field = m.search_fields(title="x", body="y")
    assert not matched and field is None


def test_empty_text_no_match():
    m = Matcher(["cat"], mode="any")
    assert not m.search(None).matched
    assert not m.search("").matched


def test_blank_keywords_rejected():
    with pytest.raises(ValueError):
        Matcher(["  ", ""], mode="any")


def test_bad_mode_rejected():
    with pytest.raises(ValueError):
        Matcher(["cat"], mode="nonsense")


def test_duplicate_keywords_deduped():
    m = Matcher(["cat", "cat"], mode="all")
    # de-duped to a single keyword, so "all" is satisfied by one hit
    assert m.search("cat").matched
