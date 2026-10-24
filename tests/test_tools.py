"""Client helpers: they must normalise exactly like the contract and reject what it rejects."""

import re

import pytest

from scripts import palimpsest_tools as tools

AUTHOR = "0x" + "ab" * 20
GOOD = dict(title="A title", abstract="a" * 50, elements=["first element text", "second element text"])


def test_clean_collapses_whitespace_and_nul():
    assert tools.clean("  a \t b\n\nc\x00d ") == "a b c d"


def test_normalize_elements_accepts_text_or_lists_and_drops_blanks():
    assert tools.normalize_elements("one one one one\r\n\r\n two two two two \n") == [
        "one one one one", "two two two two"]
    assert tools.normalize_elements(["  x  y ", "", "z"]) == ["x y", "z"]


def test_commitment_is_stable_and_normalisation_insensitive():
    salt = "s" * 20
    first = tools.commitment(AUTHOR, **GOOD, salt=salt)
    assert re.fullmatch(r"0x[0-9a-f]{64}", first)
    again = tools.commitment(AUTHOR.upper().replace("0X", "0x"), "  A   title ", GOOD["abstract"],
                             "first element text\nsecond  element text", salt)
    assert first == again


@pytest.mark.parametrize("change", [
    {"title": "Another"}, {"abstract": "b" * 50}, {"elements": ["first element text", "third element text"]},
    {"salt": "t" * 20},
])
def test_every_field_changes_the_commitment(change):
    base = dict(GOOD, salt="s" * 20)
    assert tools.commitment(AUTHOR, **base) != tools.commitment(AUTHOR, **dict(base, **change))


def test_bad_salts_are_refused():
    for salt in ("short", "x" * 65, "has space has space", "bad/slash/bad/slash"):
        with pytest.raises(ValueError):
            tools.commitment(AUTHOR, **GOOD, salt=salt)


def test_new_salt_is_valid_and_unique():
    salts = {tools.new_salt() for _ in range(50)}
    assert len(salts) == 50
    assert all(tools.SALT_PATTERN.fullmatch(salt) for salt in salts)


def test_validate_claim_reports_every_problem():
    assert tools.validate_claim(**GOOD) == []
    problems = tools.validate_claim("", "short", ["x"])
    assert len(problems) >= 3
    assert any("hidden" in p for p in tools.validate_claim("t\u200b", "a" * 50, GOOD["elements"]))
    assert any("distinct" in p for p in tools.validate_claim("t", "a" * 50, ["same same same", "SAME same  same"]))


def test_needed_disclosures_matches_the_documented_examples():
    assert [tools.needed_disclosures(n, 60) for n in (2, 3, 4, 5, 8)] == [2, 2, 3, 3, 5]
    assert tools.needed_disclosures(5, 100) == 5
    assert tools.needed_disclosures(2, 50) == 1
