"""LPG target layout: index specifications and traversal templates (P13).

Collapsing every node type into one collection and every edge type into one
edge collection costs you the two things the ``pg`` layout gets for free:

1. **Finding entry points.** "All Accounts" was a collection scan of exactly the
   Accounts; now it is a scan of *every* node. Fixed by an index on the label
   array (:func:`vertex_indexes`).
2. **Filtering traversals.** An outbound step used to be scoped by which edge
   collection you walked; now every edge type shares one collection, so the
   engine must load each incident edge and discard the ones that do not match.
   Fixed by **vertex-centric indexes** (:func:`edge_indexes`) — persistent
   indexes led by ``_from`` / ``_to`` and continuing into the edge's ``type``
   and the *copied* endpoint labels. Because those labels live on the edge
   document (see :class:`~r2g.types.LpgLayout`), the filter is answerable from
   the index; if they lived only on the neighbour node, an index on the edge
   collection could never reach them.

The templates in :func:`traversal_templates` are written to *match* those
indexes — same fields, equality-first — so the optimizer can actually use them.
Whether it does is not a matter of belief: assert it with
:func:`uses_vertex_centric_index` against a real ``explain``.
"""

from __future__ import annotations

from typing import Any, Dict, List

from r2g.types import LpgLayout

#: Index names are stable so re-provisioning is idempotent and an operator can
#: recognise them in ``db._collection(...).getIndexes()``.
VERTEX_LABEL_INDEX = "r2g_lpg_node_labels"
EDGE_OUTBOUND_INDEX = "r2g_lpg_vci_outbound"
EDGE_INBOUND_INDEX = "r2g_lpg_vci_inbound"


def vertex_indexes(lpg: LpgLayout) -> List[Dict[str, Any]]:
    """Index specs for the single node collection.

    One persistent index over the expanded label array, so ``FILTER 'Account'
    IN doc.labels`` resolves by index instead of scanning every node of every
    type. This is the traversal *entry point*; without it the fast traversal
    below is reached only after a full collection scan.
    """
    return [
        {
            "type": "persistent",
            "fields": [f"{lpg.label_field}[*]"],
            "name": VERTEX_LABEL_INDEX,
            "sparse": False,
            "unique": False,
        }
    ]


def edge_indexes(lpg: LpgLayout) -> List[Dict[str, Any]]:
    """Vertex-centric index specs for the single edge collection.

    Two indexes, one per direction. Each leads with the endpoint the traversal
    looks up by, then the edge ``type``, then the *other* end's copied labels:

    - outbound: ``[_from, type, toLabels[*]]`` — "edges out of this node, of
      this type, landing on that kind of node"
    - inbound:  ``[_to, type, fromLabels[*]]`` — the mirror image

    Ordering matters. ``_from``/``_to`` must lead because that is the lookup the
    traversal performs; ``type`` precedes the labels because it is always an
    equality match, which keeps the label condition usable as the last field.
    Each spec expands exactly one array — a persistent index may not expand two.

    A query filtering on only ``_from`` and ``type`` still uses the outbound
    index (a usable prefix), so the two specs cover the unlabelled cases too.
    """
    return [
        {
            "type": "persistent",
            "fields": ["_from", lpg.type_field, f"{lpg.to_labels_field}[*]"],
            "name": EDGE_OUTBOUND_INDEX,
            "sparse": False,
            "unique": False,
        },
        {
            "type": "persistent",
            "fields": ["_to", lpg.type_field, f"{lpg.from_labels_field}[*]"],
            "name": EDGE_INBOUND_INDEX,
            "sparse": False,
            "unique": False,
        },
    ]


def graph_edge_definition(lpg: LpgLayout) -> Dict[str, Any]:
    """The single named-graph edge definition for the LPG layout.

    One edge collection, and both endpoints are the one node collection — so
    the graph permits any type to connect to any type, exactly as an LPG
    should. Type constraints move from the schema to the ``type``/``labels``
    fields, which is what the indexes above police.
    """
    return {
        "edge_collection": lpg.edge_collection,
        "from_vertex_collections": [lpg.node_collection],
        "to_vertex_collections": [lpg.node_collection],
    }


