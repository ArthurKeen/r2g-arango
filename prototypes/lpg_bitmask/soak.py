"""Hunt the index-build cliff at the customer's real label shape.

The scaling ladder built indexes BEFORE loading, so index maintenance was folded
into throughput and could not be seen on its own. Here the data is loaded bare
and each index is built afterwards and timed separately, which is what exposes a
cliff: index construction is where a working set that no longer fits in memory
stops behaving linearly.

The comparison is the point, at 20 labels per edge:

    bitmask [_from, typeBits]    -> 1 index entry per edge
    array   [_from, types[*]]    -> 20 index entries per edge

So the array form builds a structure 20x larger over identical data. If the
bitmask holds linear while the array bends, that is the sizing argument.
"""
from __future__ import annotations
import random, sys, time
from pathlib import Path
import requests
from arango import ArangoClient
sys.path.insert(0, str(Path(__file__).parent))
from dictionary import LabelDictionary

ENDPOINT, PW = "http://localhost:8540", "r2g_test_2026"
# The customer's shape: 20 labels on every node and every edge.
NODE_VOCAB = [f"NL{i:02d}" for i in range(30)]
EDGE_VOCAB = [f"EL{i:02d}" for i in range(25)]
LPN = LPE = 20
BATCH = 50_000


def build_index(coll, spec, label):
    t0 = time.time()
    coll.add_index(spec)
    return time.time() - t0


def rung(n_edges, n_nodes):
    nd = LabelDictionary("n"); nd.extend(NODE_VOCAB)
    ed = LabelDictionary("e"); ed.extend(EDGE_VOCAB)
    rng = random.Random(9)
    client = ArangoClient(hosts=ENDPOINT)
    sysdb = client.db("_system", username="root", password=PW)
    name = f"soak_{n_edges}"
    sysdb.delete_database(name, ignore_missing=True); sysdb.create_database(name)
    db = client.db(name, username="root", password=PW)
    try:
        nodes = db.create_collection("nodes")
        edges = db.create_collection("edges", edge=True)
        buf = []
        for i in range(n_nodes):
            labs = rng.sample(NODE_VOCAB, LPN)
            buf.append({"_key": f"N{i}", "labels": labs, "labelBits": nd.mask(labs)})
            if len(buf) >= BATCH: nodes.insert_many(buf); buf = []
        if buf: nodes.insert_many(buf)

        t0 = time.time(); buf = []
        for _ in range(n_edges):
            t = rng.sample(EDGE_VOCAB, LPE)
            buf.append({"_from": f"nodes/N{rng.randrange(n_nodes)}",
                        "_to": f"nodes/N{rng.randrange(n_nodes)}",
                        "types": t, "typeBits": ed.mask(t)})
            if len(buf) >= BATCH: edges.insert_many(buf); buf = []
        if buf: edges.insert_many(buf)
        load = time.time() - t0

        # Build each index on the SAME loaded data, timed separately.
        tb = build_index(edges, {"type": "persistent", "fields": ["_from", "typeBits"],
                                 "name": "bits"}, "bitmask")
        ta = build_index(edges, {"type": "persistent", "fields": ["_from", "types[*]"],
                                 "name": "arr"}, "array")
        stats = edges.statistics()
        idx = {i["name"]: i for i in edges.indexes()}
        mem = ""
        try:
            import subprocess
            mem = subprocess.run(["docker","stats","--no-stream","--format","{{.MemUsage}}",
                                  "r2g-test-arangodb"], capture_output=True, text=True,
                                 timeout=20).stdout.strip().split("/")[0].strip()
        except Exception:
            pass
        print(f"{n_edges:>10,} {load:>7.0f}s {n_edges/load:>9,.0f} "
              f"{tb:>9.1f}s {ta:>9.1f}s {ta/max(tb,0.001):>7.1f}x "
              f"{stats['indexes']['size']/1e6:>9.0f} {mem:>10}", flush=True)
    finally:
        sysdb.delete_database(name, ignore_missing=True); client.close()


if __name__ == "__main__":
    print(f"{LPN} labels/node, {LPE} labels/edge "
          f"({len(NODE_VOCAB)} / {len(EDGE_VOCAB)} distinct)")
    print("indexes built AFTER load, timed separately\n")
    print(f"{'edges':>10} {'load':>8} {'edges/s':>9} {'bitmask':>10} {'array':>10} "
          f"{'ratio':>7} {'idx MB':>9} {'arango mem':>10}")
    for ne, nn in [(2_000_000, 400_000), (5_000_000, 1_000_000), (10_000_000, 2_000_000)]:
        rung(ne, nn)
