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

    Two indexes, one per direction, each leading with the endpoint the traversal
    looks up by and then the edge ``type``:

    - outbound: ``[_from, type]``
    - inbound:  ``[_to, type]``

    **The copied endpoint labels are deliberately NOT index fields.** An
    array-expansion field stores **one index entry per element**, and a
    traversal scans every entry under the ``_from``/``type`` prefix — so an
    index of ``[_from, type, toLabels[*]]`` makes the traversal return each edge
    *once per label on its target*, duplicating paths. Measured on 3.12.11 with
    edges carrying 1/2/3/4 labels: the built-in edge index and this flat spec
    each returned every edge once, while the array spec returned them 1/2/3/4
    times. Adding the label filter does **not** narrow it — the label condition
    is applied after the prefix scan — and ``uniqueEdges: 'global'`` is rejected
    outright (ERR 10), so there is no query-side repair.

    Dropping the array field costs nothing measurable: the flat spec is still
    selected, still serves the pushed-down ``p.edges[*].type ALL == @type``
    condition, and the label filter is evaluated against an edge document the
    traversal has already loaded. The label *copies* still earn their place —
    they let the filter run without materializing the neighbour vertex — they
    just must not be indexed.

    The array expansion remains correct on the **vertex** collection
    (:func:`vertex_indexes`), where an equality lookup matches one entry per
    document and the access path is an ``IndexNode`` rather than a traversal.
    """
    return [
        {
            "type": "persistent",
            "fields": ["_from", lpg.type_field],
            "name": EDGE_OUTBOUND_INDEX,
            "sparse": False,
            "unique": False,
        },
        {
            "type": "persistent",
            "fields": ["_to", lpg.type_field],
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

    Note what is *not* wrong with ``[*]``: it is **not** slow. The optimizer
    pushes ``p.edges[*].f ALL == @v`` into the traverser as a *global edge
    condition* and serves it from the vertex-centric index — measured on
    3.12.9. On a **scalar** field that makes it the right tool (the depth-ranged
    templates use it for ``type``). The problem is specific to list-valued
    fields, where the pushed-down condition is fast **and wrong**: it matches
    nothing, which is worse than a slow correct filter.

    So for a label array there is no correct pushed-down all-hops form. The
    choices are an inline array filter —
    ``LENGTH(p.edges[* FILTER @l IN CURRENT.toLabels]) == LENGTH(p.edges)`` —
    which is correct but post-filtered, or restricting the label test to a
    single hop (where ``e`` *is* the only edge and the VCI serves it), or
    testing the destination vertex's own ``labels``, which is what the
    depth-ranged templates do.

    Beware also that ``e`` is not a shortcut for "every edge": at depth > 1 it
    is the **final** edge of the emitted path, so a ``FILTER`` on ``e`` in a
    multi-hop traversal constrains only the last hop.
    """
    if mode not in LABEL_MODES:
        raise ValueError(f"label mode must be one of {LABEL_MODES}, got {mode!r}")
    if mode == "has":
        return f"@{bind} IN {field}"
    return f"@{bind} {mode.upper()} IN {field}"


