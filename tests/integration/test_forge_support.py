"""Tests for the shared ``≡`` helpers in ``forge_support``.

These need no database — they live here because the helper does, and everything
in this directory is auto-marked ``integration`` by conftest. All four dialect
roundtrips route their ADR-0006 D-3 equivalence assertion through these two
functions, so a defect here weakens every roundtrip at once and is invisible in
each of them individually.
"""

from __future__ import annotations

import pytest

from .forge_support import normalized_entities, normalized_relationships


def test_normalized_entities_keeps_distinct_entities():
    got = normalized_entities(
        [
            {"name": "Account", "properties": [{"name": "account_name"}]},
            {"name": "Contact", "properties": [{"name": "FULL_NAME"}]},
        ]
    )
    # physical spellings normalize to the conceptual lowerCamel form
    assert got == {"Account": {"accountName"}, "Contact": {"fullName"}}


def test_normalized_entities_refuses_a_repeated_entity():
    """Regression: a dict comprehension keyed by name kept only the last copy,
    so a model reporting one entity twice compared equal to reporting it once —
    in all four roundtrips simultaneously."""
    with pytest.raises(AssertionError, match="duplicate entity"):
        normalized_entities(
            [
                {"name": "Account", "properties": [{"name": "a"}]},
                {"name": "Account", "properties": [{"name": "b"}]},
            ]
        )


def test_normalized_relationships_refuses_a_repeated_relationship():
    """Same defect in set form: a repeated relationship folded into one."""
    rel = {"type": "contactsToAccounts", "fromEntity": "Contact", "toEntity": "Account"}
    assert len(normalized_relationships([rel])) == 1
    with pytest.raises(AssertionError, match="duplicate relationship"):
        normalized_relationships([rel, dict(rel)])
