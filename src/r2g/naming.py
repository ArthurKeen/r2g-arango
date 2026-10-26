"""Identifier naming conventions for generated graph names.

R2G passes source table and column names through verbatim by default. This
module provides a small, dependency-free engine to re-case those identifiers
into a chosen convention (``snake_case``, ``camelCase``, ``PascalCase``) and to
apply a :class:`~r2g.types.NamingConvention` across a whole
:class:`~r2g.types.MappingConfig`.

Why materialize instead of transform-on-read? Keeping the converted names
directly in ``target_collection`` / ``field_mappings`` / ``edge_collection``
means the UI and downstream pipeline see exactly what will be written, and the
user can still hand-tweak any individual name afterwards.

Edge endpoints (``_from`` / ``_to``) are resolved from the *target* collection
names by the transformer layer, so renaming a collection here does not break
edge references as long as :class:`~r2g.types.EdgeDefinition.from_collection`
(a source-table key) is left untouched.
"""

from __future__ import annotations

import re

from r2g.types import (
    RESERVED_ATTRIBUTES,
    MappingConfig,
    NameCase,
    NamingConvention,
    Schema,
)

# Split an identifier into words. Handles snake_case, kebab-case, dotted, spaced,
# camelCase, PascalCase and acronym runs (e.g. "HTTPServer" -> ["HTTP", "Server"]).
_WORD_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z0-9]+|[A-Z]+|[0-9]+")


def edge_collection_name(from_table: str, to_table: str) -> str:
    """The edge collection r2g's forward Auto-Map derives for a foreign key.

    One rule, one home. This string was written out longhand in five places —
    ``config.generate_default_config``, ``config_migrate``, ``fk_inference``,
    ``classification`` and the Federation Forge — with nothing linking them,
    while the Forge's whole correctness claim (``introspect(generate(O)) == O``)
    depends on its copy matching Auto-Map's byte for byte. Changing the rule in
    one place used to leave the Forge emitting the old name and every live
    roundtrip failing on a confusing relationship mismatch rather than at the
    line that changed.

    Callers that must disambiguate a collision append their own suffix (Auto-Map
    appends the FK's columns); this returns the undisambiguated base.
    """
    return f"{from_table}_to_{to_table}"


def split_identifier(name: str) -> list[str]:
    """Break ``name`` into normalized word tokens (order preserved)."""
    words: list[str] = []
    for part in re.split(r"[^A-Za-z0-9]+", name):
        if part:
            words.extend(_WORD_RE.findall(part))
    return [w for w in words if w]


#: Same word in both forms; neither :func:`pluralize` nor :func:`singularize`
#: changes it.
_INVARIANT = frozenset(
    {"data", "metadata", "media", "news", "series", "species", "gps", "sms", "dns", "analytics"}
)

#: Singular words that already end in ``s``. Their plural adds ``es``, except
#: ``-is`` words, which go to ``-es`` (``analysis`` -> ``analyses``).
_SINGULAR_ENDING_IN_S = frozenset(
    {
        "status", "bus", "campus", "virus", "alias", "bonus", "census", "corpus",
        "lens", "gas", "canvas", "atlas", "chassis",
        "analysis", "diagnosis", "crisis", "thesis", "hypothesis", "synopsis",
        "synthesis", "parenthesis", "emphasis",
    }
)

#: Irregular plural -> singular. :func:`pluralize` uses the reverse.
_IRREGULAR = {
    "people": "person",
    "children": "child",
    "men": "man",
    "women": "woman",
    "heroes": "hero",
    "potatoes": "potato",
    "tomatoes": "tomato",
    "echoes": "echo",
    "quizzes": "quiz",
    "criteria": "criterion",
    "phenomena": "phenomenon",
    "matrices": "matrix",
    "oxen": "ox",
    # Data-modelling usage, not the dictionary's: a table of customer demographics
    # holds rows that are each one demographic (Northwind models CustomerDemographic).
    "demographics": "demographic",
    "vertices": "vertex",
}
#: Plural -> singular for :func:`singularize` only (``index`` pluralizes to ``indexes``).
_SINGULARIZE_ONLY = {"indices": "index"}
_IRREGULAR_PLURAL = {v: k for k, v in _IRREGULAR.items()}

#: ``-ie`` singulars, whose plural ``-ies`` must not become ``-y``.
_IE_SINGULARS = frozenset(
    {
        "movie", "cookie", "calorie", "zombie", "rookie", "selfie", "hoodie", "pie", "tie",
        "lie", "brownie", "genie", "smoothie", "collie", "magpie", "birdie", "prairie",
        "reverie", "sortie", "lingerie",
    }
)
#: ``-che`` singulars, whose plural ``-ches`` must not lose its ``e``.
_CHE_SINGULARS = frozenset({"cache", "niche", "avalanche", "headache", "moustache", "cliche"})


