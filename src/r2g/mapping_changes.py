"""What a change to a mapping touches, and the class that governs each part (P14.9, P14.10).

Under a control plane every change is checked against the policy on an ontology class:

* a table on its own class; a column on its table's class; a relationship on the class of
  the table it starts from. A property term on a column or relationship records meaning and
  does not change the check;
* changing a table's class is checked on the old class and the new one;
* a table that names no class, and every mapping-wide setting, on the project-wide rules
  (a check with no term).

A save is compared with the saved mapping **in full**: every field of every table and
relationship, and every mapping-wide setting, through the model's own serialised form, so a
field added to the model later is compared without anyone listing it here. The reload diff
(:mod:`r2g.mapping_diff`) plans migrations and is not used for this: it does not see field
expressions, extra labels, collection type, join tables, shared keys, the naming convention
or the graph layout.

Loads and migrations are checked on what they write: each table whose rows they write, on its
class, and each relationship, on its start table's class.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from r2g.types import CollectionMapping, EdgeDefinition, MappingConfig

#: The term of a check that falls under the project-wide rules.
PROJECT_WIDE = None


@dataclass(frozen=True)
class Item:
    """One part of a mapping that a change touches."""

    #: Where in the mapping: ``collections/<key>``, ``edges/<edge>(<from> -> <to>)`` or
    #: ``mapping/<setting>``.
    where: str
    #: The classes it is checked on, in order; None is the project-wide rules.
    terms: tuple[str | None, ...]
    #: What differs, for the reason shown and the record: field names, or ``added`` and
    #: ``removed``; for a load or migration, what it does there.
    what: tuple[str, ...]


def _dump(model: Any) -> Any:
    return model.model_dump(mode="json")


def _differing(old: dict[str, Any] | None, new: dict[str, Any] | None) -> tuple[str, ...]:
    if old is None:
        return ("added",)
    if new is None:
        return ("removed",)
    return tuple(sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k)))


def _unique(terms: Iterable[str | None]) -> tuple[str | None, ...]:
    return tuple(dict.fromkeys(terms))


def edge_where(edge: EdgeDefinition) -> str:
    """A relationship's full identity: two relationships may share an edge collection."""
    return (f"edges/{edge.edge_collection}({edge.from_collection}."
            f"{','.join(edge.from_fields)} -> {edge.to_collection}."
            f"{','.join(edge.to_fields)})")


def start_classes(config: MappingConfig, table: str) -> tuple[str | None, ...]:
    """The classes of the table a relationship starts from. ``from_collection`` names a
    source table; a mapping may map it more than once, and then every one counts."""
    found = [c for c in config.collections.values() if c.source_table == table]
    if not found and table in config.collections:
        found = [config.collections[table]]
    return _unique(c.term for c in found) if found else (PROJECT_WIDE,)


def save_changes(saved: MappingConfig | None, new: MappingConfig) -> list[Item]:
    """Every difference between ``saved`` (None: nothing saved yet) and ``new``."""
    old = saved if saved is not None else MappingConfig()
    items: list[Item] = []

    old_d, new_d = _dump(old), _dump(new)
    for key in sorted((set(old_d) | set(new_d)) - {"collections", "edges"}):
        if old_d.get(key) != new_d.get(key):
            items.append(Item(f"mapping/{key}", (PROJECT_WIDE,), (key,)))

    for key in sorted(set(old.collections) | set(new.collections)):
        o: CollectionMapping | None = old.collections.get(key)
        n: CollectionMapping | None = new.collections.get(key)
        od = None if o is None else _dump(o)
        nd = None if n is None else _dump(n)
        if od == nd:
            continue
        terms = _unique([*([o.term] if o is not None else []), *([n.term] if n is not None else [])])
        items.append(Item(f"collections/{key}", terms, _differing(od, nd)))

    def by_identity(config: MappingConfig) -> dict[str, list[EdgeDefinition]]:
        out: dict[str, list[EdgeDefinition]] = {}
        for edge in config.edges:
            out.setdefault(edge_where(edge), []).append(edge)
        return out

    old_e, new_e = by_identity(old), by_identity(new)
    for where in sorted(set(old_e) | set(new_e)):
        before = sorted(json.dumps(_dump(e), sort_keys=True) for e in old_e.get(where, []))
        after = sorted(json.dumps(_dump(e), sort_keys=True) for e in new_e.get(where, []))
        if before == after:
            continue
        terms = _unique([*(t for e in old_e.get(where, []) for t in start_classes(old, e.from_collection)),
                         *(t for e in new_e.get(where, []) for t in start_classes(new, e.from_collection))])
        if len(before) == 1 and len(after) == 1:
            what = _differing(json.loads(before[0]), json.loads(after[0]))
        else:
            what = _differing(json.loads(before[0]) if before else None,
                              json.loads(after[0]) if after else None)
        items.append(Item(where, terms, what))
    return items


def load_items(config: MappingConfig, include: Iterable[str] | None = None,
               exclude: Iterable[str] | None = None) -> list[Item]:
    """What a load writes: each table it loads, and each relationship starting from one,
    filtered as the load filters them (by source table)."""
    inc = None if include is None else set(include)
    exc = set(exclude or ())

    def loads(table: str) -> bool:
        return (inc is None or table in inc) and table not in exc

    items = [Item(f"collections/{key}", (c.term,), ("load",))
             for key, c in sorted(config.collections.items()) if loads(c.source_table)]
    items += [Item(edge_where(e), start_classes(config, e.from_collection), ("load",))
              for e in config.edges if loads(e.from_collection)]
    return items


def written_by(name: str, *configs: MappingConfig) -> tuple[str | None, ...]:
    """The classes of what a database collection holds, in any of ``configs``: tables
    loaded into it, relationships stored in it, or, in the labelled-property-graph layout,
    everything. Project-wide when nothing in the mappings writes it (the named graph)."""
    terms: list[str | None] = []
    for config in configs:
        lpg = config.graph_layout == "lpg"
        for c in config.collections.values():
            if c.target_collection == name or (lpg and name == config.lpg.node_collection):
                terms.append(c.term)
        for e in config.edges:
            if e.edge_collection == name or (lpg and name == config.lpg.edge_collection):
                terms.extend(start_classes(config, e.from_collection))
    return _unique(terms) if terms else (PROJECT_WIDE,)


def migration_items(actions: Iterable[Any], old: MappingConfig, new: MappingConfig) -> list[Item]:
    """What a migration writes: each collection its plan touches, on the classes of what
    the collection holds before or after."""
    by_collection: dict[str, list[str]] = {}
    for action in actions:
        by_collection.setdefault(action.collection or "", []).append(action.action_type)
    return [Item(f"collections/{name}" if name else "graph",
                 written_by(name, old, new) if name else (PROJECT_WIDE,),
                 tuple(dict.fromkeys(kinds)))
            for name, kinds in sorted(by_collection.items())]