def all_hops_label_predicate(
    labels_field: str, *, bind: str = "label", path: str = "p.edges"
) -> str:
    """"Every hop carries this label" — the one form that is correct *and* pushed.

    Expressed by inverting: select the hops that **lack** the label with an
    inline ``FILTER``, then assert that set is empty.

    ::

        p.edges[* FILTER @label NOT IN CURRENT.toLabels]._key NONE != null

    Two details are load-bearing and both were measured, not assumed:

    - **It must be the negated ``NONE`` shape, not the positive ``ALL`` one.**
      An inline ``FILTER`` *restricts the scope* of the comparison, so
      ``p.edges[* FILTER @l IN CURRENT.toLabels]._key ALL != null`` asks "of the
      hops that have the label, do all of them have a key" — vacuously true.
      On a path where **no** hop carries the label the selected set is empty and
      ``[] ALL != null`` is ``true``, so the path is accepted. Verified: that
      form returns the paths it should reject.
    - **The projection must be an attribute that always exists** (``_key``).
      Projecting the label array instead — ``… .toLabels NONE == null`` — is
      also wrong, because a violating edge's array is not *null*, it merely
      lacks the label, so the test is trivially satisfied.

    Version behaviour (measured): on **3.12.11+** the optimizer moves this into
    the traversal, leaving no ``FilterNode`` — an inline ``FILTER`` qualifies for
    pushdown only with ``ALL``/``NONE``, without an inline ``LIMIT`` or
    ``RETURN``, and without referencing the path variable. On **3.12.9** the
    same query is correct but post-filtered. So the form is safe to emit
    against either: it gets faster on newer servers rather than changing answer.
    """
    return f"{path}[* FILTER @{bind} NOT IN CURRENT.{labels_field}]._key NONE != null"


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
        # Multi-hop: "every hop is of this type, and the vertex I land on has
        # this label". The type test uses the ``[*] ALL ==`` form deliberately —
        # on a SCALAR field it is correct, and the optimizer pushes it into the
        # traverser as a global edge condition served by the VCI. A ``FILTER``
        # on ``e`` would be wrong here: at depth > 1 ``e`` is only the FINAL
        # edge of the emitted path, so it constrains the last hop and lets paths
        # through non-matching earlier edges. The label test goes on the
        # destination vertex ``v`` rather than the edge's copy, because that is
        # what "landed on an X" means at any depth — and because no ``[*]`` form
        # can correctly test a LIST field across hops (see label_predicate).
        "outbound_by_type": (
            f"WITH {node}\n"
            f"FOR v, e, p IN 1..@depth OUTBOUND @start {edge_c}\n"
            f"  {traversal_index_hint(lpg, 'outbound')}\n"
            f"  FILTER p.edges[*].{type_f} ALL == @type\n"
            f"  FILTER @toLabel IN v.{label_f}\n"
            f"  RETURN {{vertex: v, edge: e}}"
        ),
        "inbound_by_type": (
            f"WITH {node}\n"
            f"FOR v, e, p IN 1..@depth INBOUND @start {edge_c}\n"
            f"  {traversal_index_hint(lpg, 'inbound')}\n"
            f"  FILTER p.edges[*].{type_f} ALL == @type\n"
            f"  FILTER @fromLabel IN v.{label_f}\n"
            f"  RETURN {{vertex: v, edge: e}}"
        ),
        # Single hop: `e` IS the only edge, so the edge-variable filters are
        # exactly right — and this is the shape the VCIs were designed for,
        # since type + the far endpoint's copied label are both in the index.
        "neighbors_by_type": (
            f"WITH {node}\n"
            f"FOR v, e IN 1..1 OUTBOUND @start {edge_c}\n"
            f"  {traversal_index_hint(lpg, 'outbound')}\n"
            f"  FILTER e.{type_f} == @type\n"
            f"  FILTER @toLabel IN e.{to_l}\n"
            f"  RETURN v"
        ),
        # "Every hop of this type landed on an X" — distinct from
        # outbound_by_type, which asks only that the FINAL vertex is an X.
        # Uses the inverted inline-FILTER form (see all_hops_label_predicate);
        # correct on any 3.12, and pushed into the traverser from 3.12.11.
        "outbound_all_hops_labelled": (
            f"WITH {node}\n"
            f"FOR v, e, p IN 1..@depth OUTBOUND @start {edge_c}\n"
            f"  {traversal_index_hint(lpg, 'outbound')}\n"
            f"  FILTER p.edges[*].{type_f} ALL == @type\n"
            f"  FILTER {all_hops_label_predicate(to_l, bind='toLabel')}\n"
            f"  RETURN {{vertex: v, edge: e}}"
        ),
        # The inbound mirror, so the [_to, type, fromLabels[*]] index has a
        # single-hop template that actually exercises its label field.
        "inbound_neighbors_by_type": (
            f"WITH {node}\n"
            f"FOR v, e IN 1..1 INBOUND @start {edge_c}\n"
            f"  {traversal_index_hint(lpg, 'inbound')}\n"
            f"  FILTER e.{type_f} == @type\n"
            f"  FILTER @fromLabel IN e.{from_l}\n"
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