def _same_case(target: str, like: str) -> str:
    """Spell ``target`` in the case of ``like`` (UPPER, Capitalized or lower)."""
    if like.isupper():
        return target.upper()
    if like[:1].isupper():
        return target[:1].upper() + target[1:]
    return target


def _split_tail(word: str) -> tuple[str, str]:
    """``(head, tail)`` where ``tail`` is the last word of the identifier, so a
    compound (``ORDER_STATUS``, ``usage_metrics``) inflects only its last word."""
    words = split_identifier(word)
    if words and word.endswith(words[-1]):
        tail = words[-1]
        return word[: len(word) - len(tail)], tail
    return "", word


def _singular_by_rules(word: str) -> str:
    low = word.lower()
    if low in _INVARIANT or low in _SINGULAR_ENDING_IN_S:
        return word
    irregular = _IRREGULAR.get(low) or _SINGULARIZE_ONLY.get(low)
    if irregular is not None:
        return _same_case(irregular, word)
    if low.endswith("ies") and len(word) > 3:
        if low[:-1] in _IE_SINGULARS:
            return word[:-1]
        return word[:-3] + ("Y" if word[-1].isupper() else "y")
    if low.endswith("es") and len(word) > 3:
        stem = low[:-2]
        if stem + "is" in _SINGULAR_ENDING_IN_S:  # analyses -> analysis
            return word[:-2] + ("IS" if word[-1].isupper() else "is")
        if stem in _SINGULAR_ENDING_IN_S or stem.endswith(("ss", "x", "sh", "zz")):
            return word[:-2]
        if stem.endswith("ch") and stem + "e" not in _CHE_SINGULARS:
            return word[:-2]
        # Otherwise the singular ends in e (warehouses, sizes, caches): drop only the s.
    if low.endswith("s") and not low.endswith("ss") and len(word) > 1:
        return word[:-1]
    return word


def _plural_by_rules(word: str) -> str:
    low = word.lower()
    upper = word[-1].isupper()
    if low in _INVARIANT:
        return word
    irregular = _IRREGULAR_PLURAL.get(low)
    if irregular is not None:
        return _same_case(irregular, word)
    if low in _SINGULAR_ENDING_IN_S and low.endswith("is"):
        return word[:-2] + ("ES" if upper else "es")
    if low.endswith("y") and len(word) > 1 and low[-2] not in "aeiou":
        return word[:-1] + ("IES" if upper else "ies")
    if low.endswith(("s", "x", "z", "ch", "sh")):
        return word + ("ES" if upper else "es")
    return word + ("S" if upper else "s")


def noun_lexicon():
    """The dictionary layer: relational-schema-analyzer's vendored noun lexicon.

    r2g depends on RSA, so it uses RSA's table rather than keeping a third copy;
    ``tests/test_naming_known_answers.py`` checks that arangodb-schema-analyzer's
    vendored copy has the same SHA-256.
    """
    from relational_schema_analyzer.naming import noun_lexicon as _rsa_noun_lexicon

    return _rsa_noun_lexicon()


def _singular_word(word: str) -> str:
    """Named lists, then the dictionary, then the rules (r2g-arango#30)."""
    low = word.lower()
    if (
        low in _INVARIANT
        or low in _SINGULAR_ENDING_IN_S
        or low in _IRREGULAR
        or low in _SINGULARIZE_ONLY
        or (low.endswith("ies") and low[:-1] in _IE_SINGULARS)
    ):
        return _singular_by_rules(word)
    lexicon = noun_lexicon()
    readings = lexicon.plural_to_singular.get(low)
    if readings:
        return _same_case(readings[0], word)
    if low in lexicon.nouns:  # a noun in its own right: cactus, logistics
        return word
    return _singular_by_rules(word)


def _plural_word(word: str) -> str:
    """Named lists, then the dictionary, then the rules; the inverse of :func:`_singular_word`."""
    low = word.lower()
    if low in _INVARIANT or low in _IRREGULAR_PLURAL or low in _SINGULAR_ENDING_IN_S:
        return _plural_by_rules(word)
    plurals = noun_lexicon().singular_to_plural.get(low)
    if plurals:
        return _same_case(plurals[0], word)
    return _plural_by_rules(word)


