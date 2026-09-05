# LPG label bitmasks — prototype

Explores whether a **bitmask** representation of LPG labels solves three problems
at once for a large multi-label graph:

1. **The multi-label VCI.** An array-valued label field cannot go in a
   vertex-centric index — a persistent index stores one entry per array element
   and a traversal scans them all, so each edge is returned once per label. A
   bitmask is a **scalar**, so it indexes without expansion.
2. **Memory.** A label set becomes one integer instead of N strings, on both the
   node and the endpoint copies denormalized onto each edge.
3. **Pushdown.** Scalars restore the filter forms that a list field breaks.

Compression is not a separate optimisation here — it is what makes (1) work.

## Files

| | |
| --- | --- |
| `dictionary.py` | Bidirectional label ↔ bit-position map, persisted and versioned |
| `etl.py` | Row-level labels from categorical columns + the endpoint-mask join |
| `harness.py` | Loads array and bitmask representations side by side and compares |
| `scale.py` | Scaling ladder: throughput, storage, latency |
| `fixed_degree.py` | Latency at controlled start-degree (removes a confound in `scale.py`) |
| `soak.py` | Long run hunting the index-build cliff |
| `isolate_cliff.py` | Phase-isolated re-run (load / index timed in separate processes) |
| `chunked.py` | Vocabularies past 32 labels, split across several 32-bit fields |

## Two constraints the prototype enforces rather than documents

- **32-bit ceiling — per field, not per vocabulary.** AQL's `BIT_*` functions are
  32-bit; `BIT_AND(2**32, 2**32)` returns `null` *silently*. `LabelDictionary`
  raises instead. This caught a real error while building the harness: node
  labels (30) and edge labels (5) had been put in one dictionary — 35 labels,
  over the ceiling. Separate dictionaries fixed it, and they are independent
  namespaces anyway. The ceiling bounds one *chunk*: past 32 labels the mask
  splits across `bits0, bits1, ...` (see below), so it is not a vocabulary limit.
- **Storage is not the constraint.** A document round-trips an exact integer to
  `2**63` — only the arithmetic is capped. Chunking exists to satisfy `BIT_*`,
  not to fit the number into a document.
- **Bit assignments are permanent.** A stored mask is meaningless without the
  dictionary that produced it, so renumbering silently reinterprets every row
  ever written. Adding a label is safe; renumbering is a rewrite.

## Findings (measured, ArangoDB 3.12.9)

- Bitmask and array select **identical row sets**; the array form additionally
  inflates by exactly the labels-per-edge factor (3.0x at 3 labels, stable across
  every scale tested).
- Latency is **flat** as the graph grows: 1.17 → 1.41 ms across 12x data at a
  fixed start-degree. Load throughput is flat too (~23k docs/s, −2% over 12x).
- **Chunking a large vocabulary costs index width, nothing else.** At 96 distinct
  labels / 20 per edge / 3 chunks, all three encodings return the same answer:

  | Encoding | Index build | Rows returned | Median |
  | --- | --- | --- | --- |
  | `bits0, bits1, bits2` scalars | 0.2 s | 224 (1x) | 5.4 ms |
  | the same chunks as an **array** field | 0.8 s | 672 (3x) | 10.6 ms |
  | `labels[*]` string array | 6.3 s | 4480 (20x) | 62.5 ms |

  The middle row is the trap: storing chunks as `bits: [lo, hi, ...]` and
  indexing `bits[*]` reintroduces exactly the duplication the bitmask removes —
  one index entry per chunk. Chunks must be **separate scalar fields**.
- **Neither form pushes into the traverser.** `globalEdgeConditions` is empty for
  both `BIT_AND(...)` and `@l IN e.labels`; both post-filter after the `_from`
  seek. Chunking therefore gives up no pushdown that the single mask had — the
  bitmask's win is scan volume, not pushdown.
- Storage per document could **not** be measured reliably — equivalent data gave
  a 90% spread across runs because compaction is asynchronous. Any sizing needs
  a real benchmark on target hardware.

## Vocabulary size decides the query strategy

| Distinct labels | Masks | Subset test |
| --- | --- | --- |
| ≤ ~13 | ≤ 4096 | `bits IN [...]` — **index-served** |
| larger (≤32) | too many to enumerate | `BIT_AND(...)` — correct, **post-filter** |
| > 32 | — | one `BIT_AND` per 32-label chunk — still post-filter, still one index entry per edge |

`LabelDictionary.enumerable()` is the check a query planner should make;
`chunk_fields()` and `chunk_filter()` generate the index spec and the AQL.
An earlier version of this file claimed chunking "loses the single-scalar index".
That was wrong: `chunked.py` measures it keeping one index entry per edge. What
chunking actually loses is the `IN [...]` enumeration, which was already
unavailable past ~13 labels.

## Running

Requires a live ArangoDB. Defaults to `http://localhost:8540`.

```bash
python harness.py --nodes 100000 --edges 300000
python fixed_degree.py
python soak.py
python chunked.py
```
