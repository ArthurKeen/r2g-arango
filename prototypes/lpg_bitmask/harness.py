"""Load a synthetic social graph twice — array labels and bitmask labels — and
compare. Standalone; touches no r2g module and no r2g database.

The comparison is the point. A mask is only useful if it selects exactly the
rows the array form selects, so every query is run BOTH ways and the key sets
are asserted equal. Without that, "no duplication" would just mean the bitmask
query was quietly answering a narrower question.
"""

from __future__ import annotations

import argparse, random, sys, time
from collections import Counter

import requests
from arango import ArangoClient

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from dictionary import LabelDictionary                      # noqa: E402
from etl import (EndpointMaskIndex, LabelResolver, LabelRule,  # noqa: E402
                 build_edges, build_nodes)

NODE_LABELS = ["User"] + [f"Trait{i:02d}" for i in range(29)]   # 30 total
EDGE_LABELS = ["Follows", "Blocks", "Mentions", "Reposts", "Replies"]  # 5


def make_dictionary():
    """SEPARATE dictionaries for nodes and edges.

    Not a stylistic choice — 30 node labels + 5 edge labels is 35, past the
    32-bit ceiling of AQL's BIT_* functions, so one shared vocabulary cannot
    encode both. They are independent namespaces anyway (an edge label and a
    node label never occupy the same mask), and split they fit with room: 30
    bits and 5 bits. The prototype's guard caught this attempt, which is the
    argument for enforcing the ceiling in code rather than in a comment.
    """
    nd = LabelDictionary("node_labels", version=1); nd.extend(NODE_LABELS)
    ed = LabelDictionary("edge_labels", version=1); ed.extend(EDGE_LABELS)
    return nd, ed


def make_resolver(nd, ed):
    # Row-level: the categorical column decides which traits a row carries.
    trait_map = {f"seg{i}": NODE_LABELS[1 + i] for i in range(29)}
    return LabelResolver(nd, {
        "users": [
            LabelRule(base="User"),
            LabelRule(column="segment", mapping=trait_map),
            LabelRule(predicate=lambda r: r["followers"] > 25_000, label="Trait00"),
        ],
    }), LabelResolver(ed, {
        "interactions": [LabelRule(column="kind", mapping={k: k for k in EDGE_LABELS})],
    })


def generate(n_nodes, n_edges, labels_per_node, labels_per_edge, seed=11):
    rng = random.Random(seed)
    users = []
    for i in range(n_nodes):
        users.append({"id": i, "segment": f"seg{rng.randrange(29)}",
                      "followers": rng.randint(0, 60_000),
                      "handle": f"user{i}"})
    inter = []
    for _ in range(n_edges):
        inter.append({"a": rng.randrange(n_nodes), "b": rng.randrange(n_nodes),
                      "kind": rng.choice(EDGE_LABELS)})
    return users, inter, rng


def load(db, users, inter, nres, eres, extra_node_labels, extra_edge_labels, rng):
    nodes = db.create_collection("nodes")
    edges = db.create_collection("edges", edge=True)
    # BITMASK vertex-centric indexes: scalars, so no array expansion.
    edges.add_index({"type": "persistent", "fields": ["_from", "typeBits"], "name": "vci_bits_out"})
    edges.add_index({"type": "persistent", "fields": ["_to", "typeBits"], "name": "vci_bits_in"})
    # ARRAY equivalents, for the side-by-side comparison only.
    edges.add_index({"type": "persistent", "fields": ["_from", "types[*]"], "name": "vci_arr_out"})
    nodes.add_index({"type": "persistent", "fields": ["labelBits"], "name": "node_bits"})
    nodes.add_index({"type": "persistent", "fields": ["labels[*]"], "name": "node_arr"})

    idx = EndpointMaskIndex()
    batch = []
    for doc in build_nodes(users, table="users", key_field="id",
                           resolver=nres, index=idx, label_prefix="User"):
        # Top up to the requested labels-per-node from the trait vocabulary.
        extra = [l for l in rng.sample(NODE_LABELS[1:], extra_node_labels)
                 if l not in doc["labels"]]
        doc["labels"] = doc["labels"] + extra
        doc["labelBits"] = nres.dict.mask(doc["labels"])
        idx.record(f"nodes/{doc['_key']}", doc["labelBits"])
        batch.append(doc)
        if len(batch) >= 25_000:
            nodes.insert_many(batch); batch = []
    if batch: nodes.insert_many(batch)

    batch = []
    for doc in build_edges(inter, table="interactions", from_field="a", to_field="b",
                           from_prefix="User", to_prefix="User",
                           resolver=eres, index=idx):
        extra = [l for l in rng.sample(EDGE_LABELS, extra_edge_labels)
                 if l not in doc["types"]]
        doc["types"] = doc["types"] + extra
        doc["typeBits"] = eres.dict.mask(doc["types"])
        batch.append(doc)
        if len(batch) >= 25_000:
            edges.insert_many(batch); batch = []
    if batch: edges.insert_many(batch)
    return nodes, edges, idx