def pluralize(word: str) -> str:
    """Best-effort English plural for table / relationship name heuristics.

    ``y`` after a consonant -> ``ies``; sibilant endings (``s``/``x``/``z``/
    ``ch``/``sh``) -> ``es``; ``-is`` singulars -> ``-es``; a short irregular
    table (``person`` -> ``people``); words that do not change (``data``,
    ``series``); else append ``s``. Only the last word of a compound is
    inflected. Matching is case-insensitive and added letters follow the case
    of the word's last letter -- CC-12 naming must not depend on the physical
    case of a source's identifiers.

    The same rules as relational-schema-analyzer's copy; both are tested
    against RSA's ``naming_known_answers`` list (r2g-arango#30).
    """
    if not word:
        return word
    head, tail = _split_tail(word)
    return head + _plural_word(tail)


def singularize(word: str) -> str:
    """Best-effort English singular, the inverse of :func:`pluralize`.

    ``ies`` -> ``y`` (but ``movies`` -> ``movie``); ``-es`` is dropped only
    after a sibilant (``addresses``, ``boxes``, ``batches``) or a singular that
    already ends in ``s`` (``statuses`` -> ``status``); any other ``-es`` keeps
    its ``e`` (``warehouses`` -> ``warehouse``, ``sizes`` -> ``size``); a
    trailing ``s`` (not ``ss``) is dropped; singular words that end in ``s``
    (``status``, ``analysis``), invariant words (``news``, ``series``) and a
    short irregular table (``people`` -> ``person``) are handled by name. Only
    the last word of a compound is inflected (``ORDER_STATUS`` stays put).

    Matching is case-insensitive and the replacement follows the case of the
    word, so Snowflake-style uppercase identifiers produce the same conceptual
    class as their lowercase spelling (ArthurKeen/r2g-arango#4). The rules are
    shared with relational-schema-analyzer and arangodb-schema-analyzer, and
    ``tests/test_naming_known_answers.py`` checks all three copies against RSA's
    ``naming_known_answers`` list (r2g-arango#30).
    """
    if not word:
        return word
    head, tail = _split_tail(word)
    return head + _singular_word(tail)


def convert_identifier(name: str, style: NameCase) -> str:
    """Re-case ``name`` into ``style``.

    Returns ``name`` unchanged when ``style`` is ``"preserve"`` or when the
    identifier yields no word tokens (e.g. it is empty or all punctuation).
    """
    if style == "preserve" or not name:
        return name
    words = split_identifier(name)
    if not words:
        return name
    lower = [w.lower() for w in words]
    if style == "snake":
        return "_".join(lower)
    if style == "pascal":
        return "".join(w.capitalize() for w in lower)
    if style == "camel":
        return lower[0] + "".join(w.capitalize() for w in lower[1:])
    return name


def apply_naming_convention(
    config: MappingConfig,
    convention: NamingConvention,
    schema: Schema | None = None,
) -> MappingConfig:
    """Return a copy of ``config`` with ``convention`` materialized into names.

    - **Collections**: ``target_collection`` is re-cased.
    - **Properties**: every source column gets a ``field_mappings`` entry mapping
      it to its re-cased property name (when a ``schema`` is provided so columns
      are known). Existing manual renames are preserved. ``field_expressions``
      targets are re-cased too. System fields (``_key`` etc.) are never touched.
    - **Edges**: ``edge_collection`` is re-cased. ``from_collection`` /
      ``to_collection`` are left as source-table keys (the transformer resolves
      edge endpoints from the re-cased ``target_collection``).
    """
    new = config.model_copy(deep=True)

    for cm in new.collections.values():
        if convention.collections != "preserve":
            cm.target_collection = convert_identifier(cm.target_collection, convention.collections)

        if convention.properties != "preserve":
            if schema is not None:
                table = schema.tables.get(cm.source_table)
                if table is not None:
                    for col in table.columns:
                        cname = col.name
                        if cname in RESERVED_ATTRIBUTES or cname in cm.field_mappings:
                            continue
                        converted = convert_identifier(cname, convention.properties)
                        # Never rename to a reserved attribute, and skip no-ops.
                        if converted != cname and converted not in RESERVED_ATTRIBUTES:
                            cm.field_mappings[cname] = converted
            else:
                # No schema available: re-case the targets of explicit renames only.
                cm.field_mappings = {
                    src: convert_identifier(tgt, convention.properties)
                    for src, tgt in cm.field_mappings.items()
                    if convert_identifier(tgt, convention.properties) not in RESERVED_ATTRIBUTES
                }
            for fx in cm.field_expressions:
                if fx.target not in RESERVED_ATTRIBUTES:
                    fx.target = convert_identifier(fx.target, convention.properties)

    if convention.edges != "preserve":
        for edge in new.edges:
            edge.edge_collection = convert_identifier(edge.edge_collection, convention.edges)

    new.naming_convention = convention
    return new
