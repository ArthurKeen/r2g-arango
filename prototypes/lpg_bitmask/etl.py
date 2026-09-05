"""Row-level label derivation + endpoint-mask denormalization.

Standalone prototype. This is the half r2g does NOT have: its labels are per
CollectionMapping, i.e. per TABLE, so every row of a table carries the same
label set. Here a label is derived from a row's own column VALUES, so two rows
of the same table can carry different labels — which is what "map categoricals
to labels" requires.

That change forces the second piece. An edge must carry its endpoints' label
masks (so a traversal can filter without fetching the neighbour), and if labels
are row-level then the mask is not a per-table constant that can be looked up
once — it has to come from the endpoint ROW. That is a join at edge-build time,
and it is the part that governs whether the ETL scales.
"""

from __future__ import annotations

from typing import Callable, Iterable


class LabelRule:
    """Derives labels from one row.

    ``column``/``mapping`` covers the common categorical case
    (``account_type='verified'`` -> ``Verified``); ``predicate`` covers derived
    labels that are not a single column's value (``followers > 10000`` ->
    ``Influencer``), which real datasets always seem to need.
    """

    def __init__(self, *, base: str | None = None, column: str | None = None,
                 mapping: dict | None = None,
                 predicate: Callable[[dict], bool] | None = None,
                 label: str | None = None) -> None:
        self.base, self.column, self.mapping = base, column, mapping or {}
        self.predicate, self.label = predicate, label

    def labels_for(self, row: dict) -> list[str]:
        out: list[str] = []
        if self.base:
            out.append(self.base)
        if self.column is not None:
            val = row.get(self.column)
            mapped = self.mapping.get(val)
            if mapped:
                out.extend([mapped] if isinstance(mapped, str) else mapped)
        if self.predicate is not None and self.label and self.predicate(row):
            out.append(self.label)
        return out


class LabelResolver:
    """Applies rules per table and encodes the result to a mask."""

    def __init__(self, dictionary, rules: dict[str, list[LabelRule]]) -> None:
        self.dict = dictionary
        self.rules = rules

    def labels(self, table: str, row: dict) -> list[str]:
        seen: list[str] = []
        for rule in self.rules.get(table, []):
            for lab in rule.labels_for(row):
                if lab not in seen:
                    seen.append(lab)
        return seen

    def mask(self, table: str, row: dict) -> int:
        return self.dict.mask(self.labels(table, row))


class EndpointMaskIndex:
    """The join: endpoint key -> its label mask.

    Built during the node pass so the edge pass can denormalize without a second
    scan of the source. An int per node, so it is bounded by node count rather
    than by label count — the array form would hold a list of strings per node,
    which is what makes the string representation expensive at this scale.

    A production version would spill to disk or a key-value store beyond memory;
    the point of the prototype is that the ETL shape works and what it costs.
    """

    def __init__(self) -> None:
        self._masks: dict[str, int] = {}

    def record(self, key: str, mask: int) -> None:
        self._masks[key] = mask

    def mask_for(self, key: str) -> int:
        # 0 = "no labels known". Distinguishable from "no labels", which also
        # encodes as 0 — a real implementation should separate the two, since a
        # missing endpoint is a referential-integrity problem, not a label fact.
        return self._masks.get(key, 0)

    def __len__(self) -> int:
        return len(self._masks)


def build_nodes(rows: Iterable[dict], *, table: str, key_field: str,
                resolver: LabelResolver, index: EndpointMaskIndex,
                node_collection: str = "nodes", label_prefix: str | None = None):
    """Yield node documents carrying BOTH representations.

    Both are emitted so the harness can prove the mask and the array select the
    same rows. Production would keep only the mask.
    """
    for row in rows:
        labels = resolver.labels(table, row)
        mask = resolver.dict.mask(labels)
        key = f"{label_prefix or table}_{row[key_field]}"
        index.record(f"{node_collection}/{key}", mask)
        yield {"_key": key, "labels": labels, "labelBits": mask,
               "sourceTable": table, **{k: v for k, v in row.items() if k != key_field}}


def build_edges(rows: Iterable[dict], *, table: str, from_field: str, to_field: str,
                from_prefix: str, to_prefix: str, resolver: LabelResolver,
                index: EndpointMaskIndex, node_collection: str = "nodes"):
    """Yield edge documents with the endpoints' masks denormalized onto them."""
    for row in rows:
        frm = f"{node_collection}/{from_prefix}_{row[from_field]}"
        to = f"{node_collection}/{to_prefix}_{row[to_field]}"
        etypes = resolver.labels(table, row)
        yield {
            "_from": frm, "_to": to,
            "types": etypes,
            "typeBits": resolver.dict.mask(etypes),
            # The join. Scalars, so they can sit in a vertex-centric index
            # without array expansion — which is the whole reason for the mask.
            "fromLabelBits": index.mask_for(frm),
            "toLabelBits": index.mask_for(to),
            "sourceTable": table,
        }
