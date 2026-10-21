"""Every copy of the singularizer against one list (r2g-arango#30).

r2g, relational-schema-analyzer and arangodb-schema-analyzer each keep their own
singularizer. They used to drift: each was fixed separately and they disagreed
(``warehouses`` -> ``warehous`` in three of them, ``buses`` -> ``buse`` in the
fourth). RSA publishes the list (``naming_known_answers``) and r2g depends on
RSA, so this module is where all the copies meet: r2g's own, the installed
RSA's, and the installed ASA's two (ASA vendors the list; it must match RSA's).
"""

from __future__ import annotations

import importlib
import importlib.util

import pytest
from relational_schema_analyzer.naming import pluralize as rsa_pluralize
from relational_schema_analyzer.naming import singularize as rsa_singularize
from relational_schema_analyzer.naming_known_answers import (
    INVARIANT_WORDS,
    PLURAL_SINGULAR_PAIRS,
    SINGULARIZE_ONLY,
)

from r2g import xsk
from r2g.csi import owl_entity_name
from r2g.forge import table_name
from r2g.naming import pluralize, singularize

ALL_PLURALS = PLURAL_SINGULAR_PAIRS + SINGULARIZE_ONLY


def _asa_singularizers() -> list:
    """ASA's two entry points, or a skip when ASA is not installed at all.

    An installed ASA older than 0.16.0 is a failure, not a skip: the ``test``
    extra requires 0.16.0, so a silent skip would hide the drift this checks."""
    if importlib.util.find_spec("schema_analyzer") is None:
        pytest.skip("arangodb-schema-analyzer is not installed")
    try:
        csi_naming = importlib.import_module("schema_analyzer.csi.naming")
        importlib.import_module("schema_analyzer.csi.naming_known_answers")
    except ImportError as exc:  # pragma: no cover - only on an old ASA
        pytest.fail(f"arangodb-schema-analyzer < 0.16.0 has no shared singularizer: {exc}")
    utils = importlib.import_module("schema_analyzer.utils")
    return [csi_naming.singularize, utils.singularize]


def _spellings(word: str) -> list[tuple[str, str]]:
    return [
        (word, "lower"),
        (word.upper(), "upper"),
        (word[:1].upper() + word[1:], "capitalized"),
        (f"order_{word}", "compound"),
        (f"ORDER_{word.upper()}", "upper compound"),
    ]


def _respell(answer: str, case: str) -> str:
    return {
        "lower": answer,
        "upper": answer.upper(),
        "capitalized": answer[:1].upper() + answer[1:],
        "compound": f"order_{answer}",
        "upper compound": f"ORDER_{answer.upper()}",
    }[case]


@pytest.mark.parametrize(
    "fn", [singularize, rsa_singularize, xsk._singularize], ids=["r2g", "rsa", "r2g.xsk"]
)
@pytest.mark.parametrize(("plural", "singular"), ALL_PLURALS)
def test_singularize_known_plurals(fn, plural, singular):
    for spelling, case in _spellings(plural):
        assert fn(spelling) == _respell(singular, case), spelling


@pytest.mark.parametrize("fn", [pluralize, rsa_pluralize], ids=["r2g", "rsa"])
@pytest.mark.parametrize(("plural", "singular"), PLURAL_SINGULAR_PAIRS)
def test_pluralize_known_singulars(fn, plural, singular):
    for spelling, case in _spellings(singular):
        assert fn(spelling) == _respell(plural, case), spelling


@pytest.mark.parametrize("fn", [singularize, rsa_singularize], ids=["r2g", "rsa"])
@pytest.mark.parametrize(("plural", "singular"), ALL_PLURALS)
def test_a_singular_is_left_alone(fn, plural, singular):
    for spelling, case in _spellings(singular):
        assert fn(spelling) == _respell(singular, case), spelling


@pytest.mark.parametrize("word", INVARIANT_WORDS)
def test_invariant_words_do_not_change(word):
    for spelling, _ in _spellings(word):
        assert singularize(spelling) == spelling
        assert pluralize(spelling) == spelling


@pytest.mark.parametrize(("plural", "singular"), ALL_PLURALS)
def test_asa_agrees(plural, singular):
    for fn in _asa_singularizers():
        for spelling, case in _spellings(plural) + _spellings(singular):
            assert fn(spelling) == _respell(singular, case), (fn.__module__, spelling)


def test_asa_vendors_the_same_list():
    _asa_singularizers()
    asa = importlib.import_module("schema_analyzer.csi.naming_known_answers")
    assert asa.PLURAL_SINGULAR_PAIRS == PLURAL_SINGULAR_PAIRS
    assert asa.SINGULARIZE_ONLY == SINGULARIZE_ONLY
    assert asa.INVARIANT_WORDS == INVARIANT_WORDS


@pytest.mark.parametrize(("plural", "singular"), PLURAL_SINGULAR_PAIRS)
def test_class_names_round_trip_through_the_forge(plural, singular):
    # The forge refuses a class whose table name does not normalise back to it
    # (PLAN F-2); contextual-data-fabric's Forge vocabulary depends on this.
    entity = singular[:1].upper() + singular[1:]
    assert owl_entity_name(plural) == entity
    assert owl_entity_name(table_name(entity)) == entity