def traversal_templates(lpg: LpgLayout) -> Dict[str, str]:
    """Named AQL templates whose filters line up with :func:`edge_indexes`.

    Every template filters the edge on ``type`` and on the far endpoint's
    copied label, which is precisely what the vertex-centric indexes cover.

    Two deliberate choices:

    - The label test is written ``@label IN e.<toLabels>`` rather than
      ``e.<toLabels> ALL == @label``. Over a single-element array ``ALL`` is
      trivially true, so the ``ALL`` form silently matches everything — a
      filter that looks correct and restricts nothing.
    - Each traversal names its vertex collection in a ``WITH`` clause. On a
      single server it is optional; on a **cluster** its absence fails at
      execution (error 1521), so a query verified locally would break in
      production. In this layout there is only ever one collection to name.
    """
    node = lpg.node_collection
    edge_c = lpg.edge_collection
    type_f = lpg.type_field
    to_l = lpg.to_labels_field
    from_l = lpg.from_labels_field
    label_f = lpg.label_field

    return {
        # Entry point — uses the vertex label index.
        "nodes_by_label": (
            f"FOR n IN {node}\n"
            f"  FILTER @label IN n.{label_f}\n"
            f"  LIMIT @limit\n"
            f"  RETURN n"
        ),
        "outbound_by_type": (
            f"WITH {node}\n"
            f"FOR v, e IN 1..@depth OUTBOUND @start {edge_c}\n"
            f"  FILTER e.{type_f} == @type\n"
            f"  FILTER @toLabel IN e.{to_l}\n"
            f"  RETURN {{vertex: v, edge: e}}"
        ),
        "inbound_by_type": (
            f"WITH {node}\n"
            f"FOR v, e IN 1..@depth INBOUND @start {edge_c}\n"
            f"  FILTER e.{type_f} == @type\n"
            f"  FILTER @fromLabel IN e.{from_l}\n"
            f"  RETURN {{vertex: v, edge: e}}"
        ),
        # One hop, no depth binding — the shape most likely to be index-checked.
        "neighbors_by_type": (
            f"WITH {node}\n"
            f"FOR v, e IN 1..1 OUTBOUND @start {edge_c}\n"
            f"  FILTER e.{type_f} == @type\n"
            f"  FILTER @toLabel IN e.{to_l}\n"
            f"  RETURN v"
        ),
    }


def indexes_used(explain_result: Dict[str, Any]) -> List[str]:
    """Index names an ``explain`` plan actually engages.

    Two shapes have to be handled, both learned by running this against a live
    3.12 server rather than by reading a type stub:

    - ``python-arango``'s ``explain()`` returns the plan **directly** — there is
      no ``{"plan": ...}`` wrapper to unwrap.
    - an ``IndexNode`` carries ``indexes`` as a **list**, while a
      ``TraversalNode`` carries it as a **dict** (``{"base": [...]}``). Treating
      the dict as a list iterates its keys and silently finds nothing.
    """
    names: List[str] = []
    plan = explain_result.get("plan", explain_result) or {}
    for node in plan.get("nodes", []) or []:
        raw = node.get("indexes")
        entries = raw.get("base", []) if isinstance(raw, dict) else (raw or [])
        for index in entries:
            name = index.get("name")
            if name:
                names.append(name)
    return names


def uses_vertex_centric_index(explain_result: Dict[str, Any]) -> bool:
    """True when a plan engages one of the vertex-centric indexes.

    **Measured limitation (ArangoDB 3.12.9).** A graph *traversal* always uses
    the built-in ``edge`` index; it will not select a vertex-centric index, and
    neither ``indexHint`` nor ``forceIndexHint`` overrides that. A plain
    edge-collection query *can* use one, but only when forced — the cost model
    otherwise rates the ``edge`` index cheaper. So this returns ``True`` for the
    pattern-match access path, not for traversals.

    The copied labels still pay for themselves in traversals for a different
    reason: with ``type`` and the endpoint labels *on the edge*, the filter is
    satisfied from the edge document alone (visible as ``edgeProjections`` in
    the plan), so non-matching neighbours are never materialized.
    """
    return any(
        name in (EDGE_OUTBOUND_INDEX, EDGE_INBOUND_INDEX)
        for name in indexes_used(explain_result)
    )