def indexes_used(ex):
    out = []
    for nd in ex["nodes"]:
        ix = nd.get("indexes")
        if isinstance(ix, dict):
            out += [x.get("name") for x in ix.get("base", [])]
        elif ix:
            out += [x.get("name") for x in ix]
    return out


def compare(db, nd, ed, start_key, edge_label, node_label):
    """Run each question BOTH ways; the key sets must be identical."""
    results = {}
    bit_e = ed.bit(edge_label)
    masks = ed.masks_containing(edge_label) if ed.enumerable() else None

    HB = "OPTIONS {indexHint:{edges:{outbound:{base:['vci_bits_out']}}}}"
    HA = "OPTIONS {indexHint:{edges:{outbound:{base:['vci_arr_out']}}}}"
    start = f"nodes/{start_key}"

    def run(q, bv):
        ex = db.aql.explain(q, bind_vars=bv)
        rows = list(db.aql.execute(q, bind_vars=bv))
        return rows, indexes_used(ex)

    # Q1 — 1 hop, filter on EDGE label
    qa = (f"WITH nodes\nFOR v,e IN 1..1 OUTBOUND @s edges {HA}\n"
          f"  FILTER @l IN e.types RETURN v._key")
    qb = (f"WITH nodes\nFOR v,e IN 1..1 OUTBOUND @s edges {HB}\n"
          f"  FILTER e.typeBits IN @m RETURN v._key")
    ra, ia = run(qa, {"s": start, "l": edge_label})
    rb, ib = run(qb, {"s": start, "m": masks})
    results["edge-label filter"] = (ra, ia, rb, ib)

    # Q2 — 1 hop, filter on TARGET NODE label (the denormalized copy)
    qa2 = (f"WITH nodes\nFOR v,e IN 1..1 OUTBOUND @s edges {HA}\n"
           f"  FILTER @l IN v.labels RETURN v._key")
    qb2 = (f"WITH nodes\nFOR v,e IN 1..1 OUTBOUND @s edges {HB}\n"
           f"  FILTER BIT_AND(e.toLabelBits,@b)==@b RETURN v._key")
    ra2, ia2 = run(qa2, {"s": start, "l": node_label})
    rb2, ib2 = run(qb2, {"s": start, "b": nd.bit(node_label)})
    results["node-label filter (edge copy)"] = (ra2, ia2, rb2, ib2)

    # Q3 — 2 hops, every hop of the edge label
    qa3 = (f"WITH nodes\nFOR v,e,p IN 2..2 OUTBOUND @s edges\n"
           f"  FILTER p.edges[* FILTER @l NOT IN CURRENT.types]._key NONE != null\n"
           f"  RETURN v._key")
    qb3 = (f"WITH nodes\nFOR v,e,p IN 2..2 OUTBOUND @s edges\n"
           f"  FILTER p.edges[* FILTER BIT_AND(CURRENT.typeBits,@b)!=@b]._key NONE != null\n"
           f"  RETURN v._key")
    ra3, ia3 = run(qa3, {"s": start, "l": edge_label})
    rb3, ib3 = run(qb3, {"s": start, "b": bit_e})
    results["2-hop all-hops edge label"] = (ra3, ia3, rb3, ib3)
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", type=int, default=100_000)
    ap.add_argument("--edges", type=int, default=300_000)
    ap.add_argument("--labels-per-node", type=int, default=9)
    ap.add_argument("--labels-per-edge", type=int, default=3)
    ap.add_argument("--endpoint", default="http://localhost:8540")
    ap.add_argument("--password", default="r2g_test_2026")
    a = ap.parse_args()

    nd, ed = make_dictionary()
    print(f"node dict: {nd}   edge dict: {ed}")
    print(f"node vocab enumerable: {nd.enumerable()}   edge vocab enumerable: {ed.enumerable()}")

    client = ArangoClient(hosts=a.endpoint)
    sysdb = client.db("_system", username="root", password=a.password)
    name = f"bmproto_{int(time.time())}"
    sysdb.create_database(name)
    db = client.db(name, username="root", password=a.password)
    try:
        users, inter, rng = generate(a.nodes, a.edges, a.labels_per_node, a.labels_per_edge)
        nres, eres = make_resolver(nd, ed)
        t0 = time.time()
        nodes, edges, idx = load(db, users, inter, nres, eres, a.labels_per_node,
                                 a.labels_per_edge, rng)
        el = time.time() - t0
        print(f"loaded {a.nodes:,} nodes + {a.edges:,} edges in {el:.1f}s "
              f"({(a.nodes+a.edges)/el:,.0f} docs/s); endpoint index {len(idx):,} entries\n")

        # pick a start node with plenty of out-edges
        start = list(db.aql.execute(
            "FOR e IN edges COLLECT f=e._from WITH COUNT INTO n "
            "SORT n DESC LIMIT 1 RETURN f"))[0].split("/")[1]

        print(f"start node {start}\n")
        out = compare(db, nd, ed, start, "Follows", "Trait05")
        allsame = True
        for q, (ra, ia, rb, ib) in out.items():
            # Compare SETS. The array baseline duplicates whenever its query
            # uses an array-expansion index (one index entry per element), so a
            # list comparison would measure that defect rather than whether the
            # mask selects the same rows. Set equality isolates faithfulness;
            # the duplication factor is reported separately.
            same = set(ra) == set(rb)
            allsame &= same
            da = max(Counter(ra).values()) if ra else 0
            dbb = max(Counter(rb).values()) if rb else 0
            infl = (len(ra) / len(set(ra))) if ra else 1.0
            print(f"  {q}")
            print(f"    array   rows={len(ra):6d} distinct={len(set(ra)):5d} "
                  f"maxdup={da:3d} inflation={infl:4.1f}x  idx={(ia or ['-'])[0]}")
            print(f"    bitmask rows={len(rb):6d} distinct={len(set(rb)):5d} "
                  f"maxdup={dbb:3d} inflation={(len(rb)/len(set(rb))) if rb else 1.0:4.1f}x  idx={(ib or ['-'])[0]}")
            print(f"    SAME ROWS SELECTED: {same}")
        print(f"\n  ALL QUERIES AGREE: {allsame}")

        for coll in ("nodes", "edges"):
            requests.put(f"{a.endpoint}/_db/{name}/_api/collection/{coll}/compact",
                         auth=("root", a.password), timeout=300)
        time.sleep(15)
        fn, fe = nodes.statistics(), edges.statistics()
        bn = (fn["documents_size"] + fn["indexes"]["size"]) / a.nodes
        be = (fe["documents_size"] + fe["indexes"]["size"]) / a.edges
        print(f"\n  post-compaction: {bn:.0f} B/node, {be:.0f} B/edge "
              f"(BOTH representations stored — production keeps only one)")
    finally:
        sysdb.delete_database(name, ignore_missing=True)
        client.close()


if __name__ == "__main__":
    main()
