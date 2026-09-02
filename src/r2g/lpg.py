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
indexes — same fields, equality-first — and each carries the **nested traversal
index hint** the optimizer requires (see :func:`traversal_index_hint`). Whether
the index is really used is not a matter of belief: assert it with
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


#: How a set of labels is compared against a node's or edge's label array.
#: These map 1:1 onto Cypher's label expressions, and all three are correct
#: against a **multi-label** array (verified on 3.12.9):
#:
#: - ``has``  — one label present: ``@l IN e.toLabels``            (Cypher ``:A``)
#: - ``all``  — every label present: ``@ls ALL IN e.toLabels``     (Cypher ``:A:B``)
#: - ``any``  — at least one present: ``@ls ANY IN e.toLabels``    (Cypher ``:A|B``)
#: - ``none`` — none present: ``@ls NONE IN e.toLabels``
LABEL_MODES = ("has", "all", "any", "none")


def label_predicate(field: str, *, mode: str = "has", bind: str = "label") -> str:
    """An AQL predicate testing a label array, in the form that stays correct.

    ``field`` is the full accessor (``e.toLabels``, ``n.labels``) and ``bind``
    the bind-parameter name (a scalar for ``has``, a list otherwise).

    **Write the filter against the edge variable ``e``, never against
    ``p.edges[*]``.** For a *list-valued* field the ``[*]`` form expands to a
    **nested** array — ``[["Account","Premium"]]`` — so on 3.12.9:

    - ``p.edges[*].toLabels ALL == @l`` compares each inner *array* to a scalar
      and matches nothing: it returns an **empty result, not an error**.
    - ``@l IN p.edges[*].toLabels`` looks for the scalar among inner arrays and
      likewise returns nothing.
    - ``FLATTEN(p.edges[*].toLabels)`` does work, but only expresses *some hop*
      — it cannot say "every hop", because flattening discards which hop each
      label came from.
    - chained array operators (``ALL ANY ==``) are a **syntax error**.

    A ``FILTER`` on ``e`` inside the traversal avoids all of this: AQL applies
    it per hop (so "every hop satisfies it" falls out naturally), and it is the
    only form the vertex-centric index can serve.
    """
    if mode not in LABEL_MODES:
        raise ValueError(f"label mode must be one of {LABEL_MODES}, got {mode!r}")
    if mode == "has":
        return f"@{bind} IN {field}"
    return f"@{bind} {mode.upper()} IN {field}"


def traversal_index_hint(lpg: LpgLayout, direction: str) -> str:
    """The ``OPTIONS`` clause that points a traversal at its VCI.

    A traversal will **not** pick a vertex-centric index on its own — left
    alone it always takes the built-in ``edge`` index — and the familiar flat
    form (``OPTIONS {indexHint: 'name'}``) is *silently ignored* here: no
    error, no effect, even with ``forceIndexHint``. Traversals need the nested,
    per-collection / per-direction / per-level shape, which mirrors the
    ``{"base": [...], "levels": {...}}`` structure ``explain`` reports back::

        OPTIONS {indexHint: {edges: {outbound: {base: ['r2g_lpg_vci_outbound']}}}}

    Verified on ArangoDB 3.12.9: with this hint the plan names the VCI for both
    directions, including the array-expanded label field.

    The hint is deliberately **not** forced. A missing index then costs
    performance rather than failing the query outright, and the explain-based
    tests are what catch the degradation.
    """
    index = EDGE_OUTBOUND_INDEX if direction == "outbound" else EDGE_INBOUND_INDEX
    return (
        f"OPTIONS {{indexHint: {{{lpg.edge_collection}: "
        f"{{{direction}: {{base: ['{index}']}}}}}}}}"
    )


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
            f"  {traversal_index_hint(lpg, 'outbound')}\n"
            f"  FILTER e.{type_f} == @type\n"
            f"  FILTER @toLabel IN e.{to_l}\n"
            f"  RETURN {{vertex: v, edge: e}}"
        ),
        "inbound_by_type": (
            f"WITH {node}\n"
            f"FOR v, e IN 1..@depth INBOUND @start {edge_c}\n"
            f"  {traversal_index_hint(lpg, 'inbound')}\n"
            f"  FILTER e.{type_f} == @type\n"
            f"  FILTER @fromLabel IN e.{from_l}\n"
            f"  RETURN {{vertex: v, edge: e}}"
        ),
        # One hop, no depth binding — the shape most likely to be index-checked.
        "neighbors_by_type": (
            f"WITH {node}\n"
            f"FOR v, e IN 1..1 OUTBOUND @start {edge_c}\n"
            f"  {traversal_index_hint(lpg, 'outbound')}\n"
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

    Verified on ArangoDB 3.12.9: a traversal carrying the nested hint from
    :func:`traversal_index_hint` reports the VCI in its plan, for both
    directions and including the array-expanded label field. **Without** that
    hint the traversal silently falls back to the built-in ``edge`` index — the
    exact degradation this assertion exists to catch, since it changes nothing
    about the results and everything about the cost.
    """
    return any(
        name in (EDGE_OUTBOUND_INDEX, EDGE_INBOUND_INDEX)
        for name in indexes_used(explain_result)
    )
