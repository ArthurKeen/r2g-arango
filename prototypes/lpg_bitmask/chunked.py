"""Break the 32-label ceiling: a vocabulary split across several 32-bit fields.

The single-mask design has one hard boundary. AQL's BIT_* functions are 32-bit
and fail *silently* past it — BIT_AND(2**32, 2**32) returns null, no error — so
a 33rd label cannot be tested for at all. That looked like a vocabulary limit.
It is not. Two measurements separate the concerns:

* **Storage is not the limit.** A document round-trips an exact integer to
  2**63; only the arithmetic is capped.
* **Chunking must use scalar fields, not an array.** Storing the chunks as
  `bits: [lo, hi]` and indexing `bits[*]` reintroduces exactly the duplication
  the bitmask existed to remove — one index entry per chunk. Storing them as
  `bits0, bits1, ...` keeps one entry per edge.

So the vocabulary ceiling is 32 bits *per index field*, and the real cost of a
larger vocabulary is index width: one more field and one more BIT_AND per 32
labels. This script measures that at 96 distinct labels / 3 chunks.
"""
from __future__ import annotations
import random, sys, time, uuid
from pathlib import Path
from arango import ArangoClient
sys.path.insert(0, str(Path(__file__).parent))
from dictionary import LabelDictionary

ENDPOINT, PW = "http://localhost:8540", "r2g_test_2026"
VOCAB = [f"L{i:02d}" for i in range(96)]      # 3 chunks
LABELS_PER_EDGE = 20
N_EDGES, N_NODES = 200_000, 500
TARGET = "L77"                                 # lives in chunk 2


def traversal(idx, filt, var="e"):
    return (f"WITH nodes\nFOR v, {var} IN 1..1 OUTBOUND 'nodes/N0' edges "
            f"OPTIONS {{indexHint: {{edges: {{outbound: {{base: ['{idx}']}}}}}}}} "
            f"FILTER {filt} RETURN v._key")


def probe(db, tag, q, bv):
    tn = [n for n in db.aql.explain(q, bind_vars=bv)["nodes"]
          if n["type"] == "TraversalNode"][0]
    pushed = bool(tn.get("globalEdgeConditions"))
    ts = []
    for _ in range(7):
        t0 = time.perf_counter()
        rows = list(db.aql.execute(q, bind_vars=bv))
        ts.append((time.perf_counter() - t0) * 1000)
    ts.sort()
    print(f"    {tag:34s} idx={tn['indexes']['base'][0]['name']:9s} "
          f"push={'yes' if pushed else 'no ':3s} rows={len(rows):5d} "
          f"distinct={len(set(rows)):3d} median={ts[3]:7.2f} ms")
    return set(rows)


def main():
    d = LabelDictionary("chunked"); d.extend(VOCAB)
    fields = d.chunk_fields()
    print(f"{d}  -> index fields {fields}")
    print(f"{LABELS_PER_EDGE} labels/edge, {N_EDGES:,} edges, target {TARGET} "
          f"in chunk {d._to_bit[TARGET] // d.BITS_PER_CHUNK}\n")

    cl = ArangoClient(hosts=ENDPOINT, request_timeout=3600)
    sysdb = cl.db("_system", username="root", password=PW)
    name = f"chunked_{uuid.uuid4().hex[:6]}"
    sysdb.create_database(name)
    db = cl.db(name, username="root", password=PW)
    try:
        nodes = db.create_collection("nodes")
        edges = db.create_collection("edges", edge=True)
        nodes.insert_many([{"_key": f"N{i}"} for i in range(N_NODES)])

        random.seed(11)
        buf = []
        for i in range(N_EDGES):
            labs = random.sample(VOCAB, LABELS_PER_EDGE)
            doc = {"_from": f"nodes/N{i % 200}", "_to": f"nodes/N{200 + i % 300}",
                   "labels": labs}
            for f, v in zip(fields, d.chunks(labs)):
                doc[f] = v
            # The wrong way to chunk, kept alongside to measure the difference.
            doc["bitsArr"] = d.chunks(labs)
            buf.append(doc)
            if len(buf) == 20_000:
                edges.insert_many(buf); buf = []
        if buf:
            edges.insert_many(buf)

        for spec, nm in [(["_from"] + fields, "vci_chunks"),
                         (["_from", "bitsArr[*]"], "vci_arr_masks"),
                         (["_from", "labels[*]"], "vci_labels")]:
            t0 = time.time()
            edges.add_index({"type": "persistent", "fields": spec, "name": nm})
            print(f"  built {nm:14s} {'+'.join(spec):28s} {time.time() - t0:6.1f}s")

        print("\n  'has L77' — same question, three encodings:")
        expr, bv = d.chunk_filter([TARGET])
        a = probe(db, "chunked scalars + BIT_AND", traversal("vci_chunks", expr), bv)
        b = probe(db, "chunks as an ARRAY field", traversal("vci_arr_masks", expr), bv)
        c = probe(db, "label array + IN", traversal("vci_labels", "@l IN e.labels"),
                  {"l": TARGET})
        print(f"\n  ALL THREE AGREE: {a == b == c}")
    finally:
        sysdb.delete_database(name); cl.close()


if __name__ == "__main__":
    main()
